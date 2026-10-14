"""Phases of the `3_parse` task that scope, select, parse and retry (see `3_Parse_Pipeline.py`).

Each function is one named step of the notebook: scope resolution, file selection, Docling parsing, retry, and the
documents without a parseable file. The tables are built by `build_steps.py` and written by `output_steps.py`.
Driver-side only: nothing here is shipped to the executors.
"""
import os
import shutil
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from databricks.sdk.runtime import dbutils, display, spark
from pyspark.sql import Window
from pyspark.sql import functions as F
from pyspark.sql import types as T

import doc_status
import image_utils
import selection
from format_parsers import strip_ooxml_bloat
from tables import table_exists, write_table
from leap_utils.common.logger import get_logger
from config import (
    BUSINESS_METADATA_COLS,
    CATALOG_SCHEMA,
    CHECKPOINT_BATCH_SIZE,
    CHECKPOINT_TABLE,
    EMPTY_SMALL_FILE_SIZE_BYTES,
    ENABLE_TIMING_TEST,
    ENRICHMENT_COLS,
    MAX_REPEAT,
    MIN_AREA_RATIO,
    PARSE_FILTER,
    RETRY_TEMP_TABLE,
    RUN_MODE,
    TARGET_PROCESSED_FILES_TABLE,
    VOLUME_BASE_PATH,
    VOLUME_ROOT_PATH,
)

logger = get_logger(__name__)

INGESTION_RUN_ID = str(uuid.uuid4())
JOB_RUN_ID = None

# Any other status is retried automatically on the next incremental run.
_TERMINAL_STATUSES = list(doc_status.TERMINAL_STATUSES)   # a list: pyspark isin() does not take a tuple
# A change in any of these on the same IDDOC means a revised document (ref included: Intraqual can rename in place).
_STALENESS_COLS = ["indice", "doc_date", "ref"]

# Size ceilings per format, above which a file is not parsed (Docling rasterizes pages: out-of-memory risk).
_NON_DOCLING_EXTS = ["doc", "ppt", "xls", "xlsx", "xlsm", "xlsb", "rtf", "odt", "ods"]
_OOXML_STRIP_EXTS = ["docx", "docm", "pptx", "pptm"]
_MAX_SIZE_DOCLING = 15 * 1024 * 1024   # docx/docm/pptx/pptm after stripping, and any other Docling format
_MAX_SIZE_PDF = 35 * 1024 * 1024       # large scanned PDFs must reach the LLM-OCR fallback
_MAX_SIZE_OTHER = 100 * 1024 * 1024    # antiword/openpyxl: no page-rasterization OOM risk


def set_job_run_id(job_run_id):
    """Correlate this run's rows with the parent job run (`2_manifest` writes the same id)."""
    global JOB_RUN_ID
    JOB_RUN_ID = job_run_id or None


