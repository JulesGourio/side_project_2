"""Phases of the `3_parse` task that scope, select, parse and retry (see `create_proj_chunks.py`).

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
from typing import Callable

from databricks.sdk.runtime import dbutils, display, spark
from pyspark.sql import DataFrame, Window
from pyspark.sql import functions as f
from pyspark.sql import types as T

import image_utils
import selection
from retention import snapshots_to_drop
from format_parsers import strip_ooxml_bloat
from tables import table_exists, write_table
from leap_utils.common.logger import get_logger
from config import (
    PIPELINE_WRITE_ENV,
    BUSINESS_METADATA_COLS,
    CHECKPOINT_BATCH_SIZE,
    CHECKPOINT_SNAPSHOT_KEEP_DAYS,
    CHECKPOINT_TABLE,
    EMPTY_SMALL_FILE_SIZE_BYTES,
    ENABLE_TIMING_TEST,
    ENRICHMENT_COLS,
    GPU_MIN_PDFS,
    GPU_QUEUE_TABLE,
    MAX_PARSE_FILES,
    MAX_REPEAT,
    MIN_AREA_RATIO,
    PARSE_FILTER,
    RETRY_TEMP_TABLE,
    RUN_MODE,
    RUN_SCOPE_TABLE,
    TARGET_PROCESSED_FILES_TABLE,
    VOLUME_BASE_PATH,
    VOLUME_ROOT_PATH,
    IMAGES_PENDING,
    TERMINAL_STATUSES,
)

logger = get_logger(__name__)

INGESTION_RUN_ID = str(uuid.uuid4())
JOB_RUN_ID = None

# Any other status is retried automatically on the next incremental run.
_TERMINAL_STATUSES = list(TERMINAL_STATUSES)  # a list: pyspark isin() does not take a tuple
# Checkpoint statuses that the same bytes would give again: the file is not re-parsed. ERROR / EMPTY_TEXT are retried;
# so is SKIP_TOO_LARGE, which costs no parse (the size gate re-skips it) and must follow a raised ceiling.
_REUSABLE_STATUSES = ["SUCCESS", "SKIPPED_EMPTY_SMALL_FILE"]
# A change in any of these on the same IDDOC means a revised document (ref included: Intraqual can rename in place).
_STALENESS_COLS = ["indice", "doc_date", "ref"]

# Size ceilings per format, above which a file is not parsed (Docling rasterizes pages: out-of-memory risk).
_NON_DOCLING_EXTS = ["doc", "ppt", "xls", "xlsx", "xlsm", "xlsb", "rtf", "odt", "ods"]
_OOXML_STRIP_EXTS = ["docx", "docm", "pptx", "pptm"]
_MAX_SIZE_DOCLING = (
    15 * 1024 * 1024
)  # docx/docm/pptx/pptm after stripping, and any other Docling format
_MAX_SIZE_PDF = 35 * 1024 * 1024  # large scanned PDFs must reach the LLM-OCR fallback
_MAX_SIZE_OTHER = 100 * 1024 * 1024  # antiword/openpyxl: no page-rasterization OOM risk


def set_job_run_id(job_run_id) -> None:
    """Correlate this run's rows with the parent job run (`2_manifest` writes the same id).

    :param job_run_id: Id of the parent Databricks job run; None or empty when unknown.
    """
    global JOB_RUN_ID
    JOB_RUN_ID = job_run_id or None


def resolve_parse_scope(df_business_meta) -> tuple[set, set]:
    """Resolve which IDDOCs need parsing this run.

    Full mode: everything in the manifest. Incremental mode: manifest minus
    already-terminal IDDOCs, plus any of those flagged as revised since their
    last parse. Returns (target_iddocs, revised_iddocs).

    :param df_business_meta: Business metadata of the manifest, with ``IDDOC`` and ``parse_content``.
    """
    # parse_content=False: pre-cutoff document beyond ARCHIVE_MAX_DOCS (notice chunk only).
    df_parseable = (
        df_business_meta.filter(f.col("parse_content"))
        if "parse_content" in df_business_meta.columns
        else df_business_meta
    )
    manifest_iddocs = {r.IDDOC for r in df_parseable.select("IDDOC").distinct().collect()}
    already_done_iddocs = set()
    revised_iddocs = set()

    if RUN_MODE == "incremental":
        try:
            df_pf = spark.table(TARGET_PROCESSED_FILES_TABLE)
            already_done_iddocs = {
                r.IDDOC
                for r in df_pf.filter(f.col("parse_status").isin(_TERMINAL_STATUSES))
                .select("IDDOC")
                .distinct()
                .collect()
            }
            logger.info(
                f"[INCREMENTAL] {len(already_done_iddocs)} IDDOCs already terminal in {TARGET_PROCESSED_FILES_TABLE}"
            )

            missing_cols = [c for c in _STALENESS_COLS if c not in df_pf.columns]
            if missing_cols:
                logger.info(
                    f"[REVISION] Columns missing from parsed_documents ({', '.join(missing_cols)}) — detection skipped this run."
                )
            else:
                df_pf_revision = (
                    df_pf.filter(f.col("parse_status") == "SUCCESS")
                    .select("IDDOC", *[f.col(c).alias(f"_pf_{c}") for c in _STALENESS_COLS])
                    .dropDuplicates(["IDDOC"])
                )
                # A NULL _pf_<col> has no value to compare with: revision cannot be told, so the IDDOC is not flagged.
                is_revised = f.lit(False)
                for c in _STALENESS_COLS:
                    is_revised = is_revised | (
                        f.col(f"_pf_{c}").isNotNull() & ~f.col(c).eqNullSafe(f.col(f"_pf_{c}"))
                    )
                revised_iddocs = {
                    r.IDDOC
                    for r in df_business_meta.select("IDDOC", *_STALENESS_COLS)
                    .join(df_pf_revision, on="IDDOC", how="inner")
                    .filter(is_revised)
                    .select("IDDOC")
                    .distinct()
                    .collect()
                }
                logger.info(
                    f"[REVISION] {len(revised_iddocs)} IDDOCs revised since last parse ({', '.join(_STALENESS_COLS)} changed) — re-parsing."
                )
        except Exception as exc:
            logger.warning(
                f"[INCREMENTAL] Table {TARGET_PROCESSED_FILES_TABLE} unreadable ({exc}) — no IDDOC excluded."
            )
    elif RUN_MODE == "full":
        logger.info("[FULL] Parsing all documents. Tables will be overwritten.")
    elif RUN_MODE == "rechunk":
        logger.info(
            "[RECHUNK] No parsing: chunks rebuilt from the checkpoint for every document. Tables will be overwritten."
        )

    pending_iddocs = manifest_iddocs - (already_done_iddocs - revised_iddocs)
    if isinstance(PARSE_FILTER, (list, tuple, set)):
        target_iddocs = pending_iddocs & set(PARSE_FILTER)
    else:
        target_iddocs = pending_iddocs

    logger.info(
        f"[SCOPE] manifest={len(manifest_iddocs)} | already terminal={len(already_done_iddocs)} "
        f"| revised={len(revised_iddocs)} | to scan={len(target_iddocs)}"
    )
    return target_iddocs, revised_iddocs


def select_files_for_target_iddocs(
    target_iddocs, df_business_meta
) -> tuple[DataFrame, DataFrame, DataFrame]:
    """Scan only the target IDDOCs' folders, rank every candidate file per
    IDDOC, and pick the winner.

    Returns (df_files, df_matched_full, df_content) — df_matched_full/df_content
    are kept for the retry phase (rank-2 candidates), df_files feeds parsing.

    :param target_iddocs: IDDOCs whose folders are scanned.
    :param df_business_meta: Business metadata of the manifest, with the enrichment columns.
    """
    if target_iddocs:
        root_dbfs = VOLUME_ROOT_PATH.replace("/Volumes/", "dbfs:/Volumes/")
        root_items = dbutils.fs.ls(root_dbfs)
        matching_paths = [
            f"{VOLUME_ROOT_PATH}/{item.name.rstrip('/')}"
            for item in root_items
            if selection.extract_iddoc_from_name(item.name) in target_iddocs
        ]
        logger.info(
            f"[SCAN] {len(matching_paths)} target folder(s) out of {len(root_items)} total in the volume"
        )
    else:
        matching_paths = []
        logger.info(
            "[SCAN] Nothing to scan — every manifest IDDOC is already processed (or outside PARSE_FILTER)."
        )

    df_meta_raw, df_content = selection.scan_volume_paths(spark, matching_paths)
    df_meta_raw.cache()

    df_matched_full = selection.rank_candidates(df_meta_raw).cache()

    df_selected = selection.select_best_files(df_matched_full, df_business_meta)
    df_files = selection.attach_content(df_selected, df_content, INGESTION_RUN_ID).cache()

    return df_files, df_matched_full, df_content


def _with_parse_result(df, xml_only_col) -> DataFrame:
    """Run the parsing UDF on `content` and unpack its result into the checkpoint columns (content dropped).

    :param df: Files with ``content`` and ``source_file_extension`` columns.
    :param xml_only_col: Column that routes a file to the XML fallback only, or a literal False.
    """
    return (
        df.withColumn(
            "result",
            image_utils.parse_and_extract_images_udf(
                f.col("content"),
                f.col("source_file_extension"),
                f.col("IDDOC").try_cast("string"),
                f.lit(VOLUME_BASE_PATH),
                f.lit(MIN_AREA_RATIO),
                f.lit(MAX_REPEAT),
                f.lit(ENABLE_TIMING_TEST),
                xml_only_col,
            ),
        )
        .withColumns(
            {
                "document_text": f.col("result.text"),
                "parser_error": f.col("result.parser_error"),
                "parser_strategy": f.col("result.parser_strategy"),
                "parse_time_seconds": f.col("result.parse_time_seconds"),
                "images": f.col("result.images"),
                "timings": f.col("result.timings"),
            }
        )
        .withColumn("image_count", f.size(f.col("images")))
        .drop("result", "content")
    )


def make_parse_fn(cluster_parallelism) -> Callable[[DataFrame, int], DataFrame]:
    """Build the per-batch parse function, closing over cluster_parallelism.

    One partition per file, capped at cluster parallelism: the GPU slots (one task per GPU) are fed from a
    fine-grained queue instead of waiting on a few coarse partitions that each bundle many files. Python workers
    are reused across tasks, so Docling is still imported once per worker.

    :param cluster_parallelism: Number of tasks the cluster runs in parallel.
    :return: parse function taking a batch of files and its size.
    """

    def parse(df, file_count) -> DataFrame:
        """Parse one batch of files and set the parse status of each.

        :param df: Batch of files, with ``content`` and the file metadata.
        :param file_count: Number of files in the batch.
        :return: batch with the parse columns and ``parse_status``.
        """
        partitions = max(1, min(file_count, cluster_parallelism))
        has_xml_fallback_col = "_xml_fallback_only" in df.columns
        xml_fallback_col = f.col("_xml_fallback_only") if has_xml_fallback_col else f.lit(False)
        df_parsed = _with_parse_result(df.repartition(partitions), xml_fallback_col).drop(
            "_xml_fallback_only"
        )
        return df_parsed.withColumn(
            "parse_status",
            f.when(
                (f.length(f.trim(f.col("document_text"))) == 0)
                & (f.col("source_file_size_bytes") <= EMPTY_SMALL_FILE_SIZE_BYTES),
                f.lit("SKIPPED_EMPTY_SMALL_FILE"),
            )
            .when(f.col("parser_error").isNotNull(), f.lit("ERROR"))
            .when(f.length(f.trim(f.col("document_text"))) == 0, f.lit("EMPTY_TEXT"))
            .otherwise(f.lit("SUCCESS")),
        )

    return parse


def drop_old_checkpoint_snapshots(
    now: datetime, keep_days: int = CHECKPOINT_SNAPSHOT_KEEP_DAYS
) -> None:
    """Drop the checkpoint snapshots older than ``keep_days``; the most recent one is always kept.

    :param now: current time
    :param keep_days: age in days from which a snapshot is dropped
    """
    schema = f"{PIPELINE_WRITE_ENV}_proj.qualibot"
    try:
        names = [
            r["tableName"]
            for r in spark.sql(f"SHOW TABLES IN {schema} LIKE '_pipeline_checkpoint_*'").collect()
        ]
    except Exception as exc:
        logger.warning(f"[SNAPSHOT] Could not list the checkpoint snapshots: {exc}")
        return
    for name in snapshots_to_drop(names, now, keep_days):
        try:
            spark.sql(f"DROP TABLE IF EXISTS {schema}.{name}")
            logger.info(f"[SNAPSHOT] Dropped {schema}.{name}")
        except Exception as exc:
            logger.warning(f"[SNAPSHOT] Could not drop {schema}.{name}: {exc}")


def snapshot_or_log_checkpoint(num_files) -> None:
    """Full run: shallow-clone the live checkpoint before it's overwritten.
    Incremental run: just log the current Delta version for manual
    time-travel restore.

    :param num_files: Number of files this run parses.
    """
    if RUN_MODE == "full" and num_files > 0:
        snap_ts = datetime.now().strftime("%Y%m%d_%H%M")
        snap_table = f"{PIPELINE_WRITE_ENV}_proj.qualibot._pipeline_checkpoint_{snap_ts}"
        try:
            spark.table(CHECKPOINT_TABLE)
            spark.sql(f"CREATE TABLE IF NOT EXISTS {snap_table} SHALLOW CLONE {CHECKPOINT_TABLE}")
            logger.info(f"[SNAPSHOT] {CHECKPOINT_TABLE} -> {snap_table}")
        except Exception:
            logger.info("[SNAPSHOT] No existing checkpoint to save.")
        drop_old_checkpoint_snapshots(datetime.now())
    elif RUN_MODE == "incremental":
        try:
            hist = spark.sql(f"DESCRIBE HISTORY {CHECKPOINT_TABLE} LIMIT 1")
            version_rows = hist.select("version", "timestamp").collect()
            if version_rows:
                v = version_rows[0]
                logger.info(
                    f"[CHECKPOINT] Current version = {v['version']} ({v['timestamp']}) "
                    f"| restore: versionAsOf={v['version']} on {CHECKPOINT_TABLE}"
                )
        except Exception:
            logger.info("[CHECKPOINT] No existing checkpoint yet.")


def read_checkpoint_deduped(columns=None) -> DataFrame:
    """Read parsing_checkpoint with exactly one row per IDDOC — dedup by
    IDDOC (not source_path), preferring SUCCESS then the most recent attempt.

    `columns`: only these (plus the dedup keys), so a caller that needs a few columns does not sort every document text.

    :param columns: Columns to read besides the dedup keys, or None for all of them.
    """
    w = Window.partitionBy("IDDOC").orderBy(
        f.when(f.col("parse_status") == "SUCCESS", 0).otherwise(1),
        f.desc("ingestion_timestamp"),
    )
    df = spark.table(CHECKPOINT_TABLE)
    if columns:
        keys = ["IDDOC", "parse_status", "ingestion_timestamp"]
        df = df.select(*keys, *[c for c in columns if c not in keys])
    return df.withColumn("_rn", f.row_number().over(w)).filter(f.col("_rn") == 1).drop("_rn")


def _reusable_checkpoint_rows() -> DataFrame:
    """Checkpoint rows whose result a new parse of the same bytes would only repeat: SUCCESS, the deterministic skips,
    and the scanned PDFs already queued for LLM-OCR (a re-parse would delete their rendered pages)."""
    return spark.table(CHECKPOINT_TABLE).filter(
        f.col("parse_status").isin(_REUSABLE_STATUSES)
        | f.col("parser_strategy").eqNullSafe("pending_llm_ocr:pdf")
    )


def exclude_already_parsed(df_files) -> DataFrame:
    """Resume support: drop the files whose IDDOC already has a reusable checkpoint row for the same bytes.

    Keyed on (IDDOC, document_sha256), not on the path: a checkpoint copied from another environment, or a file
    moved between the D_ and Dm_ folders, keeps its content but not its path. Returns the filtered, cached df_files.

    :param df_files: Selected files, from ``select_files_for_target_iddocs``.
    """
    if not table_exists(CHECKPOINT_TABLE):
        logger.info("[RESUME] No existing checkpoint — every selected file will be parsed.")
        return df_files

    df_reusable = _reusable_checkpoint_rows().select("IDDOC", "document_sha256").distinct()
    before = df_files.count()
    df_files = df_files.join(df_reusable, on=["IDDOC", "document_sha256"], how="left_anti").cache()
    after = df_files.count()

    # Why the remaining files are parsed: an IDDOC never seen, or seen with other bytes (or only in error).
    known_iddocs = spark.table(CHECKPOINT_TABLE).select("IDDOC").distinct()
    n_known = df_files.join(known_iddocs, on="IDDOC", how="left_semi").count()
    logger.info(
        f"[RESUME] {before - after} of {before} selected files reused from the checkpoint | to parse: {after} "
        f"({after - n_known} IDDOC(s) absent from the checkpoint, {n_known} present with other bytes or in error)"
    )
    return df_files


def enforce_parse_budget(num_files) -> None:
    """Stop the task before any GPU parse when more files are pending than `MAX_PARSE_FILES` allows.

    A daily run parses tens of files; thousands mean the checkpoint was not recognised (wrong table, copy missing),
    which costs a day of GPU. A full re-parse on purpose sets the ceiling to 0 (job parameter max_parse_files).

    :param num_files: Number of files pending parse.
    """
    if MAX_PARSE_FILES and num_files > MAX_PARSE_FILES:
        raise RuntimeError(
            f"[BUDGET] {num_files} files to parse, above max_parse_files={MAX_PARSE_FILES}. Nothing was parsed. "
            f"Check the [RESUME] line above and that {CHECKPOINT_TABLE} holds the previous parses; to re-parse on "
            f"purpose, run the job with the parameter max_parse_files set to 0 (or a higher ceiling)."
        )
    logger.info(f"[BUDGET] {num_files} file(s) to parse (ceiling {MAX_PARSE_FILES or 'none'})")


def setup_workers(repo_dir) -> int:
    """Ship the parsing modules and settings to the executors; returns the number of parallel tasks.

    `addPyFile` reaches executors that join late too (`sys.path` only covers the driver). On a single-user cluster the
    settings are broadcast; on a shared one the workers read the JSON copy written on the volume.

    :param repo_dir: Folder of the parsing modules, shipped to the executors.
    """
    import config as cfg
    from worker import broadcast_config, configure, write_worker_config

    for module in cfg.WORKER_MODULES:
        spark.sparkContext.addPyFile(os.path.join(repo_dir, module))
    spark.conf.set("app.parsing.volume_base_path", cfg.VOLUME_BASE_PATH)
    spark.conf.set("spark.sql.files.maxPartitionBytes", "134217728")
    spark.conf.set("spark.sql.execution.arrow.pyspark.enabled", "true")
    spark.conf.set("spark.sql.execution.arrow.maxRecordsPerBatch", "1")

    configure(
        OFFLINE_MODELS_DIR=cfg.OFFLINE_MODELS_DIR,
        VOLUME_BASE_PATH=cfg.VOLUME_BASE_PATH,
        ANTIWORD_BIN=f"{repo_dir}/{cfg.ANTIWORD_BIN_RELATIVE}",
        ANTIWORD_SHARE_DIR=f"{repo_dir}/{cfg.ANTIWORD_SHARE_RELATIVE}",
        WORKER_CONFIG_JSON=os.path.join(cfg.VOLUME_BASE_PATH, "_parsing_config.json"),
        USE_GPU=cfg.USE_GPU,
        DO_OCR=cfg.DO_OCR,
        GPU_OCR_FALLBACK=cfg.GPU_OCR_FALLBACK,
        TABLE_STRUCTURE_MODE=cfg.TABLE_STRUCTURE_MODE,
        GENERATE_PICTURE_IMAGES=cfg.GENERATE_PICTURE_IMAGES,
        IMAGE_SCALE=cfg.IMAGE_SCALE,
        MIN_AREA_RATIO=cfg.MIN_AREA_RATIO,
        MAX_REPEAT=cfg.MAX_REPEAT,
        USE_TIKTOKEN=cfg.USE_TIKTOKEN,
        CHARS_PER_TOKEN=cfg.CHARS_PER_TOKEN,
        MIN_CHUNK_TOKENS=cfg.MIN_CHUNK_TOKENS,
        TARGET_CHUNK_TOKENS=cfg.TARGET_CHUNK_TOKENS,
        MAX_CHUNK_TOKENS=cfg.MAX_CHUNK_TOKENS,
        MAX_CHUNK_CHARS=cfg.MAX_CHUNK_CHARS,
        CHUNK_OVERLAP_RATIO=cfg.CHUNK_OVERLAP_RATIO,
    )
    try:
        _ = spark.sparkContext
        broadcast_config(spark)
    except Exception:
        logger.warning(
            "broadcast_config skipped (shared cluster) - workers read the JSON copy on the volume"
        )
    write_worker_config(spark)
    check_docling_on_workers()

    try:
        parallelism = int(spark.sparkContext.defaultParallelism)
        if parallelism < 1:
            raise ValueError(f"defaultParallelism={parallelism}")
    except Exception as exc:
        logger.warning(f"Could not read defaultParallelism ({exc}) - falling back to 4")
        parallelism = 4

    from importlib import metadata

    def _version(package) -> str:
        """Installed version of a package.

        :param package: Name of the package.
        :return: the version, or ``absent`` when the package is not installed.
        """
        try:
            return metadata.version(package)
        except metadata.PackageNotFoundError:
            return "absent"

    # The versions actually loaded, next to the pins of the %pip cells and of the GPU task.
    logger.info(
        "Packages | "
        + " | ".join(
            f"{p}={_version(p)}"
            for p in (
                "docling",
                "docling-core",
                "docling-ibm-models",
                "torch",
                "great_expectations",
            )
        )
    )
    logger.info(
        f"Run {INGESTION_RUN_ID} | mode={RUN_MODE} | parallelism={parallelism} | GPU={cfg.USE_GPU} | "
        f"table_mode={cfg.TABLE_STRUCTURE_MODE} | filter={PARSE_FILTER}"
    )
    return parallelism


def check_docling_on_workers() -> None:
    """Import Docling once on an executor before any parse, and stop the task with the worker's own traceback if it fails.

    Inside the parse UDF the same failure surfaces only as a Python worker crash, after the scan and the hashing.
    """

    def probe(_) -> list[tuple[str, str]]:
        """Import Docling on the executor that runs this partition.

        :param _: Unused partition index.
        :return: one ``(status, detail)`` pair: the numpy version, or the traceback.
        """
        import traceback

        try:
            import format_parsers

            format_parsers._get_docling()
            import numpy

            return [("ok", f"numpy={numpy.__version__}")]
        except BaseException:
            return [("error", traceback.format_exc()[-4000:])]

    status, detail = spark.sparkContext.parallelize([0], 1).mapPartitions(probe).collect()[0]
    if status != "ok":
        raise RuntimeError(f"Docling cannot be imported on the executors:\n{detail}")
    logger.info(f"[WORKERS] Docling imports on the executors ({detail})")


def split_for_gpu(df_files) -> tuple[DataFrame, DataFrame | None, int]:
    """(df_cpu, df_gpu, n_gpu): the PDFs go to the GPU task only when more than `GPU_MIN_PDFS` are left.

    Docling uses its GPU models (layout, tables) on PDFs only. A daily run has a handful of PDFs: parsed here on CPU,
    the GPU cluster is never started.

    :param df_files: Selected files, after the size gate.
    """
    is_pdf = f.col("source_file_extension") == "pdf"
    n_pdf = df_files.filter(is_pdf).count()
    if n_pdf <= GPU_MIN_PDFS:
        logger.info(
            f"[ROUTE] {n_pdf} PDF(s) <= {GPU_MIN_PDFS}: everything parsed on CPU, no GPU task"
        )
        return df_files, None, 0
    logger.info(
        f"[ROUTE] {n_pdf} PDF(s) > {GPU_MIN_PDFS}: the PDFs go to the GPU task, the other formats stay on CPU"
    )
    return df_files.filter(~is_pdf).cache(), df_files.filter(is_pdf), n_pdf


def build_run_scope(target_iddocs, revised_iddocs) -> DataFrame:
    """One row per IDDOC in scope this run, with the run ids: what 3c_chunks rebuilds and how the run is correlated.

    :param target_iddocs: IDDOCs in scope this run.
    :param revised_iddocs: IDDOCs in scope that were revised.
    :return: one row per IDDOC, with the run ids and the run mode.
    """
    rows = [(int(i), i in revised_iddocs) for i in sorted(target_iddocs)]
    return spark.createDataFrame(rows, "IDDOC long, is_revised boolean").withColumns(
        {
            "ingestion_run_id": f.lit(INGESTION_RUN_ID),
            "job_run_id": f.lit(JOB_RUN_ID).try_cast("string"),
            "run_mode": f.lit(RUN_MODE),
            "written_at": f.current_timestamp(),
        }
    )


def read_run_scope() -> tuple[set, set]:
    """(target_iddocs, revised_iddocs) written by 3_parse for this run; adopts its ingestion run id.

    Refuses the scope of another job run: 3c_chunks would rebuild the wrong documents.
    """
    global INGESTION_RUN_ID
    rows = spark.table(RUN_SCOPE_TABLE).collect()
    if not rows:
        logger.info("[SCOPE] No IDDOC in scope this run")
        return set(), set()
    scope_job_run = rows[0]["job_run_id"]
    if JOB_RUN_ID and scope_job_run and scope_job_run != JOB_RUN_ID:
        raise RuntimeError(
            f"{RUN_SCOPE_TABLE} was written by job run {scope_job_run}, not by this run {JOB_RUN_ID}"
        )
    INGESTION_RUN_ID = rows[0]["ingestion_run_id"]
    target_iddocs = {r["IDDOC"] for r in rows}
    revised_iddocs = {r["IDDOC"] for r in rows if r["is_revised"]}
    logger.info(
        f"[SCOPE] {len(target_iddocs)} IDDOC(s) in scope ({len(revised_iddocs)} revised), "
        f"run {INGESTION_RUN_ID}, job run {scope_job_run}"
    )
    return target_iddocs, revised_iddocs


def read_gpu_queue() -> DataFrame | None:
    """The PDFs 3_parse left for the GPU task, their bytes read again from the volume (same hash only).

    :return: PDF files to parse, or None when the queue is empty.
    """
    df_queue = spark.table(GPU_QUEUE_TABLE).filter(f.col("ingestion_run_id") == INGESTION_RUN_ID)
    paths = [r.source_path for r in df_queue.select("source_path").collect()]
    if not paths:
        return None
    df_content = (
        spark.read.format("binaryFile")
        .load(paths)
        .select(f.col("path").alias("source_path"), "content")
    )
    df_files = (
        df_queue.join(df_content, on="source_path", how="inner")
        .filter(f.sha2(f.col("content"), 256) == f.col("document_sha256"))
        .cache()
    )
    found = df_files.count()
    if found < len(paths):
        logger.warning(
            f"[GPU QUEUE] {len(paths) - found} file(s) changed or vanished since 3_parse: left for the next run"
        )
    logger.info(f"[GPU QUEUE] {found} PDF(s) to parse")
    return df_files


def files_parsed_this_run() -> int:
    """Number of checkpoint rows written by this run (3_parse and the GPU task share the ingestion run id)."""
    if not table_exists(CHECKPOINT_TABLE):
        return 0
    return (
        spark.table(CHECKPOINT_TABLE).filter(f.col("ingestion_run_id") == INGESTION_RUN_ID).count()
    )


def cleanup_stale_image_folders(df_files) -> None:
    """Delete image folders for every IDDOC about to be (re)parsed, so a
    retry/rebuild never mixes old and new extracted images. Must run after the resume exclusion
    (exclude_already_parsed).

    :param df_files: Files about to be parsed.
    """
    iddocs_to_parse = [
        r.IDDOC
        for r in df_files.select("IDDOC").filter(f.col("IDDOC").isNotNull()).distinct().collect()
    ]

    def _rm_folder(iddoc) -> bool:
        """Remove the image folder of one IDDOC, if it exists.

        :param iddoc: IDDOC whose image folder is removed.
        :return: True when a folder was removed.
        """
        folder = os.path.join(VOLUME_BASE_PATH, str(iddoc))
        if os.path.isdir(folder):
            shutil.rmtree(folder)
            return True
        return False

    with ThreadPoolExecutor(max_workers=8) as pool:
        removed = sum(pool.map(_rm_folder, iddocs_to_parse))
    logger.info(f"Cleaned {removed}/{len(iddocs_to_parse)} existing image folders")


def apply_oom_size_gate(df_files) -> tuple[DataFrame, DataFrame]:
    """Split df_files into (df_to_parse, df_skipped) using per-format OOM
    ceilings. OOXML files above the Docling-safe size but within the
    fallback ceiling are flagged _xml_fallback_only (routed straight to the
    zip/XML fallback, no Docling).

    :param df_files: Selected files, with their content.
    :return: files to parse, and files too large for every parser.
    """
    strip_ooxml_bloat_udf = f.udf(strip_ooxml_bloat, T.BinaryType())

    df_files = df_files.withColumn(
        "content",
        f.when(
            f.col("source_file_extension").isin(_OOXML_STRIP_EXTS),
            strip_ooxml_bloat_udf(f.col("content"), f.col("source_file_extension")),
        ).otherwise(f.col("content")),
    ).withColumn("_gate_size_bytes", f.length(f.col("content")))

    max_size_col = (
        f.when(f.col("source_file_extension").isin(_NON_DOCLING_EXTS), f.lit(_MAX_SIZE_OTHER))
        .when(f.col("source_file_extension") == "pdf", f.lit(_MAX_SIZE_PDF))
        .when(f.col("source_file_extension").isin(_OOXML_STRIP_EXTS), f.lit(_MAX_SIZE_OTHER))
        .otherwise(f.lit(_MAX_SIZE_DOCLING))
    )
    is_too_large = f.col("_gate_size_bytes") > max_size_col
    df_files = df_files.withColumn(
        "_xml_fallback_only",
        f.col("source_file_extension").isin(_OOXML_STRIP_EXTS)
        & (f.col("_gate_size_bytes") > f.lit(_MAX_SIZE_DOCLING)),
    )

    # Cached here so strip_ooxml_bloat_udf's lineage runs once, not on every count()/filter() below.
    df_to_parse = df_files.filter(~is_too_large).drop("_gate_size_bytes").cache()
    df_skipped = (
        df_files.filter(is_too_large).drop("_gate_size_bytes", "_xml_fallback_only").cache()
    )
    return df_to_parse, df_skipped


def write_skipped_too_large(df_skipped, skipped_count) -> None:
    """Persist oversized files directly as SKIP_TOO_LARGE, no parse attempt.

    :param df_skipped: Files over the size ceiling.
    :param skipped_count: Number of files over the size ceiling.
    """
    logger.warning(f"{skipped_count} files excluded (beyond even the XML fallback ceiling).")
    df_skipped_result = (
        df_skipped.drop("content")
        .withColumn("document_text", f.lit(""))
        .withColumn(
            "parser_error",
            f.concat(
                f.lit("TOO_LARGE:"),
                f.round(f.col("source_file_size_bytes") / 1024 / 1024, 1).try_cast("string"),
                f.lit("MB"),
            ),
        )
        .withColumn("parser_strategy", f.lit("skipped"))
        .withColumn("parse_time_seconds", f.lit(0.0).try_cast("float"))
        .withColumn(
            "images",
            f.array().try_cast(
                "array<struct<image_id:int,page_no:int,label:string,area_ratio:float,captions:array<string>,context_text:string,volume_path:string,image_width:int,image_height:int>>"
            ),
        )
        .withColumn("timings", f.lit(None).try_cast("map<string,float>"))
        .withColumn("image_count", f.lit(0))
        .withColumn("parse_status", f.lit("SKIP_TOO_LARGE"))
    )
    write_table(df_skipped_result, CHECKPOINT_TABLE, "append", merge_schema=True)
    logger.info(f"{skipped_count} files marked SKIP_TOO_LARGE in the checkpoint.")


def run_docling_batches(df_to_parse, parse_count, parse_fn) -> None:
    """Parse df_to_parse, checkpointing every CHECKPOINT_BATCH_SIZE files so
    progress survives a crash. A single small batch is written directly.

    :param df_to_parse: Files to parse.
    :param parse_count: Number of files to parse.
    :param parse_fn: Parse function, as built by ``make_parse_fn``.
    """
    if parse_count <= CHECKPOINT_BATCH_SIZE:
        logger.info(f"Direct parsing ({parse_count} files, no batching needed)...")
        df_parsed = parse_fn(df_to_parse, parse_count)
        write_table(df_parsed, CHECKPOINT_TABLE, "append", merge_schema=True)
        return

    num_batches = (parse_count + CHECKPOINT_BATCH_SIZE - 1) // CHECKPOINT_BATCH_SIZE
    logger.info(f"{num_batches} batch(es) of up to {CHECKPOINT_BATCH_SIZE} files...")
    # The batches are fixed once, on the driver, from the paths alone: df_to_parse is defined against the checkpoint, so a
    # batch numbered inside the plan would shift as soon as a batch is written, and files would be skipped.
    paths = sorted(row["source_path"] for row in df_to_parse.select("source_path").distinct().collect())
    if len(paths) != parse_count:
        raise RuntimeError(f"{len(paths)} distinct files to parse, {parse_count} counted")

    parsed_total = 0
    for batch_idx in range(num_batches):
        batch_paths = paths[batch_idx::num_batches]
        df_batch = df_to_parse.filter(f.col("source_path").isin(batch_paths))
        batch_size = len(batch_paths)
        parsed_total += batch_size

        logger.info(f"Batch {batch_idx + 1}/{num_batches} ({batch_size} files)...")
        bt0 = time.time()
        df_batch_parsed = parse_fn(df_batch, batch_size)
        write_table(df_batch_parsed, CHECKPOINT_TABLE, "append", merge_schema=True)
        logger.info(f"Batch {batch_idx + 1}/{num_batches} saved in {time.time() - bt0:.1f}s")

    if parsed_total != parse_count:
        raise RuntimeError(f"{parsed_total} files went through the batches, {parse_count} were to be parsed")


def log_run_summary(df_to_parse, parse_count, elapsed) -> None:
    """Log OK/SKIP/ERR counts scoped to exactly the files parsed THIS run
    (source_path, document_sha256) — not the whole checkpoint.

    :param df_to_parse: Files parsed this run.
    :param parse_count: Number of files parsed this run.
    :param elapsed: Duration of the parse in seconds.
    """
    df_this_run = (
        spark.table(CHECKPOINT_TABLE)
        .filter(f.col("ingestion_run_id") == INGESTION_RUN_ID)
        .select("source_path", "document_sha256", "parse_status")
        .join(
            df_to_parse.select("source_path", "document_sha256"),
            on=["source_path", "document_sha256"],
            how="inner",
        )
        .dropDuplicates(["source_path", "document_sha256"])
    )
    run_errors = df_this_run.filter(
        ~f.col("parse_status").isin("SUCCESS", "SKIP_TOO_LARGE")
    ).count()
    run_skips = df_this_run.filter(f.col("parse_status") == "SKIP_TOO_LARGE").count()
    run_ok = parse_count - run_errors - run_skips

    logger.info(
        f"Parsed {parse_count} files in {elapsed:.1f}s "
        f"({parse_count / max(elapsed, 0.01):.1f} f/s) | OK: {run_ok} | "
        f"SKIP_TOO_LARGE: {run_skips} | ERR: {run_errors}"
    )


def parse_pending_files(df_files, parse_fn) -> None:
    """Full "parse everything pending" phase: OOM gate, direct/batched
    Docling parsing, run summary. df_files must already be resume-excluded.

    :param df_files: Selected files, already without the reusable checkpoint rows.
    :param parse_fn: Parse function, as built by ``make_parse_fn``.
    """
    df_to_parse, df_skipped = apply_oom_size_gate(df_files)
    skipped_count = df_skipped.count()
    fallback_only_count = df_to_parse.filter(f.col("_xml_fallback_only")).count()
    parse_count = df_to_parse.count()

    if skipped_count > 0:
        write_skipped_too_large(df_skipped, skipped_count)

    if fallback_only_count > 0:
        logger.info(
            f"{fallback_only_count} docx/pptx files too large for Docling "
            f"will use the direct XML fallback (no rasterization, no ai_parse_document)."
        )

    logger.info(f"{parse_count} files to parse with Docling...")
    if parse_count == 0:
        logger.info("Nothing to parse (all remaining files are excluded XLSX).")
        return

    t0 = time.time()
    run_docling_batches(df_to_parse, parse_count, parse_fn)
    log_run_summary(df_to_parse, parse_count, time.time() - t0)


def _images_pending_iddocs() -> DataFrame:
    """IDDOCs parked as IMAGES_PENDING: text parsed, only images left. Their checkpoint row still says EMPTY_TEXT,
    so the scans of failed documents skip them: they are not parsed again.

    :return: IDDOC column of the documents waiting for their images.
    """
    if not table_exists(TARGET_PROCESSED_FILES_TABLE):
        return spark.createDataFrame([], "IDDOC long")
    return (
        spark.table(TARGET_PROCESSED_FILES_TABLE)
        .filter(f.col("parse_status") == IMAGES_PENDING)
        .select("IDDOC")
        .distinct()
    )


def show_failed_documents(df_business_meta) -> None:
    """Display the documents this run failed to parse, before the rank-2 retry.

    Only this run's rows count: a checkpoint can hold failures written by an earlier parser.

    :param df_business_meta: Business metadata of the manifest, with an ``IDDOC`` column.
    """
    df_full_pipeline = read_checkpoint_deduped(
        [
            "ingestion_run_id",
            "source_file_name",
            "source_file_extension",
            "parser_strategy",
            "parser_error",
            "source_file_size_bytes",
        ]
    ).filter(f.col("ingestion_run_id") == INGESTION_RUN_ID)
    df_failed_inspect = (
        df_full_pipeline.join(df_business_meta.select("IDDOC").distinct(), on="IDDOC", how="inner")
        .join(_images_pending_iddocs(), on="IDDOC", how="left_anti")
        .filter(f.col("parse_status").isin("ERROR", "EMPTY_TEXT"))
        .filter(
            ~f.col("parser_strategy").eqNullSafe("pending_llm_ocr:pdf")
        )  # queued for step 4, not actually failed
        .select(
            "IDDOC",
            "source_file_name",
            "source_file_extension",
            "parse_status",
            "parser_strategy",
            "parser_error",
            "source_file_size_bytes",
        )
        .orderBy("source_file_extension", "IDDOC")
    )
    failed_total = df_failed_inspect.count()
    logger.warning(
        f"{failed_total} documents failed after Docling parsing — "
        f"will be retried below with each IDDOC's next-best file variant, if any"
    )

    display(
        df_failed_inspect.groupBy("source_file_extension", "parse_status", "parser_strategy")
        .agg(f.count("*").alias("count"), f.first("parser_error").alias("sample_error"))
        .orderBy(f.desc("count"))
    )
    display(df_failed_inspect)


def retry_failed_iddocs(df_business_meta) -> None:
    """Re-attempt every failed IDDOC with its rank-2 candidate file (if any),
    then replace the failed checkpoint rows with the successful retries.

    Runs after both parse tasks (CPU and GPU): only the folders of the failed IDDOCs are scanned again.

    :param df_business_meta: Business metadata of the manifest, with an ``IDDOC`` column.
    """
    df_full_pipeline = read_checkpoint_deduped(["parser_strategy"])
    failed_iddocs = [
        r.IDDOC
        for r in df_full_pipeline.join(
            df_business_meta.select("IDDOC").distinct(), on="IDDOC", how="inner"
        )  # still in current scope
        .join(_images_pending_iddocs(), on="IDDOC", how="left_anti")
        .filter(f.col("parse_status").isin("ERROR", "EMPTY_TEXT") & f.col("IDDOC").isNotNull())
        .filter(
            ~f.col("parser_strategy").eqNullSafe("pending_llm_ocr:pdf")
        )  # queued for step 4, not actually failed
        .select("IDDOC")
        .distinct()
        .collect()
    ]
    logger.info(f"Failed IDDOCs to retry: {len(failed_iddocs)}")
    if not failed_iddocs:
        logger.info("Nothing to retry.")
        return
    _, df_matched_full, df_content = select_files_for_target_iddocs(
        set(failed_iddocs), df_business_meta
    )

    # .ppt stays a rank-2 candidate: image_fallbacks.fallback_parse_ppt_ole parses it.
    df_alt = (
        df_matched_full.filter(f.col("IDDOC").isin(failed_iddocs))
        .filter(
            (f.col("priority_rank") == 2) & (f.col("ext_priority") < selection.UNSUPPORTED_PRIORITY)
        )
        .drop(*selection.RANK_HELPER_COLS)
    )
    retry_paths = [r.source_path for r in df_alt.select("source_path").collect()]

    if not retry_paths:
        logger.info("No rank-2 alternatives found.")
        return

    logger.info(f"Retrying {len(retry_paths)} files...")
    # Read ONLY the needed binary content (avoids full volume re-scan).
    df_content_retry = df_content.filter(f.col("source_path").isin(retry_paths))
    df_retry_src = (
        df_alt.join(df_content_retry, on="source_path", how="inner")
        .withColumn("document_sha256", f.sha2(f.col("content"), 256))
        .withColumn("ingestion_run_id", f.lit(INGESTION_RUN_ID))
        .withColumn("ingestion_timestamp", f.current_timestamp())
    )
    biz_cols = [c for c in ENRICHMENT_COLS if c in df_business_meta.columns]
    df_retry_src = df_retry_src.join(
        f.broadcast(df_business_meta.select("IDDOC", *biz_cols).dropDuplicates(["IDDOC"])),
        on="IDDOC",
        how="left",
    )
    df_retry = _with_parse_result(
        df_retry_src.repartition(max(1, len(retry_paths))), f.lit(False)
    ).withColumn(
        "parse_status",
        f.when(f.col("parser_error").isNotNull(), f.lit("ERROR"))
        .when(f.length(f.trim(f.col("document_text"))) == 0, f.lit("EMPTY_TEXT"))
        .otherwise(f.lit("SUCCESS")),
    )

    write_table(df_retry, RETRY_TEMP_TABLE, "overwrite", overwrite_schema=True)

    df_retry_ok = spark.table(RETRY_TEMP_TABLE).filter(f.col("parse_status") == "SUCCESS")
    ok_count = df_retry_ok.count()
    logger.info(f"Retry recovered {ok_count}/{len(failed_iddocs)} IDDOCs")

    if ok_count > 0:
        ok_iddocs = [r.IDDOC for r in df_retry_ok.select("IDDOC").collect()]
        df_keep = spark.table(CHECKPOINT_TABLE).filter(
            ~(f.col("parse_status").isin("ERROR", "EMPTY_TEXT") & f.col("IDDOC").isin(ok_iddocs))
        )
        write_table(
            df_keep.unionByName(df_retry_ok, allowMissingColumns=True),
            CHECKPOINT_TABLE,
            "overwrite",
            overwrite_schema=True,
        )
        logger.info("Checkpoint updated with retry results.")

    spark.sql(f"DROP TABLE IF EXISTS {RETRY_TEMP_TABLE}")


def mark_empty_folders(target_iddocs, df_files, df_business_meta) -> set:
    """Write SKIPPED_EMPTY_FOLDER for target IDDOCs with no parseable file on the volume.

    These never go through parsing, so their rows go straight to `parsed_documents`.
    Returns `target_iddocs` without them, so retry / build steps skip them.

    :param target_iddocs: IDDOCs to parse this run.
    :param df_files: Selected files, with an ``IDDOC`` column.
    :param df_business_meta: Business metadata of the manifest.
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
        df_business_meta.filter(f.col("IDDOC").isin(list(empty_folder_iddocs)))
        .select("IDDOC", *manifest_cols)
        .withColumns(
            {
                "parse_status": f.lit("SKIPPED_EMPTY_FOLDER"),
                "removal_reason": f.lit("NO_PARSEABLE_FILE_ON_VOLUME"),
                "is_in_rag": f.lit(False),
                "is_filtered_by_date": f.lit(False),
                "ingestion_run_id": f.lit(INGESTION_RUN_ID),
                "ingestion_timestamp": f.current_timestamp(),
                "job_run_id": f.lit(JOB_RUN_ID),
                "change_type": f.lit("NEW"),
            }
        )
    )
    write_table(df_empty_folder_rows, TARGET_PROCESSED_FILES_TABLE, "append", merge_schema=True)
    logger.info(
        f"[EMPTY FOLDER] Wrote {len(empty_folder_iddocs)} row(s) to {TARGET_PROCESSED_FILES_TABLE}"
    )
    return target_iddocs - empty_folder_iddocs
