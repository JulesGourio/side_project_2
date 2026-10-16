# Databricks notebook source
# One-shot: removes the former DEV schema dev_landingzone.qualibot (index, tables, volumes, then the schema).
# Only that schema is ever touched: the catalog and its other schemas stay as they are.

import logging

logger = logging.getLogger("drop_dev_landingzone")
logging.basicConfig(level=logging.INFO)

# First run: keep the next line (lists only). Second run: comment it and uncomment the line below it (drops).
DRY_RUN = True
# DRY_RUN = False

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


for endpoint in client.vector_search_endpoints.list_endpoints():
    for found in client.vector_search_indexes.list_indexes(endpoint_name=endpoint.name):
        logger.info("index seen: %s (endpoint %s)", found.name, endpoint.name)
        if found.name.startswith(SCHEMA + ".") and found.name not in INDEXES:
            INDEXES.append(found.name)

hidden = []
for row in spark.sql(
    f"SELECT table_name, table_type FROM dev_landingzone.information_schema.tables WHERE table_schema = 'qualibot'"
).collect():
    logger.info("information_schema: %s (%s)", row["table_name"], row["table_type"])
    hidden.append(row["table_name"])

for index in INDEXES:
    assert index.startswith(SCHEMA + "."), index
    run(f"index {index}", (lambda i=index: None) if DRY_RUN else (lambda i=index: client.vector_search_indexes.delete_index(i)))

tables = [r["tableName"] for r in spark.sql(f"SHOW TABLES IN {SCHEMA}").collect()]
for table in tables:
    full = f"{SCHEMA}.{table}"
    rows = spark.table(full).count() if table not in {i.split('.')[-1] for i in INDEXES} else "index"
    logger.info("table %s (%s rows)", full, rows)
    run(f"table {full}", (lambda: None) if DRY_RUN else (lambda f=full: spark.sql(f"DROP TABLE IF EXISTS {f}")))

def drop_leftover(full):
    try:
        spark.sql(f"DROP TABLE IF EXISTS {full}")
    except Exception:
        spark.sql(f"DROP VIEW IF EXISTS {full}")


# Internal objects of a deleted index (event log, online view) that SHOW TABLES does not list.
for name in hidden:
    full = f"{SCHEMA}.`{name}`"
    run(f"leftover {full}", (lambda: None) if DRY_RUN else (lambda f=full: drop_leftover(f)))

volumes = [r["volume_name"] for r in spark.sql(f"SHOW VOLUMES IN {SCHEMA}").collect()]
for volume in volumes:
    full = f"{SCHEMA}.{volume}"
    run(f"volume {full}", (lambda: None) if DRY_RUN else (lambda f=full: spark.sql(f"DROP VOLUME IF EXISTS {f}")))

run(f"schema {SCHEMA}", (lambda: None) if DRY_RUN else (lambda: spark.sql(f"DROP SCHEMA IF EXISTS {SCHEMA}")))

if failures:
    raise RuntimeError(f"not dropped: {failures}")
