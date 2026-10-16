# Databricks notebook source
# One-shot: read access of the DEV job service principal to the Intraqual sources (volume and bronze tables).
# Submitted as the UAT job service principal, which already reads these sources and can grant on them.

import logging

logger = logging.getLogger("grant_dev_sources")
logging.basicConfig(level=logging.INFO)

PRINCIPAL = "fde6ff28-739f-4a41-b61e-604a298c8478"
STATEMENTS = [
    f"GRANT USE CATALOG ON CATALOG prod_landingzone TO `{PRINCIPAL}`",
    f"GRANT USE SCHEMA ON SCHEMA prod_landingzone.intraqual TO `{PRINCIPAL}`",
    f"GRANT READ VOLUME ON VOLUME prod_landingzone.intraqual.intraqual_documents TO `{PRINCIPAL}`",
    f"GRANT USE CATALOG ON CATALOG prod_bronze TO `{PRINCIPAL}`",
    f"GRANT USE SCHEMA ON SCHEMA prod_bronze.intraqual TO `{PRINCIPAL}`",
    f"GRANT SELECT ON SCHEMA prod_bronze.intraqual TO `{PRINCIPAL}`",
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
