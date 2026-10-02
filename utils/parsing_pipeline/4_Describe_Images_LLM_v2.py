# Databricks notebook source


# COMMAND ----------

# MAGIC %md
# MAGIC # 04 — Describe Images with Vision LLM
# MAGIC
# MAGIC **Description:**
# MAGIC Describes `PENDING` images from `image_metadata` using a vision LLM, then
# MAGIC injects the descriptions as enriched chunks into the `chunks` table.
# MAGIC
# MAGIC **Workflow:**
# MAGIC 1. Load images with status = `PENDING` from `image_metadata`
# MAGIC 2. Call the vision LLM asynchronously (batched, with concurrency control)
# MAGIC 3. UPDATE descriptions back into `image_metadata` (targeted row-level updates)
# MAGIC 4. Build image-based chunks and APPEND/overwrite them into `chunks`
# MAGIC
# MAGIC **Highlighted complexities:**
# MAGIC This notebook has two sequential read-then-write phases (description, then
# MAGIC chunk injection) where the second phase's read of `image_metadata`
# MAGIC intentionally happens AFTER the first phase's writes, to pick up the
# MAGIC freshly-described rows. So not every table read is hoisted into the
# MAGIC top-level Inputs section below — the second phase's reads stay attached to
# MAGIC their own Data Transformations subsection, right where they need to run.
# MAGIC
# MAGIC **Pre-requisite:** task `3_parse` must run first to populate `image_metadata`.
# MAGIC
# MAGIC **Intended Pipeline**
# MAGIC - Qualibot Parsing Pipeline — Daily (task `4_describe_images`)
# MAGIC
# MAGIC **Input Tables Pipeline**
# MAGIC - `{PARSING_CATALOG_SCHEMA}.image_metadata{PARSING_TABLE_SUFFIX}`
# MAGIC - `{PARSING_CATALOG_SCHEMA}.chunks{PARSING_TABLE_SUFFIX}` (max chunk index per IDDOC)
# MAGIC - `{PARSING_CATALOG_SCHEMA}.processed_files{PARSING_TABLE_SUFFIX}` (doc_date)
# MAGIC
# MAGIC **Inputs Reference Data**
# MAGIC - *(none)*
# MAGIC
# MAGIC **Output Tables (Pipeline)**
# MAGIC - `{PARSING_CATALOG_SCHEMA}.image_metadata{PARSING_TABLE_SUFFIX}` (description/status columns)
# MAGIC - `{PARSING_CATALOG_SCHEMA}.chunks{PARSING_TABLE_SUFFIX}`
# MAGIC - `{PARSING_CATALOG_SCHEMA}.src_chunks_as{PARSING_TABLE_SUFFIX}` / `src_chunks_is{PARSING_TABLE_SUFFIX}`
# MAGIC - `{PARSING_CATALOG_SCHEMA}.processed_files{PARSING_TABLE_SUFFIX}` (EMPTY_TEXT -> SUCCESS / SKIPPED_EMPTY_IMAGES promotion, once all of a document's images are described)

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
# MAGIC
# MAGIC No `%pip install` — deps are job-cluster libraries in `parsing_pipeline.job.yml`.

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
sys.path.insert(0, REPO_DIR)

from pyspark.sql import functions as F
from pyspark.sql import Row

# addPyFile propagates these to every executor, not just the driver — required for worker imports.
for _mod in ("utils.py", "image_utils.py", "selection.py", "config.py"):
    spark.sparkContext.addPyFile(os.path.join(REPO_DIR, _mod))

from utils import configure, token_count_udf, intraqual_ref_url
from image_utils import describe_all_images, safe_requests_per_minute

from config import *

SOURCE_IMAGE_TABLE = TARGET_IMAGE_METADATA_TABLE

# Needed by token_count_udf
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

_TEMP_TABLE = f"{CATALOG_SCHEMA}._image_updates_temp"

print(f"RUN_MODE={RUN_MODE} | Model={LLM_MODEL_ENDPOINT} | concurrency={LLM_MAX_CONCURRENT} | batch={LLM_BATCH_SIZE or 'ALL'}")

