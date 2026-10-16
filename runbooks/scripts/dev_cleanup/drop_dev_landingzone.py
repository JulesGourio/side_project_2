# Databricks notebook source
# One-shot: removes the former DEV schema dev_landingzone.qualibot (index, tables, volumes, then the schema).
# Submitted as the DEV job service principal. Only that schema is ever touched; dry_run lists without dropping.

import logging

logger = logging.getLogger("drop_dev_landingzone")
logging.basicConfig(level=logging.INFO)

dbutils.widgets.removeAll()
dbutils.widgets.dropdown("dry_run", "true", ["true", "false"])
DRY_RUN = dbutils.widgets.get("dry_run") == "true"

SCHEMA = "dev_landingzone.qualibot"
INDEXES = [f"{SCHEMA}.chunks_index"]

from databricks.sdk import WorkspaceClient

client = WorkspaceClient()
failures = []


def run(label, action):
    try:
        action()
        logger.info("%s: %s", "would drop" if DRY_RUN else "dropped", label)
    except Exception as error:
        failures.append(label)
        logger.error("failed: %s: %s", label, error)


for index in INDEXES:
    assert index.startswith(SCHEMA + "."), index
    run(f"index {index}", (lambda i=index: None) if DRY_RUN else (lambda i=index: client.vector_search_indexes.delete_index(i)))

tables = [r["tableName"] for r in spark.sql(f"SHOW TABLES IN {SCHEMA}").collect()]
for table in tables:
    full = f"{SCHEMA}.{table}"
    rows = spark.table(full).count() if table not in {i.split('.')[-1] for i in INDEXES} else "index"
    logger.info("table %s (%s rows)", full, rows)
    run(f"table {full}", (lambda: None) if DRY_RUN else (lambda f=full: spark.sql(f"DROP TABLE IF EXISTS {f}")))

volumes = [r["volume_name"] for r in spark.sql(f"SHOW VOLUMES IN {SCHEMA}").collect()]
for volume in volumes:
    full = f"{SCHEMA}.{volume}"
    run(f"volume {full}", (lambda: None) if DRY_RUN else (lambda f=full: spark.sql(f"DROP VOLUME IF EXISTS {f}")))

run(f"schema {SCHEMA}", (lambda: None) if DRY_RUN else (lambda: spark.sql(f"DROP SCHEMA IF EXISTS {SCHEMA}")))

if failures:
    raise RuntimeError(f"not dropped: {failures}")
