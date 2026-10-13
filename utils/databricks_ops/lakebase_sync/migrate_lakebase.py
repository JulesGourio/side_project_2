"""Lakebase schema migrations — independent of the app's own deployment.

Uses generate_database_credential (same method the app uses at startup) to
get full DDL rights without requiring a redeploy.

Run once after every new column or table is added.

USAGE
-----
    python utils/databricks_ops/lakebase_sync/migrate_lakebase.py --env DEV
    python utils/databricks_ops/lakebase_sync/migrate_lakebase.py --env UAT
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import psycopg2
from databricks.sdk import WorkspaceClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # utils/databricks_ops/ — shared config.py
from config import LAKEBASE_PROJECTS, LAKEBASE

sys.path.insert(0, str(Path(__file__).resolve().parent))  # this directory — shared migrations.py
from migrations import MIGRATIONS, apply_migrations


def sdk_lakebase_connect(env: str) -> psycopg2.extensions.connection:
    """Lakebase connection via generate_database_credential (full DDL rights)."""
    cfg = LAKEBASE_PROJECTS.get(env)
    if not cfg:
        raise ValueError(f"Env '{env}' missing from LAKEBASE_PROJECTS in config.py")

    profile = LAKEBASE[env]["profile"]
    w = WorkspaceClient(profile=profile)

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


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--env", choices=list(LAKEBASE_PROJECTS), default="DEV",
                    help="Target Lakebase environment. Default: DEV")
    args = ap.parse_args()

    print(f"Connecting to Lakebase {args.env} via generate_database_credential...")
    conn = sdk_lakebase_connect(args.env)

    try:
        applied, failed = apply_migrations(conn, MIGRATIONS)
        print(f"\nDone — {len(applied)}/{len(MIGRATIONS)} migration(s) applied.")
        if failed:
            print(f"Failed ({len(failed)}):")
            for name, err in failed:
                print(f"  [{name}] {err.strip()}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
