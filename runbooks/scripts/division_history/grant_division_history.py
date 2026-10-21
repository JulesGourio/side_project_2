# Databricks notebook source
# One-shot: read access of the DEV job service principal to the UAT division history table, so the DEV table can be copied from it.
# Submitted as the UAT job service principal, which owns the table.

import logging

logger = logging.getLogger("grant_division_history")
logging.basicConfig(level=logging.INFO)

PRINCIPAL = "fde6ff28-739f-4a41-b61e-604a298c8478"
STATEMENTS = [
    f"GRANT USE CATALOG ON CATALOG uat_proj TO `{PRINCIPAL}`",
    f"GRANT USE SCHEMA ON SCHEMA uat_proj.qualibot TO `{PRINCIPAL}`",
    f"GRANT SELECT ON TABLE uat_proj.qualibot.division_history TO `{PRINCIPAL}`",
]

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
