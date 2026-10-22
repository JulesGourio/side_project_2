# Databricks notebook source
# MAGIC %md
# MAGIC # PROJ PARSE_MANIFEST
# MAGIC
# MAGIC **Description:**
# MAGIC Builds the `parse_manifest` read by task `3_parse` and reconciles the volume root with the current Qualibot scope.
# MAGIC The phases are functions of `manifest_steps.py`, applied in this order:
# MAGIC * **Scope gate** (`revision_status_code = 1 AND lifecycle_state_code = 7 AND is_hidden = false` on `document_revisions_intraqual`): only current, validated, visible documents pass.
# MAGIC * **Category exclusion** (none today): documents whose principal category belongs to `EXCLUDED_IDCATS` are logged as
# MAGIC   `SKIPPED_CATEGORY_EXCLUDED` and removed from the parse manifest.
# MAGIC * **Folder name convention**: only root folders named `D_<IDDOC>` or `Dm_<IDDOC>` are processed (case-insensitive); the IDDOC cannot be extracted from other names.
# MAGIC * **Orphan detection**: folders whose IDDOC is not in `document_revisions_intraqual` stay on the volume,
# MAGIC   are excluded from parsing and logged as `SKIPPED_IDDOC_NOT_FOUND`.
# MAGIC * **Date cutoff = a parse budget, not an exclusion**: documents published before `DOC_DATE_CUTOFF` stay in the manifest, but only the
# MAGIC   `ARCHIVE_MAX_DOCS` most recent get `parse_content=True`; `3_parse` routes those to `chunks_archive` (impact search only).
# MAGIC   Documents under a root of `DATE_EXEMPT_ROOT_IDCATS` (`date_exempt`) are not concerned: they are parsed and indexed whatever their date.
# MAGIC
# MAGIC **Highlighted complexities:**
# MAGIC Out-of-scope folders stay on the volume and are never parsed. The cleanup of derived tables (stale chunks, image metadata)
# MAGIC removes the indexed content of those documents.
# MAGIC
# MAGIC **Intended Pipeline**
# MAGIC - Qualibot Parsing Pipeline — Daily (task `2_manifest`)
# MAGIC
# MAGIC **Inputs Data**
# MAGIC - `{PIPELINE_WRITE_ENV}_proj.qualibot.category_reference`, `parsed_documents`
# MAGIC - `{REFERENCE_READ_ENV}_gold.quality_system.document_revisions_intraqual` (Intraqual data asset)
# MAGIC - `{PARSING_VOLUME_ROOT_PATH}` volume listing
# MAGIC
# MAGIC **Output Tables (Pipeline)**
# MAGIC - `{PIPELINE_WRITE_ENV}_proj.qualibot.parse_manifest`
# MAGIC - `{PIPELINE_WRITE_ENV}_proj.qualibot.parsed_documents` (skip rows)
# MAGIC - `{PIPELINE_WRITE_ENV}_proj.qualibot.chunks` / `chunks_archive` / `image_metadata` (stale-IDDOC pruning only)

# COMMAND ----------

# MAGIC %md
# MAGIC # Technical debt
# MAGIC - Pruning, un-skip and reconciliation write to Delta tables in `# Data Transformations`, before the manifest is written: a failure in a later step leaves those tables already pruned.
# MAGIC - The `V_QUALIBOT` view filters (`DIFFTOTALE`, confidentiality) are not re-applied here; the scope gate relies on the volume having been filled through that view.

# COMMAND ----------

# MAGIC %md
# MAGIC # Configuration

# COMMAND ----------

# MAGIC %md
# MAGIC ## Config Widgets
# MAGIC The environments, the quality bypass and the debug mode are widgets: the job passes them as base parameters,
# MAGIC and the notebook copies them to the environment that `config` reads when it is imported.


# COMMAND ----------

import os

dbutils.widgets.removeAll()

dbutils.widgets.text("pipeline_write_env", "", "Environment the tables are written to: dev, uat or prod")
dbutils.widgets.text("pipeline_read_env", "", "Environment of the tables another task of this job wrote (empty: pipeline_write_env)")
dbutils.widgets.text("reference_read_env", "prod", "Environment of the reference tables (Intraqual data asset)")
dbutils.widgets.dropdown("bypass_quality_checks", "false", ["false", "true"], "true: RED quality checks are logged as AMBER")
dbutils.widgets.text("debug", "false")
dbutils.widgets.dropdown("log_level", "info", ["debug", "info", "warning", "error", "critical"])

PIPELINE_WRITE_ENV = dbutils.widgets.get("pipeline_write_env")
if PIPELINE_WRITE_ENV not in ("dev", "uat", "prod"):
    raise ValueError(f"pipeline_write_env must be dev, uat or prod, got {PIPELINE_WRITE_ENV!r}")
