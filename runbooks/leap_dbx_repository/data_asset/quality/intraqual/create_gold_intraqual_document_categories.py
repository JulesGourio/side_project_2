# Databricks notebook source
# MAGIC %md
# MAGIC
# MAGIC # CREATE GOLD INTRAQUAL DOCUMENT CATEGORIES
# MAGIC
# MAGIC **Description:**
# MAGIC Creates the gold bridge between document revisions and categories, keeping every
# MAGIC attachment rather than only the main one. Use it to count a document under all of its
# MAGIC categories; the document tables keep only the main one. Each category is placed in its
# MAGIC tree with its full path and root. A second table lists every ancestor of every category,
# MAGIC so that everything under a given category is reachable with one join at any depth.
# MAGIC
# MAGIC **Highlighted complexities:**
# MAGIC - Section Tr 1 — the category tree is walked parent by parent to resolve every ancestor
# MAGIC
# MAGIC **Intended Pipeline**
# MAGIC - Data asset - Quality - Intraqual
# MAGIC
# MAGIC **Input Tables Pipeline**
# MAGIC - {pipeline_read_env}_silver.quality_system.document_categories_intraqual
# MAGIC - {pipeline_read_env}_silver.quality_system.categories_intraqual
# MAGIC - {pipeline_read_env}_silver.quality_system.documents_intraqual
# MAGIC
# MAGIC **Inputs Reference Data**
# MAGIC - N/A
# MAGIC
# MAGIC **Output Tables (Pipeline)**
# MAGIC - {pipeline_write_env}_gold.quality_system.document_categories_intraqual
# MAGIC - {pipeline_write_env}_gold.quality_system.document_categories_intraqual_exposed
# MAGIC - {pipeline_write_env}_gold.quality_system.category_ancestors_intraqual
# MAGIC - {pipeline_write_env}_gold.quality_system.category_ancestors_intraqual_exposed

# COMMAND ----------

# MAGIC %md
# MAGIC # Technical Debt
# MAGIC
# MAGIC N/A

# COMMAND ----------

# MAGIC %md
# MAGIC # Configuration

# COMMAND ----------

# MAGIC %md
# MAGIC ## Config Standard Package Imports

# COMMAND ----------

# STANDARD FUNCTION
import pyspark.sql.functions as f

# COMMAND ----------

# MAGIC %md
# MAGIC ### Config LEAP Function Imports

# COMMAND ----------

# LEAP FUNCTION
from leap_utils.data_asset import table_utils
from leap_utils.common import logger

# COMMAND ----------

# Quality checks
from great_expectations.dataset import SparkDFDataset
from leap_utils.common.validation import gx_validation

# COMMAND ----------

# MAGIC %md
# MAGIC ## Config Widgets

# COMMAND ----------

# MAGIC %md
# MAGIC ### Pipeline Environments

# COMMAND ----------

dbutils.widgets.removeAll()

dbutils.widgets.text("pipeline_write_env", "dev")
PIPELINE_WRITE_ENV = dbutils.widgets.get("pipeline_write_env")

dbutils.widgets.text("pipeline_read_env", "dev")
PIPELINE_READ_ENV = dbutils.widgets.get("pipeline_read_env")

dbutils.widgets.text("reference_read_env", "prod")
REFERENCE_READ_ENV = dbutils.widgets.get("reference_read_env")

# COMMAND ----------

# MAGIC %md
# MAGIC ### Debug Boolean

# COMMAND ----------

dbutils.widgets.text("debug", "False")
DEBUG = dbutils.widgets.get("debug").lower() == "true"

dbutils.widgets.dropdown(
    "log_level",
    defaultValue="info",
    choices=["debug", "info", "warning", "error", "critical"],
)
LOG_LEVEL = dbutils.widgets.get("log_level")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Config logger

# COMMAND ----------

log = logger.setup_applevel_logger(log_level=logger.LOGGER_MAPPING[LOG_LEVEL])

# COMMAND ----------

# MAGIC %md
# MAGIC ## Config - Common Functions (across whole notebook)

# COMMAND ----------

# N/A

# COMMAND ----------

# MAGIC %md
# MAGIC ## Config other

# COMMAND ----------

SOURCE_SCHEMA = "quality_system"
DESTINATION_SCHEMA = "quality_system"

