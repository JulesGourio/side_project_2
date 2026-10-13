# Databricks notebook source
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

# The notebook's directory is on sys.path (migrations) and is the cwd: one level up is utils/ (ops_config).
sys.path.insert(0, os.path.dirname(os.getcwd()))
from migrations import MIGRATIONS, apply_migrations
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
        "This job is reserved for doccompare_test until the migration is validated."
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