# COMMAND ----------

# MAGIC %md
# MAGIC # Inputs

# COMMAND ----------

# MAGIC %md
# MAGIC ## Images needing a description
# MAGIC
# MAGIC - `"incremental"` -> only `PENDING` images (default, safe to re-run).
# MAGIC - `"full"` -> reset ALL image statuses to `PENDING` first, then process all.

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
    # ERROR is retried like PENDING (failed LLM call, e.g. a blank response) --
    # DONE/SKIPPED/SKIPPED_DECORATIVE stay terminal.
    .filter(F.col("status").isin("PENDING", "ERROR") & F.col("volume_path").isNotNull())
)
pending_count = df_pending.count()

# ── Status breakdown for clear logging ──
_status_counts = {r["status"]: r["cnt"] for r in df_pending.groupBy("status").agg(F.count("*").alias("cnt")).collect()}
_pending_only = _status_counts.get("PENDING", 0)
_error_retry = _status_counts.get("ERROR", 0)
print(f"Images to process: {pending_count} total ({_pending_only} new PENDING + {_error_retry} ERROR retries)")
if pending_count == 0:
    print("✅ Nothing to do — no PENDING or ERROR images.")
elif _error_retry > 0 and _pending_only == 0:
    print(f"  ℹ️  No new images — only retrying {_error_retry} previously failed images.")

# COMMAND ----------

# MAGIC %md
# MAGIC # Data Preparation

# COMMAND ----------

df_to_process = df_pending.limit(LLM_BATCH_SIZE) if (LLM_BATCH_SIZE and pending_count > LLM_BATCH_SIZE) else df_pending
rows_to_process = [r.asDict() for r in df_to_process.collect()]

print(f"Batch: {len(rows_to_process)} images ({_pending_only} new + {_error_retry} retries)"
      + (f" | batched from {pending_count}" if LLM_BATCH_SIZE and pending_count > LLM_BATCH_SIZE else ""))

# COMMAND ----------

# MAGIC %md
# MAGIC # Data Transformations

# COMMAND ----------

# MAGIC %md
# MAGIC ## Describe images (checkpointed)
# MAGIC
# MAGIC Calls the vision LLM in fixed-size chunks, each persisted (MERGE)
# MAGIC immediately, so partial progress survives a mid-run failure.
# MAGIC `nest_asyncio` is required to run `asyncio.run()` inside a Databricks notebook.

# COMMAND ----------

import math
import nest_asyncio
nest_asyncio.apply()


def _merge_chunk_results(results):
    """Persist one chunk's results into SOURCE_IMAGE_TABLE via MERGE."""
    update_rows = [
        Row(
            IDDOC=int(r["IDDOC"]),
            image_id=int(r["image_id"]),
            description=r["description"],
            input_tokens=int(r.get("input_tokens") or 0),
            output_tokens=int(r.get("output_tokens") or 0),
            status=r["status"],
            described_at=datetime.now(),
        )
        for r in results
    ]
    # MERGE requires a deduped source batch — a duplicate (IDDOC, image_id) pair crashes it.
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
    print("✅ Nothing to describe — no PENDING or ERROR images found in image_metadata.")
