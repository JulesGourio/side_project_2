# Databricks notebook source
"""Notebook version of migrate_lakebase.py: applies migrations.py to a Lakebase database from a workspace job. One-shot.

Not deployed (runbooks/scripts/ is excluded from the bundle sync) and no bundle job runs it any more. To run it by hand: import
this file into the workspace with `databricks workspace import <path> --file runbooks/scripts/lakebase/migrate_lakebase_job.py
--format SOURCE --language PYTHON --overwrite --profile <profile>`, then run it with the widgets. The default
database is doccompare_test; the real doccompare database of UAT is refused.
"""
import logging
import os
import sys

from databricks.sdk import WorkspaceClient

# Serverless job tasks have no spark_env_vars: parameters come through widgets, whose defaults also serve interactive
# runs.
dbutils.widgets.removeAll()
dbutils.widgets.text("LAKEBASE_PROJECT_ID", "qualibot")
dbutils.widgets.text("LAKEBASE_BRANCH", "production")
dbutils.widgets.text("LAKEBASE_ENDPOINT", "primary")
# Safe by default: meant for the disposable doccompare_test database. Never default to "doccompare" (real UAT data).
dbutils.widgets.text("LAKEBASE_DATABASE", "doccompare_test")

LAKEBASE_PROJECT_ID = dbutils.widgets.get("LAKEBASE_PROJECT_ID")
LAKEBASE_BRANCH = dbutils.widgets.get("LAKEBASE_BRANCH")
LAKEBASE_ENDPOINT = dbutils.widgets.get("LAKEBASE_ENDPOINT")
LAKEBASE_DATABASE = dbutils.widgets.get("LAKEBASE_DATABASE")

# The cwd is this folder (runbooks/scripts/lakebase/): the repo root is three levels up, utils/ holds ops_config.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.getcwd()))), "utils"))
from migrations import MIGRATIONS, apply_migrations  # this folder
from ops_config import lakebase_connect_with

logger = logging.getLogger("migrate_lakebase_job")
logger.setLevel(logging.INFO)
if not logger.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S"))
    logger.addHandler(_handler)
    logger.propagate = False

w = WorkspaceClient()

# "doccompare" is the real UAT database in the UAT workspace; the DEV copy has its own disposable one.
_UAT_HOST = "dbc-3a17bfce-9e88.cloud.databricks.com"
if LAKEBASE_DATABASE == "doccompare" and _UAT_HOST in (w.config.host or ""):
    raise RuntimeError(
        "LAKEBASE_DATABASE='doccompare' rejected on this job: it is the real UAT database. "
        "This job only writes to doccompare_test."
    )

logger.info(f"Connecting to Lakebase {LAKEBASE_PROJECT_ID}/{LAKEBASE_BRANCH}/{LAKEBASE_DATABASE}...")
conn = lakebase_connect_with(w, LAKEBASE_PROJECT_ID, LAKEBASE_BRANCH, LAKEBASE_ENDPOINT, LAKEBASE_DATABASE)
try:
    applied, failed = apply_migrations(conn, MIGRATIONS)
    logger.info(f"Done: {len(applied)}/{len(MIGRATIONS)} migration(s) applied to {LAKEBASE_DATABASE}.")
    if failed:
        logger.warning(f"Failed ({len(failed)}):")
        for name, err in failed:
            logger.warning(f"  [{name}] {err.strip()}")
finally:
    conn.close()
