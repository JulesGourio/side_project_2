# Databricks notebook source
# MAGIC %md
# MAGIC
# MAGIC # CREATE GOLD INTRAQUAL DOCUMENTS
# MAGIC
# MAGIC **Description:**
# MAGIC Creates the two central gold tables of the Intraqual asset. The first holds one row per
# MAGIC document revision, joined to its category, type, people and custom form values, and
# MAGIC placed in its revision sequence. The second keeps one row per document, its most recent
# MAGIC revision, which is the grain most questions about a document need. Neither table interprets the
# MAGIC data: no business rule is computed here.
# MAGIC
# MAGIC **Highlighted complexities:**
# MAGIC - Section Prep 3 — custom fields are stored as key-value pairs and are pivoted to columns
# MAGIC - Section Tr 2 — the validity window is derived from the revision sequence
# MAGIC
# MAGIC **Intended Pipeline**
# MAGIC - Data asset - Quality - Intraqual
# MAGIC
# MAGIC **Input Tables Pipeline**
# MAGIC - {pipeline_read_env}_silver.quality_system.documents_intraqual
# MAGIC - {pipeline_read_env}_silver.quality_system.categories_intraqual
# MAGIC - {pipeline_read_env}_silver.quality_system.document_categories_intraqual
# MAGIC - {pipeline_read_env}_gold.quality_system.document_categories_intraqual_exposed
# MAGIC - {pipeline_read_env}_silver.quality_system.document_types_intraqual
# MAGIC - {pipeline_read_env}_silver.quality_system.users_intraqual
# MAGIC - {pipeline_read_env}_silver.quality_system.document_form_values_intraqual
# MAGIC
# MAGIC **Inputs Reference Data**
# MAGIC - N/A
# MAGIC
# MAGIC **Output Tables (Pipeline)**
# MAGIC - {pipeline_write_env}_gold.quality_system.document_revisions_intraqual
# MAGIC - {pipeline_write_env}_gold.quality_system.document_revisions_intraqual_exposed
# MAGIC - {pipeline_write_env}_gold.quality_system.documents_intraqual
# MAGIC - {pipeline_write_env}_gold.quality_system.documents_intraqual_exposed

# COMMAND ----------

# MAGIC %md
# MAGIC # Technical Debt
# MAGIC
# MAGIC 1. Item in section Prep 3 — custom field names are free text at source, so the column
# MAGIC    mapping is declared in Config. It belongs in a reference table.
# MAGIC 2. Item in section Quality Checks — a document can carry several current revisions, or
# MAGIC    none. Neither is resolvable here.
# MAGIC 3. Item in section Prep 1 — the lifecycle state code is carried through unlabelled: no
# MAGIC    reference table exists for its values.
# MAGIC 4. Item in section Tr 2 — publication is held to the day, so two revisions published on
# MAGIC    the same date both cover it.

# COMMAND ----------

# MAGIC %md
# MAGIC # Configuration

# COMMAND ----------

# MAGIC %md
# MAGIC ## Config Standard Package Imports

# COMMAND ----------

# STANDARD FUNCTION
import pyspark.sql.functions as f
from pyspark.sql.window import Window

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

REVISIONS_TABLE = "document_revisions_intraqual"
REVISIONS_PK = "document_revision_ID"

DOCUMENTS_TABLE = "documents_intraqual"
DOCUMENTS_PK = "document_ID"

# The referential is multilingual but only one slot is ever populated. Every label read by
# the business comes from this slot; the others are empty or hold untranslated content.
BASE_LANGUAGE_ID = 1

# A revision is the live one when it carries this status
CURRENT_REVISION_STATUS_CODE = 1

# Custom field labels are long free text at source and cannot produce usable column names.
# The identifiers are stable, so the business name is attached to the identifier instead.
FORM_FIELD_COLUMN_BY_ID = {
    1: "internal_applicability",
    2: "supplier_applicability",
    3: "level_3_applicability",
    4: "level_4_applicability",
    5: "form_comment",
    6: "part_21g_applicability",
    7: "part_145_applicability",
    8: "customer_deliverable_applicability",
    14: "part_21j_applicability",
    15: "document_official_language",
    16: "master_document_reference",
}

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
# MAGIC ### Import Documents
# MAGIC
# MAGIC **df_documents_raw** holds every revision of every document. It is the driving table of
# MAGIC this notebook and its grain is the revision, not the document.

# COMMAND ----------

df_documents_raw = spark.read.table(
    f"{PIPELINE_READ_ENV}_silver.{SOURCE_SCHEMA}.documents_intraqual"
)

# COMMAND ----------

# MAGIC %md
# MAGIC ### Import Categories and Document Categories

# COMMAND ----------

df_categories_raw = spark.read.table(
    f"{PIPELINE_READ_ENV}_silver.{SOURCE_SCHEMA}.categories_intraqual"
)

