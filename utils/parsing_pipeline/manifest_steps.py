"""Phases of the `2_manifest` task (see `2_Cleanup_Volume.py`).

Reconciles the derived tables and the volume root with the current Qualibot scope, then builds `parse_manifest`.
Driver side only: nothing here is shipped to the executors.
"""
import re
from datetime import datetime

from databricks.sdk.runtime import dbutils, spark
from pyspark.sql import Window
from pyspark.sql import functions as F
from pyspark.sql.types import LongType, StringType, StructField, StructType

import selection
from utils import logger
from config import (
    ARCHIVE_MAX_DOCS,
    DIVISION_REFERENCE_TABLE,
    DOC_DATE_CUTOFF,
    EXCLUDED_IDCATS,
    GD_DOC_FALLBACK,
    GD_DOC_LATEST,
    GD_TYPDOC_LATEST,
    MANUAL_REF_EXCLUSIONS,
    PARSE_MANIFEST_TABLE,
    TARGET_CHUNK_TABLE,
    TARGET_CHUNK_TABLE_ARCHIVE,
    TARGET_IMAGE_METADATA_TABLE,
    TARGET_PROCESSED_FILES_TABLE,
)

# Root folders are named D_<IDDOC> or Dm_<IDDOC>; anything else cannot give an IDDOC.
IDDOC_RE = re.compile(r"^[Dd]m?_(\d+)", re.IGNORECASE)

_DERIVED_TABLES = (TARGET_CHUNK_TABLE, TARGET_CHUNK_TABLE_ARCHIVE, TARGET_IMAGE_METADATA_TABLE)
_STATUSES_TO_PRUNE = ("SUCCESS", "ERROR", "EMPTY_TEXT")
# A row with one of these statuses blocks the IDDOC from being parsed, so it must go when the IDDOC re-enters scope.
_PERMANENT_SKIP_STATUSES = (
    "SKIPPED_REF_OUT_OF_SCOPE", "SKIPPED_IDDOC_NOT_FOUND", "SKIPPED_REF_MANUAL",
    "SKIPPED_SOURCE_REMOVED", "SKIPPED_EMPTY_FOLDER", "SKIPPED_CATEGORY_EXCLUDED",
)


def _id_list(iddocs):
    return ",".join(str(i) for i in iddocs)


def _delete_iddocs(tables, iddocs):
    """DELETE the IDDOCs from every table that exists; a failing table is logged, the others still run."""
    id_list = _id_list(iddocs)
    for table in tables:
        try:
            spark.sql(f"DELETE FROM {table} WHERE IDDOC IN ({id_list})")
        except Exception as exc:
            logger.warning(f"Could not prune {table}: {exc}")


def doc_rows(table):
    """IDDOC -> (ref, title) for every known document of a Intraqual document table."""
    return {
        r.IDDOC: (r.ref, r.titre)
        for r in (
            spark.read.table(table)
            .select(F.col("IDDOC"), F.col("REF").alias("ref"), F.col("TITRE").alias("titre"))
            .filter(F.col("IDDOC").isNotNull())
            .dropDuplicates(["IDDOC"])
            .collect()
        )
    }


def find_stale_iddocs(df_scope_docs):
    """Previously parsed IDDOCs that are no longer in scope (SKIPPED_* rows are reconciled separately)."""
    try:
        return [
            r.IDDOC for r in
            spark.table(TARGET_PROCESSED_FILES_TABLE)
            .filter(F.col("parse_status").isin(*_STATUSES_TO_PRUNE))
            .select("IDDOC").distinct()
            .join(df_scope_docs.select("IDDOC"), on="IDDOC", how="left_anti")
            .collect()
        ]
    except Exception as exc:
        logger.info(f"No existing {TARGET_PROCESSED_FILES_TABLE} to prune against ({exc}).")
        return []


def prune_stale_content(stale_iddocs):
    """A new revision gets a new IDDOC: when the old one leaves scope, its derived rows are removed."""
    if not stale_iddocs:
        logger.info("Nothing to prune - no indexed IDDOC has left scope since the last run.")
        return
    _delete_iddocs(_DERIVED_TABLES, stale_iddocs)
    spark.sql(
        f"DELETE FROM {TARGET_PROCESSED_FILES_TABLE} "
        f"WHERE parse_status IN {_STATUSES_TO_PRUNE} AND IDDOC IN ({_id_list(stale_iddocs)})"
    )
    logger.info(f"Pruned {len(stale_iddocs)} out-of-scope IDDOCs from chunks/image_metadata/processed_files.")


