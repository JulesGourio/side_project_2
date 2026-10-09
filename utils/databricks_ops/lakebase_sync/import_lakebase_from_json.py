"""Import Lakebase JSON exports (see export_lakebase_to_json.py) into the
TARGET workspace's Lakebase database.

Overwrites existing rows: DELETE then INSERT per table, all in one
transaction (a failure midway rolls back to the target's exact prior state).
Meant to run as a CI pipeline step authenticated as whichever identity has
access to the TARGET workspace (--profile) -- no personal login role needed.

Prerequisite: the target app must have started at least once so its own
_ensure_schema() (server/services/lakebase.py) already created the tables.

USAGE:
    python import_lakebase_from_json.py --profile job-runner-sa-prod --in-dir staging/lakebase
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import psycopg2.extras
from databricks.sdk import WorkspaceClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # utils/databricks_ops/ — shared config.py
from config import MIGRATE_TO_PROD_TABLE_ORDER

PROJECT_ID = "qualibot"
BRANCH = "production"
ENDPOINT = "primary"


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


def ordered_files(in_dir: Path) -> list[Path]:
    available = {p.stem: p for p in in_dir.glob("*.json")}
    ordered = [available.pop(t) for t in MIGRATE_TO_PROD_TABLE_ORDER if t in available]
    return ordered + [available[k] for k in sorted(available)]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--profile", required=True, help="Databricks CLI profile for the TARGET workspace.")
    ap.add_argument("--database", default="doccompare")
    ap.add_argument("--in-dir", default="staging/lakebase")
    args = ap.parse_args()

    in_dir = Path(args.in_dir)
    files = ordered_files(in_dir)
    if not files:
        raise SystemExit(f"No JSON file found in {in_dir}")

    conn = connect(args.profile, args.database)
    conn.autocommit = False
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT table_name FROM information_schema.tables
                WHERE table_schema = 'public' AND table_type = 'BASE TABLE'
                """
            )
            existing = {row[0] for row in cur.fetchall()}

        for f in files:
            table = f.stem
            if table not in existing:
                raise RuntimeError(
                    f"Table '{table}' does not exist on the target — "
                    "start the app at least once so it creates its schema first."
                )
            rows = [json.loads(line) for line in f.read_text(encoding="utf-8").splitlines() if line.strip()]
            with conn.cursor() as cur:
                # JSON/JSONB columns (chat_turns.config, chat_retrieved_chunks.hits, errors.context…)
                # come back from the export as dicts/lists: psycopg2 cannot adapt a dict and would
                # send a list as a Postgres ARRAY, so they are wrapped explicitly.
                cur.execute(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema = 'public' AND table_name = %s AND data_type IN ('json', 'jsonb')",
                    (table,),
                )
                json_columns = {row[0] for row in cur.fetchall()}
                cur.execute(f'DELETE FROM "{table}"')
                if rows:
                    columns = list(rows[0].keys())
                    col_list = ", ".join(f'"{c}"' for c in columns)
                    values = [[psycopg2.extras.Json(r[c]) if c in json_columns and r.get(c) is not None
                               else r.get(c) for c in columns] for r in rows]
                    psycopg2.extras.execute_values(cur, f'INSERT INTO "{table}" ({col_list}) VALUES %s', values)
                # Explicit-id inserts don't advance SERIAL sequences — without this,
                # the app's next INSERT reuses an id already imported and hits a
                # duplicate-key error on the primary key.
                cur.execute("SELECT pg_get_serial_sequence(%s, 'id')", (table,))
                seq = cur.fetchone()[0]
                if seq:
                    cur.execute(f'SELECT setval(%s, COALESCE((SELECT MAX(id) FROM "{table}"), 1))', (seq,))
            print(f"  {table}: {len(rows)} row(s) imported")

        conn.commit()
        print("Done, committed.")
    except Exception:
        conn.rollback()
        print("Failed — rolled back, target untouched.", file=sys.stderr)
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    main()
