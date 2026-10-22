# Databricks notebook source
# One-shot reset of the PROD Unity Catalog objects of QualiBOT. Submitted as the PROD job service principal.
# Phase "prepare": creates prod_proj.qualibot and its volumes, copies docling_models and doc_compare from prod_landingzone.qualibot.
# Phase "drop": drops the Vector Search indexes and the tables of prod_proj.qualibot (MLflow trace tables kept), and the
# schema prod_landingzone.qualibot only when drop_old_schema is true, all only when the copied volumes are readable.
# prod_landingzone.intraqual is never touched.

import logging

from databricks.sdk import WorkspaceClient

logger = logging.getLogger("drop_prod_objects")
logging.basicConfig(level=logging.INFO)

dbutils.widgets.removeAll()
dbutils.widgets.dropdown("phase", "prepare", ["prepare", "drop"], "Phase")
dbutils.widgets.dropdown("dry_run", "true", ["true", "false"], "Dry run (list only)")
PHASE = dbutils.widgets.get("phase")
DRY_RUN = dbutils.widgets.get("dry_run") == "true"
dbutils.widgets.dropdown("drop_old_schema", "false", ["false", "true"], "Drop prod_landingzone.qualibot (its parsing state is cloned first)")
DROP_OLD_SCHEMA = dbutils.widgets.get("drop_old_schema") == "true"

OLD_SCHEMA = "prod_landingzone.qualibot"
NEW_SCHEMA = "prod_proj.qualibot"
COPIED_VOLUMES = ["docling_models", "doc_compare"]
EMPTY_VOLUMES = ["images"]

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

if PHASE == "prepare":
    run(f"CREATE SCHEMA IF NOT EXISTS {NEW_SCHEMA}")
    for volume in COPIED_VOLUMES + EMPTY_VOLUMES:
        run(f"CREATE VOLUME IF NOT EXISTS {NEW_SCHEMA}.{volume}")
    for volume in COPIED_VOLUMES:
        source = f"/Volumes/prod_landingzone/qualibot/{volume}"
        target = f"/Volumes/prod_proj/qualibot/{volume}"
        logger.info("%scopy %s -> %s", "[dry run] " if DRY_RUN else "", source, target)
        if not DRY_RUN:
            try:
                dbutils.fs.cp(source, target, recurse=True)
            except Exception as error:
                failures.append(f"copy {volume}")
                logger.error("copy failed: %s: %s", volume, error)

# COMMAND ----------

if PHASE == "drop":
    # The old schema holds the only copy of the offline Docling models until they are readable in prod_proj.
    for volume in COPIED_VOLUMES:
        path = f"/Volumes/prod_proj/qualibot/{volume}"
        entries = dbutils.fs.ls(path)
        logger.info("%s: %d entries", path, len(entries))
        if not entries:
            raise RuntimeError(f"{path} is empty: run the prepare phase before dropping {OLD_SCHEMA}")

    endpoints = [e.name for e in w.vector_search_endpoints.list_endpoints()]
    logger.info("Vector Search endpoints: %s", endpoints)
    for endpoint in endpoints:
        for index in w.vector_search_indexes.list_indexes(endpoint_name=endpoint):
            if index.name.startswith((OLD_SCHEMA + ".", NEW_SCHEMA + ".")):
                logger.info("%sindex: %s", "[dry run] " if DRY_RUN else "", index.name)
                if not DRY_RUN:
                    try:
                        w.vector_search_indexes.delete_index(index_name=index.name)
                    except Exception as error:
                        failures.append(index.name)
                        logger.error("index not deleted: %s: %s", index.name, error)

    for row in spark.sql(f"SHOW TABLES IN {NEW_SCHEMA}").collect():
        if "_otel_" in row.tableName:
            logger.info("kept (MLflow traces): %s", row.tableName)
        else:
            run(f"DROP TABLE IF EXISTS {NEW_SCHEMA}.`{row.tableName}`")

    if DROP_OLD_SCHEMA:
        run(f"DROP SCHEMA IF EXISTS {OLD_SCHEMA} CASCADE")
    else:
        logger.info("kept: %s (drop_old_schema = false)", OLD_SCHEMA)

# COMMAND ----------

if failures:
    raise RuntimeError(f"not done: {failures}")