DESTINATION_TABLE = "document_categories_intraqual"
PK_COLUMN = "document_categories_intraqual_ID"

CATEGORY_ANCESTORS_TABLE = "category_ancestors_intraqual"
CATEGORY_ANCESTORS_PK = "category_ancestors_intraqual_ID"

# The referential is multilingual but only one slot is ever populated
BASE_LANGUAGE_ID = 1

# The source tree is 7 levels deep. The walk stops well beyond that, so a parent cycle at
# source surfaces as a quality failure instead of an endless walk.
MAX_HIERARCHY_DEPTH = 20

CATEGORY_PATH_SEPARATOR = " > "

# Only used to check the walk against the depth the source declares
EXPOSED_EXCLUDED_COLUMNS = ["resolved_category_depth"]

# COMMAND ----------

# MAGIC %md
# MAGIC # Inputs

# COMMAND ----------

# MAGIC %md
# MAGIC ## Inputs - Functions

# COMMAND ----------

# N/A

# COMMAND ----------

# MAGIC %md
# MAGIC ## Inputs - LEAP tables

# COMMAND ----------

# MAGIC %md
# MAGIC ### Import Document Categories
# MAGIC
# MAGIC **df_document_categories_raw** is the driving table: one row per revision and category,
# MAGIC main and secondary alike.

# COMMAND ----------

df_document_categories_raw = spark.read.table(
    f"{PIPELINE_READ_ENV}_silver.{SOURCE_SCHEMA}.document_categories_intraqual"
)

# COMMAND ----------

# MAGIC %md
# MAGIC ### Import Categories

# COMMAND ----------

df_categories_raw = spark.read.table(
    f"{PIPELINE_READ_ENV}_silver.{SOURCE_SCHEMA}.categories_intraqual"
)

# COMMAND ----------

# MAGIC %md
# MAGIC ### Import Documents
# MAGIC
# MAGIC **df_documents_raw** supplies the few document attributes needed to make a row of this
# MAGIC bridge readable without a further join.

# COMMAND ----------

df_documents_raw = spark.read.table(
    f"{PIPELINE_READ_ENV}_silver.{SOURCE_SCHEMA}.documents_intraqual"
)

# COMMAND ----------

# MAGIC %md
# MAGIC ## Inputs - File Read

# COMMAND ----------

# N/A

# COMMAND ----------

# MAGIC %md
# MAGIC ## Inputs - Manual declaration

# COMMAND ----------

# N/A

# COMMAND ----------

# MAGIC %md
# MAGIC # Data Preparation

# COMMAND ----------

# MAGIC %md
# MAGIC ## Prep 1 - Restrict the category referential to the populated language slot
# MAGIC
# MAGIC Every category is repeated once per language slot. Joining without this filter multiplies
# MAGIC the bridge by the number of slots.

# COMMAND ----------

df_categories_prep = df_categories_raw.filter(
    f.col("language_ID") == BASE_LANGUAGE_ID
).select("category_ID", "parent_category_ID", "category_name", "category_level")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Prep 2 - Reduce the documents to their identifying attributes

# COMMAND ----------

df_documents_prep = df_documents_raw.select(
    "document_revision_ID",
    "document_ID",
    "document_reference",
    "document_title",
    "document_index",
    "revision_status_code",
    "published_date",
)

# COMMAND ----------

# MAGIC %md
# MAGIC # Data Transformations

# COMMAND ----------

# MAGIC %md
# MAGIC ## Tr 1 - Resolve every ancestor of every category
# MAGIC
# MAGIC A document filed under a sub-category also belongs to every category above it. One row
# MAGIC per category and ancestor answers "everything under X" with a single join, at any depth.

# COMMAND ----------

df_category_parents = df_categories_prep.select(
    f.col("category_ID").alias("ancestor_category_ID"),
    f.col("parent_category_ID").alias("next_ancestor_category_ID"),
    f.col("category_name").alias("ancestor_category_name"),
)

# Every category is its own ancestor at distance 0, so a filter on a category keeps it
df_frontier = df_categories_prep.select(
    "category_ID",
    f.col("category_ID").alias("ancestor_category_ID"),
    f.lit(0).alias("levels_between"),
)