PIPELINE_READ_ENV = dbutils.widgets.get("pipeline_read_env") or PIPELINE_WRITE_ENV
DEBUG = dbutils.widgets.get("debug").lower() == "true"
LOG_LEVEL = dbutils.widgets.get("log_level")

# config reads the environment when it is imported, so the widget values are copied there first.
os.environ["PIPELINE_WRITE_ENV"] = PIPELINE_WRITE_ENV
os.environ["PIPELINE_READ_ENV"] = dbutils.widgets.get("pipeline_read_env")
os.environ["REFERENCE_READ_ENV"] = dbutils.widgets.get("reference_read_env")
os.environ["BYPASS_QUALITY_CHECKS"] = dbutils.widgets.get("bypass_quality_checks")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Config Standard Package Imports
# MAGIC The repository folder is added to `sys.path` because a job task does not do it for a notebook.

# COMMAND ----------

import os
import sys

_ctx = dbutils.notebook.entry_point.getDbutils().notebook().getContext()
REPO_DIR = "/Workspace" + os.path.dirname(_ctx.notebookPath().get())
sys.path.insert(
    0, os.path.dirname(os.path.dirname(REPO_DIR))
)  # leap_utils/; inserted first so that REPO_DIR (its own `utils` module) stays ahead
sys.path.insert(0, REPO_DIR)

from pyspark.sql import functions as f

# COMMAND ----------

# MAGIC %md
# MAGIC ## Config Function Imports
# MAGIC `manifest_steps` holds the phases of this task; `categories`, `tables` and `config` live next to this notebook.

# COMMAND ----------

import categories
import manifest_steps
import tables

tables.require_gx()  # fails here, not after the work, on a cluster without Great Expectations
from leap_utils.common import logger as log_utils

from config import DOC_SCOPE_FILTER, EXCLUDED_IDCATS, VOLUME_ROOT_PATH

# COMMAND ----------

# MAGIC %md
# MAGIC ## Config logger
# MAGIC The logger is created once the widgets are read, so the level they give applies to every line after.

# COMMAND ----------

logger = log_utils.setup_applevel_logger(log_level=log_utils.LOGGER_MAPPING[LOG_LEVEL])

# COMMAND ----------

# MAGIC %md
# MAGIC ### Job parameters
# MAGIC `job_run_id` is written on every `parsed_documents` row so `3_parse` rows and these rows can be correlated.

# COMMAND ----------

dbutils.widgets.text(
    "job_run_id", "", "Parent job run_id (correlates with 3_parse's parsed_documents rows)"
)

JOB_RUN_ID = dbutils.widgets.get("job_run_id") or None

logger.info(f"Volume: {VOLUME_ROOT_PATH}")
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
# MAGIC filters have no practical effect **unless** a file reached the volume outside that view.

# COMMAND ----------

df_scope_docs = categories.load_scope_docs(spark)
scope_iddocs = {r.IDDOC for r in df_scope_docs.select("IDDOC").collect()}
logger.info(f"{len(scope_iddocs)} IDDOCs in the Qualibot perimeter")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Parsed IDDOCs that left scope
# MAGIC Compared with `parsed_documents` (SUCCESS, ERROR, EMPTY_TEXT and IMAGES_PENDING only; the SKIPPED rows are reconciled in Tr. 5). They are pruned in Tr. 1.

# COMMAND ----------

stale_iddocs = manifest_steps.find_stale_iddocs(df_scope_docs)

# COMMAND ----------

# MAGIC %md
# MAGIC ## Known documents, inside and outside scope
# MAGIC IDDOC to (REF, title) for every revision of `document_revisions_intraqual`. Used later to tell an out-of-scope document from a true orphan.

# COMMAND ----------

all_rows = manifest_steps.doc_rows()

# COMMAND ----------

# MAGIC %md
# MAGIC ## Main category of each IDDOC
# MAGIC `document_revisions_intraqual` carries one main category per revision, with its name (French labels only).

# COMMAND ----------

