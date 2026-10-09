"""Central configuration for utils/databricks_ops/.

All scripts import from this file instead of redefining connections,
profiles, and credentials every time.
"""
from __future__ import annotations

import json
import subprocess

import psycopg2

# ── Databricks CLI profiles ────────────────────────────────────────────────────
PROFILE_DEV  = "DEV"
PROFILE_UAT  = "UAT"
PROFILE_PROD = "qualibot-prod"

# ── SQL Warehouse (UAT — access to system.access.audit) ───────────────────────
WAREHOUSE_ID = "13eff4a6095513cb"

# ── Lakebase environments ──────────────────────────────────────────────────────
# Each env just needs the CLI profile to obtain the OAuth token; the host/user
# are resolved dynamically at connect time (see lakebase_connect below) via
# w.postgres.list_endpoints + w.current_user.me() — the token must come from
# the same workspace as the targeted Lakebase.
LAKEBASE: dict[str, dict] = {
    "UAT": {
        "profile": PROFILE_UAT,
    },
    # Isolated database (qualibot-uat-test) — same Lakebase project/branch/
    # endpoint as UAT, just a different database. Never "doccompare".
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

# ── copy_Lakebase_tables: source → destination ────────────────────────────────
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
# generate_database_credential needs the Lakebase project_id, not a PostgreSQL URI.
# project_id is the Lakebase project name as shown in the Databricks UI.
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


def lakebase_connect(env: str) -> psycopg2.extensions.connection:
    """Returns a psycopg2 connection via generate_database_credential (same method as the app)."""
    if env not in LAKEBASE_PROJECTS:
        raise ValueError(f"Unknown Lakebase environment: '{env}'. Available: {list(LAKEBASE_PROJECTS)}")
    from databricks.sdk import WorkspaceClient
    cfg     = LAKEBASE_PROJECTS[env]
    profile = LAKEBASE[env]["profile"]
    w       = WorkspaceClient(profile=profile)

    branch_path   = f"projects/{cfg['project_id']}/branches/{cfg['branch']}"
    endpoint_path = f"{branch_path}/endpoints/{cfg['endpoint']}"

    eps = list(w.postgres.list_endpoints(parent=branch_path))
    if not eps:
        raise RuntimeError(f"No endpoint found for {branch_path}")
    host = eps[0].status.hosts.host

    me       = w.current_user.me()
    username = me.user_name or me.display_name or ""
    if not username:
        raise RuntimeError("Could not resolve the current identity.")

    cred  = w.postgres.generate_database_credential(endpoint=endpoint_path)
    token = cred.token
    if not token:
        raise RuntimeError("generate_database_credential returned an empty token.")

    return psycopg2.connect(
        host=host, port=5432, database=cfg["database"],
        user=username, password=token, sslmode="require",
    )
