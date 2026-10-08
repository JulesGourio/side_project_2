# Databricks notebook source
# /// script
# [tool.databricks.environment]
# environment_version = "5"
# ///
# DBTITLE 1,Remediation: rebuild image_metadata for 64 orphan IDDOCs
# MAGIC %md
# MAGIC # Rebuild `image_metadata` pour 64 IDDOCs orphelins
# MAGIC
# MAGIC **Problème** : 64 IDDOCs parsés en juillet 2026 (runs initiaux) ont `parse_status=SUCCESS` avec
# MAGIC `image_count > 0` dans `processed_files`, mais **aucune entrée** dans `image_metadata`.
# MAGIC Leurs images n'ont jamais été décrites par le LLM.
# MAGIC
# MAGIC **Ce script** :
# MAGIC 1. Identifie les 64 IDDOCs orphelins
# MAGIC 2. Lit leurs données depuis `_pipeline_checkpoint` (images array)
# MAGIC 3. Reconstruit les lignes `image_metadata` avec status=PENDING
# MAGIC 4. MERGE dans `image_metadata` (idempotent, sans toucher aux entrées existantes)
# MAGIC 5. Le prochain run de `4_Describe_Images_LLM` traitera automatiquement les PENDING
# MAGIC
# MAGIC **Exécution** : une seule fois, ~30s sur serverless. Idempotent (safe à re-lancer).

# COMMAND ----------

# DBTITLE 1,Config
import os
from pyspark.sql import functions as F, Window

CATALOG_SCHEMA = os.environ.get("PARSING_CATALOG_SCHEMA", "uat_landingzone.qualibot")
TABLE_SUFFIX = os.environ.get("PARSING_TABLE_SUFFIX", "_v1")

TARGET_IMAGE_METADATA_TABLE = f"{CATALOG_SCHEMA}.image_metadata{TABLE_SUFFIX}"
TARGET_PROCESSED_FILES_TABLE = f"{CATALOG_SCHEMA}.processed_files{TABLE_SUFFIX}"
CHECKPOINT_TABLE = f"{CATALOG_SCHEMA}._pipeline_checkpoint{TABLE_SUFFIX}"

print(f"image_metadata : {TARGET_IMAGE_METADATA_TABLE}")
print(f"processed_files: {TARGET_PROCESSED_FILES_TABLE}")
print(f"checkpoint     : {CHECKPOINT_TABLE}")

# COMMAND ----------

# DBTITLE 1,Identify orphan IDDOCs
# IDDOCs: SUCCESS + image_count>0 in processed_files, but ZERO rows in image_metadata
df_orphan_iddocs = (
    spark.table(TARGET_PROCESSED_FILES_TABLE).alias("pf")
    .join(
        spark.table(TARGET_IMAGE_METADATA_TABLE).select("IDDOC").distinct().alias("im"),
        on="IDDOC", how="left_anti",
    )
    .filter(
        (F.col("parse_status") == "SUCCESS") & (F.col("image_count") > 0)
    )
    .select("IDDOC")
    .distinct()
)
orphan_count = df_orphan_iddocs.count()
print(f"Orphan IDDOCs to remediate: {orphan_count}")

if orphan_count == 0:
    print("Nothing to do — all SUCCESS IDDOCs with images already have image_metadata.")
    dbutils.notebook.exit("SKIP: 0 orphans")

# COMMAND ----------

# DBTITLE 1,Read checkpoint (deduped, best row per IDDOC — prefer rows WITH images)
# Dedup checkpoint: one row per IDDOC.
# IMPORTANT: prefer rows WITH images first (July 2026 SUCCESS rows have images=NULL,
# while September 2026 EMPTY_TEXT retries have images populated).
w = Window.partitionBy("IDDOC").orderBy(
    F.when(F.col("images").isNotNull() & (F.size("images") > 0), 0).otherwise(1),
    F.when(F.col("parse_status") == "SUCCESS", 0).otherwise(1),
    F.desc("ingestion_timestamp"),
)
df_ckpt = (
    spark.table(CHECKPOINT_TABLE)
    .join(F.broadcast(df_orphan_iddocs), on="IDDOC", how="inner")
    .withColumn("_rn", F.row_number().over(w))
    .filter(F.col("_rn") == 1)
    .drop("_rn")
)

