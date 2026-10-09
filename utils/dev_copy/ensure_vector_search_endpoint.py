# Databricks notebook source
# MAGIC %md
# MAGIC # DEV copy — make sure the Vector Search endpoint exists
# MAGIC
# MAGIC **Description:**
# MAGIC Second task of the DEV job `Z_1_Qualibot_Copy_Uat_To_Dev`. Creates the
# MAGIC Vector Search endpoint (STANDARD) if it doesn't exist yet and waits until
# MAGIC it is ONLINE, so the next task (`5_Sync_Vector_Indexes.py`, reused as is
# MAGIC from the parsing pipeline) can create the `chunks_index` on it.
# MAGIC Idempotent: an existing endpoint is left untouched.

# COMMAND ----------

import time

from databricks.sdk import WorkspaceClient
from databricks.sdk.errors import NotFound
from databricks.sdk.service.vectorsearch import EndpointType

dbutils.widgets.text("vector_search_endpoint", "qualibot")
dbutils.widgets.text("wait_minutes", "30")

NAME = dbutils.widgets.get("vector_search_endpoint").strip()
WAIT_MINUTES = int(dbutils.widgets.get("wait_minutes") or "0")

w = WorkspaceClient()

import logging

logger = logging.getLogger("ensure_vector_search_endpoint")
logger.setLevel(logging.INFO)
if not logger.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S"))
    logger.addHandler(_handler)
    logger.propagate = False


try:
    ep = w.vector_search_endpoints.get_endpoint(endpoint_name=NAME)
    logger.info(f"Endpoint {NAME} already exists.")
except NotFound:
    logger.info(f"Endpoint {NAME} not found — creating (STANDARD)...")
    w.vector_search_endpoints.create_endpoint(name=NAME, endpoint_type=EndpointType.STANDARD)
    ep = w.vector_search_endpoints.get_endpoint(endpoint_name=NAME)

deadline = time.time() + WAIT_MINUTES * 60
while True:
    state = str(ep.endpoint_status.state.value if ep.endpoint_status and ep.endpoint_status.state else "UNKNOWN")
    logger.info(f"{NAME}: {state}")
    if state == "ONLINE":
        break
    if state in ("OFFLINE", "RED_STATE", "DELETED"):
        raise RuntimeError(f"Endpoint {NAME} is {state}: {ep.endpoint_status.message if ep.endpoint_status else ''}")
    if time.time() > deadline:
        raise RuntimeError(f"Endpoint {NAME} still {state} after {WAIT_MINUTES} min — re-run the job once it is ONLINE")
    time.sleep(30)
    ep = w.vector_search_endpoints.get_endpoint(endpoint_name=NAME)