def resolve_parse_scope(df_business_meta):
    """Resolve which IDDOCs need parsing this run.

    Full mode: everything in the manifest. Incremental mode: manifest minus
    already-terminal IDDOCs, plus any of those flagged as revised since their
    last parse. Returns (target_iddocs, has_target_iddocs).
    """
    # parse_content=False: pre-cutoff document beyond ARCHIVE_MAX_DOCS (notice chunk only).
    df_parseable = (df_business_meta.filter(F.col("parse_content"))
                    if "parse_content" in df_business_meta.columns else df_business_meta)
    manifest_iddocs = {r.IDDOC for r in df_parseable.select("IDDOC").distinct().collect()}
    already_done_iddocs = set()
    revised_iddocs = set()

    if RUN_MODE == "incremental":
        try:
            df_pf = spark.table(TARGET_PROCESSED_FILES_TABLE)
            already_done_iddocs = {
                r.IDDOC for r in df_pf
                .filter(F.col("parse_status").isin(_TERMINAL_STATUSES))
                .select("IDDOC").distinct().collect()
            }
            logger.info(f"[INCREMENTAL] {len(already_done_iddocs)} IDDOCs already terminal in {TARGET_PROCESSED_FILES_TABLE}")

            missing_cols = [c for c in _STALENESS_COLS if c not in df_pf.columns]
            if missing_cols:
                logger.info(f"[REVISION] Columns missing from processed_files ({', '.join(missing_cols)}) — detection skipped this run.")
            else:
                df_pf_revision = (
                    df_pf.filter(F.col("parse_status") == "SUCCESS")
                    .select("IDDOC", *[F.col(c).alias(f"_pf_{c}") for c in _STALENESS_COLS])
                    .dropDuplicates(["IDDOC"])
                )
                # A NULL _pf_<col> has no value to compare with: revision cannot be told, so the IDDOC is not flagged.
                is_revised = F.lit(False)
                for c in _STALENESS_COLS:
                    is_revised = is_revised | (F.col(f"_pf_{c}").isNotNull() & ~F.col(c).eqNullSafe(F.col(f"_pf_{c}")))
                revised_iddocs = {
                    r.IDDOC for r in df_business_meta
                    .select("IDDOC", *_STALENESS_COLS)
                    .join(df_pf_revision, on="IDDOC", how="inner")
                    .filter(is_revised)
                    .select("IDDOC").distinct().collect()
                }
                logger.info(f"[REVISION] {len(revised_iddocs)} IDDOCs revised since last parse ({', '.join(_STALENESS_COLS)} changed) — re-parsing.")
        except Exception as exc:
            logger.warning(f"[INCREMENTAL] Table {TARGET_PROCESSED_FILES_TABLE} unreadable ({exc}) — no IDDOC excluded.")
    elif RUN_MODE == "full":
        logger.info("[FULL] Parsing all documents. Tables will be overwritten.")
    elif RUN_MODE == "rechunk":
        logger.info("[RECHUNK] No parsing: chunks rebuilt from the checkpoint for every document. Tables will be overwritten.")

    pending_iddocs = manifest_iddocs - (already_done_iddocs - revised_iddocs)
    if isinstance(PARSE_FILTER, (list, tuple, set)):
        target_iddocs = pending_iddocs & set(PARSE_FILTER)
    else:
        target_iddocs = pending_iddocs

    logger.info(f"[SCOPE] manifest={len(manifest_iddocs)} | already terminal={len(already_done_iddocs)} "
                f"| revised={len(revised_iddocs)} | to scan={len(target_iddocs)}")
    return target_iddocs, revised_iddocs


def select_files_for_target_iddocs(target_iddocs, df_business_meta):
    """Scan only the target IDDOCs' folders, rank every candidate file per
    IDDOC, and pick the winner.

    Returns (df_files, df_matched_full, df_content) — df_matched_full/df_content
    are kept for the retry phase (rank-2 candidates), df_files feeds parsing.
    """
    if target_iddocs:
        root_dbfs = VOLUME_ROOT_PATH.replace("/Volumes/", "dbfs:/Volumes/")
        root_items = dbutils.fs.ls(root_dbfs)
        matching_paths = [
            f"{VOLUME_ROOT_PATH}/{item.name.rstrip('/')}"
            for item in root_items
            if selection.extract_iddoc_from_name(item.name) in target_iddocs
        ]
        logger.info(f"[SCAN] {len(matching_paths)} target folder(s) out of {len(root_items)} total in the volume")
    else:
        matching_paths = []
        logger.info("[SCAN] Nothing to scan — every manifest IDDOC is already processed (or outside PARSE_FILTER).")

    df_meta_raw, df_content = selection.scan_volume_paths(spark, matching_paths)
    df_meta_raw.cache()

    df_matched_full = selection.rank_candidates(df_meta_raw).cache()

    df_selected = selection.select_best_files(df_matched_full, df_business_meta)
    df_files = selection.attach_content(df_selected, df_content, INGESTION_RUN_ID).cache()

    return df_files, df_matched_full, df_content


def _with_parse_result(df, xml_only_col):
    """Run the parsing UDF on `content` and unpack its result into the checkpoint columns (content dropped)."""
    return (
        df.withColumn("result", image_utils.parse_and_extract_images_udf(
            F.col("content"), F.col("source_file_extension"), F.col("IDDOC").try_cast("string"),
            F.lit(VOLUME_BASE_PATH), F.lit(MIN_AREA_RATIO), F.lit(MAX_REPEAT), F.lit(ENABLE_TIMING_TEST),
            xml_only_col))
        .withColumns({
            "document_text": F.col("result.text"),
            "parser_error": F.col("result.parser_error"),
            "parser_strategy": F.col("result.parser_strategy"),
            "parse_time_seconds": F.col("result.parse_time_seconds"),
            "images": F.col("result.images"),
            "timings": F.col("result.timings"),
        })
        .withColumn("image_count", F.size(F.col("images")))
        .drop("result", "content")
    )


