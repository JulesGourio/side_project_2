"""Apply the idempotent Lakebase schema migrations (migrations.py) from a workstation.

Independent of the app deployment: connects with generate_database_credential for full DDL rights.
Run it after every new column or table is added to migrations.py.

    python utils/lakebase_sync/migrate_lakebase.py --env DEV
    python utils/lakebase_sync/migrate_lakebase.py --env UAT
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # utils/: ops_config
from ops_config import LAKEBASE_PROJECTS, lakebase_connect

sys.path.insert(0, str(Path(__file__).resolve().parent))  # this directory: migrations
from migrations import MIGRATIONS, apply_migrations

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
