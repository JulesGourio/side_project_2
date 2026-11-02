# Databricks notebook source
# Rechunk of PROD, export: writes dev_proj.qualibot.chunks_rechunk_prod as Parquet files in the DEV staging volume, to be
# copied to the PROD staging volume. Reads one table, writes only that folder. One cell, DEV workspace, your account.
import logging
import sys

logger = logging.getLogger("rechunk_export_dev")
logger.setLevel(logging.INFO)
if not logger.handlers:
    _handler = logging.StreamHandler(sys.stdout)
    _handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    logger.addHandler(_handler)
logger.propagate = False

SOURCE_TABLE = "dev_proj.qualibot.chunks_rechunk_prod"
EXPORT_PATH = "/Volumes/dev_proj/qualibot/staging/chunks_rechunk_prod"

df = spark.table(SOURCE_TABLE)
rows = df.count()
ids = df.select("chunk_id").distinct().count()
if rows == 0 or ids != rows:
    raise RuntimeError(f"{SOURCE_TABLE}: {rows} rows, {rows - ids} duplicate chunk_id: nothing is exported")
df.repartition(16).write.mode("overwrite").parquet(EXPORT_PATH)
written = spark.read.parquet(EXPORT_PATH).count()
logger.info("%s: %d rows exported to %s (read back: %d)", SOURCE_TABLE, rows, EXPORT_PATH, written)
