# Databricks notebook source
# One-shot cleanup of the PROD Unity Catalog objects of QualiBOT. Submitted as the PROD job service principal.
# Drops the old schema prod_landingzone.qualibot, the tables and Vector Search indexes of prod_proj.qualibot. Keeps the volumes
# (docling_models, doc_compare) and the MLflow trace tables (*_otel_*). prod_landingzone.intraqual is never touched.

import logging

from databricks.sdk import WorkspaceClient

logger = logging.getLogger("drop_prod_objects")
logging.basicConfig(level=logging.INFO)

dbutils.widgets.removeAll()
dbutils.widgets.dropdown("dry_run", "true", ["true", "false"], "Dry run (list only)")
DRY_RUN = dbutils.widgets.get("dry_run") == "true"

OLD_SCHEMA = "prod_landingzone.qualibot"
NEW_SCHEMA = "prod_proj.qualibot"
VECTOR_SEARCH_ENDPOINT = "qualibot"
KEPT_VOLUMES = ["docling_models", "doc_compare"]

w = WorkspaceClient()
failures = []


def run(statement: str) -> None:
    """Run one statement, or only log it in dry run; a failure is collected, not raised.

    :param statement: SQL statement
    :return: None
    """
    logger.info("%s%s", "[dry run] " if DRY_RUN else "", statement)
    if DRY_RUN:
        return
    try:
        spark.sql(statement)
    except Exception as error:
        failures.append(statement)
        logger.error("failed: %s: %s", statement, error)


# COMMAND ----------

# The offline Docling models are slow to rebuild: stop if the copy in prod_proj is missing or empty.
for volume in KEPT_VOLUMES:
    path = f"/Volumes/prod_proj/qualibot/{volume}"
    try:
        entries = dbutils.fs.ls(path)
    except Exception as error:
        raise RuntimeError(f"volume {path} unreadable: {error}")
    logger.info("%s: %d entries", path, len(entries))
    if volume == "docling_models" and not entries:
        raise RuntimeError(f"{path} is empty: copy the models before dropping {OLD_SCHEMA}")

# COMMAND ----------

for schema in (OLD_SCHEMA, NEW_SCHEMA):
    try:
        indexes = [i.name for i in w.vector_search_indexes.list_indexes(endpoint_name=VECTOR_SEARCH_ENDPOINT)
                   if i.name.startswith(schema + ".")]
    except Exception as error:
        failures.append(f"list indexes {schema}")
        logger.error("indexes not listed: %s", error)
        continue
    for name in indexes:
        logger.info("%sindex: %s", "[dry run] " if DRY_RUN else "", name)
        if not DRY_RUN:
            try:
                w.vector_search_indexes.delete_index(index_name=name)
            except Exception as error:
                failures.append(name)
                logger.error("index not deleted: %s: %s", name, error)

# COMMAND ----------

rows = spark.sql(f"SHOW TABLES IN {NEW_SCHEMA}").collect()
for row in rows:
    if "_otel_" in row.tableName:
        logger.info("kept (MLflow traces): %s", row.tableName)
        continue
    run(f"DROP TABLE IF EXISTS {NEW_SCHEMA}.`{row.tableName}`")

# COMMAND ----------

if spark.sql(f"SHOW SCHEMAS IN prod_landingzone LIKE 'qualibot'").count():
    run(f"DROP SCHEMA IF EXISTS {OLD_SCHEMA} CASCADE")
else:
    logger.info("%s absent", OLD_SCHEMA)

if failures:
    raise RuntimeError(f"not done: {failures}")
