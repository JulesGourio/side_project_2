"""Export every Lakebase table to local JSON Lines files.

Authenticates with the CLI profile (--profile) of the workspace that hosts the source database.
The files are loaded by import_lakebase_from_json.py.

USAGE:
    python export_lakebase_to_json.py --profile job-runner-sa-uat --out-dir staging/lakebase
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

import psycopg2.extras
from databricks.sdk import WorkspaceClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # utils/: ops_config
from ops_config import MIGRATE_TO_PROD_TABLES_TO_SKIP, json_serializer, lakebase_connect_with

PROJECT_ID = "qualibot"
BRANCH = "production"
ENDPOINT = "primary"


def connect(profile: str, database: str):
    return lakebase_connect_with(WorkspaceClient(profile=profile), PROJECT_ID, BRANCH, ENDPOINT, database)


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
