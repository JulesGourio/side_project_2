# Databricks notebook source
# Chunks for PROD, built in DEV: every row of dev_proj.qualibot.chunks, plus the rows of prod_proj.qualibot.chunks that
# DEV lacks (documents only PROD has, and image passages of shared documents absent from DEV). Text passages of a
# document DEV has always come from DEV. Writes dev_proj.qualibot.chunks_union_prod; reads PROD only. One cell, DEV
# workspace, your account.
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
TARGET_TABLE = "dev_proj.qualibot.chunks_union_prod"

dev = spark.table(DEV_CHUNKS)
prod = spark.table(PROD_CHUNKS).select(*dev.columns)
dev_docs = dev.select("IDDOC").distinct()
dev_ids = dev.select("chunk_id").distinct()

prod_only_docs = prod.join(dev_docs, "IDDOC", "left_anti")
prod_images_missing = (prod.where(F.col("chunk_content_type") == "image")
                       .join(dev_docs, "IDDOC", "left_semi")
                       .join(dev_ids, "chunk_id", "left_anti"))
union = dev.unionByName(prod_only_docs).unionByName(prod_images_missing)

union.write.format("delta").mode("overwrite").option("overwriteSchema", "true").saveAsTable(TARGET_TABLE)
result = spark.table(TARGET_TABLE)

rows = result.count()
duplicates = rows - result.select("chunk_id").distinct().count()
logger.info("%s: %d rows (DEV %d, PROD documents absent from DEV %d rows, PROD images absent from DEV %d rows), "
            "%d documents, %d duplicate chunk_id",
            TARGET_TABLE, rows, dev.count(), prod_only_docs.count(), prod_images_missing.count(),
            result.select("IDDOC").distinct().count(), duplicates)
missing_prod_docs = spark.table(PROD_CHUNKS).select("IDDOC").distinct().join(result.select("IDDOC").distinct(),
                                                                             "IDDOC", "left_anti").count()
logger.info("PROD documents absent from %s: %d", TARGET_TABLE, missing_prod_docs)
if duplicates or missing_prod_docs:
    raise RuntimeError("The union has duplicate chunk_id or misses PROD documents: do not export it")
