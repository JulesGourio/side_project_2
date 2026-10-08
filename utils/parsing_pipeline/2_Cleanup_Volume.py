# Databricks notebook source
# /// script
# [tool.databricks.environment]
# environment_version = "5"
# ///
# MAGIC %md
# MAGIC 02 - # Cleanup Volume and Build Parse Manifest
# MAGIC
# MAGIC Build the `parse_manifest` used by task `3_parse` and reconcile the volume
# MAGIC root with the current Qualibot scope.
# MAGIC
# MAGIC This notebook applies the following filters sequentially:
# MAGIC
# MAGIC * **Scope gate** (`courant = 1 AND etat = 7 AND nonvisible = 0` on
# MAGIC   `gd_doc_latest`): only current, validated, visible documents pass.
# MAGIC * **Category exclusion** (IDCAT 3798 — ONE_QMS-5S): documents whose
# MAGIC   principal category belongs to `EXCLUDED_IDCATS` are logged as
# MAGIC   `SKIPPED_CATEGORY_EXCLUDED` and removed from the parse manifest.
# MAGIC * **Folder name convention**: only root folders named `D_<IDDOC>` or
# MAGIC   `Dm_<IDDOC>` are processed (case-insensitive). Other names are ignored
# MAGIC   because the IDDOC cannot be extracted from them.
# MAGIC * **Orphan detection**: folders whose extracted IDDOC does not exist in
# MAGIC   `gd_doc_latest` (nor in the optional fallback table) are kept on the
# MAGIC   volume but excluded from parsing and logged as `SKIPPED_IDDOC_NOT_FOUND`.
# MAGIC * **Date cutoff = a parse budget, not an exclusion**: documents published
# MAGIC   before `DOC_DATE_CUTOFF` stay in the manifest, but only the
# MAGIC   `ARCHIVE_MAX_DOCS` most recent get `parse_content=True`. `3_parse` routes
# MAGIC   those to `chunks_archive` (impact search only) instead of the RAG tables.
# MAGIC
# MAGIC Physical deletion of out-of-scope folders only happens when `DRY_RUN` is
# MAGIC set to `non`. Derived-table cleanup (stale chunks, image metadata) always
# MAGIC runs because it only removes indexed content, not source files.
# MAGIC
# MAGIC **Intended Pipeline**
# MAGIC * Qualibot Parsing Pipeline — Daily (task `2_manifest`)
# MAGIC
# MAGIC **Pipeline Inputs**
# MAGIC * `{PARSING_CATALOG_SCHEMA}.category_reference{PARSING_TABLE_SUFFIX}`
# MAGIC * `{PARSING_CATALOG_SCHEMA}.processed_files{PARSING_TABLE_SUFFIX}`
# MAGIC
# MAGIC **Reference Inputs**
# MAGIC * `{PARSING_INTRAQUAL_BRONZE}.gd_doc_latest`
# MAGIC * `{PARSING_INTRAQUAL_BRONZE}.gd_doc_cat_latest`
# MAGIC * `{PARSING_INTRAQUAL_BRONZE}.gd_cat_latest`
# MAGIC * `{PARSING_INTRAQUAL_BRONZE}.gd_typdoc_latest`
# MAGIC * `{PARSING_VOLUME_ROOT_PATH}` volume listing
# MAGIC
# MAGIC **Outputs**
# MAGIC * `{PARSING_CATALOG_SCHEMA}.parse_manifest{PARSING_TABLE_SUFFIX}`
# MAGIC * `{PARSING_CATALOG_SCHEMA}.processed_files{PARSING_TABLE_SUFFIX}`
# MAGIC * `{PARSING_CATALOG_SCHEMA}.chunks{PARSING_TABLE_SUFFIX}` / `chunks_archive` / `image_metadata` for stale-IDDOC pruning only

# COMMAND ----------

# MAGIC %md
# MAGIC # Configuration

# COMMAND ----------

# MAGIC %md
# MAGIC ## Import project modules and Spark helpers

# COMMAND ----------

import os
import re
import sys

_ctx = dbutils.notebook.entry_point.getDbutils().notebook().getContext()
REPO_DIR = "/Workspace" + os.path.dirname(_ctx.notebookPath().get())
sys.path.insert(0, REPO_DIR)

