"""Apply the idempotent Lakebase schema changes of migrations.py to one Lakebase database. One-shot, run by hand.

Runs from a workstation, not from the app or a bundle job. Connects with generate_database_credential, which gives
the DDL rights. Run from the repo root:

    python oneshot/lakebase/migrate_lakebase.py --env DEV
    python oneshot/lakebase/migrate_lakebase.py --env UAT
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

# Repo root is parents[2] of this file (oneshot/lakebase/): utils/ops_config.py is imported from there.
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "utils"))
from ops_config import LAKEBASE_PROJECTS, lakebase_connect

from migrations import MIGRATIONS, apply_migrations  # this folder, next to this script

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("migrate_lakebase")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--env", choices=list(LAKEBASE_PROJECTS), default="DEV",
                    help="Target Lakebase environment. Default: DEV")
    args = ap.parse_args()

    logger.info(f"Connecting to Lakebase {args.env}...")
    conn = lakebase_connect(args.env)
    try:
        applied, failed = apply_migrations(conn, MIGRATIONS)
        logger.info(f"Done: {len(applied)}/{len(MIGRATIONS)} migration(s) applied.")
        if failed:
            logger.warning(f"Failed ({len(failed)}):")
            for name, err in failed:
                logger.warning(f"  [{name}] {err.strip()}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