def make_parse_fn(cluster_parallelism):
    """Build the per-batch parse function, closing over cluster_parallelism.

    Partitioning strategy: one partition per file, capped at cluster
    parallelism, so the ~2 concurrent GPU slots (1 task/GPU, enforced by
    Databricks' GPU-ML runtime) are kept continuously fed from a fine-grained
    queue instead of waiting on a few coarse partitions that each bundle many
    files sequentially. PySpark reuses Python worker processes across tasks
    by default, so Docling is still only imported once per worker even with
    many small partitions.
    """
    def parse(df, file_count):
        partitions = max(1, min(file_count, cluster_parallelism))
        has_xml_fallback_col = "_xml_fallback_only" in df.columns
        xml_fallback_col = F.col("_xml_fallback_only") if has_xml_fallback_col else F.lit(False)
        df_parsed = _with_parse_result(df.repartition(partitions), xml_fallback_col).drop("_xml_fallback_only")
        return df_parsed.withColumn(
            "parse_status",
            F.when(
                (F.length(F.trim(F.col("document_text"))) == 0)
                & (F.col("source_file_size_bytes") <= EMPTY_SMALL_FILE_SIZE_BYTES),
                F.lit("SKIPPED_EMPTY_SMALL_FILE"),
            )
            .when(F.col("parser_error").isNotNull(), F.lit("ERROR"))
            .when(F.length(F.trim(F.col("document_text"))) == 0, F.lit("EMPTY_TEXT"))
            .otherwise(F.lit("SUCCESS")),
        )
    return parse


def snapshot_or_log_checkpoint(num_files):
    """Full run: shallow-clone the live checkpoint before it's overwritten.
    Incremental run: just log the current Delta version for manual
    time-travel restore."""
    if RUN_MODE == "full" and num_files > 0:
        snap_ts = datetime.now().strftime("%Y%m%d_%H%M")
        snap_table = f"{CATALOG_SCHEMA}._pipeline_checkpoint_{snap_ts}"
        try:
            spark.table(CHECKPOINT_TABLE)
            spark.sql(f"CREATE TABLE IF NOT EXISTS {snap_table} SHALLOW CLONE {CHECKPOINT_TABLE}")
            logger.info(f"[SNAPSHOT] {CHECKPOINT_TABLE} -> {snap_table}")
        except Exception:
            logger.info("[SNAPSHOT] No existing checkpoint to save.")
    elif RUN_MODE == "incremental":
        try:
            hist = spark.sql(f"DESCRIBE HISTORY {CHECKPOINT_TABLE} LIMIT 1")
            version_rows = hist.select("version", "timestamp").collect()
            if version_rows:
                v = version_rows[0]
                logger.info(f"[CHECKPOINT] Current version = {v['version']} ({v['timestamp']}) "
                            f"| restore: versionAsOf={v['version']} on {CHECKPOINT_TABLE}")
        except Exception:
            logger.info("[CHECKPOINT] No existing checkpoint yet.")


def read_checkpoint_deduped():
    """Read _pipeline_checkpoint with exactly one row per IDDOC — dedup by
    IDDOC (not source_path), preferring SUCCESS then the most recent attempt.
    """
    w = Window.partitionBy("IDDOC").orderBy(
        F.when(F.col("parse_status") == "SUCCESS", 0).otherwise(1),
        F.desc("ingestion_timestamp"),
    )
    return (
        spark.table(CHECKPOINT_TABLE)
        .withColumn("_rn", F.row_number().over(w))
        .filter(F.col("_rn") == 1)
        .drop("_rn")
    )


