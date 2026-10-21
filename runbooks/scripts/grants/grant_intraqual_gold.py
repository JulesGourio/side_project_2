# Databricks notebook source
# One-shot: read access of the UAT and DEV job service principals to the Intraqual data asset tables.
# Submitted as the UAT job service principal, which must hold GRANT rights on prod_gold.quality_system.

import logging

logger = logging.getLogger("grant_intraqual_gold")
logging.basicConfig(level=logging.INFO)

PRINCIPALS = [
    "3e5cd4e5-f765-4760-b974-ee7715258b39",
    "fde6ff28-739f-4a41-b61e-604a298c8478",
]
STATEMENTS = [
    statement
    for principal in PRINCIPALS
    for statement in (
        f"GRANT USE CATALOG ON CATALOG prod_gold TO `{principal}`",
        f"GRANT USE SCHEMA ON SCHEMA prod_gold.quality_system TO `{principal}`",
        f"GRANT SELECT ON TABLE prod_gold.quality_system.document_revisions_intraqual TO `{principal}`",
        f"GRANT SELECT ON TABLE prod_gold.quality_system.category_ancestors_intraqual TO `{principal}`",
    )
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