else:
    # Safe throughput bounded by the endpoint's quota (bottleneck = OUTPUT/OTPM).
    _rpm = safe_requests_per_minute(
        LLM_ITPM_BUDGET, LLM_OTPM_BUDGET, LLM_QPH_BUDGET,
        LLM_AVG_INPUT_TOKENS, LLM_AVG_OUTPUT_TOKENS,
    )
    print(f"Rate limiter : {_rpm:.0f} req/min  (ITPM<={LLM_ITPM_BUDGET:,}, OTPM<={LLM_OTPM_BUDGET:,}, QPH<={LLM_QPH_BUDGET:,})")

    n_chunks = math.ceil(len(rows_to_process) / LLM_CHECKPOINT_CHUNK_SIZE)
    print(f"{len(rows_to_process)} images -> {n_chunks} chunk(s) of up to {LLM_CHECKPOINT_CHUNK_SIZE}, "
          f"persisted after each chunk")

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
            print(f"[FATAL] chunk {chunk_idx + 1}/{n_chunks} describe_all_images failed: {str(e)[:300]}")
            print(f"        {total_done} ok / {total_err} err from prior chunks are already persisted.")
            raise

        done = sum(1 for r in described_images if r["status"] == "DONE")
        err = sum(1 for r in described_images if r["status"] == "ERROR")
        tin = sum(r.get("input_tokens", 0) for r in described_images)
        tout = sum(r.get("output_tokens", 0) for r in described_images)

        _merge_chunk_results(described_images)

        # Free memory between chunks to prevent OOM on single-node clusters (Bug3 fix)
        del described_images
        import gc; gc.collect()

        total_done += done; total_err += err; total_tin += tin; total_tout += tout
        print(f"[chunk {chunk_idx + 1}/{n_chunks}] {len(chunk)} images in {time.time() - t0:.1f}s | "
              f"ok={done} err={err} | tokens {tin:,} in / {tout:,} out | "
              f"cumulative: ok={total_done} err={total_err}")

    spark.sql(f"DROP TABLE IF EXISTS {_TEMP_TABLE}")

    remaining_pending = spark.table(SOURCE_IMAGE_TABLE).filter(F.col("status") == "PENDING").count()
    remaining_error = spark.table(SOURCE_IMAGE_TABLE).filter(F.col("status") == "ERROR").count()
    print(f"\n{'='*60}")
    print(f"Description summary: {total_done} ok / {total_err} err over {len(rows_to_process)} images")
    print(f"  Tokens: {total_tin:,} in / {total_tout:,} out")
    print(f"  Remaining: {remaining_pending} PENDING, {remaining_error} ERROR (will retry next run)")
    if remaining_error > 0:
        _err_docs = spark.table(SOURCE_IMAGE_TABLE).filter(F.col("status") == "ERROR").select("IDDOC").distinct().count()
        print(f"  ⚠️  {remaining_error} images in ERROR across {_err_docs} documents")
    if remaining_pending:
        print("  → Re-run for the rest.")
    elif remaining_error == 0:
        print("  → All images described successfully.")
    else:
        print("  → No PENDING left, but ERROR images remain — they will be retried next run.")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Build image chunks from described images
# MAGIC
# MAGIC Reads happen here, not in Inputs, since `df_described` must run after the
# MAGIC description loop's writes above. Only `status == DONE` with a real,
# MAGIC non-"SKIP" description above the length threshold gets indexed.
# MAGIC
# MAGIC Scoped by an anti-join on `chunk_id` against `chunks` (not by "processed
# MAGIC this run") — see README.md#orphaned-image-chunks-after-a-mid-run-crash:
# MAGIC restricting to this run's own images meant a run that crashed AFTER
# MAGIC merging DONE statuses but BEFORE this injection phase left those images
# MAGIC permanently un-chunked (DONE is never re-selected as PENDING, and no
# MAGIC later run's in-memory batch would include them again). The anti-join
# MAGIC makes this phase idempotent and self-healing: it also picks up any DONE
# MAGIC image from an earlier run that never got chunked, at the cost of a full
# MAGIC scan of DONE images each run instead of just this run's small batch —
# MAGIC cheap at current volumes (tens of thousands of rows).

# COMMAND ----------

df_described = (
    spark.table(SOURCE_IMAGE_TABLE)
    .filter(
        (F.col("status") == "DONE")
        & F.col("description").isNotNull()
        & (F.length(F.trim(F.col("description"))) >= F.lit(MIN_INDEXABLE_DESC_CHARS))
        & (~F.upper(F.trim(F.col("description"))).startswith("SKIP"))
    )
    .withColumn("chunk_id", F.concat_ws("-", F.col("IDDOC").cast("string"), F.lit("IMG"),
                                        F.lpad(F.col("image_id").cast("string"), 3, "0")))
)

