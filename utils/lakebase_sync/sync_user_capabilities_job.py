# Databricks notebook source
import logging
import os
import sys

from databricks.sdk import WorkspaceClient

# Parameters come through widgets: serverless tasks have no spark_env_vars. Classic compute cannot reach
# the Lakebase endpoint.
dbutils.widgets.removeAll()
dbutils.widgets.text("LAKEBASE_PROJECT_ID", "qualibot")
dbutils.widgets.text("LAKEBASE_BRANCH", "production")
dbutils.widgets.text("LAKEBASE_ENDPOINT", "primary")
dbutils.widgets.text("LAKEBASE_DATABASE", "doccompare")
dbutils.widgets.dropdown("DRY_RUN", "false", ["true", "false"])

LAKEBASE_PROJECT_ID = dbutils.widgets.get("LAKEBASE_PROJECT_ID")
LAKEBASE_BRANCH = dbutils.widgets.get("LAKEBASE_BRANCH")
LAKEBASE_ENDPOINT = dbutils.widgets.get("LAKEBASE_ENDPOINT")
LAKEBASE_DATABASE = dbutils.widgets.get("LAKEBASE_DATABASE")
DRY_RUN = dbutils.widgets.get("DRY_RUN") == "true"

sys.path.insert(0, os.path.dirname(os.getcwd()))
from ops_config import lakebase_connect_with
from user_capabilities import AUDIT_SQL, ensure_columns, merge_groups, read_stored_groups, write_groups

logger = logging.getLogger("sync_user_capabilities_job")
logger.setLevel(logging.INFO)
if not logger.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S"))
    logger.addHandler(_handler)
    logger.propagate = False

# COMMAND ----------

# The job identity needs SELECT on system.access.audit; without it this read fails and nothing is written.
events = [
    (row["user_id"], row["email"], row["group_name"], bool(row["is_member"]))
    for row in spark.sql(AUDIT_SQL).collect()
]
logger.info(f"{len(events)} audit event(s) on the tracked groups")

# COMMAND ----------

conn = lakebase_connect_with(
    WorkspaceClient(), LAKEBASE_PROJECT_ID, LAKEBASE_BRANCH, LAKEBASE_ENDPOINT, LAKEBASE_DATABASE
)
try:
    merged = merge_groups(events, read_stored_groups(conn))
    for user_id, entry in sorted(merged.items(), key=lambda item: item[1]["email"] or item[0]):
        logger.info(f"  {entry['email'] or user_id}: chat={entry['can_chat']} compare={entry['can_compare']} "
                    f"groups={entry['groups']}")
    if DRY_RUN:
        logger.info(f"DRY_RUN: {len(merged)} visitor(s) computed, nothing written")
    else:
        ensure_columns(conn)
        logger.info(f"{write_groups(conn, merged)} visitor(s) written to {LAKEBASE_DATABASE}.users")
finally:
    conn.close()
