"""Phases of the `3_parse` task that write the tables: parsed_documents, chunks, chunks_archive, image_metadata, the
run health, the change log, the archive notices, and the ghost-document guard.

Driver-side only: nothing here is shipped to the executors.
"""

from databricks.sdk.runtime import spark
from delta.tables import DeltaTable
from pyspark.sql import functions as f

from leap_utils.common.logger import get_logger
from tables import (
    add_missing_columns,
    ensure_primary_key,
    merge_into,
    overwrite_rows,
    table_exists,
    write_table,
)
from config import (
    PIPELINE_WRITE_ENV,
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

logger = get_logger(__name__)

# Kept from the existing row when it is DONE, SKIPPED or FAILED_LIMIT: the description and what it cost.
_IMAGE_MERGE_TEMP = f"{PIPELINE_WRITE_ENV}_proj.qualibot._image_metadata_merge_temp"


def _merge_image_metadata(df_image_metadata) -> None:
    """Merge image_metadata instead of delete-then-append.

    The rows arrive with the descriptions `build_steps.build_image_metadata` decided to keep (same image only): the
    merge takes them as they are, a previous description must not come back on an image that moved.

    :param df_image_metadata: Image rows to write, with the descriptions to keep.
    """
    if not table_exists(TARGET_IMAGE_METADATA_TABLE):
        # First write into an empty catalog: the merge needs the table to exist.
        write_table(df_image_metadata, TARGET_IMAGE_METADATA_TABLE, "append", merge_schema=True)
        logger.info(f"Created {TARGET_IMAGE_METADATA_TABLE} (first write)")
        return
    add_missing_columns(df_image_metadata, TARGET_IMAGE_METADATA_TABLE)
    # The source is saved first: it was built from the table it is merged into.
    write_table(df_image_metadata, _IMAGE_MERGE_TEMP, "overwrite", overwrite_schema=True)
    merge_into(spark.table(_IMAGE_MERGE_TEMP), TARGET_IMAGE_METADATA_TABLE, ["IDDOC", "image_id"])
    spark.sql(f"DROP TABLE IF EXISTS {_IMAGE_MERGE_TEMP}")
    logger.info(f"Merged into {TARGET_IMAGE_METADATA_TABLE}")


def _write_run_health_summary(df_processed_files, job_run_id, run_mode) -> None:
    """One row per pipeline run in TARGET_HEALTH_TABLE — REF/error counts for the
    monitoring dashboard. Scoped to this run's df_processed_files: a FULL run's
    row covers the whole corpus, not just net-new docs vs the prior run.

    :param df_processed_files: Documents of this run, one row each.
    :param job_run_id: Id of the Databricks job run.
    :param run_mode: Run mode: ``incremental``, ``full`` or ``rechunk``.
    """
    status = f.col("parse_status")
    is_success = status == "SUCCESS"
    is_skipped = status.startswith("SKIP")
    is_error = ~is_success & ~is_skipped

    counts = df_processed_files.agg(
        f.count("*").alias("total_docs"),
        f.sum(is_success.try_cast("int")).alias("docs_success"),
        f.sum(is_skipped.try_cast("int")).alias("docs_skipped"),
        f.sum(is_error.try_cast("int")).alias("docs_error"),
        f.sort_array(f.collect_set(f.when(is_success, f.col("ref")))).alias("refs_success"),
        f.sum("image_count").alias("total_image_count"),
        f.avg("parse_time_seconds").alias("avg_parse_time_seconds"),
        f.sum("parse_time_seconds").alias("total_parse_time_seconds"),
    )
    by_status = (
        df_processed_files.groupBy("parse_status")
        .count()
        .agg(
            f.map_from_entries(f.collect_list(f.struct("parse_status", "count"))).alias(
                "docs_by_status"
            )
        )
    )
    errors = df_processed_files.filter(is_error).agg(
        f.collect_list(f.struct("IDDOC", "ref", "parse_status", "error_trace")).alias("errors")
    )

    df_health = (
        counts.crossJoin(by_status)
        .crossJoin(errors)
        .withColumn("job_run_id", f.lit(job_run_id))
        .withColumn("run_mode", f.lit(run_mode))
        .withColumn("run_date", f.current_date())
        .withColumn("written_at", f.current_timestamp())
    )
    write_table(df_health, TARGET_HEALTH_TABLE, "append", merge_schema=True)
    logger.info(f"Wrote run health summary to {TARGET_HEALTH_TABLE}")


def log_document_changes(df_business_meta, target_iddocs, revised_iddocs, job_run_id) -> None:
    """Append one row per NEW/REVISED document to TARGET_CHANGE_LOG_TABLE -- must run before parsed_documents gets rewritten below, since it reads the pre-run row for old ref/indice/doc_date.

    :param df_business_meta: Business metadata of the documents, with an ``IDDOC`` column.
    :param target_iddocs: IDDOCs parsed in this run.
    :param revised_iddocs: IDDOCs whose revision changed in this run.
    :param job_run_id: Id of the Databricks job run.
    """
    if not target_iddocs:
        return

    df_target = df_business_meta.filter(f.col("IDDOC").isin(list(target_iddocs)))

    try:
        df_existing = (
            spark.table(TARGET_PROCESSED_FILES_TABLE)
            .select(
                f.col("IDDOC"),
                f.col("ref").alias("old_ref"),
                f.col("indice").alias("old_indice"),
                f.col("doc_date").alias("old_doc_date"),
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
            f.when(f.col("old_ref").isNull(), f.lit("NEW"))
            .when(f.col("IDDOC").isin(list(revised_iddocs)), f.lit("REVISED"))
            .otherwise(f.lit(None)),
        )
        .filter(f.col("event_type").isNotNull())
        .withColumns({"job_run_id": f.lit(job_run_id), "logged_at": f.current_timestamp()})
        .withColumnRenamed("indice", "new_indice")
        .withColumnRenamed("doc_date", "new_doc_date")
        .select(
            "IDDOC",
            "ref",
            "event_type",
            "old_ref",
            "old_indice",
            "new_indice",
            "old_doc_date",
            "new_doc_date",
            "job_run_id",
            "logged_at",
        )
    )

    try:
        # A repaired run keeps its job run id: the changes it already logged are not logged twice.
        df_logged = (
            spark.table(TARGET_CHANGE_LOG_TABLE)
            .filter(f.col("job_run_id") == job_run_id)
            .select("IDDOC", "event_type", "job_run_id")
            .distinct()
        )
        df_log = df_log.join(df_logged, on=["IDDOC", "event_type", "job_run_id"], how="left_anti")
    except Exception:
        logger.info(
            f"[CHANGE LOG] No existing {TARGET_CHANGE_LOG_TABLE} to check for repeated rows."
        )

    n = df_log.count()
    if n == 0:
        logger.info("[CHANGE LOG] No new/revised documents to log this run.")
        return

    write_table(df_log, TARGET_CHANGE_LOG_TABLE, "append", merge_schema=True)
    logger.info(
        f"[CHANGE LOG] Logged {n} document change(s) (NEW/REVISED) to {TARGET_CHANGE_LOG_TABLE}"
    )


def write_outputs(
    df_processed_files,
    df_chunks_all,
    df_chunks_archive,
    df_image_metadata,
    job_run_id,
    revised_iddocs=frozenset(),
) -> None:
    """Write parsed_documents/chunks/image_metadata — overwrite on a full or
    rechunk run, append (+ MERGE for image_metadata) on an incremental run.

    :param df_processed_files: Documents of this run, one row each, for ``parsed_documents``.
    :param df_chunks_all: Chunks for the chat table.
    :param df_chunks_archive: Chunks of the documents published before ``DOC_DATE_CUTOFF``.
    :param df_image_metadata: Image rows to write.
    :param job_run_id: Id of the Databricks job run.
    :param revised_iddocs: IDDOCs whose revision changed; their old image rows are cleared.
    """
    spark.conf.set("spark.databricks.delta.optimizeWrite.enabled", "true")
    spark.conf.set("spark.databricks.delta.autoCompact.enabled", "true")

    if RUN_MODE in ("full", "rechunk"):
        write_table(
            df_processed_files, TARGET_PROCESSED_FILES_TABLE, "overwrite", overwrite_schema=True
        )
        logger.info(f"Wrote {TARGET_PROCESSED_FILES_TABLE}")

        overwrite_rows(df_chunks_all, TARGET_CHUNK_TABLE)
        ensure_primary_key(TARGET_CHUNK_TABLE, ["chunk_id"])
        logger.info(f"Wrote {TARGET_CHUNK_TABLE} (ALL)")

        write_table(
            df_chunks_archive, TARGET_CHUNK_TABLE_ARCHIVE, "overwrite", overwrite_schema=True
        )
        logger.info(f"Wrote {TARGET_CHUNK_TABLE_ARCHIVE} (pre-{DOC_DATE_CUTOFF})")

        write_table(
            df_image_metadata, TARGET_IMAGE_METADATA_TABLE, "overwrite", overwrite_schema=True
        )
        ensure_primary_key(TARGET_IMAGE_METADATA_TABLE, ["IDDOC", "image_id"])
        logger.info(f"Wrote {TARGET_IMAGE_METADATA_TABLE}")

        logger.info("[FULL] All tables overwritten.")

    elif RUN_MODE == "incremental":
        new_iddocs = [r.IDDOC for r in df_processed_files.select("IDDOC").distinct().collect()]
        if new_iddocs:
            iddoc_list = ",".join(str(i) for i in new_iddocs)
            for tbl in [
                TARGET_CHUNK_TABLE,
                TARGET_CHUNK_TABLE_ARCHIVE,
                TARGET_PROCESSED_FILES_TABLE,
            ]:
                if table_exists(tbl):
                    spark.sql(f"DELETE FROM {tbl} WHERE IDDOC IN ({iddoc_list})")
            logger.info(
                f"Cleaned old rows for {len(new_iddocs)} re-processed IDDOCs (chunks + parsed_documents)"
            )

        write_table(df_processed_files, TARGET_PROCESSED_FILES_TABLE, "append", merge_schema=True)
        logger.info(f"Appended to {TARGET_PROCESSED_FILES_TABLE}")

        write_table(df_chunks_all, TARGET_CHUNK_TABLE, "append", merge_schema=True)
        logger.info(f"Appended to {TARGET_CHUNK_TABLE} (ALL)")

        write_table(df_chunks_archive, TARGET_CHUNK_TABLE_ARCHIVE, "append", merge_schema=True)
        logger.info(f"Appended to {TARGET_CHUNK_TABLE_ARCHIVE} (pre-{DOC_DATE_CUTOFF})")

        # Revised IDDOCs have new content: their old image rows must go, or the merge keeps them.
        if revised_iddocs:
            revised_list = ",".join(str(i) for i in revised_iddocs)
            if table_exists(TARGET_IMAGE_METADATA_TABLE):
                spark.sql(
                    f"DELETE FROM {TARGET_IMAGE_METADATA_TABLE} WHERE IDDOC IN ({revised_list})"
                )
                logger.info(
                    f"Cleared stale image_metadata for {len(revised_iddocs)} revised IDDOC(s) before merge."
                )

        _merge_image_metadata(df_image_metadata)
        logger.info("[INCREMENTAL] New rows appended (stale failures cleaned).")

    # CDF required for Vector Search index sync; 60 days of history so a few failed daily runs never age out
    # the CDF the index resumes from.
    try:
        spark.sql(f"""ALTER TABLE {TARGET_CHUNK_TABLE} SET TBLPROPERTIES (
            delta.enableChangeDataFeed = true,
            delta.deletedFileRetentionDuration = 'interval 60 days',
            delta.logRetentionDuration = 'interval 60 days')""")
        logger.info(f"CDF and 60-day history set on {TARGET_CHUNK_TABLE}")
    except Exception as e:
        logger.warning(f"CDF not set on {TARGET_CHUNK_TABLE}: {e}")

    _write_run_health_summary(df_processed_files, job_run_id, RUN_MODE)

    logger.info("[NEXT] Run create_proj_image_metadata to describe PENDING images.")


def write_archive_notices(df_notices) -> None:
    """Rewrite TARGET_ARCHIVE_NOTICE_TABLE, then reconcile the notices held by the
    chat's `chunks` table: MERGEd in when ARCHIVE_NOTICES_IN_RAG is on, removed otherwise.
    MERGE (not delete + append) so Change Data Feed only carries real changes.

    :param df_notices: One archive notice per document published before the cutoff.
    """
    write_table(df_notices, TARGET_ARCHIVE_NOTICE_TABLE, "overwrite", overwrite_schema=True)
    df_notices = spark.table(TARGET_ARCHIVE_NOTICE_TABLE)
    logger.info(
        f"[NOTICES] Wrote {df_notices.count()} archive notice(s) to {TARGET_ARCHIVE_NOTICE_TABLE} "
        f"(in RAG tables: {ARCHIVE_NOTICES_IN_RAG})"
    )

    is_notice = f"chunk_content_type = '{ARCHIVE_NOTICE_CONTENT_TYPE}'"
    for tbl in [TARGET_CHUNK_TABLE]:
        if not table_exists(tbl):
            continue
        if not ARCHIVE_NOTICES_IN_RAG:
            if spark.table(tbl).filter(is_notice).limit(1).count():
                spark.sql(f"DELETE FROM {tbl} WHERE {is_notice}")
                logger.info(f"[NOTICES] Removed archive notices from {tbl}")
            continue
        df_src = df_notices
        # Align on the target's own columns/types (it may hold columns notices don't have).
        df_src = df_src.select(
            *[
                (f.col(field.name) if field.name in df_notices.columns else f.lit(None))
                .try_cast(field.dataType)
                .alias(field.name)
                for field in spark.table(tbl).schema
            ]
        )
        # leap_utils merge_into cannot delete the rows of the target that the source lacks under a condition: this merge stays on DeltaTable.
        (
            DeltaTable.forName(spark, tbl)
            .alias("t")
            .merge(df_src.alias("s"), "t.chunk_id = s.chunk_id")
            .whenMatchedUpdateAll(condition="NOT (t.chunk_sha256 <=> s.chunk_sha256)")
            .whenNotMatchedInsertAll()
            .whenNotMatchedBySourceDelete(condition=f"t.{is_notice}")
            .execute()
        )
        logger.info(f"[NOTICES] Merged archive notices into {tbl}")


def flag_ghost_documents() -> None:
    """Flag SUCCESS documents that ended up with zero chunks as MISSING_CHUNKS.

    Runs right after the write instead of waiting for the daily SQL alert;
    the next incremental run re-parses the flagged documents.
    """
    df_ghosts = (
        spark.table(TARGET_PROCESSED_FILES_TABLE)
        .filter(
            (f.col("parse_status") == "SUCCESS")
            & (f.col("is_in_rag") == True)
            & (f.col("is_filtered_by_date") == False)
        )
        .join(
            spark.table(TARGET_CHUNK_TABLE).select("IDDOC").distinct(), on="IDDOC", how="left_anti"
        )
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