# Image chunks of pre-cutoff documents live in chunks_archive, not chunks.
_chunk_tables_with_images = [t for t in (TARGET_CHUNK_TABLE, TARGET_CHUNK_TABLE_ARCHIVE) if spark.catalog.tableExists(t)]
for _tbl in _chunk_tables_with_images:
    df_existing_image_chunk_ids = (
        spark.table(_tbl)
        .filter(F.col("chunk_content_type") == "image")
        .select("chunk_id")
    )
    df_described = df_described.join(F.broadcast(df_existing_image_chunk_ids), on="chunk_id", how="left_anti")

described_count = df_described.count()
if described_count == 0:
    _total_done = spark.table(SOURCE_IMAGE_TABLE).filter(
        (F.col("status") == "DONE") & F.col("description").isNotNull()
    ).count()
    _total_error = spark.table(SOURCE_IMAGE_TABLE).filter(F.col("status") == "ERROR").count()
    print(f"No new image chunks to inject.")
    print(f"  {_total_done} DONE images → all already have chunks in {TARGET_CHUNK_TABLE}")
    if _total_error > 0:
        print(f"  {_total_error} ERROR images → not injected (will retry description next run)")
else:
    print(f"{described_count} new image chunks to inject into chunk tables.")
    df_text_chunks = spark.table(TARGET_CHUNK_TABLE).select("IDDOC", "chunk_index", "chunk_content_type")
    if spark.catalog.tableExists(TARGET_CHUNK_TABLE_ARCHIVE):
        df_text_chunks = df_text_chunks.unionByName(
            spark.table(TARGET_CHUNK_TABLE_ARCHIVE).select("IDDOC", "chunk_index", "chunk_content_type")
        )
    df_max_idx = (
        df_text_chunks
        .filter(F.col("chunk_content_type") != "image")
        .groupBy("IDDOC").agg(F.max("chunk_index").alias("max_text_index"))
    )

    # doc_date isn't on image_metadata — joined from processed_files for the prefix below.
    df_doc_dates = spark.table(TARGET_PROCESSED_FILES_TABLE).select("IDDOC", "doc_date").dropDuplicates(["IDDOC"])

    df_image_chunks = (
        df_described.join(df_max_idx, on="IDDOC", how="left")
        .join(F.broadcast(df_doc_dates), on="IDDOC", how="left")
        .withColumn("url", intraqual_ref_url(F.col("ref")))
        .withColumns({
            "max_text_index": F.coalesce(F.col("max_text_index"), F.lit(-1)),
            "chunk_index": F.coalesce(F.col("max_text_index"), F.lit(-1)) + F.lit(1) + F.col("image_id"),
            # Duplicates utils.source_prefixed_text(): this prefix has an extra "Image: page N, label" field.
            "chunk_text": (F.concat(
                F.lit("[Source: "), F.coalesce(F.col("ref"), F.lit("")),
                F.lit(" | Title: "), F.coalesce(F.col("titre"), F.lit("")),
                F.lit(" | Division: "), F.coalesce(F.col("division"), F.lit("")),
                F.lit(" | Category: "), F.coalesce(F.col("niveau_plus_1"), F.lit("")),
                F.when(F.col("niveau_plus_2").isNotNull(),
                       F.concat(F.lit(" > "), F.col("niveau_plus_2"))).otherwise(F.lit("")),
                F.lit(" | Date de diffusion: "), F.coalesce(F.date_format(F.col("doc_date"), "yyyy-MM-dd"), F.lit("inconnue")),
                F.lit(" | Image: page "), F.coalesce(F.col("page_no").cast("string"), F.lit("?")),
                F.lit(", "), F.col("label"), F.lit("]\n\n"), F.col("description"),
            ) if EMBED_SOURCE_PREFIX else F.col("description")),
        })
        .withColumns({
            "chunk_token_count": token_count_udf(F.col("chunk_text")),
            "chunk_id": F.concat_ws("-", F.col("IDDOC").cast("string"), F.lit("IMG"),
                                    F.lpad(F.col("image_id").cast("string"), 3, "0")),
            "chunk_content_type": F.lit("image"),
            "semantic_headers": F.to_json(F.struct(
                F.col("label").alias("image_label"),
                F.col("page_no").cast("string").alias("page"),
                F.col("volume_path").alias("volume_path"),
                F.col("area_ratio").cast("string").alias("area_ratio"),
                F.col("captions").cast("string").alias("captions"),
            )),
            "chunk_sha256": F.sha2(F.col("chunk_text"), 256),
        })
        .withColumn("REF", F.coalesce(F.col("ref"), F.col("source_file_name")))
        .select(
            "IDDOC", "REF", "division",
            "chunk_id", "chunk_index", "chunk_text",
            "chunk_token_count", "chunk_content_type", "semantic_headers", "chunk_sha256",
            "url", "doc_date",
        )
    )

