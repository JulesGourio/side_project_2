"""Export every Lakebase table to local JSON Lines files.

Meant to run as a CI pipeline step authenticated as whichever identity has
access to the SOURCE workspace (--profile) -- no personal login role needed.
Paired with import_lakebase_from_json.py, run in a later pipeline step
against the TARGET workspace.

USAGE:
    python export_lakebase_to_json.py --profile job-runner-sa-uat --out-dir staging/lakebase
"""
from __future__ import annotations

import argparse
import datetime
import json
import sys
from decimal import Decimal
from pathlib import Path

import psycopg2
import psycopg2.extras
from databricks.sdk import WorkspaceClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # utils/ — shared config.py
from ops_config import MIGRATE_TO_PROD_TABLES_TO_SKIP

PROJECT_ID = "qualibot"
BRANCH = "production"
ENDPOINT = "primary"


def json_serializer(obj):
    if isinstance(obj, (datetime.date, datetime.datetime)):
        return obj.isoformat()
    if isinstance(obj, Decimal):
        return float(obj)
    if isinstance(obj, (bytes, memoryview)):
        return obj.hex() if isinstance(obj, bytes) else bytes(obj).hex()
    return str(obj)


def connect(profile: str, database: str):
    w = WorkspaceClient(profile=profile)
    branch_path = f"projects/{PROJECT_ID}/branches/{BRANCH}"
    endpoint_path = f"{branch_path}/endpoints/{ENDPOINT}"

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

    return psycopg2.connect(
        host=host, port=5432, database=database,
        user=username, password=cred.token, sslmode="require",
    )


import logging

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("export_lakebase_to_json")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--profile", required=True, help="Databricks CLI profile for the SOURCE workspace.")
    ap.add_argument("--database", default="doccompare")
    ap.add_argument("--out-dir", default="staging/lakebase")
    args = ap.parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    conn = connect(args.profile, args.database)
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT table_name FROM information_schema.tables
                WHERE table_schema = 'public' AND table_type = 'BASE TABLE'
                ORDER BY table_name
                """
            )
            tables = [row[0] for row in cur.fetchall()]

        logger.info(f"{len(tables)} table(s) found (skipping {sorted(MIGRATE_TO_PROD_TABLES_TO_SKIP)}):")
        for table in tables:
            if table in MIGRATE_TO_PROD_TABLES_TO_SKIP:
                logger.info(f"  {table}... skipped")
                continue
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(f'SELECT * FROM "{table}"')
                rows = [dict(r) for r in cur.fetchall()]
            out_file = out_dir / f"{table}.json"
            with open(out_file, "w", encoding="utf-8") as f:
                for row in rows:
                    f.write(json.dumps(row, ensure_ascii=False, default=json_serializer) + "\n")
            logger.info(f"  {table}: {len(rows)} row(s) -> {out_file}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
