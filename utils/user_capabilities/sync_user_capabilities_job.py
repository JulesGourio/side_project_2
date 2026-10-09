# Databricks notebook source
# MAGIC %md
# MAGIC # Sync user capabilities — UAT only (job, self-contained)
# MAGIC
# MAGIC Reads `system.access.audit` (UAT's own copy, per-workspace — no DEV
# MAGIC warehouse needed, unlike the CLI twin `sync_user_capabilities.py`) and
# MAGIC writes `can_chat`/`can_compare`/`groups` to Lakebase. `LAKEBASE_DATABASE`
# MAGIC is a job parameter so the same notebook targets `doccompare_test` or
# MAGIC `doccompare` via `base_parameters` only (see `databricks.yml`).
# MAGIC
# MAGIC Merge rule: for each tracked group, the audit is authoritative for any
# MAGIC (user, group) pair it mentions; pairs absent from the audit (outside its
# MAGIC ~1-year retention) keep whatever is already in `users.groups`.

# COMMAND ----------

# MAGIC %pip install psycopg2-binary --quiet
dbutils.library.restartPython()

# COMMAND ----------

dbutils.widgets.dropdown("dry_run", "true", ["true", "false"], "Dry run (preview only, no write)")
dbutils.widgets.text("LAKEBASE_PROJECT_ID", "qualibot")
dbutils.widgets.text("LAKEBASE_BRANCH", "production")
dbutils.widgets.text("LAKEBASE_ENDPOINT", "primary")
# Safe-by-default: this job is in validation against the disposable
# doccompare_test database. Switch to "doccompare" only via the qualibot-uat
# target's base_parameters, once a validated run confirms the computed rights
# are correct.
dbutils.widgets.text("LAKEBASE_DATABASE", "doccompare_test")

DRY_RUN = dbutils.widgets.get("dry_run") == "true"
LAKEBASE_PROJECT_ID = dbutils.widgets.get("LAKEBASE_PROJECT_ID")
LAKEBASE_BRANCH = dbutils.widgets.get("LAKEBASE_BRANCH")
LAKEBASE_ENDPOINT = dbutils.widgets.get("LAKEBASE_ENDPOINT")
LAKEBASE_DATABASE = dbutils.widgets.get("LAKEBASE_DATABASE")

import os
import sys

# Path(__file__) is not the deployed notebook path in a job task. Databricks puts the notebook's own directory on
# sys.path, so os.getcwd() is that
# directory and one level up is utils/, where ops_config.py lives.
sys.path.insert(0, os.path.dirname(os.getcwd()))
from ops_config import CAPS_CHAT_GROUPS, CAPS_COMPARE_GROUPS, CAPS_ALL_GROUPS

import logging

logger = logging.getLogger("sync_user_capabilities_job")
logger.setLevel(logging.INFO)
if not logger.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S"))
    logger.addHandler(_handler)
    logger.propagate = False


logger.info(f"dry_run={DRY_RUN}  database={LAKEBASE_DATABASE}  groups tracked={len(CAPS_ALL_GROUPS)}")

# COMMAND ----------

# MAGIC %md ### 1. Read the audit log (this workspace's own `system.access.audit`)

# COMMAND ----------

groups_sql = ", ".join(f"'{g}'" for g in sorted(CAPS_ALL_GROUPS))

audit_df = spark.sql(f"""
    WITH ev AS (
        SELECT
            request_params['targetUserId']    AS user_id,
            request_params['targetUserName']  AS email,
            request_params['targetGroupName'] AS group_name,
            action_name,
            ROW_NUMBER() OVER (
                PARTITION BY request_params['targetUserId'], request_params['targetGroupName']
                ORDER BY event_time DESC
            ) AS rn
        FROM system.access.audit
        WHERE action_name IN ('addPrincipalToGroup', 'removePrincipalFromGroup')
          AND request_params['targetGroupName'] IN ({groups_sql})
    )
    SELECT user_id, email, group_name, action_name
    FROM ev
    WHERE rn = 1
""")

audit: dict[tuple[str, str], dict] = {}
for row in audit_df.collect():
    if not row.user_id:
        continue
    audit[(row.user_id, row.group_name)] = {
        "is_member": row.action_name == "addPrincipalToGroup",
        "email": row.email,
    }

adds = sum(1 for v in audit.values() if v["is_member"])
removes = len(audit) - adds
logger.info(f"{len(audit)} (user, group) pair(s) — {adds} active, {removes} removed")

# COMMAND ----------

# MAGIC %md ### 2. Connect to Lakebase (ambient identity, no profile)

# COMMAND ----------

import psycopg2
from databricks.sdk import WorkspaceClient

w = WorkspaceClient()  # ambient auth — this notebook's own identity in this workspace

branch_path = f"projects/{LAKEBASE_PROJECT_ID}/branches/{LAKEBASE_BRANCH}"
endpoint_path = f"{branch_path}/endpoints/{LAKEBASE_ENDPOINT}"