from pyspark.sql import functions as F
import selection
from config import (
    VOLUME_ROOT_PATH,
    GD_DOC_LATEST, GD_DOC_FALLBACK, GD_DOC_CAT_LATEST, GD_CAT_LATEST,
    DOC_SCOPE_FILTER, MANUAL_REF_EXCLUSIONS,
    TARGET_PROCESSED_FILES_TABLE, TARGET_CHUNK_TABLE, TARGET_CHUNK_TABLE_ARCHIVE,
    TARGET_IMAGE_METADATA_TABLE,
)

# Self-joins against a materialized view are blocked by default; several joins below rely on one.
spark.conf.set("spark.databricks.remoteFiltering.blockSelfJoins", "false")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Define run controls

# COMMAND ----------

dbutils.widgets.dropdown("DRY_RUN", "oui", ["oui", "non"],
                         "DRY_RUN — 'non' to actually delete")
dbutils.widgets.text("JOB_RUN_ID", "", "Parent job run_id (correlates with 3_parse's processed_files rows)")

DRY_RUN = dbutils.widgets.get("DRY_RUN").lower() != "non"
JOB_RUN_ID = dbutils.widgets.get("JOB_RUN_ID") or None

print(f"Mode        : {'DRY RUN (simulation — nothing will be deleted)' if DRY_RUN else 'ACTUAL DELETION'}")
print(f"Volume      : {VOLUME_ROOT_PATH}")
print(f"Doc table   : {GD_DOC_LATEST}")
print(f"Scope       : {DOC_SCOPE_FILTER}")

# Categories excluded from parsing by IDCAT.
EXCLUDED_IDCATS = {3798}  # ONE_QMS-5S
if EXCLUDED_IDCATS:
    print(f"Cat excl    : {EXCLUDED_IDCATS}")

# COMMAND ----------

# MAGIC %md
# MAGIC # Inputs

# COMMAND ----------

# MAGIC %md
# MAGIC ## Load the Qualibot scope
# MAGIC
# MAGIC The perimeter is applied at the IDDOC level, not at the REF level. One REF
# MAGIC can span several revisions, each with its own IDDOC.
# MAGIC
# MAGIC ### Upstream filters already applied by the Intraqual sync view
# MAGIC
# MAGIC The SQL Server view `V_QUALIBOT` controls which files are synced to the
# MAGIC Databricks volume. Its filters are **stricter** than this notebook's scope
# MAGIC gate. Everything on the volume has already passed all of these:
# MAGIC
# MAGIC | Filter | Column / join | Value |
# MAGIC | --- | --- | --- | --- |
# MAGIC | Current revision | `COURANT` | `= 1` |
# MAGIC | Validated status | `ETAT` | `= 7` |
# MAGIC | Visible | `NONVISIBLE` | `= 0` |
# MAGIC | Fully distributed | `DIFFTOTALE` | `= 1` |
# MAGIC | Not confidential | `GD_CHAMPDOC_DOC.IDCHAMPDOC IN (3, 4)` + `ALPHA IN ('n/a', ' No', 'no')` | non-confidential only
# MAGIC | Latest revision per REF | `MAX(IDDOC) GROUP BY REF` | one IDDOC per REF
# MAGIC
# MAGIC Because the volume content is pre-filtered by this view, the two missing
# MAGIC filters have no practical effect **unless** files were synced before the
# MAGIC view was in place.

# COMMAND ----------

df_scope_docs = selection.load_scope_docs(spark)
scope_iddocs = {r.IDDOC for r in df_scope_docs.select("IDDOC").collect()}
print(f"{len(scope_iddocs)} IDDOCs in the Qualibot perimeter")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Find previously parsed IDDOCs that left scope

# COMMAND ----------

try:
    # SUCCESS + ERROR + EMPTY_TEXT only -- SKIPPED_*/FILTERED_BY_DATE are already reconciled below.
    stale_iddocs = [
        r.IDDOC for r in
        spark.table(TARGET_PROCESSED_FILES_TABLE)
        .filter(F.col("parse_status").isin("SUCCESS", "ERROR", "EMPTY_TEXT"))
        .select("IDDOC").distinct()
        .join(df_scope_docs.select("IDDOC"), on="IDDOC", how="left_anti")
        .collect()
    ]
