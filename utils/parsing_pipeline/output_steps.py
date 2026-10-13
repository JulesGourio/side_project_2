"""Phases of the `3_parse` task that write the tables: processed_files, chunks, chunks_archive, image_metadata, the
run health, the change log, the archive notices, and the ghost-document guard.

Driver-side only: nothing here is shipped to the executors.
"""
from databricks.sdk.runtime import spark
from delta.tables import DeltaTable
from pyspark.sql import functions as F

from tables import write_table
from utils import logger
from config import (
    ARCHIVE_NOTICES_IN_RAG,
    ARCHIVE_NOTICE_CONTENT_TYPE,
    DOC_DATE_CUTOFF,
    RUN_MODE,
    TARGET_ARCHIVE_NOTICE_TABLE,
    TARGET_CHANGE_LOG_TABLE,
    TARGET_CHUNK_TABLE,
    TARGET_CHUNK_TABLE_ARCHIVE,
    TARGET_HEALTH_TABLE,
    TARGET_IMAGE_METADATA_TABLE,
    TARGET_PROCESSED_FILES_TABLE,
)


def _merge_image_metadata(df_image_metadata):
    """MERGE image_metadata instead of delete-then-append: status/description/
    tokens only take the fresh value when the existing row isn't already
    DONE/SKIPPED."""
    if not spark.catalog.tableExists(TARGET_IMAGE_METADATA_TABLE):
        # First-ever write on a fresh catalog (e.g. a brand-new environment with
        # no promoted checkpoint) — DeltaTable.forName() requires the table to
        # already exist, so there's nothing to MERGE into yet.
        write_table(df_image_metadata, TARGET_IMAGE_METADATA_TABLE, "append", merge_schema=True)
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
        F.sum(is_success.try_cast("int")).alias("docs_success"),
        F.sum(is_skipped.try_cast("int")).alias("docs_skipped"),
        F.sum(is_error.try_cast("int")).alias("docs_error"),
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

    df_health = (
        counts.crossJoin(by_status).crossJoin(errors)
        .withColumn("job_run_id", F.lit(job_run_id))
        .withColumn("run_mode", F.lit(run_mode))
        .withColumn("run_date", F.current_date())
        .withColumn("written_at", F.current_timestamp())
    )
    write_table(df_health, TARGET_HEALTH_TABLE, "append", merge_schema=True)
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

    write_table(df_log, TARGET_CHANGE_LOG_TABLE, "append", merge_schema=True)
    logger.info(f"[CHANGE LOG] Logged {n} document change(s) (NEW/REVISED) to {TARGET_CHANGE_LOG_TABLE}")


def write_outputs(df_processed_files, df_chunks_all, df_chunks_archive,
                  df_image_metadata, job_run_id, revised_iddocs=frozenset()):
    """Write processed_files/chunks/image_metadata — overwrite on a full or
    rechunk run, append (+ MERGE for image_metadata) on an incremental run."""
    spark.conf.set("spark.databricks.delta.optimizeWrite.enabled", "true")
    spark.conf.set("spark.databricks.delta.autoCompact.enabled", "true")

    if RUN_MODE in ("full", "rechunk"):
        write_table(df_processed_files, TARGET_PROCESSED_FILES_TABLE, "overwrite", overwrite_schema=True)
        logger.info(f"Wrote {TARGET_PROCESSED_FILES_TABLE}")

        write_table(df_chunks_all, TARGET_CHUNK_TABLE, "overwrite", overwrite_schema=True)
        logger.info(f"Wrote {TARGET_CHUNK_TABLE} (ALL)")

        write_table(df_chunks_archive, TARGET_CHUNK_TABLE_ARCHIVE, "overwrite", overwrite_schema=True)
        logger.info(f"Wrote {TARGET_CHUNK_TABLE_ARCHIVE} (pre-{DOC_DATE_CUTOFF})")

        write_table(df_image_metadata, TARGET_IMAGE_METADATA_TABLE, "overwrite", overwrite_schema=True)
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

        write_table(df_processed_files, TARGET_PROCESSED_FILES_TABLE, "append", merge_schema=True)
        logger.info(f"Appended to {TARGET_PROCESSED_FILES_TABLE}")

        write_table(df_chunks_all, TARGET_CHUNK_TABLE, "append", merge_schema=True)
        logger.info(f"Appended to {TARGET_CHUNK_TABLE} (ALL)")

        write_table(df_chunks_archive, TARGET_CHUNK_TABLE_ARCHIVE, "append", merge_schema=True)
        logger.info(f"Appended to {TARGET_CHUNK_TABLE_ARCHIVE} (pre-{DOC_DATE_CUTOFF})")

        # Revised IDDOCs have new content, so _merge_image_metadata's keep_prev logic must not preserve their old image rows.
        if revised_iddocs:
            revised_list = ",".join(str(i) for i in revised_iddocs)
            if spark.catalog.tableExists(TARGET_IMAGE_METADATA_TABLE):
                spark.sql(f"DELETE FROM {TARGET_IMAGE_METADATA_TABLE} WHERE IDDOC IN ({revised_list})")
                logger.info(f"Cleared stale image_metadata for {len(revised_iddocs)} revised IDDOC(s) before merge.")

        _merge_image_metadata(df_image_metadata)
        logger.info("[INCREMENTAL] New rows appended (stale failures cleaned).")

    # CDF required for Vector Search index sync; 60 days of history so a few failed daily runs never age out
    # the CDF the index resumes from (the _v3 -> _v1 incident, utils/parsing_pipeline/README.md).
    try:
        spark.sql(f"""ALTER TABLE {TARGET_CHUNK_TABLE} SET TBLPROPERTIES (
            delta.enableChangeDataFeed = true,
            delta.deletedFileRetentionDuration = 'interval 60 days',
            delta.logRetentionDuration = 'interval 60 days')""")
        logger.info(f"CDF and 60-day history set on {TARGET_CHUNK_TABLE}")
    except Exception as e:
        logger.warning(f"CDF not set on {TARGET_CHUNK_TABLE}: {e}")

    _write_run_health_summary(df_processed_files, job_run_id, RUN_MODE)

    logger.info("[NEXT] Run 4_Describe_Images_LLM to describe PENDING images.")


def write_archive_notices(df_notices):
    """Rewrite TARGET_ARCHIVE_NOTICE_TABLE, then reconcile the notices held by the
    chat's `chunks` table: MERGEd in when ARCHIVE_NOTICES_IN_RAG is on, removed otherwise.
    MERGE (not delete + append) so Change Data Feed only carries real changes."""
    write_table(df_notices, TARGET_ARCHIVE_NOTICE_TABLE, "overwrite", overwrite_schema=True)
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
            (F.col(f.name) if f.name in df_notices.columns else F.lit(None)).try_cast(f.dataType).alias(f.name)
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