eps = list(w.postgres.list_endpoints(parent=branch_path))
if not eps:
    raise RuntimeError(f"No endpoint found for {branch_path}")
host = eps[0].status.hosts.host

me = w.current_user.me()
username = me.user_name or me.display_name or ""
if not username:
    raise RuntimeError("Could not resolve the current identity.")

cred = w.postgres.generate_database_credential(endpoint=endpoint_path)
if not cred.token:
    raise RuntimeError("generate_database_credential returned an empty token.")

conn = psycopg2.connect(
    host=host, port=5432, database=LAKEBASE_DATABASE,
    user=username, password=cred.token, sslmode="require",
)
logger.info(f"Connected to {host}/{LAKEBASE_DATABASE} as {username}")

# COMMAND ----------

# MAGIC %md ### 3. Merge audit → `users` and write (unless dry run)

# COMMAND ----------

updated = 0
preview_rows = []
try:
    with conn.cursor() as cur:
        cur.execute("""
            SELECT EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name='users' AND column_name='groups'
            )
        """)
        has_groups = cur.fetchone()[0]

        cur.execute("""
            SELECT EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name='users' AND column_name='is_manual'
            )
        """)
        has_is_manual = cur.fetchone()[0]

        extra = ", is_manual" if has_is_manual else ""
        if has_groups:
            cur.execute(f"SELECT user_id, email, groups{extra} FROM users")
            db_rows = {
                row[0]: {"email": row[1], "groups": set(row[2] or []), "is_manual": bool(row[3]) if has_is_manual else False}
                for row in cur.fetchall()
            }
        else:
            cur.execute(f"SELECT user_id, email{extra} FROM users")
            db_rows = {
                row[0]: {"email": row[1], "groups": set(), "is_manual": bool(row[2]) if has_is_manual else False}
                for row in cur.fetchall()
            }

        audit_users: dict[str, dict] = {}
        for (uid, grp), info in audit.items():
            entry = audit_users.setdefault(uid, {"email": info["email"], "groups": set()})
            entry["email"] = entry["email"] or info["email"]
            if info["is_member"]:
                entry["groups"].add(grp)

        for uid in set(audit_users) | set(db_rows):
            db = db_rows.get(uid, {"email": None, "groups": set(), "is_manual": False})
            if db["is_manual"]:
                continue
            audit_u = audit_users.get(uid, {"email": None, "groups": set()})

            merged_groups = set(db["groups"])
            for grp in CAPS_ALL_GROUPS:
                if (uid, grp) in audit:
                    if audit[(uid, grp)]["is_member"]:
                        merged_groups.add(grp)
                    else:
                        merged_groups.discard(grp)

            email = db["email"] or audit_u["email"]
            can_chat = bool(merged_groups & CAPS_CHAT_GROUPS)
            can_compare = bool(merged_groups & CAPS_COMPARE_GROUPS)
            groups_list = sorted(merged_groups)

            if not email:
                continue

            preview_rows.append((email, can_chat, can_compare, groups_list))

            if DRY_RUN:
                continue

            # Optional columns assembled dynamically (same 4 variants as
            # sync_user_capabilities.py — keep in sync).
            extra_cols = []
            if has_groups:
                extra_cols.append(("groups", groups_list))

            set_extra = "".join(f", {c}=%s" for c, _ in extra_cols)
            cur.execute(
                f"UPDATE users SET can_chat=%s, can_compare=%s{set_extra}, updated_at=NOW() WHERE email=%s",
                (can_chat, can_compare, *[v for _, v in extra_cols], email),
            )
            if cur.rowcount:
                updated += 1
                continue

            cols = "".join(f", {c}" for c, _ in extra_cols)
            placeholders = ", %s" * len(extra_cols)
            conflict_extra = "".join(f", {c} = EXCLUDED.{c}" for c, _ in extra_cols)
            cur.execute(
                f"""INSERT INTO users (user_id, email, can_chat, can_compare{cols})
                    VALUES (%s, %s, %s, %s{placeholders})
                    ON CONFLICT (user_id) DO UPDATE SET
                        email       = COALESCE(EXCLUDED.email, users.email),
                        can_chat    = EXCLUDED.can_chat,
                        can_compare = EXCLUDED.can_compare{conflict_extra},
                        updated_at  = NOW()""",
                (uid, email, can_chat, can_compare, *[v for _, v in extra_cols]),
            )
            updated += 1

    if not DRY_RUN:
        conn.commit()
finally:
    conn.close()

if DRY_RUN:
    logger.info(f"[dry-run] {len(preview_rows)} user(s) computed — nothing written. Sample:")
    for row in preview_rows[:20]:
        logger.info("%s", " ".join(str(x) for x in (" ", row,)))
else:
    logger.info(f"{updated} user(s) updated in {LAKEBASE_DATABASE} (groups / can_chat / can_compare).")


# COMMAND ----------