except Exception as exc:
    stale_iddocs = []
    print(f"No existing {TARGET_PROCESSED_FILES_TABLE} to prune against ({exc}).")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Build the IDDOC to document lookup
# MAGIC
# MAGIC This lookup covers known documents inside and outside scope. It is used later
# MAGIC to distinguish out-of-scope documents from true orphans.

# COMMAND ----------

def _iddoc_rows(table):
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

primary_rows = _iddoc_rows(GD_DOC_LATEST)
fallback_rows = (
    {k: v for k, v in _iddoc_rows(GD_DOC_FALLBACK).items() if k not in primary_rows}
    if (GD_DOC_FALLBACK or "").strip() else {}
)

# COMMAND ----------

# MAGIC %md
# MAGIC ## Map each IDDOC to its principal category
# MAGIC
# MAGIC Join `gd_doc_cat_latest` and `gd_cat_latest` to resolve the main category
# MAGIC label for each IDDOC.

# COMMAND ----------

df_doc_cat = (
    spark.read.table(GD_DOC_CAT_LATEST)
    .filter(F.lower(F.col("principale")) == "oui")
    .select(
        F.col("iddoc").alias("IDDOC"),
        F.col("idcat").alias("IDCAT"),
    )
    .dropDuplicates(["IDDOC"])
)

df_cat = (
    spark.read.table(GD_CAT_LATEST)
    .filter(F.col("IDLG") == 1)
    .select(F.col("IDCAT"), F.col("NOMCAT"), F.col("NIVEAU"))
)

# COMMAND ----------

# MAGIC %md
# MAGIC ## List the volume root

# COMMAND ----------

VOLUME_DBFS = VOLUME_ROOT_PATH.replace("/Volumes/", "dbfs:/Volumes/")

try:
    root_items = dbutils.fs.ls(VOLUME_DBFS)
except Exception as exc:
    raise RuntimeError(f"Unable to list {VOLUME_DBFS}: {exc}")

print(f"{len(root_items)} items at the volume root level")

# COMMAND ----------

# MAGIC %md
# MAGIC # Preparation

# COMMAND ----------

# MAGIC %md
# MAGIC ## Materialize lookup dictionaries for folder classification

# COMMAND ----------

all_rows       = {**primary_rows, **fallback_rows}
iddoc_to_ref   = {iddoc: v[0] for iddoc, v in all_rows.items()}
iddoc_to_titre = {iddoc: v[1] for iddoc, v in all_rows.items()}

print(f"Primary   ({GD_DOC_LATEST}) : {len(primary_rows):>6} IDDOCs")
if fallback_rows:
    print(f"Fallback  ({GD_DOC_FALLBACK}) : {len(fallback_rows):>6} additional IDDOCs")
print(f"Total     IDDOC->REF available  : {len(iddoc_to_ref):>6}")

df_iddoc_cat = (
    df_doc_cat
    .join(df_cat, on="IDCAT", how="left")
    .select("IDDOC", "NOMCAT", "NIVEAU")
)

iddoc_to_cat   = {r.IDDOC: r.NOMCAT for r in df_iddoc_cat.collect()}
iddoc_to_idcat = {r.IDDOC: r.IDCAT  for r in df_doc_cat.collect()}

print(f"{len(iddoc_to_cat)} IDDOCs with a principal category")
print(f"  {GD_DOC_CAT_LATEST} x {GD_CAT_LATEST}")

# COMMAND ----------

# MAGIC %md
# MAGIC # Transformations

# COMMAND ----------

# MAGIC %md
# MAGIC ## Remove stale derived content
# MAGIC
# MAGIC A new revision gets a new IDDOC. When the old IDDOC leaves scope, this step
# MAGIC removes its stale derived rows.

# COMMAND ----------

if stale_iddocs:
    _stale_list = ",".join(str(i) for i in stale_iddocs)
    for _tbl in [TARGET_CHUNK_TABLE, TARGET_CHUNK_TABLE_ARCHIVE, TARGET_IMAGE_METADATA_TABLE]:
        try:
            spark.sql(f"DELETE FROM {_tbl} WHERE IDDOC IN ({_stale_list})")
        except Exception:
            pass  # table doesn't exist yet
    spark.sql(
        f"DELETE FROM {TARGET_PROCESSED_FILES_TABLE} "
        f"WHERE parse_status IN ('SUCCESS', 'ERROR', 'EMPTY_TEXT') AND IDDOC IN ({_stale_list})"
    )
    print(f"Pruned {len(stale_iddocs)} out-of-scope IDDOCs from chunks/image_metadata/processed_files.")