def exclude_already_parsed(df_files):
    """Resume support: drop files already SUCCESS in the checkpoint, keyed on
    (source_path, document_sha256). Returns the filtered, cached df_files.
    """
    if not table_exists(CHECKPOINT_TABLE):
        logger.info("No existing checkpoint — starting from scratch.")
        return df_files

    df_existing_ckpt = spark.table(CHECKPOINT_TABLE)
    existing_count = df_existing_ckpt.count()
    if existing_count == 0:
        return df_files

    already_done = (
        df_existing_ckpt
        .filter(F.col("parse_status") == "SUCCESS")
        .select("source_path", "document_sha256").distinct()
    )
    before = df_files.count()
    df_files = df_files.join(already_done, on=["source_path", "document_sha256"], how="left_anti").cache()
    after = df_files.count()
    logger.info(f"[RESUME] {existing_count} checkpoint rows "
                f"({already_done.count()} terminal on (source_path, document_sha256), SUCCESS). "
                f"Remaining to parse: {after} (of {before} total)")
    if after == 0:
        logger.info("All files are already parsed (successfully) in the checkpoint.")
    return df_files


def cleanup_stale_image_folders(df_files):
    """Delete image folders for every IDDOC about to be (re)parsed, so a
    retry/rebuild never mixes old and new extracted images. Must run AFTER
    the resume-exclusion join above.
    """
    iddocs_to_parse = [
        r.IDDOC for r in df_files.select("IDDOC")
        .filter(F.col("IDDOC").isNotNull()).distinct().collect()
    ]

    def _rm_folder(iddoc):
        folder = os.path.join(VOLUME_BASE_PATH, str(iddoc))
        if os.path.isdir(folder):
            shutil.rmtree(folder)
            return True
        return False

    with ThreadPoolExecutor(max_workers=8) as pool:
        removed = sum(pool.map(_rm_folder, iddocs_to_parse))
    logger.info(f"Cleaned {removed}/{len(iddocs_to_parse)} existing image folders")


def apply_oom_size_gate(df_files):
    """Split df_files into (df_to_parse, df_skipped) using per-format OOM
    ceilings. OOXML files above the Docling-safe size but within the
    fallback ceiling are flagged _xml_fallback_only (routed straight to the
    zip/XML fallback, no Docling).
    """
    strip_ooxml_bloat_udf = F.udf(strip_ooxml_bloat, T.BinaryType())

    df_files = df_files.withColumn(
        "content",
        F.when(F.col("source_file_extension").isin(_OOXML_STRIP_EXTS),
               strip_ooxml_bloat_udf(F.col("content"), F.col("source_file_extension")))
        .otherwise(F.col("content"))
    ).withColumn("_gate_size_bytes", F.length(F.col("content")))

    max_size_col = (
        F.when(F.col("source_file_extension").isin(_NON_DOCLING_EXTS), F.lit(_MAX_SIZE_OTHER))
        .when(F.col("source_file_extension") == "pdf", F.lit(_MAX_SIZE_PDF))
        .when(F.col("source_file_extension").isin(_OOXML_STRIP_EXTS), F.lit(_MAX_SIZE_OTHER))
        .otherwise(F.lit(_MAX_SIZE_DOCLING))
    )
    is_too_large = F.col("_gate_size_bytes") > max_size_col
    df_files = df_files.withColumn(
        "_xml_fallback_only",
        F.col("source_file_extension").isin(_OOXML_STRIP_EXTS)
        & (F.col("_gate_size_bytes") > F.lit(_MAX_SIZE_DOCLING))
    )

    # Cached here so strip_ooxml_bloat_udf's lineage runs once, not on every count()/filter() below.
    df_to_parse = df_files.filter(~is_too_large).drop("_gate_size_bytes").cache()
    df_skipped = df_files.filter(is_too_large).drop("_gate_size_bytes", "_xml_fallback_only").cache()
    return df_to_parse, df_skipped