# Only rows with actual images
df_with_images = df_ckpt.filter(F.size(F.col("images")) > 0)
iddocs_with_images = df_with_images.select("IDDOC").distinct().count()
total_images = df_with_images.select(F.explode("images")).count()
print(f"Checkpoint rows with images: {iddocs_with_images} IDDOCs, {total_images} images")

# COMMAND ----------

# DBTITLE 1,Build image_metadata rows
import uuid

INGESTION_RUN_ID = f"remediation-{str(uuid.uuid4())[:8]}"

# Determine initial image status:
#   - volume_path not null + width>0 + height>0 → PENDING (needs LLM description)
#   - otherwise → SKIPPED (decorative / broken / missing)
def _image_status(vp, w, h):
    return (
        F.when(
            vp.isNull() | (w.isNull()) | (h.isNull()) | (w <= 0) | (h <= 0),
            F.lit("SKIPPED")
        ).otherwise(F.lit("PENDING"))
    )

df_image_metadata = (
    df_with_images
    .select(
        "IDDOC", "source_file_name", "ref", "titre",
        "division", "niveau_plus_1", "niveau_plus_2",
        F.posexplode("images").alias("img_pos", "img"),
    )
    .select(
        "IDDOC", "source_file_name", "ref", "titre",
        "division", "niveau_plus_1", "niveau_plus_2",
        F.col("img.image_id").alias("image_id"),
        F.col("img.page_no").alias("page_no"),
        F.col("img.label").alias("label"),
        F.col("img.area_ratio").alias("area_ratio"),
        F.col("img.captions").alias("captions"),
        F.col("img.context_text").alias("context_text"),
        F.col("img.volume_path").alias("volume_path"),
        F.col("img.image_width").alias("image_width"),
        F.col("img.image_height").alias("image_height"),
        _image_status(
            F.col("img.volume_path"), F.col("img.image_width"), F.col("img.image_height")
        ).alias("status"),
        F.lit(None).cast("string").alias("description"),
        F.lit(None).cast("integer").alias("input_tokens"),
        F.lit(None).cast("integer").alias("output_tokens"),
        F.lit(None).cast("timestamp").alias("described_at"),
        F.lit(INGESTION_RUN_ID).alias("ingestion_run_id"),
        F.current_timestamp().alias("ingestion_timestamp"),
    )
)

row_count = df_image_metadata.count()
pending = df_image_metadata.filter(F.col("status") == "PENDING").count()
skipped = df_image_metadata.filter(F.col("status") == "SKIPPED").count()
print(f"Built {row_count} image_metadata rows: {pending} PENDING, {skipped} SKIPPED")

# COMMAND ----------

# DBTITLE 1,MERGE into image_metadata (idempotent)
from delta.tables import DeltaTable

DeltaTable.forName(spark, TARGET_IMAGE_METADATA_TABLE).alias("tgt").merge(
    df_image_metadata.alias("src"),
    "tgt.IDDOC = src.IDDOC AND tgt.image_id = src.image_id",
).whenNotMatchedInsertAll().execute()

print(f"✅ MERGE complete — {row_count} rows inserted into {TARGET_IMAGE_METADATA_TABLE}")
print(f"   {pending} images PENDING → will be described by the next run of 4_Describe_Images_LLM")

# COMMAND ----------

# DBTITLE 1,Verification
# Verify: no more orphans
remaining = spark.sql(f"""
    SELECT COUNT(DISTINCT pf.IDDOC)
    FROM {TARGET_PROCESSED_FILES_TABLE} pf
    LEFT JOIN (SELECT DISTINCT IDDOC FROM {TARGET_IMAGE_METADATA_TABLE}) im ON pf.IDDOC = im.IDDOC
    WHERE pf.parse_status = 'SUCCESS' AND pf.image_count > 0 AND im.IDDOC IS NULL
""").collect()[0][0]

if remaining == 0:
    print(f"✅ Vérification OK — 0 IDDOC SUCCESS restant sans image_metadata")
else:
    print(f"⚠️  {remaining} IDDOCs encore orphelins — vérifier le checkpoint")

# Show new status distribution for remediated IDDOCs
orphan_list = [r.IDDOC for r in df_orphan_iddocs.collect()]
print(f"\nDistribution des images remédiées :")
(
    spark.table(TARGET_IMAGE_METADATA_TABLE)
    .filter(F.col("IDDOC").isin(orphan_list))
    .groupBy("status")
    .agg(F.count("*").alias("count"), F.countDistinct("IDDOC").alias("iddocs"))
    .orderBy(F.desc("count"))
    .show()
)

# COMMAND ----------

