"""Phases of the `3_parse` task that build the tables: image_metadata, parsed_documents and the chunks.

Everything here returns lazy DataFrames from the parse checkpoint; the writes are in `output_steps.py`.
Driver-side only: nothing here is shipped to the executors.
"""
from databricks.sdk.runtime import spark
from pyspark import StorageLevel
from pyspark.sql import Column, DataFrame, Window
from pyspark.sql import functions as F

import image_utils
import worker
from parse_steps import read_checkpoint_deduped
from leap_utils.common.logger import get_logger
from tables import table_exists
from config import (
    ARCHIVE_NOTICE_CONTENT_TYPE,
    ARCHIVE_NOTICE_MARKER,
    BOILERPLATE_MIN_DOCS,
    BUSINESS_METADATA_COLS,
    CHARS_PER_TOKEN,
    CLEAN_FORMULA_ARTIFACTS,
    CLEAN_IMAGE_PLACEHOLDERS,
    DEDUPLICATE_CHUNKS,
    DIVISION_REFERENCE_TABLE,
    DOC_DATE_CUTOFF,
    EMBED_SOURCE_PREFIX,
    HIERARCHY_COLS,
    MANIFEST_METADATA_COLS,
    MAX_CHUNKS_SPREADSHEET,
    PARSE_MANIFEST_TABLE,
    RAG_EXCLUSION_TABLE,
    RUN_MODE,
    TARGET_CHUNK_TABLE,
    TARGET_IMAGE_METADATA_TABLE,
    EMPTY_TEXT,
    IMAGES_PENDING,
    IMAGE_RETRIED_STATUSES,
)

logger = get_logger(__name__)

_SPREADSHEET_EXTS = ("xlsx", "xls", "xlsm")


def with_fresh_business_metadata(df_checkpoint) -> DataFrame:
    """Replace checkpoint's business-metadata columns with fresh values,
    falling back to the checkpoint's own value when an IDDOC isn't in scope.
    division/niveau_plus_* come from DIVISION_REFERENCE_TABLE, not
    parse_manifest, which goes stale.

    :param df_checkpoint: Parse checkpoint rows, one per IDDOC.
    """
    df_manifest = spark.table(PARSE_MANIFEST_TABLE).select(
        F.col("IDDOC"), *[F.col(c).alias(f"_fresh_{c}") for c in MANIFEST_METADATA_COLS]
    )
    df_hierarchy = spark.table(DIVISION_REFERENCE_TABLE).select(
        F.col("IDDOC"), *[F.col(c).alias(f"_fresh_{c}") for c in HIERARCHY_COLS]
    )
    df = df_checkpoint
    for c in BUSINESS_METADATA_COLS:
        if c not in df.columns:
            df = df.withColumn(c, F.lit(None))
    df = (
        df.join(F.broadcast(df_manifest), on="IDDOC", how="left")
        .join(F.broadcast(df_hierarchy), on="IDDOC", how="left")
    )
    for c in BUSINESS_METADATA_COLS:
        df = df.withColumn(c, F.coalesce(F.col(f"_fresh_{c}"), F.col(c)))
    return df.drop(*[f"_fresh_{c}" for c in BUSINESS_METADATA_COLS])


def _with_attempt_columns(df) -> DataFrame:
    """`attempts` and `last_error_kind` on every image row; NULL where the table does not have them yet.

    :param df: Image rows.
    """
    for name, dtype in (("attempts", "int"), ("last_error_kind", "string")):
        if name not in df.columns:
            df = df.withColumn(name, F.lit(None).try_cast(dtype))
    return df


def same_image_condition() -> Column:
    """True when the previous row of (IDDOC, image_id) describes the image now at that id.

    The ids are positions in the parse output: a re-parse that changes the number or the order of the images moves
    them. A description is kept when the document was not parsed again after it was written, or when the image at
    that id has the same page and the same size.

    :return: true where the previous description still applies to the image.
    """
    parsed_before = F.col("_prev_described_at").isNotNull() & (F.col("_prev_described_at") >= F.col("_parsed_at"))
    same_shape = (
        F.col("image_width").isNotNull() & F.col("image_height").isNotNull()
        & F.col("image_width").eqNullSafe(F.col("_prev_image_width"))
        & F.col("image_height").eqNullSafe(F.col("_prev_image_height"))
        & F.col("page_no").eqNullSafe(F.col("_prev_page_no"))
    )
    return F.coalesce(parsed_before | same_shape, F.lit(False))