df_document_categories_raw = spark.read.table(
    f"{PIPELINE_READ_ENV}_silver.{SOURCE_SCHEMA}.document_categories_intraqual"
)

# COMMAND ----------

# MAGIC %md
# MAGIC ### Import Category Positions
# MAGIC
# MAGIC **df_category_positions_raw** carries the path and root of each category, resolved once in
# MAGIC the category bridge so the tree walk is not repeated here.

# COMMAND ----------

df_category_positions_raw = spark.read.table(
    f"{PIPELINE_READ_ENV}_gold.{SOURCE_SCHEMA}.document_categories_intraqual_exposed"
)

# COMMAND ----------

# MAGIC %md
# MAGIC ### Import Document Types and Users

# COMMAND ----------

df_document_types_raw = spark.read.table(
    f"{PIPELINE_READ_ENV}_silver.{SOURCE_SCHEMA}.document_types_intraqual"
)

df_users_raw = spark.read.table(
    f"{PIPELINE_READ_ENV}_silver.{SOURCE_SCHEMA}.users_intraqual"
)

# COMMAND ----------

# MAGIC %md
# MAGIC ### Import Form Values

# COMMAND ----------

df_form_values_raw = spark.read.table(
    f"{PIPELINE_READ_ENV}_silver.{SOURCE_SCHEMA}.document_form_values_intraqual"
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
# MAGIC ## Prep 1 - Restrict the referentials to the populated language slot
# MAGIC
# MAGIC Categories and document types repeat every row once per language slot. Joining without
# MAGIC this filter multiplies the document table by the number of slots.

# COMMAND ----------

df_categories_prep = df_categories_raw.filter(
    f.col("language_ID") == BASE_LANGUAGE_ID
).select("category_ID", "parent_category_ID", "category_name", "category_level")

# Path and root depend on the category alone, so any of its attachments gives the same values
df_category_positions_prep = df_category_positions_raw.select(
    "category_ID",
    "category_path",
    "category_ID_path",
    "root_category_ID",
    "root_category_name",
    "is_leaf_category",
).dropDuplicates(["category_ID"])

df_document_types_prep = df_document_types_raw.filter(
    f.col("language_ID") == BASE_LANGUAGE_ID
).select("document_type_ID", "document_type", "review_period_months")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Prep 2 - Resolve one main category per revision
# MAGIC
# MAGIC A revision can be attached to several categories, one of which is flagged as the main one.
# MAGIC A few revisions carry more than one main flag, so the lowest category identifier is kept
# MAGIC to make the choice repeatable between runs rather than left to row order.
# MAGIC
# MAGIC Only the main category reaches this table. The other attachments are published one row
# MAGIC each in the category bridge, which is where a document is counted under all of them.

# COMMAND ----------

window_main_category = Window.partitionBy("document_revision_ID").orderBy(
    f.col("category_ID").asc()
)

df_main_category = (
    df_document_categories_raw.filter(f.col("is_main_category"))
    .withColumn("main_category_rank", f.row_number().over(window_main_category))
    .filter(f.col("main_category_rank") == 1)
    .select("document_revision_ID", "category_ID")
)

# COMMAND ----------

# MAGIC %md
# MAGIC ## Prep 3 - Pivot the custom form fields
# MAGIC
# MAGIC The custom fields are stored one row per revision and field. Pivoting them gives the
# MAGIC document row its fields as columns, filterable without an extra join.

# COMMAND ----------

form_field_ids = sorted(FORM_FIELD_COLUMN_BY_ID.keys())

df_form_values_pivot = (
    df_form_values_raw.filter(f.col("form_field_ID").isin(form_field_ids))
    .groupBy("document_revision_ID")
    .pivot("form_field_ID", form_field_ids)
    .agg(f.first("form_field_value"))
)

df_form_values_prep = df_form_values_pivot.withColumnsRenamed(
    {str(field_id): FORM_FIELD_COLUMN_BY_ID[field_id] for field_id in form_field_ids}
)

# COMMAND ----------

# MAGIC %md
# MAGIC ## Prep 4 - Reduce the user referential to a display name

# COMMAND ----------

df_users_prep = df_users_raw.select(
    f.col("user_ID"),
    f.concat_ws(" ", f.col("first_name"), f.col("last_name")).alias("user_name"),
    f.col("email").alias("user_email"),
)

# COMMAND ----------

# MAGIC %md
# MAGIC # Data Transformations

# COMMAND ----------

# MAGIC %md
# MAGIC ## Tr 1 - Join the revisions to their referentials
# MAGIC
# MAGIC Every join is a left join: a revision whose type or category has been deleted at source
# MAGIC must still reach Gold rather than disappear from the document count.

# COMMAND ----------

df_revisions_joined = (
    df_documents_raw.join(df_main_category, on="document_revision_ID", how="left")
    .join(df_categories_prep, on="category_ID", how="left")
    .join(df_category_positions_prep, on="category_ID", how="left")
    .join(df_document_types_prep, on="document_type_ID", how="left")
    .join(df_form_values_prep, on="document_revision_ID", how="left")
    .join(
        df_users_prep.select(
            f.col("user_ID").alias("writer_user_ID"),
            f.col("user_name").alias("writer_name"),
            f.col("user_email").alias("writer_email"),
        ),
        on="writer_user_ID",
        how="left",
    )
    .join(
        df_users_prep.select(
            f.col("user_ID").alias("issuer_user_ID"),
            f.col("user_name").alias("issuer_name"),
            f.col("user_email").alias("issuer_email"),
        ),
        on="issuer_user_ID",
        how="left",
    )
)

log.info(
    "Revisions joined to categories, types, users and form values - null rates on the "
    "joined keys are validated in the Quality Checks section"
)

# COMMAND ----------

# MAGIC %md
# MAGIC ## Tr 2 - Rank the revisions and derive the validity window
# MAGIC
# MAGIC The source numbers its own revisions, and that number is kept as the authority. The rank
# MAGIC computed here only orders the rows so that each revision can inherit the publication date
# MAGIC of the one that replaced it, which is what makes the referential readable at a past date.

# COMMAND ----------

window_revision = Window.partitionBy("document_ID").orderBy(
    f.col("document_revision_ID").asc()
)

# A revision is superseded by the next one that was actually published, which is not always
# the one that follows it: a revision can carry no publication date at all.
window_following_revisions = (
    Window.partitionBy("document_ID")
    .orderBy(f.col("document_revision_ID").asc())
    .rowsBetween(1, Window.unboundedFollowing)
)

column_next_published_date = f.first(
    f.col("published_date"), ignorenulls=True
).over(window_following_revisions)

df_revisions_ranked = (
    df_revisions_joined.withColumn(
        "revision_rank", f.row_number().over(window_revision)
    )
    .withColumn("total_revisions", f.count("*").over(Window.partitionBy("document_ID")))
    .withColumn(
        "is_current_revision",
        f.col("revision_status_code") == CURRENT_REVISION_STATUS_CODE,
    )
    .withColumn("valid_from_date", f.col("published_date"))
    .withColumn(
        "valid_to_date",
        # A revision that was never published has no window at all, and one still in force
        # has no end. Where a document was republished the same day, the window closes on
        # that day rather than the day before, which would end it before it began.
        f.when(
            f.col("published_date").isNotNull()
            & column_next_published_date.isNotNull(),
            f.greatest(
                f.date_sub(column_next_published_date, 1), f.col("published_date")
            ),
        ),
    )
)

# COMMAND ----------

# MAGIC %md
# MAGIC ## Tr 3 - Primary key positioning

# COMMAND ----------

df_revisions_end = df_revisions_ranked

# Position the primary key first — the select reorders, it does not filter
df_revisions_end = df_revisions_end.select(
    [REVISIONS_PK] + [c for c in df_revisions_end.columns if c != REVISIONS_PK]
)

# COMMAND ----------

# MAGIC %md
# MAGIC ## Tr 4 - Reduce to one row per document
# MAGIC
# MAGIC The most recent revision is kept rather than the current one: around a tenth of documents
# MAGIC have no revision flagged current, and dropping them would silently shrink the referential.

# COMMAND ----------

window_latest_revision = Window.partitionBy("document_ID").orderBy(
    f.col("revision_rank").desc()
)

df_documents_end = (
    df_revisions_end.withColumn(
        "latest_revision_rank", f.row_number().over(window_latest_revision)
    )
    .filter(f.col("latest_revision_rank") == 1)
    .drop("latest_revision_rank")
)

df_documents_end = df_documents_end.select(
    [DOCUMENTS_PK] + [c for c in df_documents_end.columns if c != DOCUMENTS_PK]
)

# COMMAND ----------

# MAGIC %md
# MAGIC # Quality Checks

# COMMAND ----------

# MAGIC %md
# MAGIC ## Quality Check - Document Revisions

# COMMAND ----------

df_gx_revisions = SparkDFDataset(df_revisions_end, persist=False)

red_revisions_results = [
    df_gx_revisions.expect_column_values_to_not_be_null(column=REVISIONS_PK),
    df_gx_revisions.expect_column_values_to_be_unique(column=REVISIONS_PK),
    df_gx_revisions.expect_column_values_to_not_be_null(column="document_ID"),
]

gx_validation.validate_and_log_gx_results(
    quality_check_results=red_revisions_results,
    validation_level="RED",
    info_message="Every revision is uniquely identified and attached to a document",
    error_message="Quality checks failed for Intraqual Document Revisions gold table - PK is not unique, contains nulls, or a revision lost its document identity in the joins",
    pipeline_write_env=PIPELINE_WRITE_ENV,
    table_name=REVISIONS_TABLE,
    log_to_event_table=True,
    log_successes=True,
)

# COMMAND ----------

# MAGIC %md
# MAGIC ## Quality Check - Date completeness
# MAGIC
# MAGIC Age and review rules on a document rest on these two dates. A missing publication
# MAGIC date is not a pipeline fault, but it silently removes a document from any such rule, so
# MAGIC the size of that population has to stay visible.

# COMMAND ----------

amber_date_results = [
    df_gx_revisions.expect_column_values_to_not_be_null(column="published_date"),
    df_gx_revisions.expect_column_values_to_not_be_null(column="next_review_date"),
]

gx_validation.validate_and_log_gx_results(
    quality_check_results=amber_date_results,
    validation_level="AMBER",
    info_message="Every revision carries a publication date and a next review date",
    error_message="Some Intraqual revisions have no publication or review date and will fall out of any age-based rule built on them",
    pipeline_write_env=PIPELINE_WRITE_ENV,
    table_name=REVISIONS_TABLE,
    log_to_event_table=True,
    log_successes=True,
)

# COMMAND ----------

# MAGIC %md
# MAGIC ## Quality Check - Referential joins
# MAGIC
# MAGIC Each of these is a left join on a referential that can be incomplete at source. The rows
# MAGIC survive either way; what matters is that the gaps stay visible instead of being read as
# MAGIC genuinely uncategorised or untyped documents.

# COMMAND ----------

amber_join_results = [
    df_gx_revisions.expect_column_values_to_not_be_null(column="category_ID"),
    df_gx_revisions.expect_column_values_to_not_be_null(column="document_type"),
]

gx_validation.validate_and_log_gx_results(
    quality_check_results=amber_join_results,
    validation_level="AMBER",
    info_message="Every revision resolved a main category and a document type",
    error_message="Some Intraqual revisions have no main category or no recognised document type - they remain in the table but cannot be grouped in reporting",
    pipeline_write_env=PIPELINE_WRITE_ENV,
    table_name=REVISIONS_TABLE,
    log_to_event_table=True,
    log_successes=True,
)

# COMMAND ----------

# MAGIC %md
# MAGIC ## Quality Check - Documents
# MAGIC
# MAGIC The document table must carry exactly one row per document. The current-revision count is
# MAGIC checked separately: a document with several current revisions is a source condition this
# MAGIC notebook cannot resolve, and one with none is expected for retired documents.

# COMMAND ----------

df_gx_documents = SparkDFDataset(df_documents_end, persist=False)

red_documents_results = [
    df_gx_documents.expect_column_values_to_not_be_null(column=DOCUMENTS_PK),
    df_gx_documents.expect_column_values_to_be_unique(column=DOCUMENTS_PK),
]

gx_validation.validate_and_log_gx_results(
    quality_check_results=red_documents_results,
    validation_level="RED",
    info_message="Every document appears exactly once",
    error_message="Quality checks failed for Intraqual Documents gold table - the reduction to one row per document did not hold",
    pipeline_write_env=PIPELINE_WRITE_ENV,
    table_name=DOCUMENTS_TABLE,
    log_to_event_table=True,
    log_successes=True,
)

# COMMAND ----------

df_current_revision_count = df_revisions_end.groupBy("document_ID").agg(
    f.sum(f.col("is_current_revision").cast("int")).alias("current_revision_count")
)

df_gx_current_revision = SparkDFDataset(df_current_revision_count, persist=False)

gx_validation.validate_and_log_gx_results(
    quality_check_results=[
        df_gx_current_revision.expect_column_values_to_be_between(
            column="current_revision_count", min_value=0, max_value=1
        )
    ],
    validation_level="AMBER",
    info_message="No document carries more than one current revision",
    error_message="Some Intraqual documents carry several current revisions - the reporting layer will count them more than once",
    pipeline_write_env=PIPELINE_WRITE_ENV,
    table_name=DOCUMENTS_TABLE,
    log_to_event_table=True,
    log_successes=True,
)

# COMMAND ----------

if DEBUG:
    df_revisions_end.limit(5).display()
    df_documents_end.limit(5).display()

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
# MAGIC ### Output - Gold Intraqual Document Revisions

# COMMAND ----------

save_gold_table(df_revisions_end, REVISIONS_TABLE, REVISIONS_PK)

# COMMAND ----------

# MAGIC %md
# MAGIC ### Output - Gold Intraqual Documents

# COMMAND ----------

save_gold_table(df_documents_end, DOCUMENTS_TABLE, DOCUMENTS_PK)

# COMMAND ----------

# MAGIC %md
# MAGIC ## Output - Files

# COMMAND ----------

# N/A