else:
    print("Nothing to prune — no indexed IDDOC has left scope since the last run.")

# COMMAND ----------

# DBTITLE 1,Revision-duplicate pruning (Bug fix: stale chunks from old revision IDDOCs)
# Remove stale revision chunks for REFs that still have multiple in-scope IDDOCs.
# Keep the latest `indice` per REF, then use the highest IDDOC as the tiebreaker.
#
# Keep `processed_files` unchanged on purpose. Deleting that row would make the
# old IDDOC eligible for parsing again on the next run.

from pyspark.sql import Window as _W

_chunk_tables = [TARGET_CHUNK_TABLE, TARGET_CHUNK_TABLE_ARCHIVE]

try:
    if spark.catalog.tableExists(TARGET_CHUNK_TABLE):
        df_chunks_iddocs = (
            spark.table(TARGET_CHUNK_TABLE)
            .select("REF", "IDDOC").distinct()
        )
        # Only REFs with >1 IDDOC
        df_dup_refs = (
            df_chunks_iddocs.groupBy("REF")
            .agg(F.count("IDDOC").alias("n"))
            .filter(F.col("n") > 1)
            .select("REF")
        )
        _dup_count = df_dup_refs.count()
        if _dup_count > 0:
            df_with_indice = (
                df_chunks_iddocs
                .join(df_dup_refs, on="REF", how="inner")
                .join(
                    spark.table(GD_DOC_LATEST).select("IDDOC", F.col("indice").alias("_gd_indice")),
                    on="IDDOC", how="left",
                )
            )
            _w = _W.partitionBy("REF").orderBy(
                F.desc("_gd_indice"), F.desc("IDDOC")
            )
            stale_revision_iddocs = [
                r.IDDOC for r in
                df_with_indice
                .withColumn("_rn", F.row_number().over(_w))
                .filter(F.col("_rn") > 1)
                .select("IDDOC").distinct().collect()
            ]
            if stale_revision_iddocs:
                _rev_list = ",".join(str(i) for i in stale_revision_iddocs)
                for _tbl in _chunk_tables:
                    try:
                        spark.sql(f"DELETE FROM {_tbl} WHERE IDDOC IN ({_rev_list})")
                    except Exception:
                        pass
                # Clean image_metadata for stale revision IDDOCs too.
                try:
                    spark.sql(
                        f"DELETE FROM {TARGET_IMAGE_METADATA_TABLE} "
                        f"WHERE IDDOC IN ({_rev_list})"
                    )
                except Exception:
                    pass
                print(f"Pruned {len(stale_revision_iddocs)} stale-revision IDDOC(s) "
                      f"across {_dup_count} REF(s) from chunks + image_metadata "
                      f"(processed_files rows kept to prevent re-parse loop).")
            else:
                print("No revision duplicates found in chunks.")
        else:
            print("No REF with multiple IDDOCs in chunks — nothing to prune.")
    else:
        print(f"{TARGET_CHUNK_TABLE} does not exist yet — revision pruning skipped.")
