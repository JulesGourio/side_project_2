# Databricks notebook source
# Chunks for PROD, built in DEV: every row of dev_proj.qualibot.chunks, plus the rows of prod_proj.qualibot.chunks that
# DEV lacks (documents only PROD has, and image passages of shared documents absent from DEV). Text passages of a
# document DEV has always come from DEV. A PROD image whose page and size match an image DEV already describes in the
# same document is the same image numbered differently: it is left out. Writes dev_proj.qualibot.chunks_union_prod;
# reads PROD only. One cell, DEV workspace, your account.
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
DEV_IMAGES = "dev_proj.qualibot.image_metadata"
PROD_IMAGES = "prod_proj.qualibot.image_metadata"
TARGET_TABLE = "dev_proj.qualibot.chunks_union_prod"
IMAGE_ID_RE = r"-IMG-(\d+)(-\d+)?$"
IMAGE_KEY = ["IDDOC", "page_no", "image_width", "image_height"]


def with_image_key(chunks, images):
    """Image passages with the page and size of their image.

    :param chunks: image passages, ``chunk_id`` = ``IDDOC-IMG-NNN`` or ``IDDOC-IMG-NNN-k``
    :param images: ``image_metadata`` table of the same environment
    :return: the passages with ``page_no``, ``image_width``, ``image_height`` (null when the image is unknown)
    """
    sizes = images.select(F.col("IDDOC").cast("string").alias("_doc"), F.col("image_id").cast("int").alias("_img"),
                          "page_no", "image_width", "image_height").dropDuplicates(["_doc", "_img"])
    keyed = chunks.withColumn("_doc", F.col("IDDOC").cast("string")).withColumn(
        "_img", F.regexp_extract("chunk_id", IMAGE_ID_RE, 1).try_cast("int"))
    return keyed.join(sizes, ["_doc", "_img"], "left").drop("_doc", "_img")


dev = spark.table(DEV_CHUNKS)
# DEV may carry columns PROD lacks: PROD rows get them empty; the switch writes only the columns of PROD chunks.
prod_raw = spark.table(PROD_CHUNKS)
prod = prod_raw.select(*[F.col(f.name).cast(f.dataType).alias(f.name) if f.name in prod_raw.columns
                         else F.lit(None).cast(f.dataType).alias(f.name) for f in dev.schema.fields])
dev_docs = dev.select("IDDOC").distinct()
dev_ids = dev.select("chunk_id").distinct()

prod_only_docs = prod.join(dev_docs, "IDDOC", "left_anti")
prod_images_missing = (prod.where(F.col("chunk_content_type") == "image")
                       .join(dev_docs, "IDDOC", "left_semi")
                       .join(dev_ids, "chunk_id", "left_anti"))
dev_described = (with_image_key(dev.where(F.col("chunk_content_type") == "image"), spark.table(DEV_IMAGES))
                 .where(F.col("page_no").isNotNull()).select(*IMAGE_KEY).distinct())
prod_images_keyed = with_image_key(prod_images_missing, spark.table(PROD_IMAGES))
prod_images_duplicate = prod_images_keyed.join(dev_described, IMAGE_KEY, "left_semi")
prod_images_new = prod_images_keyed.join(dev_described, IMAGE_KEY, "left_anti").select(*dev.columns)
union = dev.unionByName(prod_only_docs).unionByName(prod_images_new)

union.write.format("delta").mode("overwrite").option("overwriteSchema", "true").saveAsTable(TARGET_TABLE)
result = spark.table(TARGET_TABLE)

rows = result.count()
duplicates = rows - result.select("chunk_id").distinct().count()
logger.info("%s: %d rows (DEV %d, PROD documents absent from DEV %d rows, new PROD images %d rows), "
            "%d documents, %d duplicate chunk_id; PROD images left out as already described in DEV: %d rows",
            TARGET_TABLE, rows, dev.count(), prod_only_docs.count(), prod_images_new.count(),
            result.select("IDDOC").distinct().count(), duplicates, prod_images_duplicate.count())
missing_prod_docs = spark.table(PROD_CHUNKS).select("IDDOC").distinct().join(result.select("IDDOC").distinct(),
                                                                             "IDDOC", "left_anti").count()
logger.info("PROD documents absent from %s: %d", TARGET_TABLE, missing_prod_docs)
if duplicates or missing_prod_docs:
    raise RuntimeError("The union has duplicate chunk_id or misses PROD documents: do not export it")
