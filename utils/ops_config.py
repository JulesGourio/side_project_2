"""Shared settings and helpers for utils/ (CLI profiles, Lakebase connection).

psycopg2 is imported lazily so that modules which never touch Lakebase can import this file.
"""
from __future__ import annotations

import datetime
from decimal import Decimal
from typing import Any

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
    "DEV": {"project_id": "qualibot",  "branch": "production", "endpoint": "primary", "database": "doccompare"},
    "PROD": {"project_id": "qualibot", "branch": "production", "endpoint": "primary", "database": "doccompare"},
}

# ── Shared helpers ─────────────────────────────────────────────────────────────

def lakebase_connect_with(w, project_id: str, branch: str, endpoint: str, database: str) -> Any:
    """psycopg2 connection to a Lakebase database, authenticated by generate_database_credential (as the app does).

    ``w`` is a WorkspaceClient of the workspace that hosts the Lakebase (CLI profile or ambient notebook identity).

    :param w: WorkspaceClient of the workspace hosting the Lakebase (CLI profile or ambient notebook identity).
    :param project_id: Lakebase project name, as shown in the Databricks UI.
    :param branch: Lakebase branch name, for example ``production``.
    :param endpoint: Endpoint name of the branch, for example ``primary``.
    :param database: Name of the database to connect to.
    :return: open psycopg2 connection, authenticated with a short-lived database credential.
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


def lakebase_connect(env: str) -> Any:
    """Connection to the Lakebase of an env of LAKEBASE_PROJECTS, through its CLI profile.

    :param env: Environment key of LAKEBASE_PROJECTS, ``UAT``, ``DEV`` or ``PROD``.
    :return: open psycopg2 connection to the Lakebase of that environment.
    """
    if env not in LAKEBASE_PROJECTS:
        raise ValueError(f"Unknown Lakebase environment: '{env}'. Available: {list(LAKEBASE_PROJECTS)}")
    from databricks.sdk import WorkspaceClient
    cfg = LAKEBASE_PROJECTS[env]
    w = WorkspaceClient(profile=LAKEBASE[env]["profile"])
    return lakebase_connect_with(w, cfg["project_id"], cfg["branch"], cfg["endpoint"], cfg["database"])


def json_serializer(obj) -> Any:
    """``default=`` hook of json.dumps for Lakebase rows (dates, Decimal, bytes).

    :param obj: Value that json cannot serialise natively.
    :return: JSON-compatible representation of ``obj``.
    """
    if isinstance(obj, (datetime.date, datetime.datetime)):
        return obj.isoformat()
    if isinstance(obj, Decimal):
        return float(obj)
    if isinstance(obj, (bytes, memoryview)):
        return bytes(obj).hex()
    return str(obj)
