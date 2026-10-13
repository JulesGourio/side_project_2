# Databricks notebook source
# MAGIC %md
# MAGIC # 06 — Maintenance: Rebuild `image_metadata` (PROD) from the checkpoint
# MAGIC
# MAGIC **Description:**
# MAGIC This notebook writes to the PRODUCTION `image_metadata` table (full
# MAGIC overwrite). It does no re-parsing and no LLM call: image files are already
# MAGIC present on the volume, this notebook only rebuilds the metadata from
# MAGIC `_pipeline_checkpoint` + `parse_manifest` (for `doc_date`, absent from the
# MAGIC checkpoint).
# MAGIC
# MAGIC **When to use it:** after a schema/logic change in `image_metadata` (e.g.
# MAGIC new status, recomputed column) that doesn't warrant a full re-parse.
# MAGIC
# MAGIC **Highlighted complexities:**
# MAGIC This logic used to live in `View_Image_Descriptions.py` (a notebook
# MAGIC documented as "fully isolated from production") — extracted here so that
# MAGIC 03 actually keeps its isolation promise, and any action touching prod
# MAGIC lives in a notebook explicitly named as such.
# MAGIC
# MAGIC **Intended Pipeline**
# MAGIC - Qualibot Parsing Pipeline — Manual maintenance (not part of the daily chain)
# MAGIC
# MAGIC **Input Tables Pipeline**
# MAGIC - `{PARSING_CATALOG_SCHEMA}._pipeline_checkpoint`
# MAGIC - `{PARSING_CATALOG_SCHEMA}.parse_manifest`
# MAGIC
# MAGIC **Inputs Reference Data**
# MAGIC - *(none)*
# MAGIC
# MAGIC **Output Tables (Pipeline)**
# MAGIC - `{PARSING_CATALOG_SCHEMA}.image_metadata` (full overwrite, PROD only — gated by `CONFIRM_OVERWRITE`)

# COMMAND ----------

# MAGIC %md
# MAGIC # Technical Debt
# MAGIC
# MAGIC None.

# COMMAND ----------

# MAGIC %md
# MAGIC # Configuration

# COMMAND ----------

# MAGIC %md
# MAGIC ## Imports

# COMMAND ----------

import os
import sys
import uuid
import time
import importlib

_ctx = dbutils.notebook.entry_point.getDbutils().notebook().getContext()
REPO_DIR = "/Workspace" + os.path.dirname(_ctx.notebookPath().get())
sys.path.insert(0, REPO_DIR)

from pyspark.sql import functions as F
from pyspark.sql import Window

import image_utils
from config import CATALOG_SCHEMA, DOC_DATE_CUTOFF, PARSE_MANIFEST_TABLE

importlib.reload(image_utils)

# COMMAND ----------

# MAGIC %md
# MAGIC ## Widgets and Constants

# COMMAND ----------

dbutils.widgets.dropdown("CONFIRM_OVERWRITE", "non", ["oui", "non"],
                         "CONFIRM_OVERWRITE — 'oui' to overwrite image_metadata (PROD)")
CONFIRM_OVERWRITE = dbutils.widgets.get("CONFIRM_OVERWRITE").lower() == "oui"

PROD_CHECKPOINT     = f"{CATALOG_SCHEMA}._pipeline_checkpoint"
PROD_IMAGE_METADATA = f"{CATALOG_SCHEMA}.image_metadata"
_cutoff         = F.lit(DOC_DATE_CUTOFF).cast("date")
_rebuild_run_id = str(uuid.uuid4())

print(f"Checkpoint (source)     : {PROD_CHECKPOINT}")
print(f"image_metadata (overwritten): {PROD_IMAGE_METADATA}")

# COMMAND ----------

# MAGIC %md
# MAGIC # Inputs

# COMMAND ----------

df_chk_raw = spark.table(PROD_CHECKPOINT).filter(F.col("parse_status") == "SUCCESS")
df_dates_raw = (
    spark.table(PARSE_MANIFEST_TABLE)
    .select(F.col("IDDOC").cast("long").alias("IDDOC"), F.col("doc_date"))
)

# COMMAND ----------

# MAGIC %md
# MAGIC # Data Preparation

# COMMAND ----------

# MAGIC %md
# MAGIC ## Checkpoint: dedupe by IDDOC (several runs -> keep the most recent)

# COMMAND ----------

