# Databricks notebook source
# MAGIC %md
# MAGIC
# MAGIC # CREATE GOLD INTRAQUAL DOCUMENT RELATIONS
# MAGIC
# MAGIC **Description:**
# MAGIC Creates the two gold tables describing what a document points at and what has been done to
# MAGIC it. The first gathers every link a document carries, whether to another document or to an
# MAGIC address outside the system. The second holds the periodic review events, named with their
# MAGIC reviewer.
# MAGIC
# MAGIC **Highlighted complexities:**
# MAGIC - Section Tr 1 — internal and external links are two source tables published as one
# MAGIC
# MAGIC **Intended Pipeline**
# MAGIC - Data asset - Quality - Intraqual
# MAGIC
# MAGIC **Input Tables Pipeline**
# MAGIC - {pipeline_read_env}_silver.quality_system.document_links_intraqual
# MAGIC - {pipeline_read_env}_silver.quality_system.document_external_links_intraqual
# MAGIC - {pipeline_read_env}_silver.quality_system.document_reviews_intraqual
# MAGIC - {pipeline_read_env}_silver.quality_system.documents_intraqual
# MAGIC - {pipeline_read_env}_silver.quality_system.users_intraqual
# MAGIC
# MAGIC **Inputs Reference Data**
# MAGIC - N/A
# MAGIC
# MAGIC **Output Tables (Pipeline)**
# MAGIC - {pipeline_write_env}_gold.quality_system.document_links_intraqual
# MAGIC - {pipeline_write_env}_gold.quality_system.document_links_intraqual_exposed
# MAGIC - {pipeline_write_env}_gold.quality_system.document_reviews_intraqual
# MAGIC - {pipeline_write_env}_gold.quality_system.document_reviews_intraqual_exposed

# COMMAND ----------

# MAGIC %md
# MAGIC # Technical Debt
# MAGIC
# MAGIC 1. Item in section Tr 1 — internal and external links are published as one table with a
# MAGIC    type column, which is still to be confirmed against the specification.
# MAGIC 2. Item in section Inputs — all three source tables are populated in the production source
# MAGIC    only.

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

LINKS_TABLE = "document_links_intraqual"
LINKS_PK = "document_links_intraqual_ID"

REVIEWS_TABLE = "document_reviews_intraqual"
REVIEWS_PK = "document_review_ID"

# Link targets: another document held in the system, or an address outside it
LINK_TYPE_DOCUMENT = "document"
LINK_TYPE_EXTERNAL = "external"

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
# MAGIC ### Import Document Links

# COMMAND ----------

df_links_raw = spark.read.table(
    f"{PIPELINE_READ_ENV}_silver.{SOURCE_SCHEMA}.document_links_intraqual"
)

# COMMAND ----------

# MAGIC %md
# MAGIC ### Import External Links

# COMMAND ----------

df_external_links_raw = spark.read.table(
    f"{PIPELINE_READ_ENV}_silver.{SOURCE_SCHEMA}.document_external_links_intraqual"
)

# COMMAND ----------

# MAGIC %md
# MAGIC ### Import Document Reviews
# MAGIC
# MAGIC **df_reviews_raw** holds periodic review events. Despite its source name it is not the
# MAGIC revision history — the document table itself carries one row per revision.

# COMMAND ----------

df_reviews_raw = spark.read.table(
    f"{PIPELINE_READ_ENV}_silver.{SOURCE_SCHEMA}.document_reviews_intraqual"
)

# COMMAND ----------

# MAGIC %md
# MAGIC ### Import Documents

# COMMAND ----------

df_documents_raw = spark.read.table(
    f"{PIPELINE_READ_ENV}_silver.{SOURCE_SCHEMA}.documents_intraqual"
)

# COMMAND ----------

# MAGIC %md
# MAGIC ### Import Users

# COMMAND ----------

