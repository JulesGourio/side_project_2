"""Phases of the `3_parse` task (see `3_Parse_Pipeline.py`).

Each function is one named step of the notebook: scope resolution, file selection, Docling parsing,
retry, `image_metadata`, `processed_files` / `chunks` and their writes. The notebook only orchestrates.
Driver-side only: nothing here is shipped to the executors.
"""
import os
import shutil
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from databricks.sdk.runtime import dbutils, display, spark
from delta.tables import DeltaTable
from pyspark.sql import Window
from pyspark.sql import functions as F
from pyspark.sql import types as T

import image_utils
import selection
import utils
from utils import logger
from config import (
    ARCHIVE_NOTICES_IN_RAG,
    ARCHIVE_NOTICE_CONTENT_TYPE,
    ARCHIVE_NOTICE_MARKER,
    BOILERPLATE_MIN_DOCS,
    CATALOG_SCHEMA,
    CHARS_PER_TOKEN,
    CHECKPOINT_BATCH_SIZE,
    CLEAN_FORMULA_ARTIFACTS,
    CLEAN_IMAGE_PLACEHOLDERS,
    DEDUPLICATE_CHUNKS,
    DIVISION_REFERENCE_TABLE,
    DOC_DATE_CUTOFF,
    EMBED_SOURCE_PREFIX,
    EMPTY_SMALL_FILE_SIZE_BYTES,
    ENABLE_TIMING_TEST,
    MAX_CHUNKS_SPREADSHEET,
    MAX_REPEAT,
    MIN_AREA_RATIO,
    PARSE_FILTER,
    PARSE_MANIFEST_TABLE,
    RUN_MODE,
    TARGET_ARCHIVE_NOTICE_TABLE,
    TARGET_CHANGE_LOG_TABLE,
    TARGET_CHUNK_TABLE,
    TARGET_CHUNK_TABLE_ARCHIVE,
    TARGET_HEALTH_TABLE,
    TARGET_IMAGE_METADATA_TABLE,
    TARGET_PROCESSED_FILES_TABLE,
    VOLUME_BASE_PATH,
    TABLE_SUFFIX,
    VOLUME_ROOT_PATH,
)

CHECKPOINT_TABLE = f"{CATALOG_SCHEMA}._pipeline_checkpoint{TABLE_SUFFIX}"
RAG_EXCLUSION_TABLE = f"{CATALOG_SCHEMA}._rag_exclusions{TABLE_SUFFIX}"
RETRY_TEMP_TABLE = f"{CATALOG_SCHEMA}._retry_temp{TABLE_SUFFIX}"

INGESTION_RUN_ID = str(uuid.uuid4())
JOB_RUN_ID = None

# Any other status is retried automatically on the next incremental run.
_TERMINAL_STATUSES = ["SUCCESS", "SKIPPED_EMPTY_SMALL_FILE", "SKIPPED_REF_MANUAL", "SKIPPED_SOURCE_REMOVED",
                      "SKIPPED_EMPTY_IMAGES", "SKIPPED_EMPTY_FOLDER"]
# A change in any of these on the same IDDOC means a revised document (ref included: Intraqual can rename in place).
_STALENESS_COLS = ["indice", "doc_date", "ref"]

_MANIFEST_METADATA_COLS = [
    "ref", "titre", "type_document", "categorie", "langue", "auteur", "doc_date", "indice",
]
_HIERARCHY_COLS = [
    "division", "niveau_plus_1", "niveau_plus_2", "niveau_plus_3",
    "niveau_plus_4", "niveau_plus_5", "niveau_plus_6",
]
_BUSINESS_METADATA_COLS = _MANIFEST_METADATA_COLS + _HIERARCHY_COLS

_NON_DOCLING_EXTS = ["doc", "ppt", "xls", "xlsx", "xlsm", "xlsb", "rtf", "odt", "ods"]
_OOXML_STRIP_EXTS = ["docx", "docm", "pptx", "pptm"]
_MAX_SIZE_DOCLING = 15 * 1024 * 1024   # docx/docm/pptx/pptm after stripping, and any other Docling format
_MAX_SIZE_PDF = 35 * 1024 * 1024       # large scanned PDFs must reach the LLM-OCR fallback
_MAX_SIZE_OTHER = 100 * 1024 * 1024    # antiword/openpyxl: no page-rasterization OOM risk
_SPREADSHEET_EXTS = ("xlsx", "xls", "xlsm")


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
                # _pf_<col> NULL = parsed before this column existed — can't tell if revised, don't flag.
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

    df_matched_full = selection.rank_candidates(df_meta_raw, df_business_meta).cache()

    # For scope-coverage diagnostics (build_unified_audit), run 2_Cleanup_Volume.py instead.
    df_selected = selection.select_best_files(df_matched_full, df_business_meta, parse_filter=PARSE_FILTER)
    df_files = selection.attach_content(df_selected, df_content, INGESTION_RUN_ID).cache()

    return df_files, df_matched_full, df_content


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
        return (
            df.repartition(partitions)
            .withColumn("result", image_utils.parse_and_extract_images_udf(
                F.col("content"), F.col("source_file_extension"), F.col("IDDOC").cast("string"),
                F.lit(VOLUME_BASE_PATH), F.lit(MIN_AREA_RATIO), F.lit(MAX_REPEAT), F.lit(ENABLE_TIMING_TEST),
                xml_fallback_col))
            .withColumns({
                "document_text": F.col("result.text"),
                "parser_error": F.col("result.parser_error"),
                "parser_strategy": F.col("result.parser_strategy"),
                "parse_time_seconds": F.col("result.parse_time_seconds"),
                "images": F.col("result.images"),
                "timings": F.col("result.timings"),
            })
            .withColumn("image_count", F.size(F.col("images")))
            .drop("result", "content", "_xml_fallback_only")
            .withColumn("parse_status",
                        F.when(
                            (F.length(F.trim(F.col("document_text"))) == 0)
                            & (F.col("source_file_size_bytes") <= EMPTY_SMALL_FILE_SIZE_BYTES),
                            F.lit("SKIPPED_EMPTY_SMALL_FILE"),
                        )
                        .when(F.col("parser_error").isNotNull(), F.lit("ERROR"))
                        .when(F.length(F.trim(F.col("document_text"))) == 0, F.lit("EMPTY_TEXT"))
                        .otherwise(F.lit("SUCCESS")))
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


def with_fresh_business_metadata(df_checkpoint):
    """Replace checkpoint's business-metadata columns with fresh values,
    falling back to the checkpoint's own value when an IDDOC isn't in scope.
    division/niveau_plus_* come from DIVISION_REFERENCE_TABLE, not
    parse_manifest, which goes stale.
    """
    df_manifest = spark.table(PARSE_MANIFEST_TABLE).select(
        F.col("IDDOC"), *[F.col(c).alias(f"_fresh_{c}") for c in _MANIFEST_METADATA_COLS]
    )
    df_hierarchy = spark.table(DIVISION_REFERENCE_TABLE).select(
        F.col("IDDOC"), *[F.col(c).alias(f"_fresh_{c}") for c in _HIERARCHY_COLS]
    )
    df = df_checkpoint
    for c in _BUSINESS_METADATA_COLS:
        if c not in df.columns:
            df = df.withColumn(c, F.lit(None))
    df = (
        df.join(F.broadcast(df_manifest), on="IDDOC", how="left")
        .join(F.broadcast(df_hierarchy), on="IDDOC", how="left")
    )
    for c in _BUSINESS_METADATA_COLS:
        df = df.withColumn(c, F.coalesce(F.col(f"_fresh_{c}"), F.col(c)))
    return df.drop(*[f"_fresh_{c}" for c in _BUSINESS_METADATA_COLS])


def exclude_already_parsed(df_files):
    """Resume support: drop files already SUCCESS in the checkpoint, keyed on
    (source_path, document_sha256). Returns the filtered, cached df_files.
    """
    if not spark.catalog.tableExists(CHECKPOINT_TABLE):
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
    strip_ooxml_bloat_udf = F.udf(utils.strip_ooxml_bloat, T.BinaryType())

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
            F.concat(F.lit("TOO_LARGE:"), F.round(F.col("source_file_size_bytes") / 1024 / 1024, 1).cast("string"), F.lit("MB")))
        .withColumn("parser_strategy", F.lit("skipped"))
        .withColumn("parse_time_seconds", F.lit(0.0).cast("float"))
        .withColumn("images", F.array().cast("array<struct<image_id:int,page_no:int,label:string,area_ratio:float,captions:array<string>,context_text:string,volume_path:string,image_width:int,image_height:int>>"))
        .withColumn("timings", F.lit(None).cast("map<string,float>"))
        .withColumn("image_count", F.lit(0))
        .withColumn("parse_status", F.lit("SKIP_TOO_LARGE"))
    )
    df_skipped_result.write.format("delta").mode("append") \
        .option("mergeSchema", "true").saveAsTable(CHECKPOINT_TABLE)
    logger.info(f"{skipped_count} files marked SKIP_TOO_LARGE in the checkpoint.")


