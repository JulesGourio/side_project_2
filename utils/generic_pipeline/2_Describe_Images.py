# Databricks notebook source
# MAGIC %md
# MAGIC # 02 — Describe Images with Vision LLM
# MAGIC
# MAGIC **Description:**
# MAGIC Describes `PENDING` images from `image_metadata` using a vision LLM, then
# MAGIC injects the descriptions as enriched chunks into the `chunks` table.
# MAGIC
# MAGIC Structurally identical to `parsing_pipeline/4_Describe_Images_LLM_v2`,
# MAGIC stripped of division AS/IS split and Intraqual-specific metadata prefixes.
# MAGIC
# MAGIC **Pre-requisite:** task `1_parse` must run first to populate `image_metadata`.

# COMMAND ----------

# MAGIC %md
# MAGIC # Configuration

# COMMAND ----------

# MAGIC %load_ext autoreload
# MAGIC %autoreload 2

# COMMAND ----------

import os
import sys
import time
import asyncio
from datetime import datetime

_ctx = dbutils.notebook.entry_point.getDbutils().notebook().getContext()
REPO_DIR = "/Workspace" + os.path.dirname(_ctx.notebookPath().get())
PARSING_PIPELINE_DIR = os.path.join(os.path.dirname(REPO_DIR), "parsing_pipeline")

sys.path.insert(0, REPO_DIR)
sys.path.insert(1, PARSING_PIPELINE_DIR)

from pyspark.sql import functions as F
from pyspark.sql import Row

for _mod in ("config.py", "selection.py"):
    spark.sparkContext.addPyFile(os.path.join(REPO_DIR, _mod))
for _mod in ("utils.py", "image_utils.py"):
    spark.sparkContext.addPyFile(os.path.join(PARSING_PIPELINE_DIR, _mod))

from utils import configure, token_count_udf
from image_utils import describe_all_images, safe_requests_per_minute

from config import *

SOURCE_IMAGE_TABLE = TARGET_IMAGE_METADATA_TABLE

configure(
    VOLUME_BASE_PATH=VOLUME_BASE_PATH, LLM_MODEL_ENDPOINT=LLM_MODEL_ENDPOINT,
    LLM_MAX_TOKENS=LLM_MAX_TOKENS, LLM_TEMPERATURE=LLM_TEMPERATURE,
    LLM_MAX_RETRIES=LLM_MAX_RETRIES, LLM_MAX_CONCURRENT=LLM_MAX_CONCURRENT,
    USE_TIKTOKEN=USE_TIKTOKEN, CHARS_PER_TOKEN=CHARS_PER_TOKEN,
)

# COMMAND ----------

# MAGIC %md
# MAGIC ## Authentication and Constants

# COMMAND ----------

try:
    WS_TOKEN = dbutils.secrets.get(scope="qualibot", key="serving_token")
except Exception:
    WS_TOKEN = _ctx.apiToken().get()
WS_HOST = spark.conf.get("spark.databricks.workspaceUrl")

_TEMP_TABLE = f"{CATALOG_SCHEMA}._image_updates_temp{TABLE_SUFFIX}"

print(f"RUN_MODE={RUN_MODE} | Model={LLM_MODEL_ENDPOINT} | concurrency={LLM_MAX_CONCURRENT} | batch={LLM_BATCH_SIZE or 'ALL'}")

# COMMAND ----------

# MAGIC %md
# MAGIC # Inputs
# MAGIC
# MAGIC ## Images needing a description

# COMMAND ----------

if RUN_MODE == "full":
    spark.sql(f"""
        UPDATE {SOURCE_IMAGE_TABLE}
        SET status = 'PENDING', description = NULL,
            input_tokens = NULL, output_tokens = NULL, described_at = NULL
        WHERE volume_path IS NOT NULL
    """)
    print(f"[FULL] Reset all images to PENDING in {SOURCE_IMAGE_TABLE}")

df_pending = (
    spark.table(SOURCE_IMAGE_TABLE)
    .filter(F.col("status").isin("PENDING", "ERROR") & F.col("volume_path").isNotNull())
)
pending_count = df_pending.count()

_status_counts = {r["status"]: r["cnt"] for r in df_pending.groupBy("status").agg(F.count("*").alias("cnt")).collect()}
_pending_only = _status_counts.get("PENDING", 0)
_error_retry = _status_counts.get("ERROR", 0)
print(f"Images to process: {pending_count} total ({_pending_only} new PENDING + {_error_retry} ERROR retries)")
if pending_count == 0:
    print("Nothing to do — no PENDING or ERROR images.")