def write_skipped_too_large(df_skipped, skipped_count):
    """Persist oversized files directly as SKIP_TOO_LARGE, no parse attempt."""
    logger.warning(f"{skipped_count} files excluded (beyond even the XML fallback ceiling).")
    df_skipped_result = (
        df_skipped.drop("content")
        .withColumn("document_text", F.lit(""))
        .withColumn("parser_error",
            F.concat(F.lit("TOO_LARGE:"), F.round(F.col("source_file_size_bytes") / 1024 / 1024, 1).try_cast("string"), F.lit("MB")))
        .withColumn("parser_strategy", F.lit("skipped"))
        .withColumn("parse_time_seconds", F.lit(0.0).cast("float"))
        .withColumn("images", F.array().cast("array<struct<image_id:int,page_no:int,label:string,area_ratio:float,captions:array<string>,context_text:string,volume_path:string,image_width:int,image_height:int>>"))
        .withColumn("timings", F.lit(None).cast("map<string,float>"))
        .withColumn("image_count", F.lit(0))
        .withColumn("parse_status", F.lit("SKIP_TOO_LARGE"))
    )
    write_table(df_skipped_result, CHECKPOINT_TABLE, "append", merge_schema=True)
    logger.info(f"{skipped_count} files marked SKIP_TOO_LARGE in the checkpoint.")


def run_docling_batches(df_to_parse, parse_count, parse_fn):
    """Parse df_to_parse, checkpointing every CHECKPOINT_BATCH_SIZE files so
    progress survives a crash. A single small batch is written directly."""
    if parse_count <= CHECKPOINT_BATCH_SIZE:
        logger.info(f"Direct parsing ({parse_count} files, no batching needed)...")
        df_parsed = parse_fn(df_to_parse, parse_count)
        write_table(df_parsed, CHECKPOINT_TABLE, "append", merge_schema=True)
        return

    num_batches = (parse_count + CHECKPOINT_BATCH_SIZE - 1) // CHECKPOINT_BATCH_SIZE
    logger.info(f"{num_batches} batch(es) of up to {CHECKPOINT_BATCH_SIZE} files...")
    # Stable partition key avoids a driver-side collect()+isin() per batch; reuses the already-cached df_to_parse.
    df_to_parse_batched = df_to_parse.withColumn(
        "_batch_idx", (F.monotonically_increasing_id() % F.lit(num_batches)).try_cast("int")
    ).cache()

    for batch_idx in range(num_batches):
        df_batch = df_to_parse_batched.filter(F.col("_batch_idx") == batch_idx).drop("_batch_idx")
        batch_size = df_batch.count()
        if batch_size == 0:
            continue

        logger.info(f"Batch {batch_idx + 1}/{num_batches} ({batch_size} files)...")
        bt0 = time.time()
        df_batch_parsed = parse_fn(df_batch, batch_size)
        write_table(df_batch_parsed, CHECKPOINT_TABLE, "append", merge_schema=True)
        logger.info(f"Batch {batch_idx + 1}/{num_batches} saved in {time.time() - bt0:.1f}s")


def log_run_summary(df_to_parse, parse_count, elapsed):
    """Log OK/SKIP/ERR counts scoped to exactly the files parsed THIS run
    (source_path, document_sha256) — not the whole checkpoint."""
    df_this_run = read_checkpoint_deduped().join(
        df_to_parse.select("source_path", "document_sha256"),
        on=["source_path", "document_sha256"], how="inner",
    )
    run_errors = df_this_run.filter(~F.col("parse_status").isin("SUCCESS", "SKIP_TOO_LARGE")).count()
    run_skips = df_this_run.filter(F.col("parse_status") == "SKIP_TOO_LARGE").count()
    run_ok = parse_count - run_errors - run_skips

    logger.info(f"Parsed {parse_count} files in {elapsed:.1f}s "
                f"({parse_count / max(elapsed, 0.01):.1f} f/s) | OK: {run_ok} | "
                f"SKIP_TOO_LARGE: {run_skips} | ERR: {run_errors}")


def parse_pending_files(df_files, parse_fn):
    """Full "parse everything pending" phase: OOM gate, direct/batched
    Docling parsing, run summary. df_files must already be resume-excluded."""
    df_to_parse, df_skipped = apply_oom_size_gate(df_files)
    skipped_count = df_skipped.count()
    fallback_only_count = df_to_parse.filter(F.col("_xml_fallback_only")).count()
    parse_count = df_to_parse.count()

    if skipped_count > 0:
        write_skipped_too_large(df_skipped, skipped_count)

    if fallback_only_count > 0:
        logger.info(f"{fallback_only_count} docx/pptx files too large for Docling "
                    f"will use the direct XML fallback (no rasterization, no ai_parse_document).")

    logger.info(f"{parse_count} files to parse with Docling...")
    if parse_count == 0:
        logger.info("Nothing to parse (all remaining files are excluded XLSX).")
        return

    t0 = time.time()
    run_docling_batches(df_to_parse, parse_count, parse_fn)
    log_run_summary(df_to_parse, parse_count, time.time() - t0)