_w = Window.partitionBy("IDDOC").orderBy(F.desc("ingestion_timestamp"))
df_chk = (
    df_chk_raw
    .withColumn("_rn", F.row_number().over(_w))
    .filter(F.col("_rn") == 1)
    .drop("_rn")
)
print(f"SUCCESS docs (deduplicated) : {df_chk.count():,}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Dates: dedupe by IDDOC
# MAGIC
# MAGIC Was `intraqual_docs.DateDiffusion` joined on `IdDocument`, which doesn't
# MAGIC always match `gd_doc`'s IDDOC (see `2_Cleanup_Volume.py`) and silently
# MAGIC dropped dates. `parse_manifest` carries `doc_date` resolved from `gd_doc`
# MAGIC on the real IDDOC.

# COMMAND ----------

df_dates = df_dates_raw.dropDuplicates(["IDDOC"])

# COMMAND ----------

# MAGIC %md
# MAGIC # Data Transformations

# COMMAND ----------

# MAGIC %md
# MAGIC ## Scope: image_count > 0 AND date >= DOC_DATE_CUTOFF (or unknown)

# COMMAND ----------

df_scope = (
    df_chk
    .drop("doc_date")  # parse_manifest's doc_date is authoritative — see comment above
    .join(df_dates, on="IDDOC", how="left")
    .filter(
        (F.col("image_count") > 0)
        & (F.col("doc_date").isNull() | (F.col("doc_date") >= _cutoff))
    )
)
print(f"Docs with in-scope images (>= {DOC_DATE_CUTOFF}) : {df_scope.count():,}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Image struct schema check (old/new UDF protection)

# COMMAND ----------

_img_fields = {f.name for f in df_chk.schema["images"].dataType.elementType.fields}
print(f"Available image struct fields: {sorted(_img_fields)}")


def _fc(field, dtype="string"):
    """Select img.<field>, or NULL if absent from the schema."""
    return F.col(f"img.{field}").cast(dtype) if field in _img_fields else F.lit(None).cast(dtype)

# COMMAND ----------

# MAGIC %md
# MAGIC ## Explode + status

# COMMAND ----------

df_img = (
    df_scope
    .select(
        "IDDOC", "ref", "titre", "division", "niveau_plus_1", "niveau_plus_2",
        "source_file_name", F.posexplode("images").alias("img_pos", "img")
    )
    .select(
        "IDDOC", "source_file_name", "ref", "titre", "division", "niveau_plus_1", "niveau_plus_2",
        _fc("image_id", "integer").alias("image_id"),
        _fc("page_no", "integer").alias("page_no"),
        _fc("label").alias("label"),
        _fc("area_ratio", "float").alias("area_ratio"),
        _fc("captions").alias("captions"),
        _fc("context_text").alias("context_text"),
        _fc("volume_path").alias("volume_path"),
        _fc("image_width", "integer").alias("image_width"),
        _fc("image_height", "integer").alias("image_height"),
        image_utils.image_status_col(
            _fc("volume_path"),
            _fc("image_width", "integer"),
            _fc("image_height", "integer"),
        ).alias("status"),
        F.lit(None).cast("string").alias("description"),
        F.lit(None).cast("integer").alias("input_tokens"),
        F.lit(None).cast("integer").alias("output_tokens"),
        F.lit(None).cast("timestamp").alias("described_at"),
        F.lit(_rebuild_run_id).alias("ingestion_run_id"),
        F.current_timestamp().alias("ingestion_timestamp"),
    )
)

# COMMAND ----------

# MAGIC %md
# MAGIC ## Preserve already-completed LLM descriptions
# MAGIC
# MAGIC See README.md#image-description-wipe-bug — this notebook always does a
# MAGIC full overwrite, so without this join every already-described image would
# MAGIC revert to PENDING with a NULL description.

# COMMAND ----------

if spark.catalog.tableExists(PROD_IMAGE_METADATA):
    df_prev_described = (
        spark.table(PROD_IMAGE_METADATA)
        .filter(F.col("status").isin("DONE", "SKIPPED"))
        .select(
            "IDDOC", "image_id",
            F.col("status").alias("_prev_status"),
            F.col("description").alias("_prev_description"),
            F.col("input_tokens").alias("_prev_input_tokens"),
            F.col("output_tokens").alias("_prev_output_tokens"),
            F.col("described_at").alias("_prev_described_at"),
        )
        .dropDuplicates(["IDDOC", "image_id"])
    )
    df_img = (
        df_img
        .join(F.broadcast(df_prev_described), on=["IDDOC", "image_id"], how="left")
        .withColumn("status", F.coalesce(F.col("_prev_status"), F.col("status")))
        .withColumn("description", F.coalesce(F.col("_prev_description"), F.col("description")))
        .withColumn("input_tokens", F.coalesce(F.col("_prev_input_tokens"), F.col("input_tokens")))
        .withColumn("output_tokens", F.coalesce(F.col("_prev_output_tokens"), F.col("output_tokens")))
        .withColumn("described_at", F.coalesce(F.col("_prev_described_at"), F.col("described_at")))
        .drop("_prev_status", "_prev_description", "_prev_input_tokens", "_prev_output_tokens", "_prev_described_at")
    )

# COMMAND ----------

# MAGIC %md
# MAGIC ## Preview — how many rows would be written (read-only)

# COMMAND ----------

print(f"Rows that would be written to {PROD_IMAGE_METADATA} : {df_img.count():,}")
display(df_img.groupBy("status", "label").count().orderBy("label", "status"))

# COMMAND ----------

# MAGIC %md
# MAGIC # Outputs

# COMMAND ----------

# MAGIC %md
# MAGIC ## Write (full overwrite — requires CONFIRM_OVERWRITE=oui)

# COMMAND ----------

if not CONFIRM_OVERWRITE:
    print("CONFIRM_OVERWRITE != 'oui' — nothing was written. "
          "Set the widget back to 'oui' to apply the rebuild.")
else:
    t0 = time.time()
    df_img.write.format("delta").mode("overwrite") \
        .option("overwriteSchema", "true").saveAsTable(PROD_IMAGE_METADATA)
    n = spark.table(PROD_IMAGE_METADATA).count()
    print(f"\n{PROD_IMAGE_METADATA} rebuilt — {n:,} images in {time.time() - t0:.1f}s")

    display(
        spark.table(PROD_IMAGE_METADATA)
        .groupBy("status", "label").count()
        .orderBy("label", "status")
    )
