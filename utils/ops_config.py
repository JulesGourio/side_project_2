"""Shared settings and helpers for utils/ (CLI profiles, Lakebase connection).

psycopg2 is imported lazily so that modules which never touch Lakebase can import this file.
"""
from __future__ import annotations

import datetime
from decimal import Decimal

# ── Databricks CLI profiles ────────────────────────────────────────────────────
PROFILE_DEV  = "DEV"
PROFILE_UAT  = "UAT"
PROFILE_PROD = "qualibot-prod"

# ── Lakebase environments ──────────────────────────────────────────────────────
# Host and user are resolved at connect time (lakebase_connect_with); the token must come from the workspace of the
# targeted Lakebase, hence one CLI profile per env.
LAKEBASE: dict[str, dict] = {
    "UAT": {
        "profile": PROFILE_UAT,
    },
    # Same Lakebase project as UAT, separate database: test runs never write to the UAT database.
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

# ── Lakebase: project config (read by lakebase_connect) ──────────────────────
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
    "compare_messages",
    "chat_sessions",
    "chat_messages",       # FK -> chat_sessions
    "chat_turns",          # FK -> chat_messages
    "chat_retrieved_chunks",  # FK -> chat_turns
    "compare_feedbacks",   # FK -> compare_messages
    "chat_feedbacks",      # FK -> chat_messages, chat_sessions
    "impact_requests",
    "impact_document_results",  # FK -> impact_requests
    "impact_feedbacks",         # FK -> impact_requests, compare_messages
    "impact_cache",
    "summary_cache",
    "app_errors",
    "catalog_metadata",
]

# ── Shared helpers ─────────────────────────────────────────────────────────────

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
