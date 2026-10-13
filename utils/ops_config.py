"""Shared settings and helpers for utils/ (CLI profiles, Lakebase connection, user-capability sync).

Notebooks reach it with ``sys.path.insert(0, os.path.dirname(os.getcwd()))`` (their folder is on sys.path,
one level up is utils/). psycopg2 is imported lazily so notebooks that never touch Lakebase can import this file.
"""
from __future__ import annotations

import datetime
import json
import subprocess
from decimal import Decimal

# ── Databricks CLI profiles ────────────────────────────────────────────────────
PROFILE_DEV  = "DEV"
PROFILE_UAT  = "UAT"
PROFILE_PROD = "qualibot-prod"

# ── SQL Warehouse (UAT — access to system.access.audit) ───────────────────────
WAREHOUSE_ID = "13eff4a6095513cb"

# ── Lakebase environments ──────────────────────────────────────────────────────
# Host and user are resolved at connect time (lakebase_connect_with); the token must come from the workspace of the
# targeted Lakebase, hence one CLI profile per env.
LAKEBASE: dict[str, dict] = {
    "UAT": {
        "profile": PROFILE_UAT,
    },
    # Isolated database (qualibot-uat-test): same project as UAT, other database. Never "doccompare".
    "UAT-TEST": {
        "profile": PROFILE_UAT,
    },
    "DEV": {
        "profile": PROFILE_DEV,
    },
    "PROD": {
        "profile": PROFILE_PROD,
    },
}

# ── copy_Lakebase_tables: source → destination ──────────────────────────────
COPY_SOURCE_ENV      = "UAT"          # Lakebase source (key in LAKEBASE)
COPY_TARGET_PROFILE  = PROFILE_DEV   # Destination Databricks workspace
COPY_TARGET_CATALOG  = "dev_landingzone"
COPY_TARGET_SCHEMA   = "qualibot"
COPY_STAGING_CATALOG = "dev_lab"
COPY_STAGING_SCHEMA  = "lab_jules"
COPY_STAGING_VOLUME  = "staging"
COPY_TABLES_TO_SKIP  = {
    "llm_requests",      # too large, not useful for analysis
}

# ── Lakebase: project config (for migrate_lakebase.py) ────────────────────────
# generate_database_credential needs the Lakebase project_id (name shown in the Databricks UI), not a URI.
LAKEBASE_PROJECTS: dict[str, dict] = {
    "UAT": {"project_id": "qualibot",  "branch": "production", "endpoint": "primary", "database": "doccompare"},
    "UAT-TEST": {"project_id": "qualibot", "branch": "production", "endpoint": "primary", "database": "doccompare_test"},
    "DEV": {"project_id": "qualibot",  "branch": "production", "endpoint": "primary", "database": "doccompare"},
    "PROD": {"project_id": "qualibot", "branch": "production", "endpoint": "primary", "database": "doccompare"},
}

# ── Lakebase UAT -> PROD data migration (export_lakebase_to_json.py / import_lakebase_from_json.py) ─
MIGRATE_TO_PROD_TABLES_TO_SKIP = {
    "llm_requests",      # too large, not useful to carry over
}
# FK-safe order: parents before children. Any table not listed here is copied
# afterwards, in the source's own listing order (safe default for tables with
# no incoming FK from another migrated table).
MIGRATE_TO_PROD_TABLE_ORDER = [
    "users",
    "messages",
    "chat_sessions",
    "chat_messages",       # FK -> chat_sessions
    "chat_turns",          # FK -> chat_messages
    "chat_retrieved_chunks",  # FK -> chat_turns
    "feedbacks",           # FK -> messages
    "chat_feedbacks",      # FK -> chat_messages, chat_sessions
    "impact_requests",
    "impact_document_results",  # FK -> impact_requests
    "impact_feedbacks",         # FK -> impact_requests, messages
    "impact_cache",
    "summary_cache",
    "errors",
    "knowledge_base_metadata",
]

# ── Databricks groups → capabilities (kept in sync with app.yaml) ────────────
CAPS_CHAT_GROUPS    = {"Role-Project-LEAP-End-users-Qualibot-ChatBot", "Role-Project-LEAP-CoreDev", "Role-Project-LEAP-CoreAdmin"}
CAPS_COMPARE_GROUPS = {"Role-Project-LEAP-End-users-Qualibot-DocCompare", "Role-Project-LEAP-CoreDev", "Role-Project-LEAP-CoreAdmin"}
CAPS_ALL_GROUPS     = CAPS_CHAT_GROUPS | CAPS_COMPARE_GROUPS


# ── Shared helpers ─────────────────────────────────────────────────────────────

def get_token(profile: str) -> str:
    """Returns an OAuth JWT via the Databricks CLI for the given profile."""
    result = subprocess.run(
        ["databricks", "auth", "token", "--profile", profile],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"Failed to retrieve token (profile={profile}):\n{result.stderr.strip()}")
    output = result.stdout.strip()
    try:
        token = json.loads(output).get("access_token", "")
    except json.JSONDecodeError:
        token = output
    if not token:
        raise RuntimeError(f"CLI returned an empty token (profile={profile}).")
    return token