def prune_stale_revisions():
    """Remove the chunks of older revisions of REFs that still have several in-scope IDDOCs.

    Keeps the highest `indice` per REF (highest IDDOC as tie-break). `processed_files` is left untouched on purpose:
    deleting its row would make the old IDDOC eligible for parsing again on the next run.
    """
    if not spark.catalog.tableExists(TARGET_CHUNK_TABLE):
        logger.warning(f"{TARGET_CHUNK_TABLE} does not exist yet - revision pruning skipped.")
        return

    df_chunk_iddocs = spark.table(TARGET_CHUNK_TABLE).select("REF", "IDDOC").distinct()
    df_dup_refs = (
        df_chunk_iddocs.groupBy("REF").agg(F.count("IDDOC").alias("n")).filter(F.col("n") > 1).select("REF")
    )
    n_dup_refs = df_dup_refs.count()
    if n_dup_refs == 0:
        logger.info("No REF with multiple IDDOCs in chunks - nothing to prune.")
        return

    newest_first = Window.partitionBy("REF").orderBy(F.desc("_gd_indice"), F.desc("IDDOC"))
    stale_revision_iddocs = [
        r.IDDOC for r in
        df_chunk_iddocs
        .join(df_dup_refs, on="REF", how="inner")
        .join(spark.table(GD_DOC_LATEST).select("IDDOC", F.col("indice").alias("_gd_indice")), on="IDDOC", how="left")
        .withColumn("_rn", F.row_number().over(newest_first))
        .filter(F.col("_rn") > 1)
        .select("IDDOC").distinct().collect()
    ]
    if not stale_revision_iddocs:
        logger.info("No revision duplicates found in chunks.")
        return

    _delete_iddocs(_DERIVED_TABLES, stale_revision_iddocs)
    logger.info(
        f"Pruned {len(stale_revision_iddocs)} stale-revision IDDOC(s) across {n_dup_refs} REF(s) from chunks and "
        f"image_metadata (processed_files rows kept to prevent a re-parse loop)."
    )


def classify_root_folders(root_items, scope_iddocs, iddoc_to_ref, iddoc_to_titre, iddoc_to_cat, iddoc_to_idcat):
    """Sort the volume root folders into KEEP / DELETE / ORPHAN / SKIP / MANUAL / CAT_EXCL.

    KEEP: in scope. DELETE: known but out of scope. ORPHAN: IDDOC unknown in gd_doc. SKIP: name is not D_/Dm_.
    MANUAL: REF excluded by configuration. CAT_EXCL: in scope but its principal category is excluded.
    """
    rows = {"keep": [], "delete": [], "orphan": [], "skip": [], "manual": [], "cat_excl": []}
    for item in root_items:
        name = item.name.rstrip("/")
        match = IDDOC_RE.match(name)
        if not match:
            rows["skip"].append({"name": name, "path": item.path, "reason": "NOM_ATYPIQUE"})
            continue

        iddoc = int(match.group(1))
        ref = iddoc_to_ref.get(iddoc)
        known = {"name": name, "IDDOC": iddoc, "ref": ref, "titre": iddoc_to_titre.get(iddoc),
                 "categorie": iddoc_to_cat.get(iddoc)}
        excluded = {**known, "path": item.path}

        if ref is None:
            rows["orphan"].append({"name": name, "IDDOC": iddoc, "path": item.path,
                                   "reason": "ORPHAN_IDDOC_NOT_IN_GD_DOC"})
        elif ref in MANUAL_REF_EXCLUSIONS:
            rows["manual"].append({**excluded, "reason": "REF_MANUAL_EXCLUSION"})
        elif iddoc not in scope_iddocs:
            rows["delete"].append({**excluded, "reason": "IDDOC_OUT_OF_SCOPE"})
        elif iddoc_to_idcat.get(iddoc) in EXCLUDED_IDCATS:
            rows["cat_excl"].append({**excluded, "reason": "CATEGORY_EXCLUDED"})
        else:
            rows["keep"].append(known)

    logger.info(f"KEEP   : {len(rows['keep']):>5}")
    logger.info(f"DELETE : {len(rows['delete']):>5}  (out of scope - deleted only if DRY_RUN=non)")
    logger.info(f"ORPHAN : {len(rows['orphan']):>5}  (unknown IDDOC - kept, excluded from parsing)")
    logger.info(f"SKIP   : {len(rows['skip']):>5}  (non-conforming names - untouched)")
    logger.info(f"MANUAL : {len(rows['manual']):>5}  (REF in MANUAL_REF_EXCLUSIONS)")
    logger.info(f"CATEXCL: {len(rows['cat_excl']):>5}  (IDCAT in EXCLUDED_IDCATS)")
    return rows


