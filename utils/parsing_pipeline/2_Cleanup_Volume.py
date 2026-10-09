# Databricks notebook source
# /// script
# [tool.databricks.environment]
# environment_version = "5"
# ///
# MAGIC %md
# MAGIC # 02 — Cleanup Volume and Build Parse Manifest
# MAGIC
# MAGIC Build the `parse_manifest` used by task `3_parse` and reconcile the volume
# MAGIC root with the current Qualibot scope. The phases are functions of `manifest_steps.py`.
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
# MAGIC # Technical debt
# MAGIC - Pruning, un-skip and reconciliation write to Delta tables (`chunks`, `chunks_archive`, `image_metadata`, `processed_files`) in `# Data Transformations`, before the manifest is written: a failure in a later step leaves those tables already pruned.
# MAGIC - `reconcile_processed_files` logs a failed write to `processed_files` but does not stop the task, so the manifest can be written while the skip rows are not.
# MAGIC - `manifest_steps.build_manifest` calls the private `selection._normalize_cols_upper`.
# MAGIC - The `V_QUALIBOT` view filters (`DIFFTOTALE`, confidentiality) are not re-applied here; the scope gate relies on the volume having been filled through that view.

# COMMAND ----------

# MAGIC %md
# MAGIC # Configuration
# MAGIC ## Config Imports
# MAGIC `manifest_steps` holds the phases of this task; `selection` and `config` live next to this notebook. The repository folder is added to `sys.path` because a job task does not do it for a notebook.

# COMMAND ----------

import os
import sys

_ctx = dbutils.notebook.entry_point.getDbutils().notebook().getContext()
REPO_DIR = "/Workspace" + os.path.dirname(_ctx.notebookPath().get())
sys.path.insert(0, REPO_DIR)

from pyspark.sql import functions as F

import manifest_steps
import selection
from utils import logger
from config import (
    DOC_SCOPE_FILTER, GD_CAT_LATEST, GD_DOC_CAT_LATEST, GD_DOC_FALLBACK, GD_DOC_LATEST,
    EXCLUDED_IDCATS, VOLUME_ROOT_PATH,
)

# COMMAND ----------

# MAGIC %md
# MAGIC ## Config Spark
# MAGIC Several joins below read a materialized view on both sides, which Spark blocks by default.

# COMMAND ----------

spark.conf.set("spark.databricks.remoteFiltering.blockSelfJoins", "false")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Config Widgets
# MAGIC `DRY_RUN` protects the **volume**: out-of-scope folders are only deleted when it is `non`. The cleanup of derived tables (stale chunks, image metadata) always runs, because it removes indexed content, not source files.
# MAGIC
# MAGIC `JOB_RUN_ID` is written on every `processed_files` row so `3_parse` rows and these rows can be correlated.

# COMMAND ----------

dbutils.widgets.dropdown("DRY_RUN", "oui", ["oui", "non"], "DRY_RUN — 'non' to actually delete")
dbutils.widgets.text("JOB_RUN_ID", "", "Parent job run_id (correlates with 3_parse's processed_files rows)")

DRY_RUN = dbutils.widgets.get("DRY_RUN").lower() != "non"
JOB_RUN_ID = dbutils.widgets.get("JOB_RUN_ID") or None

logger.info(f"Mode: {'DRY RUN (nothing will be deleted)' if DRY_RUN else 'ACTUAL DELETION'} | volume={VOLUME_ROOT_PATH}")
logger.info(f"Scope: {DOC_SCOPE_FILTER} | excluded categories: {sorted(EXCLUDED_IDCATS)}")

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
logger.info(f"{len(scope_iddocs)} IDDOCs in the Qualibot perimeter")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Previously parsed IDDOCs that left scope
# MAGIC Compared with `processed_files` (SUCCESS, ERROR and EMPTY_TEXT only; the SKIPPED rows are reconciled in Tr. 5). They are pruned in Tr. 1.

# COMMAND ----------

stale_iddocs = manifest_steps.find_stale_iddocs(df_scope_docs)

# COMMAND ----------

# MAGIC %md
# MAGIC ## Known documents, inside and outside scope
# MAGIC IDDOC to (REF, title) for every document of `gd_doc_latest` (and the optional fallback table). Used later to tell an out-of-scope document from a true orphan.

# COMMAND ----------

primary_rows = manifest_steps.doc_rows(GD_DOC_LATEST)
fallback_rows = (
    {k: v for k, v in manifest_steps.doc_rows(GD_DOC_FALLBACK).items() if k not in primary_rows}
    if (GD_DOC_FALLBACK or "").strip() else {}
)

# COMMAND ----------

# MAGIC %md
# MAGIC ## Principal category of each IDDOC
# MAGIC `gd_doc_cat_latest` keeps one principal category per document, `gd_cat_latest` gives its name (French labels only, `IDLG = 1`).

# COMMAND ----------

df_doc_cat = (
    spark.read.table(GD_DOC_CAT_LATEST)
    .filter(F.lower(F.col("principale")) == "oui")
    .select(F.col("iddoc").alias("IDDOC"), F.col("idcat").alias("IDCAT"))
    .dropDuplicates(["IDDOC"])
)
df_cat = (
    spark.read.table(GD_CAT_LATEST)
    .filter(F.col("IDLG") == 1)
    .select(F.col("IDCAT"), F.col("NOMCAT"), F.col("NIVEAU"))
)

# COMMAND ----------

# MAGIC %md
# MAGIC ## Volume root
# MAGIC One folder per document, named `D_<IDDOC>` or `Dm_<IDDOC>`.

# COMMAND ----------