def _images_pending_iddocs():
    """IDDOCs parked as IMAGES_PENDING: text parsed, only images left. Their checkpoint row still says EMPTY_TEXT,
    so the scans of failed documents skip them: they are not parsed again."""
    if not table_exists(TARGET_PROCESSED_FILES_TABLE):
        return spark.createDataFrame([], "IDDOC long")
    return (
        spark.table(TARGET_PROCESSED_FILES_TABLE)
        .filter(F.col("parse_status") == doc_status.IMAGES_PENDING)
        .select("IDDOC").distinct()
    )


def show_failed_documents(df_business_meta):
    """Display documents that failed Docling parsing, before the rank-2 retry.
    Scoped to IDDOCs still in scope."""
    df_full_pipeline = read_checkpoint_deduped()
    df_failed_inspect = (
        df_full_pipeline
        .join(df_business_meta.select("IDDOC").distinct(), on="IDDOC", how="inner")
        .join(_images_pending_iddocs(), on="IDDOC", how="left_anti")
        .filter(F.col("parse_status").isin("ERROR", "EMPTY_TEXT"))
        .filter(~F.col("parser_strategy").eqNullSafe("pending_llm_ocr:pdf"))  # queued for step 4, not actually failed
        .select(
            "IDDOC", "source_file_name", "source_file_extension",
            "parse_status", "parser_strategy", "parser_error", "source_file_size_bytes",
        )
        .orderBy("source_file_extension", "IDDOC")
    )
    failed_total = df_failed_inspect.count()
    logger.warning(f"{failed_total} documents failed after Docling parsing — "
                   f"will be retried below with each IDDOC's next-best file variant, if any")

    display(
        df_failed_inspect.groupBy("source_file_extension", "parse_status", "parser_strategy")
        .agg(F.count("*").alias("count"), F.first("parser_error").alias("sample_error"))
        .orderBy(F.desc("count"))
    )
    display(df_failed_inspect)