def run_docling_batches(df_to_parse, parse_count, parse_fn):
    """Parse df_to_parse, checkpointing every CHECKPOINT_BATCH_SIZE files so
    progress survives a crash. A single small batch is written directly."""
    if parse_count <= CHECKPOINT_BATCH_SIZE:
        logger.info(f"Direct parsing ({parse_count} files, no batching needed)...")
        df_parsed = parse_fn(df_to_parse, parse_count)
        df_parsed.write.format("delta").mode("append") \
            .option("mergeSchema", "true").saveAsTable(CHECKPOINT_TABLE)
        return

    num_batches = (parse_count + CHECKPOINT_BATCH_SIZE - 1) // CHECKPOINT_BATCH_SIZE
    logger.info(f"{num_batches} batch(es) of up to {CHECKPOINT_BATCH_SIZE} files...")
    # Stable partition key avoids a driver-side collect()+isin() per batch; reuses the already-cached df_to_parse.
    df_to_parse_batched = df_to_parse.withColumn(
        "_batch_idx", (F.monotonically_increasing_id() % F.lit(num_batches)).cast("int")
    ).cache()

    for batch_idx in range(num_batches):
        df_batch = df_to_parse_batched.filter(F.col("_batch_idx") == batch_idx).drop("_batch_idx")
        batch_size = df_batch.count()
        if batch_size == 0:
            continue

        logger.info(f"Batch {batch_idx + 1}/{num_batches} ({batch_size} files)...")
        bt0 = time.time()
        df_batch_parsed = parse_fn(df_batch, batch_size)
        df_batch_parsed.write.format("delta").mode("append") \
            .option("mergeSchema", "true").saveAsTable(CHECKPOINT_TABLE)
        logger.info(f"Batch {batch_idx + 1}/{num_batches} saved in {time.time() - bt0:.1f}s")


def log_run_summary(df_to_parse, parse_count, elapsed):
    """Log OK/SKIP/ERR counts scoped to exactly the files parsed THIS run
    (source_path, document_sha256) — not the whole historical checkpoint."""
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