# COMMAND ----------

# MAGIC %md
# MAGIC # Data Preparation

# COMMAND ----------

df_to_process = df_pending.limit(LLM_BATCH_SIZE) if (LLM_BATCH_SIZE and pending_count > LLM_BATCH_SIZE) else df_pending
rows_to_process = [r.asDict() for r in df_to_process.collect()]

print(f"Batch: {len(rows_to_process)} images"
      + (f" | batched from {pending_count}" if LLM_BATCH_SIZE and pending_count > LLM_BATCH_SIZE else ""))

# COMMAND ----------

# MAGIC %md
# MAGIC # Data Transformations
# MAGIC
# MAGIC ## Describe images (checkpointed)

# COMMAND ----------

import math
import nest_asyncio
nest_asyncio.apply()


def _merge_chunk_results(results):
    """Persist one chunk's results into SOURCE_IMAGE_TABLE via MERGE."""
    update_rows = [
        Row(
            IDDOC=str(r["IDDOC"]),
            image_id=int(r["image_id"]),
            description=r["description"],
            input_tokens=int(r.get("input_tokens") or 0),
            output_tokens=int(r.get("output_tokens") or 0),
            status=r["status"],
            described_at=datetime.now(),
        )
        for r in results
    ]
    df_updates = spark.createDataFrame(update_rows).dropDuplicates(["IDDOC", "image_id"])
    df_updates.write.format("delta").mode("overwrite") \
        .option("overwriteSchema", "true").saveAsTable(_TEMP_TABLE)
    spark.sql(f"""
        MERGE INTO {SOURCE_IMAGE_TABLE} AS t
        USING {_TEMP_TABLE} AS s
        ON t.IDDOC = s.IDDOC AND t.image_id = s.image_id
        WHEN MATCHED THEN UPDATE SET
            t.description = s.description,
            t.input_tokens = s.input_tokens,
            t.output_tokens = s.output_tokens,
            t.status = s.status,
            t.described_at = s.described_at
    """)


total_done = total_err = total_tin = total_tout = 0

if not rows_to_process:
    print("Nothing to describe.")
else:
    _rpm = safe_requests_per_minute(
        LLM_ITPM_BUDGET, LLM_OTPM_BUDGET, LLM_QPH_BUDGET,
        LLM_AVG_INPUT_TOKENS, LLM_AVG_OUTPUT_TOKENS,
    )
    print(f"Rate limiter : {_rpm:.0f} req/min")

    n_chunks = math.ceil(len(rows_to_process) / LLM_CHECKPOINT_CHUNK_SIZE)
    print(f"{len(rows_to_process)} images -> {n_chunks} chunk(s) of up to {LLM_CHECKPOINT_CHUNK_SIZE}")

    for chunk_idx in range(n_chunks):
        chunk = rows_to_process[chunk_idx * LLM_CHECKPOINT_CHUNK_SIZE:(chunk_idx + 1) * LLM_CHECKPOINT_CHUNK_SIZE]
        t0 = time.time()
        try:
            described_images = asyncio.run(describe_all_images(
                rows=chunk,
                ws_host=WS_HOST,
                ws_token=WS_TOKEN,
                model=LLM_MODEL_ENDPOINT,
                max_tokens=LLM_MAX_TOKENS,
                temperature=LLM_TEMPERATURE,
                max_retries=LLM_MAX_RETRIES,
                max_concurrent=LLM_MAX_CONCURRENT,
                requests_per_minute=_rpm,
            ))
        except Exception as e:
            print(f"[FATAL] chunk {chunk_idx + 1}/{n_chunks} failed: {str(e)[:300]}")
            raise

        done = sum(1 for r in described_images if r["status"] == "DONE")
        err = sum(1 for r in described_images if r["status"] == "ERROR")
        tin = sum(r.get("input_tokens", 0) for r in described_images)
        tout = sum(r.get("output_tokens", 0) for r in described_images)

        _merge_chunk_results(described_images)
        del described_images
        import gc; gc.collect()

        total_done += done; total_err += err; total_tin += tin; total_tout += tout
        print(f"[chunk {chunk_idx + 1}/{n_chunks}] {len(chunk)} images in {time.time() - t0:.1f}s | "
              f"ok={done} err={err} | tokens {tin:,} in / {tout:,} out")

    spark.sql(f"DROP TABLE IF EXISTS {_TEMP_TABLE}")
    print(f"\nDescription summary: {total_done} ok / {total_err} err over {len(rows_to_process)} images")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Build image chunks from described images