def retry_failed_iddocs(df_matched_full, df_content, df_business_meta):
    """Re-attempt every failed IDDOC with its rank-2 candidate file (if any),
    then replace the failed checkpoint rows with the successful retries."""
    df_full_pipeline = read_checkpoint_deduped()
    failed_iddocs = [
        r.IDDOC for r in df_full_pipeline
        .join(df_business_meta.select("IDDOC").distinct(), on="IDDOC", how="inner")  # still in current scope
        .join(_images_pending_iddocs(), on="IDDOC", how="left_anti")
        .filter(F.col("parse_status").isin("ERROR", "EMPTY_TEXT") & F.col("IDDOC").isNotNull())
        .filter(~F.col("parser_strategy").eqNullSafe("pending_llm_ocr:pdf"))  # queued for step 4, not actually failed
        .select("IDDOC").distinct().collect()
    ]
    logger.info(f"Failed IDDOCs to retry: {len(failed_iddocs)}")
    if not failed_iddocs:
        logger.info("Nothing to retry.")
        return

    # .ppt stays a rank-2 candidate: image_fallbacks.fallback_parse_ppt_legacy parses it.
    df_alt = (
        df_matched_full
        .filter(F.col("IDDOC").isin(failed_iddocs))
        .filter((F.col("priority_rank") == 2) & (F.col("ext_priority") < selection.UNSUPPORTED_PRIORITY))
        .drop(*selection.RANK_HELPER_COLS)
    )
    retry_paths = [r.source_path for r in df_alt.select("source_path").collect()]

    if not retry_paths:
        logger.info("No rank-2 alternatives found.")
        return

    logger.info(f"Retrying {len(retry_paths)} files...")
    # Read ONLY the needed binary content (avoids full volume re-scan).
    df_content_retry = df_content.filter(F.col("source_path").isin(retry_paths))
    df_retry_src = (
        df_alt
        .join(df_content_retry, on="source_path", how="inner")
        .withColumn("document_sha256", F.sha2(F.col("content"), 256))
        .withColumn("ingestion_run_id", F.lit(INGESTION_RUN_ID))
        .withColumn("ingestion_timestamp", F.current_timestamp())
    )
    biz_cols = [c for c in ENRICHMENT_COLS if c in df_business_meta.columns]
    df_retry_src = df_retry_src.join(
        F.broadcast(df_business_meta.select("IDDOC", *biz_cols).dropDuplicates(["IDDOC"])),
        on="IDDOC", how="left",
    )
    df_retry = _with_parse_result(df_retry_src.repartition(max(1, len(retry_paths))), F.lit(False)).withColumn(
        "parse_status",
        F.when(F.col("parser_error").isNotNull(), F.lit("ERROR"))
        .when(F.length(F.trim(F.col("document_text"))) == 0, F.lit("EMPTY_TEXT"))
        .otherwise(F.lit("SUCCESS")),
    )

    write_table(df_retry, RETRY_TEMP_TABLE, "overwrite", overwrite_schema=True)

    df_retry_ok = spark.table(RETRY_TEMP_TABLE).filter(F.col("parse_status") == "SUCCESS")
    ok_count = df_retry_ok.count()
    logger.info(f"Retry recovered {ok_count}/{len(failed_iddocs)} IDDOCs")

    if ok_count > 0:
        ok_iddocs = [r.IDDOC for r in df_retry_ok.select("IDDOC").collect()]
        df_keep = spark.table(CHECKPOINT_TABLE).filter(
            ~(F.col("parse_status").isin("ERROR", "EMPTY_TEXT") & F.col("IDDOC").isin(ok_iddocs))
        )
        write_table(df_keep.unionByName(df_retry_ok, allowMissingColumns=True), CHECKPOINT_TABLE, "overwrite",
                    overwrite_schema=True)
        logger.info("Checkpoint updated with retry results.")

    spark.sql(f"DROP TABLE IF EXISTS {RETRY_TEMP_TABLE}")


def mark_empty_folders(target_iddocs, df_files, df_business_meta):
    """Write SKIPPED_EMPTY_FOLDER for target IDDOCs with no parseable file on the volume.

    These never go through parsing, so their rows go straight to `processed_files`.
    Returns `target_iddocs` without them, so retry / build steps skip them.
    """
    selected_iddocs = {r.IDDOC for r in df_files.select("IDDOC").distinct().collect()}
    empty_folder_iddocs = target_iddocs - selected_iddocs
    if not empty_folder_iddocs:
        return target_iddocs

    logger.warning(
        f"[EMPTY FOLDER] {len(empty_folder_iddocs)} target IDDOC(s) have no parseable file on the volume "
        f"- marking as SKIPPED_EMPTY_FOLDER: {sorted(empty_folder_iddocs)}"
    )
    manifest_cols = [c for c in BUSINESS_METADATA_COLS if c in df_business_meta.columns]
    df_empty_folder_rows = (
        df_business_meta
        .filter(F.col("IDDOC").isin(list(empty_folder_iddocs)))
        .select("IDDOC", *manifest_cols)
        .withColumns({
            "parse_status": F.lit("SKIPPED_EMPTY_FOLDER"),
            "removal_reason": F.lit("NO_PARSEABLE_FILE_ON_VOLUME"),
            "include_in_rag": F.lit(False),
            "filtered_by_date": F.lit(False),
            "ingestion_run_id": F.lit(INGESTION_RUN_ID),
            "ingestion_timestamp": F.current_timestamp(),
            "job_run_id": F.lit(JOB_RUN_ID),
            "change_type": F.lit("NEW"),
        })
    )
    write_table(df_empty_folder_rows, TARGET_PROCESSED_FILES_TABLE, "append", merge_schema=True)
    logger.info(f"[EMPTY FOLDER] Wrote {len(empty_folder_iddocs)} row(s) to {TARGET_PROCESSED_FILES_TABLE}")
    return target_iddocs - empty_folder_iddocs
