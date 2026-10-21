# Databricks notebook source
# One-shot: builds the table (IDDOC, division) holding the division of each document before the category tree changed.
# UAT: from the old chunks (one division per document). DEV: copy of the UAT table. Idempotent (replaces the table).

import logging

from pyspark.sql import Window
from pyspark.sql import functions as F

logger = logging.getLogger("create_division_history")
logging.basicConfig(level=logging.INFO)

dbutils.widgets.removeAll()
dbutils.widgets.text("source_table", "uat_landingzone.qualibot.chunks_v1")
dbutils.widgets.text("target_table", "uat_proj.qualibot.division_history")
SOURCE_TABLE = dbutils.widgets.get("source_table")
TARGET_TABLE = dbutils.widgets.get("target_table")

df_source = spark.table(SOURCE_TABLE).filter(F.col("IDDOC").isNotNull() & F.col("division").isNotNull())

# Most frequent division of the document's rows: the old chunks never mix divisions within a document.
df_counts = df_source.groupBy("IDDOC", "division").count()
df_history = (
    df_counts.withColumn("rank", F.row_number().over(Window.partitionBy("IDDOC").orderBy(F.desc("count"))))
    .filter(F.col("rank") == 1)
    .select("IDDOC", "division")
)

df_history.write.mode("overwrite").option("overwriteSchema", "true").saveAsTable(TARGET_TABLE)
logger.info("written: %s", TARGET_TABLE)
for row in spark.table(TARGET_TABLE).groupBy("division").count().orderBy(F.desc("count")).collect():
    logger.info("%s: %s documents", row["division"], row["count"])