def _carry_previous_image_rows(df) -> DataFrame:
    """Keep the description, the attempt counter and the last error of each image that is still the same image.

    :param df: Image rows of the current parse.
    """
    if not table_exists(TARGET_IMAGE_METADATA_TABLE):
        return _with_attempt_columns(df.drop("_parsed_at")).withColumn("attempts", F.lit(0))
    prev = _with_attempt_columns(spark.table(TARGET_IMAGE_METADATA_TABLE))
    carried = ("status", "description", "input_tokens", "output_tokens", "described_at", "attempts", "last_error_kind",
               "page_no", "image_width", "image_height")
    prev = prev.select("IDDOC", "image_id", *[F.col(c).alias(f"_prev_{c}") for c in carried]).dropDuplicates(
        ["IDDOC", "image_id"])
    df = _with_attempt_columns(df).join(prev, on=["IDDOC", "image_id"], how="left").withColumn("_same", same_image_condition())
    kept_status = F.col("_same") & F.col("_prev_status").isin("DONE", "SKIPPED", "FAILED_LIMIT")
    df = df.withColumn("status", F.when(kept_status, F.col("_prev_status")).otherwise(F.col("status")))
    for c in ("description", "input_tokens", "output_tokens", "described_at"):
        df = df.withColumn(c, F.when(kept_status, F.col(f"_prev_{c}")).otherwise(F.col(c)))
    df = (
        df.withColumn("attempts", F.when(F.col("_same"), F.coalesce(F.col("_prev_attempts"), F.lit(0))).otherwise(F.lit(0)))
        # Only an image that is not described keeps the kind of its last failure.
        .withColumn("last_error_kind", F.when(F.col("_same") & F.col("status").isin("PENDING", "FAILED_LIMIT"),
                                              F.col("_prev_last_error_kind")))
    )
    return df.drop("_same", "_parsed_at", *[f"_prev_{c}" for c in carried])


def build_image_metadata(target_iddocs, ingestion_run_id) -> DataFrame:
    """Explode checkpoint rows' `images` array into one row per image,
    preserving any already-DONE/SKIPPED description across a rebuild.

    :param target_iddocs: IDDOCs in scope this run.
    :param ingestion_run_id: Id of the ingestion run, stored on every image row.
    """
    if RUN_MODE == "rechunk" and table_exists(TARGET_IMAGE_METADATA_TABLE):
        # Kept as is: a checkpoint copied from another environment does not list the images already described,
        # and rebuilding from it would drop their descriptions. Materialised because the same table is overwritten.
        df_kept = spark.table(TARGET_IMAGE_METADATA_TABLE).filter(F.col("IDDOC").isin(list(target_iddocs))).localCheckpoint()
        logger.info(f"Image metadata kept from {TARGET_IMAGE_METADATA_TABLE}: {df_kept.count()} rows")
        return _with_attempt_columns(df_kept)
    df_full_pipeline = read_checkpoint_deduped()
    # The checkpoint also keeps superseded revisions: only the documents in scope are used, in every run mode.
    df_full_pipeline = df_full_pipeline.filter(F.col("IDDOC").isin(list(target_iddocs)))
    df_full_pipeline = with_fresh_business_metadata(df_full_pipeline)

    # Pre-cutoff documents are described too: their image chunks go to chunks_archive (4_describe_images).
    df_image_metadata = (
        df_full_pipeline.filter(F.col("image_count") > 0)
        .select("IDDOC", "ref", "titre", "division", "niveau_plus_1", "niveau_plus_2", "source_file_name",
                F.col("ingestion_timestamp").alias("_parsed_at"), F.posexplode("images").alias("img_pos", "img"))
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
            F.lit(None).try_cast("string").alias("description"),
            F.lit(None).try_cast("integer").alias("input_tokens"),
            F.lit(None).try_cast("integer").alias("output_tokens"),
            F.lit(None).try_cast("timestamp").alias("described_at"),
            F.lit(ingestion_run_id).alias("ingestion_run_id"),
            F.current_timestamp().alias("ingestion_timestamp"),
            "_parsed_at",
        )
    )

    df_image_metadata = _carry_previous_image_rows(df_image_metadata)
    df_image_metadata = _with_rows_of_unlisted_documents(df_image_metadata, df_full_pipeline)
    # Materialised: the same table is read above and overwritten (or merged into) by the write.
    df_image_metadata = df_image_metadata.localCheckpoint()
    logger.info(f"Image metadata: {df_image_metadata.count()} rows")
    return df_image_metadata