def remove_stale_skip_rows(rows_keep):
    """Delete the permanent-skip rows of IDDOCs that are back in scope, or they would never be parsed again.

    FILTERED_BY_DATE is not in the list: it can still coexist with KEEP.
    """
    stale_skip_iddocs = set()
    try:
        if rows_keep and spark.catalog.tableExists(TARGET_PROCESSED_FILES_TABLE):
            df_keep = spark.createDataFrame([(r["IDDOC"],) for r in rows_keep], "IDDOC long")
            stale_skip_iddocs = {
                r.IDDOC for r in
                spark.table(TARGET_PROCESSED_FILES_TABLE)
                .filter(F.col("parse_status").isin(*_PERMANENT_SKIP_STATUSES))
                .join(F.broadcast(df_keep), on="IDDOC", how="inner")
                .select("IDDOC").distinct().collect()
            }
    except Exception as exc:
        logger.warning(f"Un-skip check failed: {exc}")

    if not stale_skip_iddocs:
        logger.info("Nothing to un-skip.")
        return
    spark.sql(f"DELETE FROM {TARGET_PROCESSED_FILES_TABLE} WHERE IDDOC IN ({_id_list(stale_skip_iddocs)})")
    logger.info(f"Un-skipped {len(stale_skip_iddocs)} IDDOC(s) that re-entered scope "
                f"(stale permanent-skip row removed, will be picked up by 3_parse).")


