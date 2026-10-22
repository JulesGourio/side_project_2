"""Empty the usage tables of the PROD Lakebase database. One-shot, run by hand from a workstation.

Keeps the project, the branch, the roles, the schema, `catalog_metadata` and `doc_catalog`. Run from the repo root:

    python runbooks/scripts/prod_cleanup/reset_prod_lakebase.py            # prints the row counts only
    python runbooks/scripts/prod_cleanup/reset_prod_lakebase.py --apply    # truncates
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "utils"))
from ops_config import lakebase_connect

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("reset_prod_lakebase")

TABLES = [
    "chat_turns", "chat_retrieved_chunks", "chat_messages", "chat_sessions", "chat_feedbacks", "app_errors",
    "compare_messages", "compare_feedbacks", "llm_requests", "impact_requests", "impact_document_results",
    "impact_feedbacks", "impact_cache", "summary_cache", "users",
]


def main() -> None:
    """Print the row count of each usage table, then truncate them when ``--apply`` is given.

    :return: None
    """
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true", help="Truncate the tables (default: only count the rows)")
    args = ap.parse_args()

    conn = lakebase_connect("PROD")
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'")
            existing = {row[0] for row in cur.fetchall()}
            present = [table for table in TABLES if table in existing]
            for table in present:
                cur.execute(f'SELECT count(*) FROM "{table}"')
                logger.info("%s: %s rows", table, cur.fetchone()[0])
            if not args.apply:
                logger.info("Nothing truncated (add --apply).")
                return
            cur.execute("TRUNCATE " + ", ".join(f'"{table}"' for table in present) + " RESTART IDENTITY CASCADE")
        conn.commit()
        logger.info("Truncated %d tables.", len(present))
    finally:
        conn.close()


if __name__ == "__main__":
    main()
