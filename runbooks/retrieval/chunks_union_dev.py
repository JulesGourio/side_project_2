# Databricks notebook source
# Chunks for PROD, built in DEV: every row of dev_proj.qualibot.chunks, plus every row of prod_proj.qualibot.chunks for
# the documents DEV does not have, written as Parquet files in the DEV staging volume (replaced on every run) to be
# copied to the PROD staging volume. Creates no table; reads PROD only. One cell, DEV workspace, your account.
import logging
import sys

import pyspark.sql.functions as F

logger = logging.getLogger("chunks_union_dev")
logger.setLevel(logging.INFO)
if not logger.handlers:
    _handler = logging.StreamHandler(sys.stdout)
    _handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    logger.addHandler(_handler)
logger.propagate = False

DEV_CHUNKS = "dev_proj.qualibot.chunks"
PROD_CHUNKS = "prod_proj.qualibot.chunks"
EXPORT_PATH = "/Volumes/dev_proj/qualibot/staging/chunks_union_prod"

dev = spark.table(DEV_CHUNKS)
# DEV may carry columns PROD lacks: PROD rows get them empty; the switch writes only the columns of PROD chunks.
prod_raw = spark.table(PROD_CHUNKS)
prod = prod_raw.select(*[F.col(f.name).cast(f.dataType).alias(f.name) if f.name in prod_raw.columns
                         else F.lit(None).cast(f.dataType).alias(f.name) for f in dev.schema.fields])
prod_only_docs = prod.join(dev.select("IDDOC").distinct(), "IDDOC", "left_anti")
dev.unionByName(prod_only_docs).repartition(16).write.mode("overwrite").parquet(EXPORT_PATH)

result = spark.read.parquet(EXPORT_PATH)
rows = result.count()
duplicates = rows - result.select("chunk_id").distinct().count()
missing_prod_docs = prod_raw.select("IDDOC").distinct().join(result.select("IDDOC").distinct(), "IDDOC",
                                                             "left_anti").count()
logger.info("%s: %d rows (DEV %d, PROD documents absent from DEV %d rows in %d documents), %d documents, "
            "%d duplicate chunk_id, %d PROD documents missing",
            EXPORT_PATH, rows, dev.count(), prod_only_docs.count(), prod_only_docs.select("IDDOC").distinct().count(),
            result.select("IDDOC").distinct().count(), duplicates, missing_prod_docs)
if duplicates or missing_prod_docs:
    raise RuntimeError("Duplicate chunk_id or missing PROD documents: do not copy these files to PROD")
