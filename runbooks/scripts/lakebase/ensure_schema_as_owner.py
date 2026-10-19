"""Apply the app's Lakebase schema (tables, columns, indexes) under the identity of the person running it. One-shot, run by hand.

The app creates and alters tables under its own role, so it cannot alter a table owned by someone else ('must be owner of
table'). Run by the owner of those tables, this applies what the app could not. Tables owned by another role are skipped
with a warning. The statements are the ones of server/services/infra/lakebase/schema.py: additive only, no DROP, DELETE
or TRUNCATE.

From the repository root, with the profile of the workspace:

    $env:DATABRICKS_CONFIG_PROFILE = "UAT"
    $env:LAKEBASE_PROJECT_ID = "qualibot"
    $env:LAKEBASE_DATABASE = "doccompare"
    $env:COMPARE_VOLUME_PATH = "/Volumes/uat_proj/qualibot/doc_compare"
    python runbooks/scripts/lakebase/ensure_schema_as_owner.py

--print writes the DDL to ensure_schema.sql instead of connecting (when port 5432 is blocked): run that file in the
Lakebase SQL editor as the owner.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from server.services.infra.lakebase import connection, schema

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("ensure_schema_as_owner")

# Owned by the app (written by it) or by the parsing job: not this person's to alter.
NOT_MINE = ("compare_messages", "compare_feedbacks", "catalog_metadata", "doc_catalog")
# The table a DDL statement acts on (group 1).
DDL_TARGET = re.compile(r"^(?:ALTER TABLE|CREATE TABLE IF NOT EXISTS|CREATE INDEX IF NOT EXISTS \w+ ON) (\w+)")


class _Recorder:
    def __init__(self):
        self.statements: list[str] = []

    async def execute(self, query, *args):
        self.statements.append(" ".join(query.split()))

    async def fetch(self, query, *args):
        return []

    async def fetchval(self, query, *args):
        return False


class _RecordingPool:
    def __init__(self, recorder):
        self._recorder = recorder

    def acquire(self):
        recorder = self._recorder

        class _Ctx:
            async def __aenter__(self):
                return recorder

            async def __aexit__(self, *exc):
                return False

        return _Ctx()


async def run_direct() -> None:
    cfg = connection._cfg()
    host, username, token, _ = await asyncio.to_thread(
        connection._get_host_and_token, cfg["project_id"], cfg["branch"], cfg["endpoint"])
    logger.info("Connecting to %s as %s", cfg["database"], username)
    pool = await connection._build_pool(host, username, token, cfg["database"])
    try:
        await schema.ensure_schema(pool)
    finally:
        await pool.close()
    logger.info("Done. Statements on tables this role does not own were skipped (see the warnings above).")


async def run_print(path: Path) -> None:
    recorder = _Recorder()
    await schema.ensure_schema(_RecordingPool(recorder))
    kept = []
    for statement in recorder.statements:
        target = DDL_TARGET.match(statement)
        if target and target.group(1) not in NOT_MINE:
            kept.append(statement)
    path.write_text(";\n".join(kept) + ";\n", encoding="utf-8")
    logger.info("%d statements written to %s", len(kept), path)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--print", action="store_true", dest="print_only", help="write ensure_schema.sql instead of connecting")
    args = ap.parse_args()
    asyncio.run(run_print(Path("ensure_schema.sql")) if args.print_only else run_direct())


if __name__ == "__main__":
    main()