# Root categories point at a parent that does not exist, so the inner join ends their walk
df_category_ancestors = None
for _ in range(MAX_HIERARCHY_DEPTH):
    df_step = df_frontier.join(
        df_category_parents, on="ancestor_category_ID", how="inner"
    )
    df_category_ancestors = (
        df_step
        if df_category_ancestors is None
        else df_category_ancestors.unionByName(df_step)
    )
    df_frontier = df_step.filter(
        f.col("next_ancestor_category_ID").isNotNull()
    ).select(
        "category_ID",
        f.col("next_ancestor_category_ID").alias("ancestor_category_ID"),
        (f.col("levels_between") + 1).alias("levels_between"),
    )

df_category_ancestors_end = df_category_ancestors.select(
    f.concat_ws("-", "category_ID", "ancestor_category_ID").alias(
        CATEGORY_ANCESTORS_PK
    ),
    "category_ID",
    "ancestor_category_ID",
    "ancestor_category_name",
    "levels_between",
)

# COMMAND ----------

# MAGIC %md
# MAGIC ## Tr 2 - Place each category in its tree
# MAGIC
# MAGIC The path reads root first, so sorting on it groups each branch together.

# COMMAND ----------

df_category_position = df_category_ancestors_end.groupBy("category_ID").agg(
    f.sort_array(
        f.collect_list(
            f.struct(
                "levels_between", "ancestor_category_ID", "ancestor_category_name"
            )
        ),
        asc=False,
    ).alias("ancestors_root_first"),
    (f.max("levels_between") + 1).cast("bigint").alias("resolved_category_depth"),
)

df_parent_IDs = (
    df_categories_prep.select(f.col("parent_category_ID").alias("category_ID"))
    .distinct()
    .withColumn("has_child_category", f.lit(True))
)

# Every category is its own ancestor, so the array is never empty and index 0 is the root
df_categories_enriched = (
    df_categories_prep.join(df_category_position, on="category_ID", how="left")
    .join(df_parent_IDs, on="category_ID", how="left")
    .select(
        "category_ID",
        "parent_category_ID",
        "category_name",
        "category_level",
        f.array_join(
            f.transform("ancestors_root_first", lambda a: a["ancestor_category_name"]),
            CATEGORY_PATH_SEPARATOR,
        ).alias("category_path"),
        f.transform(
            "ancestors_root_first", lambda a: a["ancestor_category_ID"]
        ).alias("category_ID_path"),
        f.col("ancestors_root_first")[0]["ancestor_category_ID"].alias(
            "root_category_ID"
        ),
        f.col("ancestors_root_first")[0]["ancestor_category_name"].alias(
            "root_category_name"
        ),
        f.col("has_child_category").isNull().alias("is_leaf_category"),
        "resolved_category_depth",
    )
)

# COMMAND ----------

# MAGIC %md
# MAGIC ## Tr 3 - Join the bridge to its category and its document
# MAGIC
# MAGIC Both joins are left joins: an attachment pointing at a category or a revision deleted at
# MAGIC source must stay visible rather than disappear from the bridge.

# COMMAND ----------

df_document_categories_end = (
    df_document_categories_raw.join(
        df_categories_enriched, on="category_ID", how="left"
    )
    .join(df_documents_prep, on="document_revision_ID", how="left")
)

log.info(
    "Category bridge joined to the category path and the document - null rates on the "
    "joined keys are validated in the Quality Checks section"
)

# COMMAND ----------

# MAGIC %md
# MAGIC ## Tr 4 - Primary key positioning

# COMMAND ----------

# The select reorders, it does not filter
df_document_categories_end = df_document_categories_end.select(
    [PK_COLUMN] + [c for c in df_document_categories_end.columns if c != PK_COLUMN]
)

# COMMAND ----------

# MAGIC %md
# MAGIC # Quality Checks

# COMMAND ----------

df_gx = SparkDFDataset(df_document_categories_end, persist=False)

red_quality_check_results = [
    df_gx.expect_column_values_to_not_be_null(column=PK_COLUMN),
    df_gx.expect_column_values_to_be_unique(column=PK_COLUMN),
]