# COMMAND ----------

# MAGIC %md
# MAGIC # Outputs

# COMMAND ----------

# MAGIC %md
# MAGIC ## Write image chunks
# MAGIC
# MAGIC MERGE on chunk_id, not delete-all-for-IDDOC + reinsert-all, so untouched
# MAGIC image chunks from earlier runs aren't rewritten every time.

# COMMAND ----------

if described_count > 0:
    from delta.tables import DeltaTable

    def _merge_image_chunks(df_chunks, table_name):
        if not spark.catalog.tableExists(table_name):
            df_chunks.write.format("delta").mode("append") \
                .option("mergeSchema", "true").saveAsTable(table_name)
            return
        (
            DeltaTable.forName(spark, table_name).alias("t")
            .merge(df_chunks.alias("s"), "t.chunk_id = s.chunk_id")
            .whenMatchedUpdateAll()
            .whenNotMatchedInsertAll()
            .execute()
        )

    # Cache to avoid Spark lazy re-evaluation after writes
    # (the left_anti join in df_described would return 0 post-merge otherwise)
    _df_image_chunks_cached = df_image_chunks.cache()
    _is_recent = F.col("doc_date").isNull() | (F.col("doc_date") >= F.lit(DOC_DATE_CUTOFF).cast("date"))
    df_archive_image_chunks = _df_image_chunks_cached.filter(~_is_recent)
    _archive_chunk_count = df_archive_image_chunks.count()
    _merge_image_chunks(df_archive_image_chunks, TARGET_CHUNK_TABLE_ARCHIVE)
    print(f"Merged {_archive_chunk_count} image chunks into {TARGET_CHUNK_TABLE_ARCHIVE} (pre-{DOC_DATE_CUTOFF})")
    df_image_chunks = _df_image_chunks_cached.filter(_is_recent)
    _chunk_count = df_image_chunks.count()
    _as_chunk_count = df_image_chunks.filter(F.col("division") == "AS").count()
    _is_chunk_count = df_image_chunks.filter(F.col("division") == "IS").count()

    _merge_image_chunks(df_image_chunks, TARGET_CHUNK_TABLE)
    print(f"Merged {_chunk_count} image chunks into {TARGET_CHUNK_TABLE}")

    # Mirrors into src_chunks_as/src_chunks_is with the same MERGE pattern, so per-division reads stay in sync.
    for _div, _tbl, _cnt in [("AS", TARGET_CHUNK_TABLE_AS, _as_chunk_count), ("IS", TARGET_CHUNK_TABLE_IS, _is_chunk_count)]:
        df_div_chunks = df_image_chunks.filter(F.col("division") == _div)
        _merge_image_chunks(df_div_chunks, _tbl)
        print(f"Merged {_cnt} image chunks into {_tbl}")

    _df_image_chunks_cached.unpersist()

    # Enable Change Data Feed on chunks table (required for Vector Search index sync)
    spark.sql(f"""
        ALTER TABLE {TARGET_CHUNK_TABLE}
        SET TBLPROPERTIES (delta.enableChangeDataFeed = true)
    """)
    print(f"CDF enabled on {TARGET_CHUNK_TABLE}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Promote EMPTY_TEXT documents whose images are now fully described
# MAGIC
# MAGIC Nothing else moves an EMPTY_TEXT document to a terminal status once its images are described -- do it here, same run.

# COMMAND ----------

from delta.tables import DeltaTable

df_empty_text_iddocs = (
    spark.table(TARGET_PROCESSED_FILES_TABLE)
    .filter(F.col("parse_status") == "EMPTY_TEXT")
    .select("IDDOC")
)
empty_text_count = df_empty_text_iddocs.count()

if empty_text_count == 0:
    print("No EMPTY_TEXT document pending promotion.")
else:
    df_their_images = spark.table(SOURCE_IMAGE_TABLE).join(F.broadcast(df_empty_text_iddocs), on="IDDOC", how="inner")

    # Ready = no PENDING image left for that IDDOC.  ERROR / EXTRACTION_FAILED /
    # SKIPPED_DECORATIVE are all terminal — an image that was attempted and failed
    # shouldn't block the document from being promoted with its successful descriptions.
    # (ERROR images ARE retried in cell 12 — if they fail again, we promote anyway.)
    df_not_ready = df_their_images.filter(F.col("status") == "PENDING").select("IDDOC").distinct()
    df_ready_iddocs = df_empty_text_iddocs.join(F.broadcast(df_not_ready), on="IDDOC", how="left_anti")
    ready_count = df_ready_iddocs.count()

    if ready_count == 0:
        print(f"{empty_text_count} EMPTY_TEXT document(s), none fully described yet.")
    else:
        df_assembled = (
            df_their_images.join(F.broadcast(df_ready_iddocs), on="IDDOC", how="inner")
            .filter((F.col("status") == "DONE") & F.col("description").isNotNull())
            .groupBy("IDDOC")
            .agg(F.concat_ws("\n\n", F.transform(
                F.sort_array(F.collect_list(F.struct(
                    F.coalesce(F.col("page_no"), F.lit(0)).alias("page_no"),
                    F.col("description").alias("d"),
                ))),
                lambda x: x["d"],
            )).alias("document_text"))
            .withColumns({
                "document_char_count": F.length("document_text"),
                "document_token_count": F.greatest(F.lit(1), F.floor(F.length("document_text") / F.lit(CHARS_PER_TOKEN)).cast("int")),
            })
        )
        promoted_count = df_assembled.count()

        if promoted_count > 0:
            (
                DeltaTable.forName(spark, TARGET_PROCESSED_FILES_TABLE).alias("t")
                .merge(df_assembled.alias("s"), "t.IDDOC = s.IDDOC")
                .whenMatchedUpdate(set={
                    "document_char_count": "s.document_char_count",
                    "document_token_count": "s.document_token_count",
                    "parse_status": "'SUCCESS'",
                    "parser_strategy": "'llm_ocr:pdf'",  # drop "pending_" now that it's resolved
                    "chunking_strategy": "'image_llm_ocr'",
                    "images_needing_description": "0",
                })
                .execute()
            )
            print(f"Promoted {promoted_count} EMPTY_TEXT document(s) to SUCCESS (all images now described).")

        # Ready but every image was SKIP -- terminal anyway so it stops being re-scanned daily.
        df_ready_empty = df_ready_iddocs.join(df_assembled.select("IDDOC"), on="IDDOC", how="left_anti")
        empty_promoted_count = df_ready_empty.count()

        if empty_promoted_count > 0:
            (
                DeltaTable.forName(spark, TARGET_PROCESSED_FILES_TABLE).alias("t")
                .merge(df_ready_empty.alias("s"), "t.IDDOC = s.IDDOC")
                .whenMatchedUpdate(set={
                    "parse_status": "'SKIPPED_EMPTY_IMAGES'",
                    "parser_strategy": "'llm_ocr:pdf'",
                    "images_needing_description": "0",
                })
                .execute()
            )
            print(f"{empty_promoted_count} EMPTY_TEXT document(s) had every image judged non-informative -> SKIPPED_EMPTY_IMAGES.")

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