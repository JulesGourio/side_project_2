# Databricks notebook source
# One-shot cleanup of the uat-test tables and volumes in uat_proj.qualibot. Submitted as the UAT job service principal, which owns them. The MLflow experiment is deleted by hand (runbook UAT 0.9 step 5): mlflow is not in the default environment.
# The names are written out one by one: no pattern match, so nothing else in the schema can be dropped.

import logging

logger = logging.getLogger("drop_uat_test_objects")
logging.basicConfig(level=logging.INFO)

SCHEMA = "uat_proj.qualibot"
TABLES = [
    "_pipeline_checkpoint_test",
    "category_reference_test",
    "chunks_archive_notices_test",
    "chunks_archive_test",
    "chunks_test",
    "document_change_log_test",
    "image_metadata_test",
    "parse_manifest_test",
    "parsing_run_health_test",
    "processed_files_test",
]
VOLUMES = ["doc_compare_test", "test_test", "images_test"]

for name in TABLES + VOLUMES:
    assert name.endswith("_test"), name

failures = []

for table in TABLES:
    full_name = f"{SCHEMA}.{table}"
    try:
        spark.sql(f"DROP TABLE IF EXISTS {full_name}")
        logger.info("table dropped (or absent): %s", full_name)
    except Exception as error:
        failures.append(full_name)
        logger.error("table not dropped: %s: %s", full_name, error)

for volume in VOLUMES:
    full_name = f"{SCHEMA}.{volume}"
    try:
        spark.sql(f"DROP VOLUME IF EXISTS {full_name}")
        logger.info("volume dropped (or absent): %s", full_name)
    except Exception as error:
        failures.append(full_name)
        logger.error("volume not dropped: %s: %s", full_name, error)

if failures:
    raise RuntimeError(f"objects not dropped: {failures}")
