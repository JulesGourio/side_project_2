"""Read-only inventory of a Lakebase database (conversations and history of the app). Run from the repo root:

    python runbooks/deploiement_prod/inventaire_lakebase.py UAT
    python runbooks/deploiement_prod/inventaire_lakebase.py PROD

Uses the CLI profile of that environment. The session is read-only: nothing can be written or deleted.
For each table of the database: rows, first and last date, distinct users.
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "utils"))
from ops_config import lakebase_connect

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("inventaire_lakebase_prod")

DATE_COLUMNS = ("created_at", "updated_at", "timestamp", "submitted_at")
USER_COLUMNS = ("user_id", "user_email", "email", "user_name")


def main() -> None:
    """Log rows, date range and distinct users of every table of the Lakebase database of the given environment.

    :return: None
    """
    env = (sys.argv[1] if len(sys.argv) > 1 else "PROD").upper()
    logger.info("Lakebase %s (read-only)", env)
    conn = lakebase_connect(env)
    conn.set_session(readonly=True)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT table_schema, table_name FROM information_schema.tables "
                "WHERE table_type = 'BASE TABLE' AND table_schema NOT IN ('pg_catalog', 'information_schema') "
                "ORDER BY 1, 2"
            )
            tables = cur.fetchall()
            for schema, table in tables:
                cur.execute(
                    "SELECT column_name FROM information_schema.columns WHERE table_schema = %s AND table_name = %s",
                    (schema, table),
                )
                columns = {row[0] for row in cur.fetchall()}
                name = f'"{schema}"."{table}"'
                cur.execute(f"SELECT count(*) FROM {name}")
                line = f"{schema}.{table}: {cur.fetchone()[0]} rows"
                date_col = next((c for c in DATE_COLUMNS if c in columns), None)
                if date_col:
                    cur.execute(f'SELECT min("{date_col}"), max("{date_col}") FROM {name}')
                    first, last = cur.fetchone()
                    line += f" | {date_col} {first} -> {last}"
                user_col = next((c for c in USER_COLUMNS if c in columns), None)
                if user_col:
                    cur.execute(f'SELECT count(DISTINCT "{user_col}") FROM {name}')
                    line += f" | {cur.fetchone()[0]} distinct {user_col}"
                logger.info(line)
        logger.info("%d tables read, nothing modified.", len(tables))
    finally:
        conn.close()


if __name__ == "__main__":
    main()
