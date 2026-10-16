# Databricks notebook source
# One-shot: access of the UAT app service principal to uat_proj.qualibot. Submitted as the UAT job service principal,
# which holds the grant rights on uat_proj; a person does not.

import logging

logger = logging.getLogger("grant_uat_app")
logging.basicConfig(level=logging.INFO)

dbutils.widgets.removeAll()
dbutils.widgets.dropdown("include_index", "false", ["false", "true"])

APP_SP = "ed8abb86-e0db-423b-8f35-5cda58e89b80"
STATEMENTS = [
    f"GRANT USE CATALOG ON CATALOG uat_proj TO `{APP_SP}`",
    f"GRANT USE SCHEMA ON SCHEMA uat_proj.qualibot TO `{APP_SP}`",
]
if dbutils.widgets.get("include_index") == "true":
    STATEMENTS.append(f"GRANT SELECT ON TABLE uat_proj.qualibot.chunks_index TO `{APP_SP}`")

failures = []
for statement in STATEMENTS:
    try:
        spark.sql(statement)
        logger.info("done: %s", statement)
    except Exception as error:
        failures.append(statement)
        logger.error("failed: %s: %s", statement, error)

if failures:
    raise RuntimeError(f"grants not applied: {failures}")
