# Databricks notebook source
# PROD: SELECT on chunks_index for the app service principal, then deletion of the folders of the staging volume except
# the Lakebase exports (any folder whose name contains "lakebase"). Files at the root of the volume are listed, not
# deleted. Runs as the PROD job service principal, serverless, one cell.
import logging
import sys

from databricks.sdk import WorkspaceClient

logger = logging.getLogger("grant_index_clean_staging_prod")
logger.setLevel(logging.INFO)
if not logger.handlers:
    _handler = logging.StreamHandler(sys.stdout)
    _handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    logger.addHandler(_handler)
logger.propagate = False

APP_NAME = "qualibot"
INDEX = "prod_proj.qualibot.chunks_index"
STAGING = "/Volumes/prod_proj/qualibot/staging"
KEEP_MARKER = "lakebase"

w = WorkspaceClient()

app_sp = w.apps.get(name=APP_NAME).service_principal_client_id
logger.info("App %s runs as %s", APP_NAME, app_sp)
for statement in [f"GRANT USE CATALOG ON CATALOG prod_proj TO `{app_sp}`",
                  f"GRANT USE SCHEMA ON SCHEMA prod_proj.qualibot TO `{app_sp}`",
                  f"GRANT SELECT ON TABLE {INDEX} TO `{app_sp}`"]:
    spark.sql(statement)
    logger.info("done: %s", statement)
for row in spark.sql(f"SHOW GRANTS ON TABLE {INDEX}").collect():
    logger.info("grant on %s: %s %s", INDEX, row["Principal"], row["ActionType"])


def delete_tree(path: str) -> int:
    """Delete a volume folder and everything under it.

    :param path: absolute folder path in a volume
    :return: number of files deleted
    """
    deleted = 0
    for entry in w.files.list_directory_contents(path):
        if entry.is_directory:
            deleted += delete_tree(entry.path)
        else:
            w.files.delete(entry.path)
            deleted += 1
    w.files.delete_directory(path)
    return deleted


for entry in list(w.files.list_directory_contents(STAGING)):
    name = entry.path.rstrip("/").rsplit("/", 1)[-1]
    if not entry.is_directory:
        logger.info("kept (file at the root): %s", entry.path)
    elif KEEP_MARKER in name.lower():
        logger.info("kept (Lakebase export): %s", entry.path)
    else:
        logger.info("deleted: %s (%d files)", entry.path, delete_tree(entry.path.rstrip("/")))
logger.info("Left in %s: %s", STAGING, [e.path for e in w.files.list_directory_contents(STAGING)])