def _with_rows_of_unlisted_documents(df_image_metadata, df_full_pipeline) -> DataFrame:
    """Add, unchanged, the image_metadata rows of the documents whose checkpoint row counts images but lists none.

    A checkpoint copied from an older pipeline keeps `image_count` but not the `images` array: rebuilding from it would
    drop every image (and description) of those documents.

    :param df_image_metadata: Image rows built from the checkpoint.
    :param df_full_pipeline: Checkpoint rows of this run, with the business metadata.
    """
    if not table_exists(TARGET_IMAGE_METADATA_TABLE):
        return df_image_metadata
    df_unlisted = df_full_pipeline.filter(
        (F.col("image_count") > 0) & (F.coalesce(F.size("images"), F.lit(0)) <= 0)).select("IDDOC").distinct()
    df_previous = _with_attempt_columns(spark.table(TARGET_IMAGE_METADATA_TABLE))
    df_kept = df_previous.join(df_unlisted, on="IDDOC", how="left_semi").select(*[
        (F.col(c) if c in df_previous.columns else F.lit(None)).try_cast(df_image_metadata.schema[c].dataType).alias(c)
        for c in df_image_metadata.columns
    ])
    n_docs = df_unlisted.count()
    if n_docs:
        logger.info(f"[IMAGES] {n_docs} document(s) without an image list in the checkpoint: their image_metadata rows are kept as is")
    return df_image_metadata.unionByName(df_kept)


def _apply_date_and_rag_filters(df_processed_files) -> tuple[DataFrame, DataFrame | None, Column]:
    """Flag is_filtered_by_date and is_in_rag on df_processed_files.

    doc_date < DOC_DATE_CUTOFF: parsed, chunks routed to chunks_archive instead of
    the RAG tables (is_in_rag=False). NULL doc_date = recent. Manual exclusions come from
    the ``RAG_EXCLUSION_TABLE`` table.
    Returns (df_processed_files, df_exclusions_or_none, cutoff_col).

    :param df_processed_files: Document rows of this run.
    """
    cutoff = F.lit(DOC_DATE_CUTOFF).try_cast("date")
    df_processed_files = df_processed_files.withColumn(
        "is_filtered_by_date",
        F.when(
            (F.col("parse_status") == "SUCCESS")
            & F.col("doc_date").isNotNull()
            & (F.col("doc_date") < cutoff),
            F.lit(True)
        ).otherwise(F.lit(False))
    )
    filtered_by_date_count = df_processed_files.filter(F.col("is_filtered_by_date") == True).count()
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
            "is_in_rag",
            F.when(F.col("_excluded").isNotNull(), F.lit(False))
             .when(F.col("is_filtered_by_date") == True, F.lit(False))
             .otherwise(F.lit(True))
        ).drop("_excluded")
    else:
        df_processed_files = df_processed_files.withColumn(
            "is_in_rag",
            F.when(F.col("is_filtered_by_date") == True, F.lit(False)).otherwise(F.lit(True))
        )
    return df_processed_files, df_exclusions, cutoff


def _build_chunks(df_full_pipeline, df_exclusions) -> DataFrame:
    """Filter to chunkable rows (SUCCESS, not manually excluded — any date),
    clean known artefacts, then explode into chunk rows.

    :param df_full_pipeline: Checkpoint rows of this run.
    :param df_exclusions: IDDOCs excluded from chunking, or None when there is no exclusion table.
    """
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

    df_for_chunking = df_for_chunking.withColumn("url", worker.intraqual_ref_url(F.col("ref")))
    # Normalised body (section line, case, spaces folded): finds the same passage in many documents.
    body_key = F.sha2(F.trim(F.regexp_replace(F.lower(F.regexp_replace(F.col("c.chunk_text"), r"^\[[^\]]*\]\s*", "")),
                                              r"\s+", " ")), 256)

    # chunk_content_type is kept on every row, not just image chunks; other per-document stats live only in parsed_documents.
    return (
        df_for_chunking
        .withColumn("chunks", worker.build_chunks_udf(F.col("document_text")))
        .withColumn("c", F.explode(F.col("chunks")))
        .withColumn("_chunk_text_full", worker.source_prefixed_text(
            F.col("c.chunk_text"), F.col("ref"), F.col("titre"),
            F.col("division"), F.col("niveau_plus_1"), doc_date_col=F.col("doc_date"),
            include_prefix=EMBED_SOURCE_PREFIX, type_col=F.col("type_document"),
        ))
        .select(
            "IDDOC",
            "source_file_extension",
            F.coalesce(F.col("ref"), F.col("source_file_name")).alias("REF"),
            F.col("division"),
            F.concat_ws("-", F.col("IDDOC").try_cast("string"),
                        F.lpad((F.col("c.chunk_index") + F.lit(1)).try_cast("string"), 6, "0")).alias("chunk_id"),
            F.col("c.chunk_index").alias("chunk_index"),
            F.col("_chunk_text_full").alias("chunk_text"),
            F.col("c.chunk_token_count").alias("chunk_token_count"),
            F.col("c.chunk_content_type").alias("chunk_content_type"),
            F.to_json(F.col("c.metadata")).alias("semantic_headers"),
            F.sha2(F.col("_chunk_text_full"), 256).alias("chunk_sha256"),
            F.col("url"),
            F.col("doc_date"),
            # Document metadata, filterable by the search.
            F.col("titre"), F.col("type_document"), F.col("indice").try_cast("string").alias("indice"),
            F.col("langue"),
            body_key.alias("body_sha256"),
        )
    )