df_users_raw = spark.read.table(
    f"{PIPELINE_READ_ENV}_silver.{SOURCE_SCHEMA}.users_intraqual"
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
# MAGIC ## Prep 1 - Reduce the documents to their identifying attributes
# MAGIC
# MAGIC Two views of the same table are needed: one for the document holding the link, one for the
# MAGIC document being pointed at.

# COMMAND ----------

df_documents_prep = df_documents_raw.select(
    "document_revision_ID",
    "document_ID",
    "document_reference",
    "document_title",
    "document_index",
    "revision_status_code",
)

df_source_document = df_documents_prep.select(
    f.col("document_revision_ID"),
    f.col("document_ID"),
    f.col("document_reference"),
    f.col("document_title"),
    f.col("document_index"),
)

df_linked_document = df_documents_prep.select(
    f.col("document_revision_ID").alias("linked_document_revision_ID"),
    f.col("document_ID").alias("linked_document_ID"),
    f.col("document_reference").alias("linked_document_reference"),
    f.col("document_title").alias("linked_document_title"),
    f.col("document_index").alias("linked_document_index"),
)

# COMMAND ----------

# MAGIC %md
# MAGIC ## Prep 2 - Reduce the user referential to a display name

# COMMAND ----------

df_users_prep = df_users_raw.select(
    f.col("user_ID").alias("reviewer_user_ID"),
    f.concat_ws(" ", f.col("first_name"), f.col("last_name")).alias("reviewer_name"),
    f.col("email").alias("reviewer_email"),
)

# COMMAND ----------

# MAGIC %md
# MAGIC # Data Transformations

# COMMAND ----------

# MAGIC %md
# MAGIC ## Tr 1 - Gather internal and external links
# MAGIC
# MAGIC A reader asking what a document points at should not have to know whether the target
# MAGIC happens to be held in the system. The two source tables are therefore published as one,
# MAGIC with a type column carrying the distinction and the key prefixed by it so the two sets
# MAGIC cannot collide.

# COMMAND ----------

df_links_typed = df_links_raw.select(
    f.concat_ws("-", f.lit(LINK_TYPE_DOCUMENT), f.col(LINKS_PK)).alias(LINKS_PK),
    f.lit(LINK_TYPE_DOCUMENT).alias("link_type"),
    f.col("document_revision_ID"),
    f.col("linked_document_revision_ID"),
    f.lit(None).cast("string").alias("external_link_url"),
    f.col("link_created_date"),
)

df_external_links_typed = df_external_links_raw.select(
    f.concat_ws(
        "-", f.lit(LINK_TYPE_EXTERNAL), f.col("document_external_links_intraqual_ID")
    ).alias(LINKS_PK),
    f.lit(LINK_TYPE_EXTERNAL).alias("link_type"),
    f.col("document_revision_ID"),
    f.lit(None).cast("bigint").alias("linked_document_revision_ID"),
    f.col("external_link_url"),
    f.col("link_created_date"),
)

df_all_links = df_links_typed.unionByName(
    df_external_links_typed, allowMissingColumns=True
)

# COMMAND ----------

# MAGIC %md
# MAGIC ## Tr 2 - Name both ends of the link
# MAGIC
# MAGIC Left joins throughout: a link whose target revision was deleted at source is itself the
# MAGIC finding, and dropping it would hide exactly the case worth seeing.

# COMMAND ----------

df_links_end = df_all_links.join(
    df_source_document, on="document_revision_ID", how="left"
).join(df_linked_document, on="linked_document_revision_ID", how="left")

df_links_end = df_links_end.select(
    [LINKS_PK] + [c for c in df_links_end.columns if c != LINKS_PK]
)

log.info(
    "Links joined to both ends - unresolved targets are validated in the Quality Checks section"
)

# COMMAND ----------

# MAGIC %md
# MAGIC ## Tr 3 - Name the reviewer and the reviewed document

# COMMAND ----------

df_reviews_end = df_reviews_raw.join(
    df_source_document, on="document_revision_ID", how="left"
).join(df_users_prep, on="reviewer_user_ID", how="left")

df_reviews_end = df_reviews_end.select(
    [REVIEWS_PK] + [c for c in df_reviews_end.columns if c != REVIEWS_PK]
)

# COMMAND ----------

# MAGIC %md
# MAGIC # Quality Checks

# COMMAND ----------

# MAGIC %md
# MAGIC ## Quality Check - Document Links

# COMMAND ----------

df_gx_links = SparkDFDataset(df_links_end, persist=False)

red_links_results = [
    df_gx_links.expect_column_values_to_not_be_null(column=LINKS_PK),
    df_gx_links.expect_column_values_to_be_unique(column=LINKS_PK),
    df_gx_links.expect_column_distinct_values_to_be_in_set(
        column="link_type", value_set={LINK_TYPE_DOCUMENT, LINK_TYPE_EXTERNAL}
    ),
]

gx_validation.validate_and_log_gx_results(
    quality_check_results=red_links_results,
    validation_level="RED",
    info_message="Every link appears exactly once and carries a known type",
    error_message="Quality checks failed for Intraqual Document Links gold table - PK is not unique, contains nulls, or an unknown link type was produced by the union",
    pipeline_write_env=PIPELINE_WRITE_ENV,
    table_name=LINKS_TABLE,
    log_to_event_table=True,
    log_successes=True,
)

# COMMAND ----------

# MAGIC %md
# MAGIC ## Quality Check - Link targets resolve
# MAGIC
# MAGIC A document link pointing at a revision that no longer exists is a broken reference in the
# MAGIC document system itself, which is worth reporting to the source rather than hiding.

# COMMAND ----------

df_document_links_only = df_links_end.filter(
    f.col("link_type") == LINK_TYPE_DOCUMENT
)

df_gx_document_links = SparkDFDataset(df_document_links_only, persist=False)

amber_links_results = [
    df_gx_document_links.expect_column_values_to_not_be_null(
        column="linked_document_ID"
    ),
    df_gx_document_links.expect_column_values_to_not_be_null(column="document_ID"),
]

gx_validation.validate_and_log_gx_results(
    quality_check_results=amber_links_results,
    validation_level="AMBER",
    info_message="Every document link resolves both the document it starts from and the one it points at",
    error_message="Some Intraqual document links point at a revision that no longer exists at source",
    pipeline_write_env=PIPELINE_WRITE_ENV,
    table_name=LINKS_TABLE,
    log_to_event_table=True,
    log_successes=True,
)

# COMMAND ----------

# MAGIC %md
# MAGIC ## Quality Check - Document Reviews

# COMMAND ----------

df_gx_reviews = SparkDFDataset(df_reviews_end, persist=False)

red_reviews_results = [
    df_gx_reviews.expect_column_values_to_not_be_null(column=REVIEWS_PK),
    df_gx_reviews.expect_column_values_to_be_unique(column=REVIEWS_PK),
]

gx_validation.validate_and_log_gx_results(
    quality_check_results=red_reviews_results,
    validation_level="RED",
    info_message="Every review event appears exactly once",
    error_message="Quality checks failed for Intraqual Document Reviews gold table - PK is not unique or contains nulls",
    pipeline_write_env=PIPELINE_WRITE_ENV,
    table_name=REVIEWS_TABLE,
    log_to_event_table=True,
    log_successes=True,
)

# COMMAND ----------

amber_reviews_results = [
    df_gx_reviews.expect_column_values_to_not_be_null(column="document_ID"),
    df_gx_reviews.expect_column_values_to_not_be_null(column="reviewer_name"),
]

gx_validation.validate_and_log_gx_results(
    quality_check_results=amber_reviews_results,
    validation_level="AMBER",
    info_message="Every review event resolves its document and its reviewer",
    error_message="Some Intraqual review events cannot be attributed to a document or a reviewer",
    pipeline_write_env=PIPELINE_WRITE_ENV,
    table_name=REVIEWS_TABLE,
    log_to_event_table=True,
    log_successes=True,
)

# COMMAND ----------

if DEBUG:
    df_links_end.limit(5).display()
    df_reviews_end.limit(5).display()

# COMMAND ----------

# MAGIC %md
# MAGIC # Outputs

# COMMAND ----------

# MAGIC %md
# MAGIC ## Output - Common Functions

# COMMAND ----------

CATALOG = f"{PIPELINE_WRITE_ENV}_gold"


def save_gold_table(df, destination_table, pk_column):
    """
    Write one gold table, attach its primary key constraint and publish its exposed view.

    :param df: The DataFrame to write.
    :type df: DataFrame
    :param destination_table: Target table name within the gold schema.
    :type destination_table: str
    :param pk_column: Primary key column to constrain.
    :type pk_column: str
    :return: None
    :rtype: None
    """
    full_path = f"{CATALOG}.{DESTINATION_SCHEMA}.{destination_table}"

    table_utils.save_table(
        dest_table=full_path,
        df=df,
        overwrite_schema=True,
        mode="overwrite",
    )

    constraint_name = f"gold_{destination_table}_PK"
    nb_cons = spark.sql(
        f"SELECT COUNT(*) FROM system.information_schema.table_constraints "
        f"WHERE table_catalog='{CATALOG}' AND table_schema='{DESTINATION_SCHEMA}' "
        f"AND table_name='{destination_table}' AND constraint_name='{constraint_name}'"
    ).first()[0]

    if nb_cons == 0:
        table_utils.set_table_column_not_null(table_path=full_path, column=pk_column)
        table_utils.add_table_primary_key(
            table_path=full_path, pk_name=constraint_name, columns=pk_column
        )
        log.info(f"Primary key constraint '{constraint_name}' added.")
    else:
        log.info(f"Primary key constraint '{constraint_name}' already exists.")

    # Columns still carrying a source prefix are the technical fields the header dictionary
    # never described. They stay in the table and out of the published contract.
    exposed_columns = [
        col
        for col in df.columns
        if not col.startswith("GD_") and not col.startswith("_")
    ]

    table_utils.create_table_view(
        src_full_tablename=full_path,
        dest_full_tablename=f"{full_path}_exposed",
        select_statement=", ".join(exposed_columns),
        replace_view=True,
    )
    log.info(f"Exposed view published: {full_path}_exposed")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Output - LEAP Tables
# MAGIC
# MAGIC ### Output - Gold Intraqual Document Links

# COMMAND ----------

save_gold_table(df_links_end, LINKS_TABLE, LINKS_PK)

# COMMAND ----------

# MAGIC %md
# MAGIC ### Output - Gold Intraqual Document Reviews

# COMMAND ----------

save_gold_table(df_reviews_end, REVIEWS_TABLE, REVIEWS_PK)

# COMMAND ----------

# MAGIC %md
# MAGIC ## Output - Files

# COMMAND ----------

# N/A