def lakebase_connect_with(w, project_id: str, branch: str, endpoint: str, database: str):
    """psycopg2 connection to a Lakebase database, authenticated by generate_database_credential (as the app does).

    ``w`` is a WorkspaceClient of the workspace that hosts the Lakebase (CLI profile or ambient notebook identity).
    """
    import psycopg2

    branch_path = f"projects/{project_id}/branches/{branch}"
    endpoint_path = f"{branch_path}/endpoints/{endpoint}"

    endpoints = list(w.postgres.list_endpoints(parent=branch_path))
    if not endpoints:
        raise RuntimeError(f"No endpoint found for {branch_path}")
    host = endpoints[0].status.hosts.host

    me = w.current_user.me()
    username = me.user_name or me.display_name or ""
    if not username:
        raise RuntimeError("Could not resolve the current identity.")

    token = w.postgres.generate_database_credential(endpoint=endpoint_path).token
    if not token:
        raise RuntimeError("generate_database_credential returned an empty token.")

    return psycopg2.connect(
        host=host, port=5432, database=database,
        user=username, password=token, sslmode="require",
    )


def lakebase_connect(env: str):
    """Connection to the Lakebase of an env of LAKEBASE_PROJECTS, through its CLI profile."""
    if env not in LAKEBASE_PROJECTS:
        raise ValueError(f"Unknown Lakebase environment: '{env}'. Available: {list(LAKEBASE_PROJECTS)}")
    from databricks.sdk import WorkspaceClient
    cfg = LAKEBASE_PROJECTS[env]
    w = WorkspaceClient(profile=LAKEBASE[env]["profile"])
    return lakebase_connect_with(w, cfg["project_id"], cfg["branch"], cfg["endpoint"], cfg["database"])


def json_serializer(obj):
    """``default=`` hook of json.dumps for Lakebase rows (dates, Decimal, bytes)."""
    if isinstance(obj, (datetime.date, datetime.datetime)):
        return obj.isoformat()
    if isinstance(obj, Decimal):
        return float(obj)
    if isinstance(obj, (bytes, memoryview)):
        return bytes(obj).hex()
    return str(obj)


def sync_users_from_audit(conn, audit: dict, dry_run: bool = False) -> tuple[int, list]:
    """Merge the audit-log group membership into ``users`` (groups, can_chat, can_compare) and commit.

    ``audit`` is {(user_id, group): {"is_member": bool, "email": str}}, the latest add/remove event per pair.
    For each tracked group (CAPS_ALL_GROUPS) the audit wins; pairs absent from it, and groups outside the tracked
    set, keep the stored value. Rows flagged ``is_manual`` and users without any email are left alone.
    Returns (rows written, [(email, can_chat, can_compare, groups)] preview of every computed row).
    """
    updated = 0
    preview: list = []
    with conn.cursor() as cur:
        cur.execute("""SELECT column_name FROM information_schema.columns
                       WHERE table_name = 'users' AND column_name IN ('groups', 'is_manual')""")
        optional = {r[0] for r in cur.fetchall()}
        has_groups, has_is_manual = "groups" in optional, "is_manual" in optional

        cols = ["user_id", "email"] + (["groups"] if has_groups else []) + (["is_manual"] if has_is_manual else [])
        cur.execute(f"SELECT {', '.join(cols)} FROM users")
        db_rows = {}
        for row in cur.fetchall():
            rec = dict(zip(cols, row))
            db_rows[rec["user_id"]] = {
                "email": rec["email"],
                "groups": set(rec.get("groups") or []),
                "is_manual": bool(rec.get("is_manual")),
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

            merged = set(db["groups"])
            for grp in CAPS_ALL_GROUPS:
                if (uid, grp) in audit:
                    (merged.add if audit[(uid, grp)]["is_member"] else merged.discard)(grp)

            email = db["email"] or audit_users.get(uid, {}).get("email")
            if not email:
                continue
            can_chat = bool(merged & CAPS_CHAT_GROUPS)
            can_compare = bool(merged & CAPS_COMPARE_GROUPS)
            groups_list = sorted(merged)
            preview.append((email, can_chat, can_compare, groups_list))
            if dry_run:
                continue

            extra = [("groups", groups_list)] if has_groups else []
            set_extra = "".join(f", {c}=%s" for c, _ in extra)
            cur.execute(
                f"UPDATE users SET can_chat=%s, can_compare=%s{set_extra}, updated_at=NOW() WHERE email=%s",
                (can_chat, can_compare, *[v for _, v in extra], email),
            )
            if cur.rowcount:
                updated += 1
                continue
            cur.execute(
                f"""INSERT INTO users (user_id, email, can_chat, can_compare{''.join(f', {c}' for c, _ in extra)})
                    VALUES (%s, %s, %s, %s{', %s' * len(extra)})
                    ON CONFLICT (user_id) DO UPDATE SET
                        email       = COALESCE(EXCLUDED.email, users.email),
                        can_chat    = EXCLUDED.can_chat,
                        can_compare = EXCLUDED.can_compare{''.join(f', {c} = EXCLUDED.{c}' for c, _ in extra)},
                        updated_at  = NOW()""",
                (uid, email, can_chat, can_compare, *[v for _, v in extra]),
            )
            updated += 1
    if not dry_run:
        conn.commit()
    return updated, preview
