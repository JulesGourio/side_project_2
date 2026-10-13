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

dbutils.widgets.removeAll()
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

# Path(__file__) is not the notebook path in a job task. The notebook's directory is on sys.path and is the cwd,
# so one level up is utils/, where ops_config.py lives.
sys.path.insert(0, os.path.dirname(os.getcwd()))
from ops_config import CAPS_ALL_GROUPS, lakebase_connect_with, sync_users_from_audit

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

# MAGIC %md ### 2. Merge audit into `users` and write (unless dry run)

# COMMAND ----------

from databricks.sdk import WorkspaceClient

w = WorkspaceClient()  # ambient identity of this notebook in this workspace
conn = lakebase_connect_with(w, LAKEBASE_PROJECT_ID, LAKEBASE_BRANCH, LAKEBASE_ENDPOINT, LAKEBASE_DATABASE)
logger.info(f"Connected to {conn.info.host}/{LAKEBASE_DATABASE} as {conn.info.user}")
try:
    updated, preview_rows = sync_users_from_audit(conn, audit, dry_run=DRY_RUN)
finally:
    conn.close()

if DRY_RUN:
    logger.info(f"[dry-run] {len(preview_rows)} user(s) computed, nothing written. Sample:")
    for row in preview_rows[:20]:
        logger.info("  %s", row)
else:
    logger.info(f"{updated} user(s) updated in {LAKEBASE_DATABASE} (groups / can_chat / can_compare).")
