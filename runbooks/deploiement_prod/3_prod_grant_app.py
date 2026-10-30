# Databricks notebook source
# One-shot. Workspace PROD. Identity: the PROD job runner (service principal b82d3b5a-b633-4738-8dde-68bb2142cb02),
# which holds the grant rights on prod_proj; your own account does not (Workflows > Create job > task notebook = this
# notebook > Run as = that service principal > Run now).
# Gives the PROD app's service principal SELECT on the chat index, the one access the deploy does not give: the
# doc_compare volume (with USE CATALOG / USE SCHEMA) comes from the app's volume resource in databricks.yml, the MLflow
# trace tables from Z_2_Qualibot_Create_Chat_Experiment. The images volume is not read by the app. Grants only, never
# revokes; every statement is idempotent. Run it once 5_sync_index has created the index.

# COMMAND ----------

import logging

logger = logging.getLogger("grant_prod_app")
logging.basicConfig(level=logging.INFO)

APP_SP = "42ba83dd-3098-4d90-b2d2-a5df7d8beb1c"  # qualibot PROD app service principal (client id)
SCHEMA = "prod_proj.qualibot"
INDEX = "prod_proj.qualibot.chunks_index"
STATEMENTS = [
    f"GRANT USE CATALOG ON CATALOG prod_proj TO `{APP_SP}`",
    f"GRANT USE SCHEMA ON SCHEMA {SCHEMA} TO `{APP_SP}`",
]

# COMMAND ----------

failures = []
for statement in STATEMENTS:
    try:
        spark.sql(statement)
        logger.info("done: %s", statement)
    except Exception as error:
        failures.append(statement)
        logger.error("failed: %s: %s", statement, error)

try:
    spark.sql(f"GRANT SELECT ON TABLE {INDEX} TO `{APP_SP}`")
    logger.info("done: SELECT on %s", INDEX)
    index_done = True
except Exception as error:
    index_done = False
    logger.warning("SELECT on %s not applied (index not created yet?): %s", INDEX, error)

if failures:
    raise RuntimeError(f"grants not applied: {failures}")
logger.info("OK: %d grants applied%s", len(STATEMENTS), "" if index_done else "; index grant still to do, run again after the first parsing run")