def show_failed_documents(df_business_meta):
    """Display documents that failed Docling parsing, before the rank-2 retry.
    Scoped to IDDOCs still in scope."""
    df_full_pipeline = read_checkpoint_deduped()
    df_failed_inspect = (
        df_full_pipeline
        .join(df_business_meta.select("IDDOC").distinct(), on="IDDOC", how="inner")
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
    then replace the failed checkpoint rows with successful retries."""
    df_full_pipeline = read_checkpoint_deduped()
    failed_iddocs = [
        r.IDDOC for r in df_full_pipeline
        .join(df_business_meta.select("IDDOC").distinct(), on="IDDOC", how="inner")  # still in current scope
        .filter(F.col("parse_status").isin("ERROR", "EMPTY_TEXT") & F.col("IDDOC").isNotNull())
        .filter(~F.col("parser_strategy").eqNullSafe("pending_llm_ocr:pdf"))  # queued for step 4, not actually failed
        .select("IDDOC").distinct().collect()
    ]
    logger.info(f"Failed IDDOCs to retry: {len(failed_iddocs)}")
    if not failed_iddocs:
        logger.info("Nothing to retry.")
        return read_checkpoint_deduped()

    # .ppt is a legitimate rank-2 candidate — image_utils._fallback_parse_ppt_legacy handles it.
    df_alt = (
        df_matched_full
        .filter(F.col("IDDOC").isin(failed_iddocs))
        .filter((F.col("priority_rank") == 2) & (F.col("ext_priority") < 99))
        .drop("ext_priority", "is_dm_file", "priority_rank", "files_count_for_iddoc")
    )
    retry_paths = [r.source_path for r in df_alt.select("source_path").collect()]

    if not retry_paths:
        logger.info("No rank-2 alternatives found.")
        return read_checkpoint_deduped()

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
    biz_cols = [c for c in ("ref", "titre", "type_document", "categorie", "langue",
                            "auteur", "document_prefixes", "division", "doc_date",
                            "niveau_plus_1", "niveau_plus_2", "niveau_plus_3",
                            "niveau_plus_4", "niveau_plus_5", "niveau_plus_6")
                if c in df_business_meta.columns]
    df_retry_src = df_retry_src.join(
        F.broadcast(df_business_meta.select("IDDOC", *biz_cols).dropDuplicates(["IDDOC"])),
        on="IDDOC", how="left",
    )
    df_retry = (
        df_retry_src.repartition(max(1, len(retry_paths)))
        .withColumn("result", image_utils.parse_and_extract_images_udf(
            F.col("content"), F.col("source_file_extension"), F.col("IDDOC").cast("string"),
            F.lit(VOLUME_BASE_PATH), F.lit(MIN_AREA_RATIO), F.lit(MAX_REPEAT), F.lit(ENABLE_TIMING_TEST),
            F.lit(False)))
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
        .withColumn("parse_status",
                    F.when(F.col("parser_error").isNotNull(), F.lit("ERROR"))
                    .when(F.length(F.trim(F.col("document_text"))) == 0, F.lit("EMPTY_TEXT"))
                    .otherwise(F.lit("SUCCESS")))
    )

    df_retry.write.format("delta").mode("overwrite") \
        .option("overwriteSchema", "true").saveAsTable(RETRY_TEMP_TABLE)

    df_retry_ok = spark.table(RETRY_TEMP_TABLE).filter(F.col("parse_status") == "SUCCESS")
    ok_count = df_retry_ok.count()
    logger.info(f"Retry recovered {ok_count}/{len(failed_iddocs)} IDDOCs")

    if ok_count > 0:
        ok_iddocs = [r.IDDOC for r in df_retry_ok.select("IDDOC").collect()]
        df_keep = spark.table(CHECKPOINT_TABLE).filter(
            ~(F.col("parse_status").isin("ERROR", "EMPTY_TEXT") & F.col("IDDOC").isin(ok_iddocs))
        )
        df_keep.unionByName(df_retry_ok, allowMissingColumns=True).write.format("delta").mode("overwrite") \
            .option("overwriteSchema", "true").saveAsTable(CHECKPOINT_TABLE)
        logger.info("Checkpoint updated with retry results.")

    spark.sql(f"DROP TABLE IF EXISTS {RETRY_TEMP_TABLE}")
    return read_checkpoint_deduped()


def validate_ref_mapping(df_chunks_all, df_processed_files):
    """Post-build validation: cross-check chunk REF against the current
    parse_manifest to catch IDDOC→REF mapping errors before they reach the
    vector index.  Logs warnings for every mismatch found and returns a
    summary dict {check_name: count}.  Does NOT block the write — the
    pipeline's incremental staleness detection will self-heal on the next
    run once the manifest is corrected, but an early warning here shortens
    the exposure window.

    Checks performed:
      1. REF mismatch — chunk REF ≠ manifest REF for the same IDDOC
      2. Orphan chunks — IDDOC present in chunks but absent from manifest
      3. Prefix mismatch — [Source: X] embedded in chunk_text ≠ REF column
      4. Trailing whitespace — REF column has leading/trailing spaces
    """
    from pyspark.sql import functions as _F

    summary = {}
    df_manifest = spark.table(PARSE_MANIFEST_TABLE).select(
        _F.col("IDDOC"), _F.col("ref").alias("manifest_ref"),
        _F.col("titre").alias("manifest_titre"),
    )

    # ── Check 1: REF mismatch ────────────────────────────────────────
    for label, df in [("all", df_chunks_all)]:
        if df is None:
            continue
        df_joined = (
            df.select("IDDOC", "REF").distinct()
            .join(df_manifest, on="IDDOC", how="left")
        )
        mismatches = df_joined.filter(
            _F.col("manifest_ref").isNotNull() & (_F.col("REF") != _F.col("manifest_ref"))
        ).collect()
        if mismatches:
            summary[f"ref_mismatch_{label}"] = len(mismatches)
            for row in mismatches:
                logger.warning(
                    f"[REF MISMATCH] chunks_{label} IDDOC={row.IDDOC}: "
                    f"chunk REF='{row.REF}' ≠ manifest REF='{row.manifest_ref}' "
                    f"(titre='{row.manifest_titre}')"
                )

    # ── Check 2: orphan chunks (IDDOC not in manifest) ───────────────
    for label, df in [("all", df_chunks_all)]:
        if df is None:
            continue
        orphans = (
            df.select("IDDOC", "REF").distinct()
            .join(df_manifest, on="IDDOC", how="left")
            .filter(_F.col("manifest_ref").isNull())
            .collect()
        )
        if orphans:
            summary[f"orphan_{label}"] = len(orphans)
            for row in orphans:
                logger.warning(
                    f"[ORPHAN] chunks_{label} IDDOC={row.IDDOC} REF='{row.REF}' "
                    f"not found in manifest — will produce ghost data in the index"
                )

    # ── Check 3: chunk_text prefix ≠ REF column ──────────────────────
    for label, df in [("all", df_chunks_all)]:
        if df is None:
            continue
        df_prefix = (
            df.withColumn(
                "_prefix_ref",
                _F.regexp_extract("chunk_text", r"\[Source:\s*(\S+)\s", 1),
            )
            .filter(
                (_F.col("_prefix_ref") != "")
                & (_F.col("_prefix_ref") != _F.col("REF"))
                & (_F.col("_prefix_ref") != _F.trim(_F.col("REF")))
            )
            .select("IDDOC", "REF", "_prefix_ref")
            .distinct()
            .collect()
        )
        if df_prefix:
            summary[f"prefix_mismatch_{label}"] = len(df_prefix)
            for row in df_prefix:
                logger.warning(
                    f"[PREFIX MISMATCH] chunks_{label} IDDOC={row.IDDOC}: "
                    f"REF column='{row.REF}' but chunk_text says [Source: {row._prefix_ref}]"
                )

    # ── Check 4: trailing whitespace ─────────────────────────────────
    for label, df in [("all", df_chunks_all)]:
        if df is None:
            continue
        ws_rows = (
            df.filter(_F.col("REF") != _F.trim(_F.col("REF")))
            .select("IDDOC", "REF").distinct().collect()
        )
        if ws_rows:
            summary[f"trailing_space_{label}"] = len(ws_rows)
            for row in ws_rows:
                logger.warning(
                    f"[TRAILING SPACE] chunks_{label} IDDOC={row.IDDOC}: "
                    f"REF='{row.REF}' has leading/trailing whitespace"
                )

    if summary:
        logger.warning(f"[VALIDATE] REF mapping issues found: {summary}")
    else:
        logger.info("[VALIDATE] REF mapping checks passed — no mismatches detected.")
    return summary


def build_image_metadata(target_iddocs):
    """Explode checkpoint rows' `images` array into one row per image,
    preserving any already-DONE/SKIPPED description across a rebuild."""
    df_full_pipeline = read_checkpoint_deduped()
    if RUN_MODE == "incremental":
        df_full_pipeline = df_full_pipeline.filter(F.col("IDDOC").isin(list(target_iddocs)))
    df_full_pipeline = with_fresh_business_metadata(df_full_pipeline)

    # Pre-cutoff documents are described too: their image chunks go to chunks_archive (4_describe_images).
    df_image_metadata = (
        df_full_pipeline.filter(F.col("image_count") > 0)
        .select("IDDOC", "ref", "titre", "division", "niveau_plus_1", "niveau_plus_2", "source_file_name",
                F.posexplode("images").alias("img_pos", "img"))
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
            image_utils.image_status_col(
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

    if spark.catalog.tableExists(TARGET_IMAGE_METADATA_TABLE):
        df_prev_described = spark.table(TARGET_IMAGE_METADATA_TABLE).filter(
            F.col("status").isin("DONE", "SKIPPED")
        ).select(
            "IDDOC", "image_id",
            F.col("status").alias("_prev_status"),
            F.col("description").alias("_prev_description"),
            F.col("input_tokens").alias("_prev_input_tokens"),
            F.col("output_tokens").alias("_prev_output_tokens"),
            F.col("described_at").alias("_prev_described_at"),
        ).dropDuplicates(["IDDOC", "image_id"])
        df_image_metadata = (
            df_image_metadata
            .join(F.broadcast(df_prev_described), on=["IDDOC", "image_id"], how="left")
            .withColumn("status", F.coalesce(F.col("_prev_status"), F.col("status")))
            .withColumn("description", F.coalesce(F.col("_prev_description"), F.col("description")))
            .withColumn("input_tokens", F.coalesce(F.col("_prev_input_tokens"), F.col("input_tokens")))
            .withColumn("output_tokens", F.coalesce(F.col("_prev_output_tokens"), F.col("output_tokens")))
            .withColumn("described_at", F.coalesce(F.col("_prev_described_at"), F.col("described_at")))
            .drop("_prev_status", "_prev_description", "_prev_input_tokens", "_prev_output_tokens", "_prev_described_at")
        )

    logger.info(f"Image metadata: {df_image_metadata.count()} rows")
    return df_image_metadata


def _apply_date_and_rag_filters(df_processed_files):
    """Flag filtered_by_date and include_in_rag on df_processed_files.

    doc_date < DOC_DATE_CUTOFF: parsed, chunks routed to chunks_archive instead of
    the RAG tables (include_in_rag=False). NULL doc_date = recent. Manual exclusions come from
    _rag_exclusions (INSERT INTO {CATALOG_SCHEMA}._rag_exclusions VALUES (IDDOC, 'reason')).
    Returns (df_processed_files, df_exclusions_or_none, cutoff_col).
    """
    cutoff = F.lit(DOC_DATE_CUTOFF).cast("date")
    df_processed_files = df_processed_files.withColumn(
        "filtered_by_date",
        F.when(
            (F.col("parse_status") == "SUCCESS")
            & F.col("doc_date").isNotNull()
            & (F.col("doc_date") < cutoff),
            F.lit(True)
        ).otherwise(F.lit(False))
    )
    filtered_by_date_count = df_processed_files.filter(F.col("filtered_by_date") == True).count()
    if filtered_by_date_count:
        logger.info(f"[DATE FILTER] {filtered_by_date_count} documents routed to chunks_archive (predating {DOC_DATE_CUTOFF})")

    try:
        df_exclusions = spark.table(RAG_EXCLUSION_TABLE).select("IDDOC").distinct()
        logger.info(f"[RAG] {df_exclusions.count()} IDDOCs excluded from chunking (table {RAG_EXCLUSION_TABLE})")
    except Exception:
        df_exclusions = None
        logger.info("[RAG] No exclusion table — every SUCCESS will be chunked")

    if df_exclusions is not None:
        df_processed_files = df_processed_files.join(
            df_exclusions.withColumn("_excluded", F.lit(True)), on="IDDOC", how="left"
        ).withColumn(
            "include_in_rag",
            F.when(F.col("_excluded").isNotNull(), F.lit(False))
             .when(F.col("filtered_by_date") == True, F.lit(False))
             .otherwise(F.lit(True))
        ).drop("_excluded")
    else:
        df_processed_files = df_processed_files.withColumn(
            "include_in_rag",
            F.when(F.col("filtered_by_date") == True, F.lit(False)).otherwise(F.lit(True))
        )
    return df_processed_files, df_exclusions, cutoff


def _build_chunks(df_full_pipeline, df_exclusions):
    """Filter to chunkable rows (SUCCESS, not manually excluded — any date),
    clean known artefacts, then explode into chunk rows."""
    if df_exclusions is not None:
        excluded_iddocs = [r.IDDOC for r in df_exclusions.collect()]
        df_for_chunking = df_full_pipeline.filter(
            (F.col("parse_status") == "SUCCESS")
            & ~F.col("IDDOC").isin(excluded_iddocs)
        )
    else:
        df_for_chunking = df_full_pipeline.filter(F.col("parse_status") == "SUCCESS")

    if CLEAN_IMAGE_PLACEHOLDERS:
        df_for_chunking = df_for_chunking.withColumn(
            "document_text", F.regexp_replace(F.col("document_text"), r"\s*<!-- image -->\s*", " ")
        )
        logger.info("[CLEAN] Removed <!-- image --> placeholders from document_text")

    if CLEAN_FORMULA_ARTIFACTS:
        df_for_chunking = df_for_chunking.withColumn(
            "document_text", F.regexp_replace(F.col("document_text"), r"\s*<!-- formula-not-decoded -->\s*", " ")
        )
        logger.info("[CLEAN] Removed '<!-- formula-not-decoded -->' artefacts from document_text")

    df_for_chunking = df_for_chunking.withColumn("url", utils.intraqual_ref_url(F.col("ref")))
    # Normalised body (section line, case, spaces folded): finds the same passage in many documents.
    body_key = F.sha2(F.trim(F.regexp_replace(F.lower(F.regexp_replace(F.col("c.chunk_text"), r"^\[[^\]]*\]\s*", "")),
                                              r"\s+", " ")), 256)

    # chunk_content_type is kept on every row, not just image chunks; other per-document stats live only in processed_files.
    return (
        df_for_chunking
        .withColumn("chunks", utils.build_chunks_udf(F.col("document_text")))
        .withColumn("c", F.explode(F.col("chunks")))
        .withColumn("_chunk_text_full", utils.source_prefixed_text(
            F.col("c.chunk_text"), F.col("ref"), F.col("titre"),
            F.col("division"), F.col("niveau_plus_1"), doc_date_col=F.col("doc_date"),
            include_prefix=EMBED_SOURCE_PREFIX, type_col=F.col("type_document"),
        ))
        .select(
            "IDDOC",
            "source_file_extension",
            F.coalesce(F.col("ref"), F.col("source_file_name")).alias("REF"),
            F.col("division"),
            F.concat_ws("-", F.col("IDDOC").cast("string"),
                        F.lpad((F.col("c.chunk_index") + F.lit(1)).cast("string"), 6, "0")).alias("chunk_id"),
            F.col("c.chunk_index").alias("chunk_index"),
            F.col("_chunk_text_full").alias("chunk_text"),
            F.col("c.chunk_token_count").alias("chunk_token_count"),
            F.col("c.chunk_content_type").alias("chunk_content_type"),
            F.to_json(F.col("c.metadata")).alias("semantic_headers"),
            F.sha2(F.col("_chunk_text_full"), 256).alias("chunk_sha256"),
            F.col("url"),
            F.col("doc_date"),
            # Document metadata, filterable by the search (audit 2026-10, P10).
            F.col("titre"), F.col("type_document"), F.col("indice").cast("string").alias("indice"),
            F.col("langue"),
            body_key.alias("body_sha256"),
        )
    )


def _dedupe_and_limit_chunks(df_chunks):
    """Drop intra-IDDOC duplicate chunks (same text hash), then cap the
    number of chunks kept per oversized spreadsheet."""
    if DEDUPLICATE_CHUNKS:
        w = Window.partitionBy("IDDOC", F.sha2(F.col("chunk_text"), 256)).orderBy("chunk_index")
        before_count = df_chunks.count()
        df_chunks = (
            df_chunks.withColumn("_rn", F.row_number().over(w))
            .filter(F.col("_rn") == 1).drop("_rn")
        )
        after_count = df_chunks.count()
        logger.info(f"[DEDUP] Removed {before_count - after_count} duplicate chunks intra-IDDOC "
                    f"({before_count} -> {after_count})")

    # Same body in BOILERPLATE_MIN_DOCS+ documents (legal mentions, standard approval blocks):
    # marked, not deleted — the search can leave them out (audit 2026-10, P9). Counted with the
    # rows already in the table on an incremental run, so a new document's copy is caught too.
    df_keys = df_chunks.select("IDDOC", "body_sha256")
    if RUN_MODE == "incremental" and spark.catalog.tableExists(TARGET_CHUNK_TABLE) \
            and "body_sha256" in spark.table(TARGET_CHUNK_TABLE).columns:
        df_keys = df_keys.unionByName(
            spark.table(TARGET_CHUNK_TABLE).filter(F.col("body_sha256").isNotNull()).select("IDDOC", "body_sha256"))
    df_common = (df_keys.groupBy("body_sha256").agg(F.countDistinct("IDDOC").alias("_docs"))
                 .filter(F.col("_docs") >= BOILERPLATE_MIN_DOCS).select("body_sha256", F.lit(True).alias("_common")))
    df_chunks = (
        df_chunks.join(F.broadcast(df_common), on="body_sha256", how="left")
        .withColumn("chunk_content_type", F.when(
            F.col("_common") & F.col("chunk_content_type").isin("text", "table", "mixed"), F.lit("boilerplate"))
            .otherwise(F.col("chunk_content_type")))
        .drop("_common")
    )

    if MAX_CHUNKS_SPREADSHEET is not None:
        w_limit = Window.partitionBy("IDDOC").orderBy("chunk_index")
        before_limit = df_chunks.count()
        df_chunks = (
            df_chunks.withColumn("_rn", F.row_number().over(w_limit))
            .filter(
                ~F.lower(F.col("source_file_extension")).isin(*_SPREADSHEET_EXTS)
                | (F.col("_rn") <= MAX_CHUNKS_SPREADSHEET)
            ).drop("_rn")
        )
        after_limit = df_chunks.count()
        truncated = before_limit - after_limit
        if truncated > 0:
            logger.info(f"[LIMIT] {truncated} spreadsheet chunks truncated "
                        f"(max {MAX_CHUNKS_SPREADSHEET}/file, {before_limit} -> {after_limit})")

    return df_chunks.drop("source_file_extension")  # chunk_index kept — notebook 02 needs it


def build_processed_files_and_chunks(target_iddocs, df_image_metadata, revised_iddocs=frozenset()):
    """Build processed_files (one row per parsed document) and the three
    chunk DataFrames (all divisions / AS / IS) from the checkpoint."""
    df_full_pipeline = read_checkpoint_deduped()
    if RUN_MODE == "incremental":
        df_full_pipeline = df_full_pipeline.filter(F.col("IDDOC").isin(list(target_iddocs)))
    df_full_pipeline = with_fresh_business_metadata(df_full_pipeline)
    # Real language (REF suffix, else the text): gd_doc has none, selection.py writes fr-FR for all.
    df_full_pipeline = df_full_pipeline.withColumn(
        "langue", utils.language_udf(F.col("document_text"), F.col("ref")))

    # NEW = first time this IDDOC reaches a terminal status; REVISED = a _STALENESS_COLS change since its last SUCCESS.
    if revised_iddocs:
        df_revised = spark.createDataFrame(
            [(i,) for i in revised_iddocs], "IDDOC long"
        ).withColumn("_is_revised", F.lit(True))
        df_full_pipeline = df_full_pipeline.join(F.broadcast(df_revised), on="IDDOC", how="left")
    else:
        df_full_pipeline = df_full_pipeline.withColumn("_is_revised", F.lit(False))

    # images_needing_description excludes decorative images (unlike image_count) so an audit can compare expected vs actual DONE+SKIPPED counts.
    df_images_needed = (
        df_image_metadata.filter(F.col("status") == "PENDING")
        .groupBy("IDDOC").agg(F.count("*").alias("images_needing_description"))
    )

    df_processed_files = (
        df_full_pipeline
        .join(F.broadcast(df_images_needed), on="IDDOC", how="left")
        .withColumns({
            "document_char_count": F.length(F.col("document_text")),
            "document_token_count": F.when(
                F.col("parse_status") == "SUCCESS",
                F.greatest(F.lit(1), F.floor(F.length(F.col("document_text")) / F.lit(CHARS_PER_TOKEN)).cast("int"))
            ).otherwise(F.lit(0)),
            "images_needing_description": F.coalesce(F.col("images_needing_description"), F.lit(0)),
            "change_type": F.when(F.coalesce(F.col("_is_revised"), F.lit(False)), F.lit("REVISED")).otherwise(F.lit("NEW")),
        })
        .withColumnRenamed("parser_error", "error_trace")
        .withColumn("job_run_id", F.lit(JOB_RUN_ID))
        .select(
            "IDDOC", "document_sha256", "source_path", "source_file_name", "source_file_extension",
            "source_folder_path", "source_file_size_bytes", "source_modification_time",
            "ref", "titre", "type_document", "categorie", "langue", "auteur", "document_prefixes",
            "division", "niveau_plus_1", "niveau_plus_2", "niveau_plus_3",
            "niveau_plus_4", "niveau_plus_5", "niveau_plus_6",
            "doc_date",   # publication date (gd_doc.dtdiff)
            "indice",     # revision key — detects a republished doc
            "ingestion_run_id", "ingestion_timestamp", "job_run_id",
            "parser_strategy", "parse_status", "error_trace", "parse_time_seconds",
            "document_char_count", "document_token_count", "image_count",
            "images_needing_description", "change_type",
        )
        .withColumn("chunking_strategy", F.when(
            F.col("parse_status") == "SUCCESS", F.lit("table_aware_hybrid")
        ).otherwise(F.lit(None)))
    )

    df_processed_files, df_exclusions, cutoff = _apply_date_and_rag_filters(df_processed_files)

    df_chunks = _build_chunks(df_full_pipeline, df_exclusions)

    # True only for a spreadsheet that really has more passages than the cap (was: every spreadsheet).
    df_over_cap = (
        df_chunks.filter(F.lower(F.col("source_file_extension")).isin(*_SPREADSHEET_EXTS))
        .groupBy("IDDOC").count()
        .filter(F.lit(MAX_CHUNKS_SPREADSHEET is not None) & (F.col("count") > F.lit(MAX_CHUNKS_SPREADSHEET or 0)))
        .select("IDDOC", F.lit(True).alias("_over_cap"))
    )
    df_processed_files = (
        df_processed_files.join(df_over_cap, on="IDDOC", how="left")
        .withColumn("chunks_truncated", F.coalesce(F.col("_over_cap"), F.lit(False)))
        .drop("_over_cap")
    )

    df_chunks = _dedupe_and_limit_chunks(df_chunks)

    is_recent = F.col("doc_date").isNull() | (F.col("doc_date") >= cutoff)
    df_chunks_archive = df_chunks.filter(~is_recent)
    df_chunks_all = df_chunks.filter(is_recent)

    logger.info("DataFrames ready (lazy). Will materialise during write.")
    return df_processed_files, df_chunks_all, df_chunks_archive


def _merge_image_metadata(df_image_metadata):
    """MERGE image_metadata instead of delete-then-append: status/description/
    tokens only take the fresh value when the existing row isn't already
    DONE/SKIPPED."""
    if not spark.catalog.tableExists(TARGET_IMAGE_METADATA_TABLE):
        # First-ever write on a fresh catalog (e.g. a brand-new environment with
        # no promoted checkpoint) — DeltaTable.forName() requires the table to
        # already exist, so there's nothing to MERGE into yet.
        df_image_metadata.write.format("delta").mode("append") \
            .option("mergeSchema", "true").saveAsTable(TARGET_IMAGE_METADATA_TABLE)
        logger.info(f"Created {TARGET_IMAGE_METADATA_TABLE} (first write)")
        return
    keep_prev = "CASE WHEN tgt.status IN ('DONE', 'SKIPPED') THEN tgt.{col} ELSE src.{col} END"
    (
        DeltaTable.forName(spark, TARGET_IMAGE_METADATA_TABLE).alias("tgt")
        .merge(df_image_metadata.alias("src"), "tgt.IDDOC = src.IDDOC AND tgt.image_id = src.image_id")
        .whenMatchedUpdate(set={
            "source_file_name": "src.source_file_name", "ref": "src.ref", "titre": "src.titre",
            "division": "src.division", "niveau_plus_1": "src.niveau_plus_1", "niveau_plus_2": "src.niveau_plus_2",
            "page_no": "src.page_no", "label": "src.label", "area_ratio": "src.area_ratio",
            "captions": "src.captions", "context_text": "src.context_text",
            "volume_path": "src.volume_path", "image_width": "src.image_width", "image_height": "src.image_height",
            "status": keep_prev.format(col="status"),
            "description": keep_prev.format(col="description"),
            "input_tokens": keep_prev.format(col="input_tokens"),
            "output_tokens": keep_prev.format(col="output_tokens"),
            "described_at": keep_prev.format(col="described_at"),
            "ingestion_run_id": "src.ingestion_run_id", "ingestion_timestamp": "src.ingestion_timestamp",
        })
        .whenNotMatchedInsertAll()
        .execute()
    )
    logger.info(f"Merged into {TARGET_IMAGE_METADATA_TABLE}")


def _write_run_health_summary(df_processed_files, job_run_id, run_mode):
    """One row per pipeline run in TARGET_HEALTH_TABLE — REF/error counts for the
    monitoring dashboard. Scoped to this run's df_processed_files: a FULL run's
    row covers the whole corpus, not just net-new docs vs the prior run."""
    status = F.col("parse_status")
    is_success = status == "SUCCESS"
    is_skipped = status.startswith("SKIP")
    is_error = ~is_success & ~is_skipped

    counts = df_processed_files.agg(
        F.count("*").alias("total_docs"),
        F.sum(is_success.cast("int")).alias("docs_success"),
        F.sum(is_skipped.cast("int")).alias("docs_skipped"),
        F.sum(is_error.cast("int")).alias("docs_error"),
        F.sort_array(F.collect_set(F.when(is_success, F.col("ref")))).alias("refs_success"),
        F.sum("image_count").alias("total_image_count"),
        F.avg("parse_time_seconds").alias("avg_parse_time_seconds"),
        F.sum("parse_time_seconds").alias("total_parse_time_seconds"),
    )
    by_status = (
        df_processed_files.groupBy("parse_status").count()
        .agg(F.map_from_entries(F.collect_list(F.struct("parse_status", "count"))).alias("docs_by_status"))
    )
    errors = df_processed_files.filter(is_error).agg(
        F.collect_list(F.struct("IDDOC", "ref", "parse_status", "error_trace")).alias("errors")
    )

    (counts.crossJoin(by_status).crossJoin(errors)
        .withColumn("job_run_id", F.lit(job_run_id))
        .withColumn("run_mode", F.lit(run_mode))
        .withColumn("run_date", F.current_date())
        .withColumn("written_at", F.current_timestamp())
        .write.format("delta").mode("append").option("mergeSchema", "true")
        .saveAsTable(TARGET_HEALTH_TABLE)
    )
    logger.info(f"Wrote run health summary to {TARGET_HEALTH_TABLE}")


def log_document_changes(df_business_meta, target_iddocs, revised_iddocs, job_run_id):
    """Append one row per NEW/REVISED document to TARGET_CHANGE_LOG_TABLE -- must run before processed_files gets rewritten below, since it reads the pre-run row for old ref/indice/doc_date."""
    if not target_iddocs:
        return

    df_target = df_business_meta.filter(F.col("IDDOC").isin(list(target_iddocs)))

    try:
        df_existing = (
            spark.table(TARGET_PROCESSED_FILES_TABLE)
            .select(
                F.col("IDDOC"),
                F.col("ref").alias("old_ref"),
                F.col("indice").alias("old_indice"),
                F.col("doc_date").alias("old_doc_date"),
            )
            .dropDuplicates(["IDDOC"])
        )
    except Exception:
        df_existing = spark.createDataFrame(
            [], "IDDOC long, old_ref string, old_indice string, old_doc_date date"
        )

    df_log = (
        df_target.join(df_existing, on="IDDOC", how="left")
        .withColumn(
            "event_type",
            F.when(F.col("old_ref").isNull(), F.lit("NEW"))
             .when(F.col("IDDOC").isin(list(revised_iddocs)), F.lit("REVISED"))
             .otherwise(F.lit(None)),
        )
        .filter(F.col("event_type").isNotNull())
        .withColumns({"job_run_id": F.lit(job_run_id), "logged_at": F.current_timestamp()})
        .withColumnRenamed("indice", "new_indice")
        .withColumnRenamed("doc_date", "new_doc_date")
        .select("IDDOC", "ref", "event_type", "old_ref", "old_indice", "new_indice",
                "old_doc_date", "new_doc_date", "job_run_id", "logged_at")
    )

    n = df_log.count()
    if n == 0:
        logger.info("[CHANGE LOG] No new/revised documents to log this run.")
        return

    df_log.write.format("delta").mode("append").option("mergeSchema", "true").saveAsTable(TARGET_CHANGE_LOG_TABLE)
    logger.info(f"[CHANGE LOG] Logged {n} document change(s) (NEW/REVISED) to {TARGET_CHANGE_LOG_TABLE}")


def write_outputs(df_processed_files, df_chunks_all, df_chunks_archive,
                  df_image_metadata, revised_iddocs=frozenset()):
    """Write processed_files/chunks/image_metadata — overwrite on a FULL
    run, append (+ MERGE for image_metadata) on an incremental run."""
    spark.conf.set("spark.databricks.delta.optimizeWrite.enabled", "true")
    spark.conf.set("spark.databricks.delta.autoCompact.enabled", "true")

    if RUN_MODE == "full":
        df_processed_files.write.format("delta").mode("overwrite") \
            .option("overwriteSchema", "true").saveAsTable(TARGET_PROCESSED_FILES_TABLE)
        logger.info(f"Wrote {TARGET_PROCESSED_FILES_TABLE}")

        df_chunks_all.write.format("delta").mode("overwrite") \
            .option("overwriteSchema", "true").saveAsTable(TARGET_CHUNK_TABLE)
        logger.info(f"Wrote {TARGET_CHUNK_TABLE} (ALL)")

        df_chunks_archive.write.format("delta").mode("overwrite") \
            .option("overwriteSchema", "true").saveAsTable(TARGET_CHUNK_TABLE_ARCHIVE)
        logger.info(f"Wrote {TARGET_CHUNK_TABLE_ARCHIVE} (pre-{DOC_DATE_CUTOFF})")

        df_image_metadata.write.format("delta").mode("overwrite") \
            .option("overwriteSchema", "true").saveAsTable(TARGET_IMAGE_METADATA_TABLE)
        logger.info(f"Wrote {TARGET_IMAGE_METADATA_TABLE}")

        logger.info("[FULL] All tables overwritten.")

    elif RUN_MODE == "incremental":
        new_iddocs = [r.IDDOC for r in df_processed_files.select("IDDOC").distinct().collect()]
        if new_iddocs:
            iddoc_list = ",".join(str(i) for i in new_iddocs)
            for tbl in [TARGET_CHUNK_TABLE, TARGET_CHUNK_TABLE_ARCHIVE, TARGET_PROCESSED_FILES_TABLE]:
                if spark.catalog.tableExists(tbl):
                    spark.sql(f"DELETE FROM {tbl} WHERE IDDOC IN ({iddoc_list})")
            logger.info(f"Cleaned old rows for {len(new_iddocs)} re-processed IDDOCs (chunks + processed_files)")

        df_processed_files.write.format("delta").mode("append") \
            .option("mergeSchema", "true").saveAsTable(TARGET_PROCESSED_FILES_TABLE)
        logger.info(f"Appended to {TARGET_PROCESSED_FILES_TABLE}")

        df_chunks_all.write.format("delta").mode("append") \
            .option("mergeSchema", "true").saveAsTable(TARGET_CHUNK_TABLE)
        logger.info(f"Appended to {TARGET_CHUNK_TABLE} (ALL)")

        df_chunks_archive.write.format("delta").mode("append") \
            .option("mergeSchema", "true").saveAsTable(TARGET_CHUNK_TABLE_ARCHIVE)
        logger.info(f"Appended to {TARGET_CHUNK_TABLE_ARCHIVE} (pre-{DOC_DATE_CUTOFF})")

        # Revised IDDOCs have new content, so _merge_image_metadata's keep_prev logic must not preserve their old image rows.
        if revised_iddocs:
            revised_list = ",".join(str(i) for i in revised_iddocs)
            if spark.catalog.tableExists(TARGET_IMAGE_METADATA_TABLE):
                spark.sql(f"DELETE FROM {TARGET_IMAGE_METADATA_TABLE} WHERE IDDOC IN ({revised_list})")
                logger.info(f"Cleared stale image_metadata for {len(revised_iddocs)} revised IDDOC(s) before merge.")

        _merge_image_metadata(df_image_metadata)
        logger.info("[INCREMENTAL] New rows appended (stale failures cleaned).")

    # CDF required for Vector Search index sync.
    try:
        spark.sql(f"ALTER TABLE {TARGET_CHUNK_TABLE} SET TBLPROPERTIES (delta.enableChangeDataFeed = true)")
        logger.info(f"CDF enabled on {TARGET_CHUNK_TABLE}")
    except Exception as e:
        logger.warning(f"CDF not set on {TARGET_CHUNK_TABLE}: {e}")

    _write_run_health_summary(df_processed_files, JOB_RUN_ID, RUN_MODE)

    logger.info("[NEXT] Run 4_Describe_Images_LLM to describe PENDING images.")


def build_archive_notices(df_business_meta):
    """One metadata-only chunk per pre-cutoff document: REF, title, revision,
    type, date and a "content not indexed" statement — never the content itself.

    A pre-cutoff document whose REF also exists on a recent document is skipped:
    the chatbot already knows that REF through real chunks.
    """
    cutoff = F.lit(DOC_DATE_CUTOFF).cast("date")
    is_archive = F.col("doc_date").isNotNull() & (F.col("doc_date") < cutoff)
    df_recent_refs = (
        df_business_meta.filter(~is_archive & F.col("ref").isNotNull())
        .select("ref").distinct()
    )

    def _line(label, col):
        return F.concat(F.lit(f"{label} : "), F.coalesce(col.cast("string"), F.lit("inconnu")))

    body = F.concat_ws(
        "\n",
        F.lit(ARCHIVE_NOTICE_MARKER),
        _line("Référence", F.col("ref")),
        _line("Titre", F.col("titre")),
        _line("Indice", F.col("indice")),
        _line("Type de document", F.col("type_document")),
        _line("Division", F.col("division")),
        _line("Catégorie", F.col("niveau_plus_1")),
        _line("Date de diffusion", F.date_format(F.col("doc_date"), "yyyy-MM-dd")),
        F.lit(f"Ce document a été diffusé avant le {DOC_DATE_CUTOFF}. Seule cette fiche d'identification "
              "est disponible dans Qualibot : son contenu n'est pas indexé et ne peut pas servir à "
              "répondre sur le fond. Le document reste consultable dans Intraqual."),
        F.lit(f"This document was published before {DOC_DATE_CUTOFF}. Only this identification record "
              "is available in Qualibot: its content is not indexed and cannot be used to answer. "
              "The document can still be consulted in Intraqual."),
    )
    return (
        df_business_meta.filter(is_archive & F.col("ref").isNotNull())
        .dropDuplicates(["IDDOC"])
        .join(df_recent_refs, on="ref", how="left_anti")
        .withColumn("chunk_text", utils.source_prefixed_text(
            body, F.col("ref"), F.col("titre"), F.col("division"), F.col("niveau_plus_1"),
            doc_date_col=F.col("doc_date"), include_prefix=EMBED_SOURCE_PREFIX,
        ))
        .select(
            "IDDOC",
            F.col("ref").alias("REF"),
            "division",
            # Real chunks start at -000001 (chunk_index 0): -000000 can never collide.
            F.concat(F.col("IDDOC").cast("string"), F.lit("-000000")).alias("chunk_id"),
            F.lit(-1).alias("chunk_index"),
            "chunk_text",
            utils.token_count_udf(F.col("chunk_text")).alias("chunk_token_count"),
            F.lit(ARCHIVE_NOTICE_CONTENT_TYPE).alias("chunk_content_type"),
            F.lit("{}").alias("semantic_headers"),
            F.sha2(F.col("chunk_text"), 256).alias("chunk_sha256"),
            utils.intraqual_ref_url(F.col("ref")).alias("url"),
            "doc_date",
        )
    )


def write_archive_notices(df_notices):
    """Rewrite TARGET_ARCHIVE_NOTICE_TABLE, then reconcile the notices held by the
    chat's `chunks` table: MERGEd in when ARCHIVE_NOTICES_IN_RAG is on, removed otherwise.
    MERGE (not delete + append) so Change Data Feed only carries real changes."""
    df_notices.write.format("delta").mode("overwrite") \
        .option("overwriteSchema", "true").saveAsTable(TARGET_ARCHIVE_NOTICE_TABLE)
    df_notices = spark.table(TARGET_ARCHIVE_NOTICE_TABLE)
    logger.info(f"[NOTICES] Wrote {df_notices.count()} archive notice(s) to {TARGET_ARCHIVE_NOTICE_TABLE} "
                f"(in RAG tables: {ARCHIVE_NOTICES_IN_RAG})")

    is_notice = f"chunk_content_type = '{ARCHIVE_NOTICE_CONTENT_TYPE}'"
    for tbl in [TARGET_CHUNK_TABLE]:
        if not spark.catalog.tableExists(tbl):
            continue
        if not ARCHIVE_NOTICES_IN_RAG:
            if spark.table(tbl).filter(is_notice).limit(1).count():
                spark.sql(f"DELETE FROM {tbl} WHERE {is_notice}")
                logger.info(f"[NOTICES] Removed archive notices from {tbl}")
            continue
        df_src = df_notices
        # Align on the target's own columns/types (it may hold columns notices don't have).
        df_src = df_src.select(*[
            (F.col(f.name) if f.name in df_notices.columns else F.lit(None)).cast(f.dataType).alias(f.name)
            for f in spark.table(tbl).schema
        ])
        (
            DeltaTable.forName(spark, tbl).alias("t")
            .merge(df_src.alias("s"), "t.chunk_id = s.chunk_id")
            .whenMatchedUpdateAll(condition="NOT (t.chunk_sha256 <=> s.chunk_sha256)")
            .whenNotMatchedInsertAll()
            .whenNotMatchedBySourceDelete(condition=f"t.{is_notice}")
            .execute()
        )
        logger.info(f"[NOTICES] Merged archive notices into {tbl}")


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
    manifest_cols = [c for c in _BUSINESS_METADATA_COLS if c in df_business_meta.columns]
    (
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
        .write.format("delta").mode("append").option("mergeSchema", "true")
        .saveAsTable(TARGET_PROCESSED_FILES_TABLE)
    )
    logger.info(f"[EMPTY FOLDER] Wrote {len(empty_folder_iddocs)} row(s) to {TARGET_PROCESSED_FILES_TABLE}")
    return target_iddocs - empty_folder_iddocs


def flag_ghost_documents():
    """Flag SUCCESS documents that ended up with zero chunks as MISSING_CHUNKS.

    Runs right after the write instead of waiting for the daily SQL alert;
    the next incremental run re-parses the flagged documents.
    """
    df_ghosts = (
        spark.table(TARGET_PROCESSED_FILES_TABLE)
        .filter(
            (F.col("parse_status") == "SUCCESS")
            & (F.col("include_in_rag") == True)
            & (F.col("filtered_by_date") == False)
        )
        .join(spark.table(TARGET_CHUNK_TABLE).select("IDDOC").distinct(), on="IDDOC", how="left_anti")
    )
    ghost_iddocs = [r.IDDOC for r in df_ghosts.select("IDDOC").collect()]
    if not ghost_iddocs:
        logger.info("[GHOST GUARD] All SUCCESS docs have chunks - OK.")
        return

    spark.sql(f"""
        UPDATE {TARGET_PROCESSED_FILES_TABLE}
        SET parse_status = 'MISSING_CHUNKS'
        WHERE IDDOC IN ({",".join(str(i) for i in ghost_iddocs)})
    """)
    logger.warning(
        f"[GHOST GUARD] {len(ghost_iddocs)} SUCCESS doc(s) with zero chunks "
        f"auto-flagged as MISSING_CHUNKS for next run: {ghost_iddocs}"
    )
