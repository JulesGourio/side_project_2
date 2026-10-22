# Databricks notebook source
# One-shot: delete the Vector Search index uat_proj.qualibot.chunks_index_test and its writeback table. Deleting the index
# removes its online view and event log. Submitted as the UAT job service principal, which created the index.

import logging
import time

from databricks.sdk import WorkspaceClient
from databricks.sdk.errors import NotFound

logger = logging.getLogger("drop_uat_test_index")
logging.basicConfig(level=logging.INFO)

INDEX = "uat_proj.qualibot.chunks_index_test"
WRITEBACK_TABLE = f"{INDEX}_writeback_table"
DELETE_WAIT_MINUTES = 10

assert INDEX.endswith("_test"), INDEX

w = WorkspaceClient()

try:
    w.vector_search_indexes.delete_index(index_name=INDEX)
    logger.info("delete requested: %s", INDEX)
except NotFound:
    logger.info("index already absent: %s", INDEX)

deadline = time.time() + DELETE_WAIT_MINUTES * 60
while time.time() < deadline:
    try:
        w.vector_search_indexes.get_index(index_name=INDEX)
    except NotFound:
        break
    time.sleep(15)
else:
    raise RuntimeError(f"{INDEX} still exists after {DELETE_WAIT_MINUTES} min")

spark.sql(f"DROP TABLE IF EXISTS {WRITEBACK_TABLE}")
logger.info("index deleted, writeback table dropped (or absent): %s", WRITEBACK_TABLE)