except Exception as exc:
    print(f"Revision-duplicate check skipped: {exc}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Classify root folders
# MAGIC
# MAGIC * `KEEP`: IDDOC is in scope and stays in the parse manifest
# MAGIC * `DELETE`: IDDOC is known but out of scope
# MAGIC * `ORPHAN`: IDDOC is unknown in `gd_doc`
# MAGIC * `SKIP`: folder name does not match `D_` or `Dm_`
# MAGIC * `MANUAL`: REF is excluded by configuration
# MAGIC * `CAT_EXCL`: IDDOC is in scope but its principal category (IDCAT) is excluded

# COMMAND ----------

IDDOC_RE = re.compile(r"^[Dd]m?_(\d+)", re.IGNORECASE)

rows_keep   = []
rows_delete = []
rows_orphan = []
rows_skip   = []
rows_manual_excl = []
rows_cat_excl    = []

for item in root_items:
    name = item.name.rstrip("/")
    m = IDDOC_RE.match(name)

    if not m:
        rows_skip.append({"name": name, "path": item.path, "reason": "NOM_ATYPIQUE"})
        continue

    iddoc = int(m.group(1))
    ref    = iddoc_to_ref.get(iddoc)
    titre  = iddoc_to_titre.get(iddoc)
    cat    = iddoc_to_cat.get(iddoc)

    if ref is None:
        rows_orphan.append({
            "name": name, "IDDOC": iddoc,
            "path": item.path, "reason": "ORPHAN_IDDOC_NOT_IN_GD_DOC",
        })
    elif ref in MANUAL_REF_EXCLUSIONS:
        rows_manual_excl.append({
            "name": name, "IDDOC": iddoc, "ref": ref,
            "titre": titre, "categorie": cat,
            "path": item.path, "reason": "REF_MANUAL_EXCLUSION",
        })
    elif iddoc not in scope_iddocs:
        rows_delete.append({
            "name": name, "IDDOC": iddoc, "ref": ref,
            "titre": titre, "categorie": cat,
            "path": item.path, "reason": "IDDOC_OUT_OF_SCOPE",
        })
    elif iddoc_to_idcat.get(iddoc) in EXCLUDED_IDCATS:
        rows_cat_excl.append({
            "name": name, "IDDOC": iddoc, "ref": ref,
            "titre": titre, "categorie": cat,
            "path": item.path, "reason": "CATEGORY_EXCLUDED",
        })
    else:
        rows_keep.append({"name": name, "IDDOC": iddoc, "ref": ref, "titre": titre, "categorie": cat})

print(f"KEEP   : {len(rows_keep):>5}")
print(f"DELETE : {len(rows_delete):>5}  (out of scope — deleted only if DRY_RUN=non)")
print(f"ORPHAN : {len(rows_orphan):>5}  (unknown IDDOC — kept, excluded from parsing)")
print(f"SKIP   : {len(rows_skip):>5}  (non-conforming names — untouched)")
print(f"MANUAL : {len(rows_manual_excl):>5}  (REF in MANUAL_REF_EXCLUSIONS)")
print(f"CATEXCL: {len(rows_cat_excl):>5}  (IDCAT in EXCLUDED_IDCATS)")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Remove stale permanent-skip rows
# MAGIC
# MAGIC If an IDDOC re-enters scope, an old permanent-skip row would block it from
# MAGIC being parsed again. This step removes those stale skip rows.
# MAGIC `FILTERED_BY_DATE` is excluded because it can still coexist with `KEEP`.

# COMMAND ----------

_PERMANENT_SKIP_STATUSES = ("SKIPPED_REF_OUT_OF_SCOPE", "SKIPPED_IDDOC_NOT_FOUND",
                            "SKIPPED_REF_MANUAL", "SKIPPED_SOURCE_REMOVED",
                            "SKIPPED_EMPTY_FOLDER", "SKIPPED_CATEGORY_EXCLUDED")
try:
    if rows_keep and spark.catalog.tableExists(TARGET_PROCESSED_FILES_TABLE):
        df_keep_iddocs = spark.createDataFrame([(r["IDDOC"],) for r in rows_keep], "IDDOC long")
        stale_skip_iddocs = {
            r.IDDOC for r in
            spark.table(TARGET_PROCESSED_FILES_TABLE)
            .filter(F.col("parse_status").isin(*_PERMANENT_SKIP_STATUSES))
            .join(F.broadcast(df_keep_iddocs), on="IDDOC", how="inner")
            .select("IDDOC").distinct().collect()
        }
    else:
        stale_skip_iddocs = set()
except Exception as exc:
    stale_skip_iddocs = set()
    print(f"Un-skip check failed: {exc}")

if stale_skip_iddocs:
    _unskip_list = ",".join(str(i) for i in stale_skip_iddocs)
    spark.sql(f"DELETE FROM {TARGET_PROCESSED_FILES_TABLE} WHERE IDDOC IN ({_unskip_list})")
    print(f"Un-skipped {len(stale_skip_iddocs)} IDDOC(s) that re-entered scope "
          f"(stale permanent-skip row removed, will be picked up by 3_parse).")
else:
    print("Nothing to un-skip.")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Reconcile non-parsed documents in processed_files
# MAGIC
# MAGIC Rows written here mark documents that must not be parsed in this run.
# MAGIC The table keeps one current row per IDDOC, so each reclassification replaces
# MAGIC the previous status for that IDDOC.

# COMMAND ----------

from datetime import datetime

LOG_RUN_ID = f"cleanup_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
LOG_TS     = datetime.now()

def _make_skipped_rows(items, status):
    rows = []
    for r in items:
        rows.append({
            "IDDOC":              r.get("IDDOC"),
            "source_path":       r["path"].replace("dbfs:", ""),
            "source_file_name":  r["name"],
            "ref":               r.get("ref"),
            "titre":             r.get("titre"),
            "categorie":         r.get("categorie"),
            "parse_status":      status,
            # Keep the per-item reason so downstream monitoring can explain why
            # the document was excluded.
            "removal_reason":    r.get("reason"),
            "include_in_rag":    False,
            "filtered_by_date":  False,
            "ingestion_run_id":  LOG_RUN_ID,
            "ingestion_timestamp": LOG_TS,
            "job_run_id":        JOB_RUN_ID,
        })
    return rows

all_skipped = (
    _make_skipped_rows(rows_delete, "SKIPPED_REF_OUT_OF_SCOPE")
    + _make_skipped_rows(rows_orphan, "SKIPPED_IDDOC_NOT_FOUND")
    + _make_skipped_rows(rows_manual_excl, "SKIPPED_REF_MANUAL")
    + _make_skipped_rows(rows_cat_excl, "SKIPPED_CATEGORY_EXCLUDED")
)

# This catches IDDOCs that vanished entirely from both the volume and gd_doc.
folder_iddocs = {r["IDDOC"] for r in (rows_keep + rows_delete + rows_orphan + rows_manual_excl + rows_cat_excl)}
try:
    existing_iddocs = {
        r.IDDOC for r in
        spark.table(TARGET_PROCESSED_FILES_TABLE)
        .filter(F.col("IDDOC").isNotNull())
        .select("IDDOC").distinct().collect()
    }
except Exception:
    existing_iddocs = set()
vanished_iddocs = existing_iddocs - folder_iddocs - set(all_rows.keys())
if vanished_iddocs:
    all_skipped += [{
        "IDDOC": iddoc, "source_path": None, "source_file_name": None,
        "ref": None, "titre": None, "categorie": None,
        "parse_status": "SKIPPED_SOURCE_REMOVED",
        # The folder is gone from the volume and was not reclassified this run.
        # This anti-join cannot distinguish a source-side deletion from another
        # disappearance cause.
        "removal_reason": "NOT_IN_VOLUME_LISTING",
        "include_in_rag": False, "filtered_by_date": False,
        "ingestion_run_id": LOG_RUN_ID, "ingestion_timestamp": LOG_TS,
        "job_run_id": JOB_RUN_ID,
    } for iddoc in vanished_iddocs]

if not all_skipped:
    print("Nothing to log into processed_files.")
elif not spark.catalog.tableExists(TARGET_PROCESSED_FILES_TABLE):
    print(f"{TARGET_PROCESSED_FILES_TABLE} does not exist yet — run 3_Parse_Pipeline (FULL) first. No write performed.")
else:
    df_new = spark.createDataFrame(all_skipped)
    reclassified = {r["IDDOC"] for r in all_skipped if r.get("IDDOC") is not None}
    try:
        if reclassified:
            iddoc_list = ",".join(str(i) for i in reclassified)
            spark.sql(f"DELETE FROM {TARGET_PROCESSED_FILES_TABLE} WHERE IDDOC IN ({iddoc_list})")
        df_new.write.format("delta").mode("append") \
            .option("mergeSchema", "true").saveAsTable(TARGET_PROCESSED_FILES_TABLE)

        n_skip = sum(1 for r in all_skipped if r["parse_status"] == "SKIPPED_REF_OUT_OF_SCOPE")
        n_orph = sum(1 for r in all_skipped if r["parse_status"] == "SKIPPED_IDDOC_NOT_FOUND")
        n_man  = sum(1 for r in all_skipped if r["parse_status"] == "SKIPPED_REF_MANUAL")
        n_van  = sum(1 for r in all_skipped if r["parse_status"] == "SKIPPED_SOURCE_REMOVED")
        n_cat  = sum(1 for r in all_skipped if r["parse_status"] == "SKIPPED_CATEGORY_EXCLUDED")
        print(f"{TARGET_PROCESSED_FILES_TABLE} updated ({LOG_RUN_ID}):")
        print(f"   SKIPPED_REF_OUT_OF_SCOPE  : {n_skip}")
        print(f"   SKIPPED_IDDOC_NOT_FOUND   : {n_orph}")
        print(f"   SKIPPED_REF_MANUAL        : {n_man}")
        print(f"   SKIPPED_SOURCE_REMOVED    : {n_van}")
        print(f"   SKIPPED_CATEGORY_EXCLUDED : {n_cat}")
    except Exception as exc:
        print(f"Error writing processed_files: {exc}")
        print(f"   Run ID  : {LOG_RUN_ID}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Build the parse manifest
# MAGIC
# MAGIC This notebook does not reuse `selection.load_business_metadata` here because
# MAGIC that helper keeps only `COURANT > 0`, while this manifest must preserve all
# MAGIC in-scope `KEEP` IDDOCs.

# COMMAND ----------

import importlib
import config as _cfg
importlib.reload(_cfg)
import selection as _sel
importlib.reload(_sel)

PARSE_MANIFEST_TABLE = _cfg.PARSE_MANIFEST_TABLE

from pyspark.sql.types import StructType, StructField, LongType, StringType

_keep_schema = StructType([
    StructField("IDDOC",     LongType()),
    StructField("ref",      StringType()),
    StructField("titre",    StringType()),
    StructField("categorie",StringType()),
])
df_keep_base = spark.createDataFrame(
    [(r["IDDOC"], r["ref"], r["titre"], r["categorie"]) for r in rows_keep],
    schema=_keep_schema,
)

# `indice` is the revision key used later by `3_Parse_Pipeline`.
def _date_col(table):
    cols = {c.lower() for c in spark.read.table(table).columns}
    src = "dtdiff" if "dtdiff" in cols else "DATEDIFF" if "datediff" in cols else None
    return F.to_date(F.col(src)) if src else F.lit(None).cast("date")

df_dates = (
    spark.read.table(GD_DOC_LATEST)
    .select(
        F.col("IDDOC"),
        _date_col(GD_DOC_LATEST).alias("doc_date"),
        F.col("INDICE").cast("string").alias("indice"),
    )
    .filter(F.col("IDDOC").isNotNull())
    .dropDuplicates(["IDDOC"])
)
# Optional fallback dates for IDDOCs missing from the primary table.
if (GD_DOC_FALLBACK or "").strip():
    df_dates_fb = (
        spark.read.table(GD_DOC_FALLBACK)
        .select(F.col("IDDOC"), _date_col(GD_DOC_FALLBACK).alias("doc_date_fb"))
        .filter(F.col("IDDOC").isNotNull())
        .dropDuplicates(["IDDOC"])
    )
else:
    df_dates_fb = df_dates.select("IDDOC").limit(0).withColumn("doc_date_fb", F.lit(None).cast("date"))

try:
    df_hier = spark.table(_cfg.DIVISION_REFERENCE_TABLE).drop("division_source")
except Exception as exc:
    raise RuntimeError(
        f"{_cfg.DIVISION_REFERENCE_TABLE} not found — run 1_Build_Category_Reference "
        f"before 2_Cleanup_Volume. ({exc})"
    )

df_typdoc = (
    _sel._normalize_cols_upper(spark.read.table(_cfg.GD_TYPDOC_LATEST))
    .filter(F.col("IDLG") == 1)
    .select(F.col("IDTYPDOC"), F.col("NOMTYPDOC").alias("type_document"))
    .dropDuplicates(["IDTYPDOC"])
)
df_iddoc_typdoc = (
    spark.read.table(GD_DOC_LATEST)
    .select("IDDOC", "IDTYPDOC")
    .filter(F.col("IDDOC").isNotNull())
    .dropDuplicates(["IDDOC"])
    .join(F.broadcast(df_typdoc), on="IDTYPDOC", how="left")
    .select("IDDOC", "type_document")
)

df_manifest_full = (
    df_keep_base
    .join(df_dates,         on="IDDOC", how="left")
    .join(F.broadcast(df_dates_fb), on="IDDOC", how="left")
    .withColumn("doc_date", F.coalesce(F.col("doc_date"), F.col("doc_date_fb")))
    .drop("doc_date_fb")
    .join(F.broadcast(df_hier), on="IDDOC", how="left")
    .join(F.broadcast(df_iddoc_typdoc), on="IDDOC", how="left")
    .withColumn("langue", F.lit("fr-FR"))
    .withColumn("auteur",  F.lit(None).cast(StringType()))
)

# Pre-cutoff documents stay in the manifest (3_parse builds their notice chunk
# from it), but only the ARCHIVE_MAX_DOCS most recent ones get parse_content=True;
# 3_parse routes those to chunks_archive and never scans the others.
_is_archive = F.col("doc_date").isNotNull() & (F.col("doc_date") < F.lit(_cfg.DOC_DATE_CUTOFF).cast("date"))
_archive_rank = F.row_number().over(
    _W.partitionBy(_is_archive).orderBy(F.desc("doc_date"), F.desc("IDDOC"))
)
df_manifest = (
    df_manifest_full
    .withColumn("_archive_rank", _archive_rank)
    .withColumn(
        "parse_content",
        ~_is_archive
        | F.lit(_cfg.ARCHIVE_MAX_DOCS < 0)
        | (F.col("_archive_rank") <= F.lit(_cfg.ARCHIVE_MAX_DOCS)),
    )
    .drop("_archive_rank")
)

# COMMAND ----------

# MAGIC %md
# MAGIC ## Preview excluded root folders

# COMMAND ----------

if rows_delete:
    display(spark.createDataFrame(rows_delete).orderBy("reason", "ref"))
else:
    print("Nothing to delete — the volume is already clean.")

if rows_orphan:
    from pyspark.sql.types import StructType, StructField, LongType, StringType
    _s = StructType([StructField("IDDOC", LongType()), StructField("name", StringType())])
    print(f"{len(rows_orphan)} ORPHAN — kept on the volume but excluded from parsing (IDDOC absent from gd_doc + fallback):")
    display(spark.createDataFrame([(r["IDDOC"], r["name"]) for r in rows_orphan], schema=_s).orderBy("IDDOC"))

# COMMAND ----------

if rows_skip:
    print(f"{len(rows_skip)} items with a non-conforming name (ignored):")
    for r in rows_skip:
        print(f"   {r['name']}  ({r['path']})")

# COMMAND ----------

# MAGIC %md
# MAGIC # Outputs

# COMMAND ----------

# MAGIC %md
# MAGIC ## Write the parse manifest

# COMMAND ----------

(
    df_manifest.write.format("delta")
    .mode("overwrite")
    .option("overwriteSchema", "true")
    .saveAsTable(PARSE_MANIFEST_TABLE)
)
_df_written = spark.table(PARSE_MANIFEST_TABLE)
_n_manifest = _df_written.count()
_n_archive = _df_written.filter(_is_archive).count()
_n_archive_parsed = _df_written.filter(_is_archive & F.col("parse_content")).count()
print(f"\nparse_manifest written: {_n_manifest} IDDOCs in scope")
print(f"   Table : {PARSE_MANIFEST_TABLE}")
print(f"   pre-{_cfg.DOC_DATE_CUTOFF} : {_n_archive} | allowed to be parsed : {_n_archive_parsed} "
      f"(PARSING_ARCHIVE_MAX_DOCS={_cfg.ARCHIVE_MAX_DOCS})")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Delete out-of-scope folders when deletion is enabled

# COMMAND ----------

if DRY_RUN:
    print(f"[DRY RUN] {len(rows_delete)} items would be deleted.")
    print("  -> Set the DRY_RUN widget to 'non' to actually delete.")
else:
    if not rows_delete:
        print("Nothing to delete.")
    else:
        print(f"Deleting {len(rows_delete)} items...\n")
        deleted = 0
        errors  = 0

        for row in rows_delete:
            try:
                dbutils.fs.rm(row["path"], recurse=True)
                deleted += 1
                if deleted % 50 == 0 or deleted == len(rows_delete):
                    print(f"  {deleted}/{len(rows_delete)} deleted")
            except Exception as exc:
                print(f"  Error on {row['path']}: {exc}")
                errors += 1

        print(f"\nDone: {deleted} deleted, {errors} errors")