def reconcile_processed_files(classified, known_iddocs, job_run_id):
    """Record in processed_files the documents that must not be parsed.

    The table keeps one current row per IDDOC, so each reclassification replaces the previous status.
    """
    run_id = f"cleanup_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    run_ts = datetime.now()

    def skipped_rows(items, status):
        return [{
            "IDDOC": r.get("IDDOC"),
            "source_path": r["path"].replace("dbfs:", ""),
            "source_file_name": r["name"],
            "ref": r.get("ref"),
            "titre": r.get("titre"),
            "categorie": r.get("categorie"),
            "parse_status": status,
            # Downstream monitoring explains why a document was excluded from this reason.
            "removal_reason": r.get("reason"),
            "include_in_rag": False,
            "filtered_by_date": False,
            "ingestion_run_id": run_id,
            "ingestion_timestamp": run_ts,
            "job_run_id": job_run_id,
        } for r in items]

    all_skipped = (
        skipped_rows(classified["delete"], "SKIPPED_REF_OUT_OF_SCOPE")
        + skipped_rows(classified["orphan"], "SKIPPED_IDDOC_NOT_FOUND")
        + skipped_rows(classified["manual"], "SKIPPED_REF_MANUAL")
        + skipped_rows(classified["cat_excl"], "SKIPPED_CATEGORY_EXCLUDED")
    )

    # Documents recorded earlier that vanished from both the volume and gd_doc. The anti-join cannot tell a
    # source-side deletion from another cause of disappearance.
    folder_iddocs = {r["IDDOC"] for key in ("keep", "delete", "orphan", "manual", "cat_excl") for r in classified[key]}
    try:
        existing_iddocs = {
            r.IDDOC for r in
            spark.table(TARGET_PROCESSED_FILES_TABLE).filter(F.col("IDDOC").isNotNull()).select("IDDOC").distinct().collect()
        }
    except Exception:
        existing_iddocs = set()
    all_skipped += [{
        "IDDOC": iddoc, "source_path": None, "source_file_name": None, "ref": None, "titre": None,
        "categorie": None, "parse_status": "SKIPPED_SOURCE_REMOVED", "removal_reason": "NOT_IN_VOLUME_LISTING",
        "include_in_rag": False, "filtered_by_date": False, "ingestion_run_id": run_id,
        "ingestion_timestamp": run_ts, "job_run_id": job_run_id,
    } for iddoc in existing_iddocs - folder_iddocs - set(known_iddocs)]

    if not all_skipped:
        logger.info("Nothing to log into processed_files.")
        return
    if not spark.catalog.tableExists(TARGET_PROCESSED_FILES_TABLE):
        logger.warning(f"{TARGET_PROCESSED_FILES_TABLE} does not exist yet - run 3_Parse_Pipeline (FULL) first. "
                       f"No write performed.")
        return

    reclassified = {r["IDDOC"] for r in all_skipped if r.get("IDDOC") is not None}
    try:
        if reclassified:
            spark.sql(f"DELETE FROM {TARGET_PROCESSED_FILES_TABLE} WHERE IDDOC IN ({_id_list(reclassified)})")
        (
            spark.createDataFrame(all_skipped).write.format("delta").mode("append")
            .option("mergeSchema", "true").saveAsTable(TARGET_PROCESSED_FILES_TABLE)
        )
    except Exception as exc:
        logger.error(f"Error writing processed_files (run {run_id}): {exc}")
        return

    logger.info(f"{TARGET_PROCESSED_FILES_TABLE} updated ({run_id}):")
    for status in ("SKIPPED_REF_OUT_OF_SCOPE", "SKIPPED_IDDOC_NOT_FOUND", "SKIPPED_REF_MANUAL",
                   "SKIPPED_SOURCE_REMOVED", "SKIPPED_CATEGORY_EXCLUDED"):
        logger.info(f"   {status:<27}: {sum(1 for r in all_skipped if r['parse_status'] == status)}")


def archive_condition():
    """Pre-cutoff documents: parsed within the ARCHIVE_MAX_DOCS budget and routed to chunks_archive."""
    return F.col("doc_date").isNotNull() & (F.col("doc_date") < F.lit(DOC_DATE_CUTOFF).cast("date"))


def _doc_date_column(table):
    cols = {c.lower() for c in spark.read.table(table).columns}
    source = "dtdiff" if "dtdiff" in cols else "DATEDIFF" if "datediff" in cols else None
    return F.to_date(F.col(source)) if source else F.lit(None).cast("date")