# MAGIC
# MAGIC No division split — single chunks table.

# COMMAND ----------

df_described = (
    spark.table(SOURCE_IMAGE_TABLE)
    .filter(
        (F.col("status") == "DONE")
        & F.col("description").isNotNull()
        & (F.length(F.trim(F.col("description"))) >= F.lit(MIN_INDEXABLE_DESC_CHARS))
        & (~F.upper(F.trim(F.col("description"))).startswith("SKIP"))
    )
    .withColumn("chunk_id", F.concat_ws("-", F.col("IDDOC"), F.lit("IMG"),
                                        F.lpad(F.col("image_id").cast("string"), 3, "0")))
)

if spark.catalog.tableExists(TARGET_CHUNK_TABLE):
    df_existing_image_chunk_ids = (
        spark.table(TARGET_CHUNK_TABLE)
        .filter(F.col("chunk_content_type") == "image")
        .select("chunk_id")
    )
    df_described = df_described.join(F.broadcast(df_existing_image_chunk_ids), on="chunk_id", how="left_anti")

described_count = df_described.count()
if described_count == 0:
    print("No new image chunks to inject.")
else:
    print(f"{described_count} new image chunks to inject.")
    df_max_idx = (
        spark.table(TARGET_CHUNK_TABLE)
        .filter(F.col("chunk_content_type") != "image")
        .groupBy("doc_id").agg(F.max("chunk_index").alias("max_text_index"))
    )

    df_image_chunks = (
        df_described
        .withColumn("doc_id", F.col("IDDOC"))
        .join(df_max_idx, on="doc_id", how="left")
        .withColumns({
            "max_text_index": F.coalesce(F.col("max_text_index"), F.lit(-1)),
            "chunk_index": F.coalesce(F.col("max_text_index"), F.lit(-1)) + F.lit(1) + F.col("image_id"),
            "chunk_text": (F.concat(
                F.lit("[Source: "), F.coalesce(F.col("source_file_name"), F.lit("")),
                F.lit(" | Title: "), F.coalesce(F.col("doc_title"), F.lit("")),
                F.lit(" | Image: page "), F.coalesce(F.col("page_no").cast("string"), F.lit("?")),
                F.lit(", "), F.col("label"), F.lit("]\n\n"), F.col("description"),
            ) if EMBED_SOURCE_PREFIX else F.col("description")),
        })
        .withColumns({
            "chunk_token_count": token_count_udf(F.col("chunk_text")),
            "chunk_id": F.concat_ws("-", F.col("IDDOC"), F.lit("IMG"),
                                    F.lpad(F.col("image_id").cast("string"), 3, "0")),
            "chunk_content_type": F.lit("image"),
            "semantic_headers": F.to_json(F.struct(
                F.col("label").alias("image_label"),
                F.col("page_no").cast("string").alias("page"),
            )),
            "chunk_sha256": F.sha2(F.col("chunk_text"), 256),
        })
        .select(
            "doc_id", "doc_title",
            "chunk_id", "chunk_index", "chunk_text",
            "chunk_token_count", "chunk_content_type", "semantic_headers", "chunk_sha256",
        )
    )

# COMMAND ----------

# MAGIC %md
# MAGIC # Outputs
# MAGIC
# MAGIC ## Write image chunks

# COMMAND ----------

if described_count > 0:
    from delta.tables import DeltaTable

    if not spark.catalog.tableExists(TARGET_CHUNK_TABLE):
        df_image_chunks.write.format("delta").mode("append") \
            .option("mergeSchema", "true").saveAsTable(TARGET_CHUNK_TABLE)
    else:
        (
            DeltaTable.forName(spark, TARGET_CHUNK_TABLE).alias("t")
            .merge(df_image_chunks.alias("s"), "t.chunk_id = s.chunk_id")
            .whenMatchedUpdateAll()
            .whenNotMatchedInsertAll()
            .execute()
        )
    print(f"Merged {described_count} image chunks into {TARGET_CHUNK_TABLE}")

    spark.sql(f"ALTER TABLE {TARGET_CHUNK_TABLE} SET TBLPROPERTIES (delta.enableChangeDataFeed = true)")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Summary

# COMMAND ----------

display(
    spark.table(SOURCE_IMAGE_TABLE).groupBy("status").agg(
        F.count("*").alias("count"),
        F.sum("input_tokens").alias("total_input_tokens"),
        F.sum("output_tokens").alias("total_output_tokens"),
    )
)