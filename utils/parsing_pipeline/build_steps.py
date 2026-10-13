"""Phases of the `3_parse` task that build the tables: image_metadata, processed_files and the chunks.

Everything here returns lazy DataFrames from the parse checkpoint; the writes are in `output_steps.py`.
Driver-side only: nothing here is shipped to the executors.
"""
from databricks.sdk.runtime import spark
from pyspark import StorageLevel
from pyspark.sql import Window
from pyspark.sql import functions as F

import image_utils
import utils
from parse_steps import read_checkpoint_deduped
from utils import logger
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
)

_SPREADSHEET_EXTS = ("xlsx", "xls", "xlsm")


def with_fresh_business_metadata(df_checkpoint):
    """Replace checkpoint's business-metadata columns with fresh values,
    falling back to the checkpoint's own value when an IDDOC isn't in scope.
    division/niveau_plus_* come from DIVISION_REFERENCE_TABLE, not
    parse_manifest, which goes stale.
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


def build_image_metadata(target_iddocs, ingestion_run_id):
    """Explode checkpoint rows' `images` array into one row per image,
    preserving any already-DONE/SKIPPED description across a rebuild."""
    df_full_pipeline = read_checkpoint_deduped()
    # rechunk: only the documents in scope — the checkpoint also keeps superseded revisions.
    if RUN_MODE in ("incremental", "rechunk"):
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
            F.lit(ingestion_run_id).alias("ingestion_run_id"),
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

    # Same body in BOILERPLATE_MIN_DOCS+ documents (legal mentions, standard approval blocks): marked, not deleted, so
    # the search can leave them out.
    # Counted with the rows already in the table on an incremental run, so a new document's copy is caught too.
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


def build_processed_files_and_chunks(target_iddocs, df_image_metadata, job_run_id, revised_iddocs=frozenset()):
    """Build processed_files (one row per parsed document) and the three
    chunk DataFrames (all divisions / AS / IS) from the checkpoint."""
    df_full_pipeline = read_checkpoint_deduped()
    # rechunk: only the documents in scope — the checkpoint also keeps superseded revisions.
    if RUN_MODE in ("incremental", "rechunk"):
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
                F.greatest(F.lit(1), F.floor(F.length(F.col("document_text")) / F.lit(CHARS_PER_TOKEN)).try_cast("int"))
            ).otherwise(F.lit(0)),
            "images_needing_description": F.coalesce(F.col("images_needing_description"), F.lit(0)),
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

    # Persisted: the chunking UDF is the expensive part, and every count/filter/write below would rerun it.
    df_chunks = _build_chunks(df_full_pipeline, df_exclusions).persist(StorageLevel.MEMORY_AND_DISK)

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
        .withColumn("chunk_text", utils.source_prefixed_text(
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
            utils.token_count_udf(F.col("chunk_text")).alias("chunk_token_count"),
            F.lit(ARCHIVE_NOTICE_CONTENT_TYPE).alias("chunk_content_type"),
            F.lit("{}").alias("semantic_headers"),
            F.sha2(F.col("chunk_text"), 256).alias("chunk_sha256"),
            utils.intraqual_ref_url(F.col("ref")).alias("url"),
            "doc_date",
        )
    )