gx_validation.validate_and_log_gx_results(
    quality_check_results=red_quality_check_results,
    validation_level="RED",
    info_message="Every document-to-category attachment appears exactly once",
    error_message="Quality checks failed for Intraqual Document Categories gold table - PK is not unique or contains nulls",
    pipeline_write_env=PIPELINE_WRITE_ENV,
    table_name=DESTINATION_TABLE,
    log_to_event_table=True,
    log_successes=True,
)

# COMMAND ----------

# MAGIC %md
# MAGIC ## Quality Check - Resolved joins
# MAGIC
# MAGIC An attachment that resolves neither its category nor its document carries no information.
# MAGIC It stays in the table, but it must be visible rather than counted as a real categorisation.

# COMMAND ----------

amber_quality_check_results = [
    df_gx.expect_column_values_to_not_be_null(column="category_name"),
    df_gx.expect_column_values_to_not_be_null(column="document_ID"),
]

gx_validation.validate_and_log_gx_results(
    quality_check_results=amber_quality_check_results,
    validation_level="AMBER",
    info_message="Every attachment resolved both its category and its document",
    error_message="Some Intraqual category attachments point at a category or a document that no longer exists at source",
    pipeline_write_env=PIPELINE_WRITE_ENV,
    table_name=DESTINATION_TABLE,
    log_to_event_table=True,
    log_successes=True,
)

# COMMAND ----------

# MAGIC %md
# MAGIC ## Quality Check - Category tree

# COMMAND ----------

df_gx_ancestors = SparkDFDataset(df_category_ancestors_end, persist=False)

red_ancestors_results = [
    df_gx_ancestors.expect_column_values_to_not_be_null(column=CATEGORY_ANCESTORS_PK),
    df_gx_ancestors.expect_column_values_to_be_unique(column=CATEGORY_ANCESTORS_PK),
    # A walk reaching the bound means a parent cycle at source, not a deep tree
    df_gx_ancestors.expect_column_max_to_be_between(
        column="levels_between", max_value=MAX_HIERARCHY_DEPTH - 1
    ),
]

gx_validation.validate_and_log_gx_results(
    quality_check_results=red_ancestors_results,
    validation_level="RED",
    info_message="Every category and ancestor pair appears once and the tree has no cycle",
    error_message="Quality checks failed for Intraqual Category Ancestors gold table - PK is not unique, contains nulls, or the category tree contains a parent cycle",
    pipeline_write_env=PIPELINE_WRITE_ENV,
    table_name=CATEGORY_ANCESTORS_TABLE,
    log_to_event_table=True,
    log_successes=True,
)

# COMMAND ----------

df_gx_categories = SparkDFDataset(
    df_categories_enriched.withColumn(
        "depth_gap", f.col("resolved_category_depth") - f.col("category_level")
    ),
    persist=False,
)

amber_categories_results = [
    df_gx_categories.expect_column_values_to_be_in_set(
        column="depth_gap", value_set=[0]
    ),
]

gx_validation.validate_and_log_gx_results(
    quality_check_results=amber_categories_results,
    validation_level="AMBER",
    info_message="The depth found by walking the tree matches the depth declared at source",
    error_message="Some Intraqual categories sit at a depth different from the one declared at source - a parent link is broken or the declared level is stale",
    pipeline_write_env=PIPELINE_WRITE_ENV,
    table_name=DESTINATION_TABLE,
    log_to_event_table=True,
    log_successes=True,
)

# COMMAND ----------

if DEBUG:
    df_document_categories_end.limit(5).display()
    df_category_ancestors_end.limit(5).display()

# COMMAND ----------

# MAGIC %md
# MAGIC # Outputs

# COMMAND ----------

# MAGIC %md
# MAGIC ## Output - Common Functions

# COMMAND ----------

# N/A

# COMMAND ----------

# MAGIC %md
# MAGIC ## Output - LEAP Tables
# MAGIC
# MAGIC ### Output - Gold Intraqual Document Categories

# COMMAND ----------

CATALOG = f"{PIPELINE_WRITE_ENV}_gold"
DESTINATION_TABLE_FULL_PATH = f"{CATALOG}.{DESTINATION_SCHEMA}.{DESTINATION_TABLE}"

table_utils.save_table(
    dest_table=DESTINATION_TABLE_FULL_PATH,
    df=df_document_categories_end,
    overwrite_schema=True,
    mode="overwrite",
)