def build_manifest(rows_keep):
    """One row per KEEP document with its revision, date, hierarchy and type.

    Does not reuse `selection.load_business_metadata`: that helper keeps only `COURANT > 0`, while the manifest
    must hold every in-scope KEEP IDDOC. `indice` is the revision key `3_parse` compares across runs.
    """
    df_keep = spark.createDataFrame(
        [(r["IDDOC"], r["ref"], r["titre"], r["categorie"]) for r in rows_keep],
        schema=StructType([
            StructField("IDDOC", LongType()), StructField("ref", StringType()),
            StructField("titre", StringType()), StructField("categorie", StringType()),
        ]),
    )

    df_dates = (
        spark.read.table(GD_DOC_LATEST)
        .select(F.col("IDDOC"), _doc_date_column(GD_DOC_LATEST).alias("doc_date"),
                F.col("INDICE").cast("string").alias("indice"))
        .filter(F.col("IDDOC").isNotNull())
        .dropDuplicates(["IDDOC"])
    )
    if (GD_DOC_FALLBACK or "").strip():
        df_dates_fallback = (
            spark.read.table(GD_DOC_FALLBACK)
            .select(F.col("IDDOC"), _doc_date_column(GD_DOC_FALLBACK).alias("doc_date_fb"))
            .filter(F.col("IDDOC").isNotNull())
            .dropDuplicates(["IDDOC"])
        )
    else:
        df_dates_fallback = df_dates.select("IDDOC").limit(0).withColumn("doc_date_fb", F.lit(None).cast("date"))

    try:
        df_hierarchy = spark.table(DIVISION_REFERENCE_TABLE).drop("division_source")
    except Exception as exc:
        raise RuntimeError(
            f"{DIVISION_REFERENCE_TABLE} not found - run 1_Build_Category_Reference before 2_Cleanup_Volume. ({exc})"
        )

    df_doc_types = (
        selection._normalize_cols_upper(spark.read.table(GD_TYPDOC_LATEST))
        .filter(F.col("IDLG") == 1)
        .select(F.col("IDTYPDOC"), F.col("NOMTYPDOC").alias("type_document"))
        .dropDuplicates(["IDTYPDOC"])
    )
    df_iddoc_type = (
        spark.read.table(GD_DOC_LATEST)
        .select("IDDOC", "IDTYPDOC")
        .filter(F.col("IDDOC").isNotNull())
        .dropDuplicates(["IDDOC"])
        .join(F.broadcast(df_doc_types), on="IDTYPDOC", how="left")
        .select("IDDOC", "type_document")
    )

    df_manifest = (
        df_keep
        .join(df_dates, on="IDDOC", how="left")
        .join(F.broadcast(df_dates_fallback), on="IDDOC", how="left")
        .withColumn("doc_date", F.coalesce(F.col("doc_date"), F.col("doc_date_fb")))
        .drop("doc_date_fb")
        .join(F.broadcast(df_hierarchy), on="IDDOC", how="left")
        .join(F.broadcast(df_iddoc_type), on="IDDOC", how="left")
        .withColumn("langue", F.lit("fr-FR"))
        .withColumn("auteur", F.lit(None).cast(StringType()))
    )

    # Pre-cutoff documents stay in the manifest (3_parse builds their notice chunk from it); only the
    # ARCHIVE_MAX_DOCS most recent get parse_content=True, and 3_parse never scans the others.
    is_archive = archive_condition()
    archive_rank = F.row_number().over(Window.partitionBy(is_archive).orderBy(F.desc("doc_date"), F.desc("IDDOC")))
    return (
        df_manifest
        .withColumn("_archive_rank", archive_rank)
        .withColumn(
            "parse_content",
            ~is_archive | F.lit(ARCHIVE_MAX_DOCS < 0) | (F.col("_archive_rank") <= F.lit(ARCHIVE_MAX_DOCS)),
        )
        .drop("_archive_rank")
    )


def write_manifest(df_manifest):
    """Overwrite parse_manifest and log how many pre-cutoff documents may be parsed."""
    (
        df_manifest.write.format("delta").mode("overwrite").option("overwriteSchema", "true")
        .saveAsTable(PARSE_MANIFEST_TABLE)
    )
    df_written = spark.table(PARSE_MANIFEST_TABLE)
    is_archive = archive_condition()
    logger.info(f"parse_manifest written: {df_written.count()} IDDOCs in scope ({PARSE_MANIFEST_TABLE})")
    logger.info(
        f"   pre-{DOC_DATE_CUTOFF} : {df_written.filter(is_archive).count()} | allowed to be parsed : "
        f"{df_written.filter(is_archive & F.col('parse_content')).count()} (PARSING_ARCHIVE_MAX_DOCS={ARCHIVE_MAX_DOCS})"
    )


def delete_out_of_scope_folders(rows_delete, dry_run):
    """Remove the out-of-scope root folders from the volume; a dry run only counts them."""
    if dry_run:
        logger.info(f"[DRY RUN] {len(rows_delete)} items would be deleted.")
        logger.info("  -> Set the DRY_RUN widget to 'non' to actually delete.")
        return
    if not rows_delete:
        logger.info("Nothing to delete.")
        return

    logger.info(f"Deleting {len(rows_delete)} items...")
    deleted = errors = 0
    for row in rows_delete:
        try:
            dbutils.fs.rm(row["path"], recurse=True)
            deleted += 1
            if deleted % 50 == 0 or deleted == len(rows_delete):
                logger.info(f"  {deleted}/{len(rows_delete)} deleted")
        except Exception as exc:
            logger.warning(f"  Error on {row['path']}: {exc}")
            errors += 1
    logger.info(f"Done: {deleted} deleted, {errors} errors")