VOLUME_DBFS = VOLUME_ROOT_PATH.replace("/Volumes/", "dbfs:/Volumes/")
try:
    root_items = dbutils.fs.ls(VOLUME_DBFS)
except Exception as exc:
    raise RuntimeError(f"Unable to list {VOLUME_DBFS}: {exc}")
logger.info(f"{len(root_items)} items at the volume root level")

# COMMAND ----------

# MAGIC %md
# MAGIC # Data Preparation
# MAGIC ## Prep1 - Lookup dictionaries
# MAGIC The folder classification (Tr. 3) does one dictionary lookup per folder instead of a join, since the volume holds thousands of folders and the lookups are small.

# COMMAND ----------

all_rows = {**primary_rows, **fallback_rows}
iddoc_to_ref = {iddoc: v[0] for iddoc, v in all_rows.items()}
iddoc_to_titre = {iddoc: v[1] for iddoc, v in all_rows.items()}
logger.info(f"IDDOC->REF available: {len(iddoc_to_ref)} ({len(primary_rows)} primary, {len(fallback_rows)} fallback)")

iddoc_to_cat = {r.IDDOC: r.NOMCAT for r in df_doc_cat.join(df_cat, on="IDCAT", how="left").select("IDDOC", "NOMCAT").collect()}
iddoc_to_idcat = {r.IDDOC: r.IDCAT for r in df_doc_cat.collect()}
logger.info(f"{len(iddoc_to_cat)} IDDOCs with a principal category")

# COMMAND ----------

# MAGIC %md
# MAGIC # Data Transformations
# MAGIC ## Tr. 1 - Remove stale derived content
# MAGIC A new revision gets a new IDDOC. When the old IDDOC leaves scope, its rows in `chunks`, `chunks_archive`, `image_metadata` and `processed_files` are deleted so the index stops serving it.

# COMMAND ----------

manifest_steps.prune_stale_content(stale_iddocs)

# COMMAND ----------

# MAGIC %md
# MAGIC ## Tr. 2 - Remove stale revision chunks
# MAGIC A REF can still have several in-scope IDDOCs. Only the newest revision (highest `indice`, then highest IDDOC) keeps its chunks. The `processed_files` rows stay on purpose: deleting them would make the old IDDOC eligible for parsing again.

# COMMAND ----------

manifest_steps.prune_stale_revisions()

# COMMAND ----------

# MAGIC %md
# MAGIC ## Tr. 3 - Classify the root folders
# MAGIC Each folder becomes KEEP, DELETE (out of scope), ORPHAN (IDDOC unknown in `gd_doc`), SKIP (name not `D_`/`Dm_`), MANUAL (REF excluded by configuration) or CAT_EXCL (excluded principal category).

# COMMAND ----------

classified = manifest_steps.classify_root_folders(
    root_items, scope_iddocs, iddoc_to_ref, iddoc_to_titre, iddoc_to_cat, iddoc_to_idcat
)

# COMMAND ----------

# MAGIC %md
# MAGIC ## Tr. 4 - Remove stale permanent-skip rows
# MAGIC A document that re-enters scope while `processed_files` still holds a permanent SKIPPED row would never be parsed again, so those rows are deleted.

# COMMAND ----------

manifest_steps.remove_stale_skip_rows(classified["keep"])

# COMMAND ----------

# MAGIC %md
# MAGIC ## Tr. 5 - Reconcile processed_files
# MAGIC Documents that must not be parsed get a SKIPPED row with the reason, so the monitoring dashboard can explain every exclusion. A document recorded earlier but absent from both the volume and `gd_doc` becomes SKIPPED_SOURCE_REMOVED.

# COMMAND ----------

manifest_steps.reconcile_processed_files(classified, all_rows.keys(), JOB_RUN_ID)

# COMMAND ----------

# MAGIC %md
# MAGIC ## Tr. 6 - Build the parse manifest
# MAGIC One row per KEEP document: REF, title, revision (`indice`), date, category hierarchy and document type. Pre-cutoff documents stay in the manifest but only the `ARCHIVE_MAX_DOCS` most recent get `parse_content = True`.

# COMMAND ----------

df_manifest = manifest_steps.build_manifest(classified["keep"])

# COMMAND ----------

# MAGIC %md
# MAGIC # Quality Checks
# MAGIC ## Review of the excluded folders
# MAGIC Informational: lists what the volume holds that will not be parsed (out of scope, unknown IDDOC, unusual name) so an unexpected exclusion is visible in the run output.

# COMMAND ----------

if classified["delete"]:
    display(spark.createDataFrame(classified["delete"]).orderBy("reason", "ref"))
else:
    logger.info("Nothing to delete - the volume is already clean.")

if classified["orphan"]:
    logger.info(f"{len(classified['orphan'])} ORPHAN folders kept on the volume but excluded from parsing")
    display(spark.createDataFrame([(r["IDDOC"], r["name"]) for r in classified["orphan"]], schema="IDDOC long, name string").orderBy("IDDOC"))

for r in classified["skip"]:
    logger.info(f"Ignored folder (non-conforming name): {r['name']} ({r['path']})")

# COMMAND ----------

# MAGIC %md
# MAGIC # Outputs
# MAGIC ## Write the parse manifest
# MAGIC Full overwrite: the manifest is the scope of the day, rebuilt every run.

# COMMAND ----------

manifest_steps.write_manifest(df_manifest)

# COMMAND ----------

# MAGIC %md
# MAGIC ## Delete out-of-scope folders
# MAGIC Only when `DRY_RUN` is `non`.

# COMMAND ----------

manifest_steps.delete_out_of_scope_folders(classified["delete"], DRY_RUN)