def _dedupe_and_limit_chunks(df_chunks) -> DataFrame:
    """Drop intra-IDDOC duplicate chunks (same text hash), then cap the
    number of chunks kept per oversized spreadsheet.

    :param df_chunks: Chunk rows.
    """
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

    # Same body in BOILERPLATE_MIN_DOCS+ documents (legal mentions, standard approval blocks): marked, not deleted, so
    # the search can leave them out.
    # Counted with the rows already in the table on an incremental run, so a new document's copy is caught too.
    df_keys = df_chunks.select("IDDOC", "body_sha256")
    if RUN_MODE == "incremental" and table_exists(TARGET_CHUNK_TABLE) \
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

    return df_chunks.drop("source_file_extension")  # chunk_index stays: 4_describe_images numbers the image passages after it


def build_processed_files_and_chunks(target_iddocs, df_image_metadata, job_run_id,
                                     revised_iddocs=frozenset()) -> tuple[DataFrame, DataFrame, DataFrame]:
    """Build parsed_documents (one row per parsed document) and the two
    chunk DataFrames (chat tables and archive) from the checkpoint.

    :param target_iddocs: IDDOCs in scope this run.
    :param df_image_metadata: Image rows of this run.
    :param job_run_id: Id of the Databricks job run.
    :param revised_iddocs: IDDOCs whose revision changed.
    """
    df_full_pipeline = read_checkpoint_deduped()
    # The checkpoint also keeps superseded revisions: only the documents in scope are used, in every run mode.
    df_full_pipeline = df_full_pipeline.filter(F.col("IDDOC").isin(list(target_iddocs)))
    df_full_pipeline = with_fresh_business_metadata(df_full_pipeline)
    # Real language (REF suffix, else the text): the manifest's langue is always fr-FR.
    df_full_pipeline = df_full_pipeline.withColumn(
        "langue", worker.language_udf(F.col("document_text"), F.col("ref")))

    # NEW = first time this IDDOC reaches a terminal status; REVISED = a _STALENESS_COLS change since its last SUCCESS.
    if revised_iddocs:
        df_revised = spark.createDataFrame(
            [(i,) for i in revised_iddocs], "IDDOC long"
        ).withColumn("_is_revised", F.lit(True))
        df_full_pipeline = df_full_pipeline.join(F.broadcast(df_revised), on="IDDOC", how="left")
    else:
        df_full_pipeline = df_full_pipeline.withColumn("_is_revised", F.lit(False))

    # images_needing_description counts the images still to describe (PENDING, or ERROR from an earlier run), not the
    # decorative ones (unlike image_count), so an audit can compare expected vs actual DONE+SKIPPED counts.
    df_images_needed = (
        df_image_metadata.filter(F.col("status").isin(*IMAGE_RETRIED_STATUSES))
        .groupBy("IDDOC").agg(F.count("*").alias("images_needing_description"))
    )

    df_processed_files = (
        df_full_pipeline
        .join(F.broadcast(df_images_needed), on="IDDOC", how="left")
        .withColumns({
            "document_char_count": F.length(F.col("document_text")),
            "document_token_count": F.when(
                F.col("parse_status") == "SUCCESS",
                F.greatest(F.lit(1), F.floor(F.length(F.col("document_text")) / F.lit(CHARS_PER_TOKEN)).try_cast("int"))
            ).otherwise(F.lit(0)),
            "images_needing_description": F.coalesce(F.col("images_needing_description"), F.lit(0)),
            # Text-less document with images still to describe: parked until 4_describe_images moves it on.
            "parse_status": F.when(
                (F.col("parse_status") == EMPTY_TEXT) & (F.col("images_needing_description") > 0),
                F.lit(IMAGES_PENDING),
            ).otherwise(F.col("parse_status")),
            "change_type": F.when(F.coalesce(F.col("_is_revised"), F.lit(False)), F.lit("REVISED")).otherwise(F.lit("NEW")),
        })
        .withColumnRenamed("parser_error", "error_trace")
        .withColumn("job_run_id", F.lit(job_run_id))
        .select(
            "IDDOC", "document_sha256", "source_path", "source_file_name", "source_file_extension",
            "source_folder_path", "source_file_size_bytes", "source_modification_time",
            "ref", "titre", "type_document", "categorie", "langue", "auteur", "document_prefixes",
            "division", "niveau_plus_1", "niveau_plus_2", "niveau_plus_3",
            "niveau_plus_4", "niveau_plus_5", "niveau_plus_6",
            "doc_date",   # publication date (document_revisions_intraqual.published_date)
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

    # Persisted: the chunking UDF is the expensive part, and every count/filter/write below would rerun it.
    df_chunks = _build_chunks(df_full_pipeline, df_exclusions).persist(StorageLevel.MEMORY_AND_DISK)

    # True only for a spreadsheet that has more passages than the cap.
    df_over_cap = (
        df_chunks.filter(F.lower(F.col("source_file_extension")).isin(*_SPREADSHEET_EXTS))
        .groupBy("IDDOC").count()
        .filter(F.lit(MAX_CHUNKS_SPREADSHEET is not None) & (F.col("count") > F.lit(MAX_CHUNKS_SPREADSHEET or 0)))
        .select("IDDOC", F.lit(True).alias("_over_cap"))
    )
    df_processed_files = (
        df_processed_files.join(df_over_cap, on="IDDOC", how="left")
        .withColumn("is_chunks_truncated", F.coalesce(F.col("_over_cap"), F.lit(False)))
        .drop("_over_cap")
    )

    df_chunks = _dedupe_and_limit_chunks(df_chunks)

    is_recent = F.col("doc_date").isNull() | (F.col("doc_date") >= cutoff)
    df_chunks_archive = df_chunks.filter(~is_recent)
    df_chunks_all = df_chunks.filter(is_recent)

    logger.info("DataFrames ready (lazy). Will materialise during write.")
    return df_processed_files, df_chunks_all, df_chunks_archive


def build_archive_notices(df_business_meta) -> DataFrame:
    """One metadata-only chunk per pre-cutoff document: REF, title, revision,
    type, date and a "content not indexed" statement — never the content itself.

    A pre-cutoff document whose REF also exists on a recent document is skipped:
    the chatbot already knows that REF through real chunks.

    :param df_business_meta: Business metadata of the documents, with an ``IDDOC`` column.
    """
    cutoff = F.lit(DOC_DATE_CUTOFF).try_cast("date")
    is_archive = F.col("doc_date").isNotNull() & (F.col("doc_date") < cutoff)
    df_recent_refs = (
        df_business_meta.filter(~is_archive & F.col("ref").isNotNull())
        .select("ref").distinct()
    )

    def _line(label, col) -> Column:
        """One ``label : value`` line of the archive notice.

        :param label: Label of the line.
        :param col: Column with the value.
        :return: the line, with "inconnu" when the value is missing.
        """
        return F.concat(F.lit(f"{label} : "), F.coalesce(col.try_cast("string"), F.lit("inconnu")))

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
        .withColumn("chunk_text", worker.source_prefixed_text(
            body, F.col("ref"), F.col("titre"), F.col("division"), F.col("niveau_plus_1"),
            doc_date_col=F.col("doc_date"), include_prefix=EMBED_SOURCE_PREFIX,
        ))
        .select(
            "IDDOC",
            F.col("ref").alias("REF"),
            "division",
            # Real chunks start at -000001 (chunk_index 0): -000000 can never collide.
            F.concat(F.col("IDDOC").try_cast("string"), F.lit("-000000")).alias("chunk_id"),
            F.lit(-1).alias("chunk_index"),
            "chunk_text",
            worker.token_count_udf(F.col("chunk_text")).alias("chunk_token_count"),
            F.lit(ARCHIVE_NOTICE_CONTENT_TYPE).alias("chunk_content_type"),
            F.lit("{}").alias("semantic_headers"),
            F.sha2(F.col("chunk_text"), 256).alias("chunk_sha256"),
            worker.intraqual_ref_url(F.col("ref")).alias("url"),
            "doc_date",
        )
    )