df_main_cat = (
    categories.read_revisions(spark)
    .filter(f.col("category_ID").isNotNull())
    .select(
        f.col("document_revision_ID").alias("IDDOC"),
        f.col("category_ID").alias("IDCAT"),
        f.col("category_name").alias("NOMCAT"),
    )
    .dropDuplicates(["IDDOC"])
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

iddoc_to_ref = {iddoc: v[0] for iddoc, v in all_rows.items()}
iddoc_to_titre = {iddoc: v[1] for iddoc, v in all_rows.items()}
logger.info(f"IDDOC->REF available: {len(iddoc_to_ref)}")

_main_cat_rows = df_main_cat.collect()
iddoc_to_cat = {r.IDDOC: r.NOMCAT for r in _main_cat_rows}
iddoc_to_idcat = {r.IDDOC: r.IDCAT for r in _main_cat_rows}
logger.info(f"{len(iddoc_to_cat)} IDDOCs with a principal category")

# COMMAND ----------

# MAGIC %md
# MAGIC # Data Transformations
# MAGIC ## Tr. 1 - Remove stale derived content
# MAGIC A new revision gets a new IDDOC. When the old IDDOC leaves scope, its rows in `chunks`, `chunks_archive`, `image_metadata` and `parsed_documents` are deleted so the index stops serving it. Its parse result in `parsing_checkpoint` is kept for `CHECKPOINT_ORPHAN_KEEP_DAYS` days (a document that comes back is not parsed again), then deleted.

# COMMAND ----------

manifest_steps.prune_stale_content(stale_iddocs)
manifest_steps.prune_old_checkpoint_rows(df_scope_docs)

# COMMAND ----------

# MAGIC %md
# MAGIC ## Tr. 2 - Remove stale revision chunks
# MAGIC A REF can still have several in-scope IDDOCs. Only the newest revision (highest `indice`, then highest IDDOC) keeps its chunks. The `parsed_documents` rows stay on purpose: deleting them would make the old IDDOC eligible for parsing again.

# COMMAND ----------

manifest_steps.prune_stale_revisions()

# COMMAND ----------

# MAGIC %md
# MAGIC ## Tr. 3 - Classify the root folders
# MAGIC Each folder becomes KEEP, DELETE (out of scope), ORPHAN (IDDOC unknown in `document_revisions_intraqual`), SKIP (name not `D_`/`Dm_`), MANUAL (REF excluded by configuration) or CAT_EXCL (excluded principal category).

# COMMAND ----------

classified = manifest_steps.classify_root_folders(
    root_items, scope_iddocs, iddoc_to_ref, iddoc_to_titre, iddoc_to_cat, iddoc_to_idcat
)

# COMMAND ----------

# MAGIC %md
# MAGIC ## Tr. 4 - Remove stale permanent-skip rows
# MAGIC A document that re-enters scope while `parsed_documents` still holds a permanent SKIPPED row would never be parsed again, so those rows are deleted.

# COMMAND ----------

manifest_steps.remove_stale_skip_rows(classified["keep"])

# COMMAND ----------

# MAGIC %md
# MAGIC ## Tr. 5 - Reconcile parsed_documents
# MAGIC Documents that must not be parsed get a SKIPPED row with the reason, so the monitoring dashboard can explain every exclusion. A document recorded earlier but absent from both the volume and `document_revisions_intraqual` becomes SKIPPED_SOURCE_REMOVED.

# COMMAND ----------

manifest_steps.reconcile_processed_files(classified, all_rows.keys(), JOB_RUN_ID)

# COMMAND ----------

# MAGIC %md
# MAGIC ## Tr. 6 - Build the parse manifest
# MAGIC One row per KEEP document: REF, title, revision (`indice`), date, category hierarchy and document type. Pre-cutoff documents stay in the manifest but only the `ARCHIVE_MAX_DOCS` most recent get `parse_content = True`.

# COMMAND ----------

df_manifest = manifest_steps.build_manifest(classified["keep"]).cache()

# COMMAND ----------

# MAGIC %md
# MAGIC # Quality Checks
# MAGIC ## Excluded folders
# MAGIC Informational: lists what the volume holds that will not be parsed (out of scope, unknown IDDOC, unusual name) so an unexpected exclusion is visible in the run output.

# COMMAND ----------

if classified["delete"]:
    display(spark.createDataFrame(classified["delete"]).orderBy("reason", "ref"))
else:
    logger.info("No out-of-scope folder on the volume.")

if classified["orphan"]:
    logger.info(
        f"{len(classified['orphan'])} ORPHAN folders kept on the volume but excluded from parsing"
    )
    display(
        spark.createDataFrame(
            [(r["IDDOC"], r["name"]) for r in classified["orphan"]],
            schema="IDDOC long, name string",
        ).orderBy("IDDOC")
    )

for r in classified["skip"]:
    logger.info(f"Ignored folder (non-conforming name): {r['name']} ({r['path']})")

# COMMAND ----------

# MAGIC %md
# MAGIC ## parse_manifest
# MAGIC `IDDOC` is the key `3_parse` joins on: a duplicate would multiply the rows of the checkpoint (RED), and an empty manifest means an upstream failure (RED). `ref` and `titre` are expected on 99 % of the documents (AMBER).

# COMMAND ----------

tables.check_dataframe(
    df_manifest,
    "parse_manifest",
    [
        tables.non_empty(),
        tables.not_null("IDDOC"),
        tables.unique("IDDOC"),
        tables.not_null("ref", mostly=0.99, severity=tables.AMBER),
        tables.not_null("titre", mostly=0.99, severity=tables.AMBER),
    ],
    logger,
)

# COMMAND ----------

# MAGIC %md
# MAGIC # Outputs
# MAGIC ## Write the parse manifest
# MAGIC Full overwrite: the manifest is the scope of the day, rebuilt every run.

# COMMAND ----------

manifest_steps.write_manifest(df_manifest)