# COMMAND ----------

# MAGIC %md
# MAGIC ### Adding constraint primary key in DBX

# COMMAND ----------

TABLE_CONSTRAINT_NAME = f"gold_{DESTINATION_TABLE}_PK"

nb_cons = spark.sql(
    f"SELECT COUNT(*) FROM system.information_schema.table_constraints "
    f"WHERE table_catalog='{CATALOG}' AND table_schema='{DESTINATION_SCHEMA}' "
    f"AND table_name='{DESTINATION_TABLE}' AND constraint_name='{TABLE_CONSTRAINT_NAME}'"
).first()[0]

if nb_cons == 0:
    table_utils.set_table_column_not_null(
        table_path=DESTINATION_TABLE_FULL_PATH, column=PK_COLUMN
    )
    table_utils.add_table_primary_key(
        table_path=DESTINATION_TABLE_FULL_PATH,
        pk_name=TABLE_CONSTRAINT_NAME,
        columns=PK_COLUMN,
    )
    log.info(f"Primary key constraint '{TABLE_CONSTRAINT_NAME}' added.")
else:
    log.info(f"Primary key constraint '{TABLE_CONSTRAINT_NAME}' already exists.")

# COMMAND ----------

# MAGIC %md
# MAGIC ### Output - Gold Exposed Intraqual Document Categories
# MAGIC
# MAGIC Columns still carrying a source prefix are the technical fields the header dictionary
# MAGIC never described. They stay in the table and out of the published contract.

# COMMAND ----------

exposed_columns = [
    col
    for col in df_document_categories_end.columns
    if not col.startswith("GD_")
    and not col.startswith("_")
    and col not in EXPOSED_EXCLUDED_COLUMNS
]

table_utils.create_table_view(
    src_full_tablename=DESTINATION_TABLE_FULL_PATH,
    dest_full_tablename=f"{DESTINATION_TABLE_FULL_PATH}_exposed",
    select_statement=", ".join(exposed_columns),
    replace_view=True,
)

# COMMAND ----------

# MAGIC %md
# MAGIC ### Output - Gold Intraqual Category Ancestors

# COMMAND ----------

CATEGORY_ANCESTORS_FULL_PATH = f"{CATALOG}.{DESTINATION_SCHEMA}.{CATEGORY_ANCESTORS_TABLE}"

table_utils.save_table(
    dest_table=CATEGORY_ANCESTORS_FULL_PATH,
    df=df_category_ancestors_end,
    overwrite_schema=True,
    mode="overwrite",
)

ANCESTORS_CONSTRAINT_NAME = f"gold_{CATEGORY_ANCESTORS_TABLE}_PK"

nb_cons = spark.sql(
    f"SELECT COUNT(*) FROM system.information_schema.table_constraints "
    f"WHERE table_catalog='{CATALOG}' AND table_schema='{DESTINATION_SCHEMA}' "
    f"AND table_name='{CATEGORY_ANCESTORS_TABLE}' "
    f"AND constraint_name='{ANCESTORS_CONSTRAINT_NAME}'"
).first()[0]

if nb_cons == 0:
    table_utils.set_table_column_not_null(
        table_path=CATEGORY_ANCESTORS_FULL_PATH, column=CATEGORY_ANCESTORS_PK
    )
    table_utils.add_table_primary_key(
        table_path=CATEGORY_ANCESTORS_FULL_PATH,
        pk_name=ANCESTORS_CONSTRAINT_NAME,
        columns=CATEGORY_ANCESTORS_PK,
    )
    log.info(f"Primary key constraint '{ANCESTORS_CONSTRAINT_NAME}' added.")
else:
    log.info(f"Primary key constraint '{ANCESTORS_CONSTRAINT_NAME}' already exists.")

table_utils.create_table_view(
    src_full_tablename=CATEGORY_ANCESTORS_FULL_PATH,
    dest_full_tablename=f"{CATEGORY_ANCESTORS_FULL_PATH}_exposed",
    select_statement=", ".join(df_category_ancestors_end.columns),
    replace_view=True,
)

# COMMAND ----------

# MAGIC %md
# MAGIC ## Output - Files

# COMMAND ----------

# N/A
