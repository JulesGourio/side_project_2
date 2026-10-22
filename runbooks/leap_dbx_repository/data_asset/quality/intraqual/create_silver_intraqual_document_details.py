# Databricks notebook source
# MAGIC %md
# MAGIC
# MAGIC # CREATE SILVER INTRAQUAL DOCUMENT DETAILS
# MAGIC
# MAGIC **Description:**
# MAGIC Creates the five silver tables attached to a document revision: custom form values,
# MAGIC periodic reviews, reference changes, links to other documents and external links. Each
# MAGIC applies string cleaning, renaming and casting via its business header table,
# MAGIC deduplication and primary key construction. No join, no filter, no enrichment.
# MAGIC
# MAGIC **Highlighted complexities:**
# MAGIC - Section Config - Common Functions — the five tables share one preparation routine
# MAGIC - Section Config other — three of the five keys include the event date, because the same
# MAGIC   pair of identifiers legitimately repeats on different dates
# MAGIC
# MAGIC **Intended Pipeline**
# MAGIC - Data asset - Quality - Intraqual
# MAGIC
# MAGIC **Input Tables Pipeline**
# MAGIC - {pipeline_read_env}_bronze.intraqual.gd_champdoc_doc_latest
# MAGIC - {pipeline_read_env}_bronze.intraqual.gd_doc_revision_latest
# MAGIC - {pipeline_read_env}_bronze.intraqual.gd_doc_historef_latest
# MAGIC - {pipeline_read_env}_bronze.intraqual.gd_doc_liendoc_latest
# MAGIC - {pipeline_read_env}_bronze.intraqual.gd_doc_lienext_latest
# MAGIC
# MAGIC **Inputs Reference Data**
# MAGIC - {reference_read_env}_bronze.headers_others.headers_intraqual_gd_champdoc_doc
# MAGIC - {reference_read_env}_bronze.headers_others.headers_intraqual_gd_doc_revision
# MAGIC - {reference_read_env}_bronze.headers_others.headers_intraqual_gd_doc_historef
# MAGIC - {reference_read_env}_bronze.headers_others.headers_intraqual_gd_doc_liendoc
# MAGIC - {reference_read_env}_bronze.headers_others.headers_intraqual_gd_doc_lienext
# MAGIC
# MAGIC **Output Tables (Pipeline)**
# MAGIC - {pipeline_write_env}_silver.quality_system.document_form_values_intraqual
# MAGIC - {pipeline_write_env}_silver.quality_system.document_reviews_intraqual
# MAGIC - {pipeline_write_env}_silver.quality_system.document_reference_changes_intraqual
# MAGIC - {pipeline_write_env}_silver.quality_system.document_links_intraqual
# MAGIC - {pipeline_write_env}_silver.quality_system.document_external_links_intraqual

# COMMAND ----------

# MAGIC %md
# MAGIC # Technical Debt
# MAGIC
# MAGIC 1. Item in section Quality Checks — some form values point at a field absent from the
# MAGIC    dictionary and cannot be labelled in Gold.
# MAGIC 2. Item in section Preparation - Document Links — links, external links and reference
# MAGIC    changes are populated in the production source only.

# COMMAND ----------

# MAGIC %md
# MAGIC # Configuration

# COMMAND ----------

# MAGIC %md
# MAGIC ## Config Standard Package Imports

# COMMAND ----------

# STANDARD FUNCTION
import pyspark.sql.functions as f
from pyspark.sql.types import StringType, TimestampType

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
# MAGIC
# MAGIC The five tables go through the same silver routine, so it is written once here and called
# MAGIC per table rather than repeated.

# COMMAND ----------

# The source stores every date as a timestamp. Only the calendar day carries business meaning.
DATE_FORMAT = "yyyy-MM-dd"


def prepare_silver_table(
    df_raw, df_headers, source_prefix, pk_name, pk_source_columns=None
):
    """
    Apply the standard Intraqual silver preparation to one source table.

    :param df_raw: The raw bronze DataFrame.
    :type df_raw: DataFrame
    :param df_headers: The business header table driving rename and cast.
    :type df_headers: DataFrame
    :param source_prefix: Prefix matching Table_name in the header table.
    :type source_prefix: str
    :param pk_name: Primary key column name, natural or constructed.
    :type pk_name: str
    :param pk_source_columns: Columns the key is built from. None when the key is natural.
    :type pk_source_columns: list[str] | None
    :return: The prepared DataFrame with the primary key as first column.
    :rtype: DataFrame
    """
    table_utils.check_schema_of_the_header_file(df_headers)

    # Blanks arrive as empty strings and would otherwise count as populated values
    string_columns = [
        c.name for c in df_raw.schema.fields if isinstance(c.dataType, StringType)
    ]
    df = df_raw.withColumns(
        {
            col: f.when(f.trim(f.col(col)) == "", None).otherwise(f.trim(f.col(col)))
            for col in string_columns
        }
    )

    # The header dictionary decides which fields are dates, so the list is read from it
    date_fields = [
        row["Field"]
        for row in df_headers.filter(f.lower(f.col("Format")) == "date")
        .select("Field")
        .collect()
    ]
    timestamp_columns = [
        c.name
        for c in df.schema.fields
        if isinstance(c.dataType, TimestampType) and c.name in date_fields
    ]
    df = df.withColumns(
        {col: f.date_format(f.col(col), DATE_FORMAT) for col in timestamp_columns}
    )

    # Fields absent from the dictionary keep the prefix, which is how Gold separates the
    # documented business columns from the technical ones when building the exposed view
    df = table_utils.add_prefix_to_columns(df, source_prefix)
    df = table_utils.rename_columns_with_sap_business_header(df, df_headers)
    df = table_utils.cast_columns_with_sap_business_headers(
        df, df_headers, date_format=DATE_FORMAT
    )

    # The source is re-extracted twice a day, so an unchanged row can arrive twice per load
    df = df.dropDuplicates()

    if pk_source_columns:
        df = df.withColumn(
            pk_name, f.concat_ws("-", *[f.col(c) for c in pk_source_columns])
        )

    # All source columns are retained — the select reorders, it does not filter
    return df.select([pk_name] + [c for c in df.columns if c != pk_name])

# COMMAND ----------

# MAGIC %md
# MAGIC ## Config other
# MAGIC
# MAGIC Three of the five keys include the event date. On these tables the same pair of
# MAGIC identifiers legitimately reappears on a later date — a document relinked, or renamed back
# MAGIC to a previous reference — so the pair alone does not identify a row.

# COMMAND ----------

DESTINATION_SCHEMA = "quality_system"

FORM_VALUES_TABLE = "document_form_values_intraqual"
FORM_VALUES_PK = "document_form_values_intraqual_ID"
FORM_VALUES_PK_COLUMNS = ["document_revision_ID", "form_field_ID"]

REVIEWS_TABLE = "document_reviews_intraqual"
REVIEWS_PK = "document_review_ID"

REFERENCE_CHANGES_TABLE = "document_reference_changes_intraqual"
REFERENCE_CHANGES_PK = "document_reference_changes_intraqual_ID"
REFERENCE_CHANGES_PK_COLUMNS = [
    "document_revision_ID",
    "previous_reference",
    "new_reference",
    "reference_change_date",
]

LINKS_TABLE = "document_links_intraqual"
LINKS_PK = "document_links_intraqual_ID"
LINKS_PK_COLUMNS = [
    "document_revision_ID",
    "linked_document_revision_ID",
    "link_created_date",
]

EXTERNAL_LINKS_TABLE = "document_external_links_intraqual"
EXTERNAL_LINKS_PK = "document_external_links_intraqual_ID"
EXTERNAL_LINKS_PK_COLUMNS = [
    "document_revision_ID",
    "external_link_url",
    "link_created_date",
]

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
# MAGIC ### Import Form Values
# MAGIC
# MAGIC **df_form_values_raw** holds the custom field values of a document as key-value pairs: one
# MAGIC row per revision and field, with the value in either a text or a numeric column depending
# MAGIC on the field type.

# COMMAND ----------

df_form_values_raw = spark.read.table(
    f"{PIPELINE_READ_ENV}_bronze.intraqual.gd_champdoc_doc_latest"
)

df_headers_form_values = spark.read.table(
    f"{REFERENCE_READ_ENV}_bronze.headers_others.headers_intraqual_gd_champdoc_doc"
)

# COMMAND ----------

# MAGIC %md
# MAGIC ### Import Document Reviews
# MAGIC
# MAGIC **df_reviews_raw** holds periodic review events. Despite its source name it is not the
# MAGIC revision history — the document table itself carries one row per revision.

# COMMAND ----------

df_reviews_raw = spark.read.table(
    f"{PIPELINE_READ_ENV}_bronze.intraqual.gd_doc_revision_latest"
)

df_headers_reviews = spark.read.table(
    f"{REFERENCE_READ_ENV}_bronze.headers_others.headers_intraqual_gd_doc_revision"
)

# COMMAND ----------

# MAGIC %md
# MAGIC ### Import Reference Changes
# MAGIC
# MAGIC **df_reference_changes_raw** records each renaming of a document reference, which is what
# MAGIC lets a document be traced across a reference change.

# COMMAND ----------

df_reference_changes_raw = spark.read.table(
    f"{PIPELINE_READ_ENV}_bronze.intraqual.gd_doc_historef_latest"
)

df_headers_reference_changes = spark.read.table(
    f"{REFERENCE_READ_ENV}_bronze.headers_others.headers_intraqual_gd_doc_historef"
)

# COMMAND ----------

# MAGIC %md
# MAGIC ### Import Document Links
# MAGIC
# MAGIC **df_links_raw** holds document-to-document links, used to show which documents a
# MAGIC procedure refers to.

# COMMAND ----------

df_links_raw = spark.read.table(
    f"{PIPELINE_READ_ENV}_bronze.intraqual.gd_doc_liendoc_latest"
)

df_headers_links = spark.read.table(
    f"{REFERENCE_READ_ENV}_bronze.headers_others.headers_intraqual_gd_doc_liendoc"
)

# COMMAND ----------

# MAGIC %md
# MAGIC ### Import External Links
# MAGIC
# MAGIC **df_external_links_raw** holds links from a document to an address outside the document
# MAGIC system.

# COMMAND ----------

df_external_links_raw = spark.read.table(
    f"{PIPELINE_READ_ENV}_bronze.intraqual.gd_doc_lienext_latest"
)

df_headers_external_links = spark.read.table(
    f"{REFERENCE_READ_ENV}_bronze.headers_others.headers_intraqual_gd_doc_lienext"
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
# MAGIC ## Preparation - Form Values

# COMMAND ----------

df_form_values_end = prepare_silver_table(
    df_raw=df_form_values_raw,
    df_headers=df_headers_form_values,
    source_prefix="GD_CHAMPDOC_DOC",
    pk_name=FORM_VALUES_PK,
    pk_source_columns=FORM_VALUES_PK_COLUMNS,
)

# COMMAND ----------

# MAGIC %md
# MAGIC ## Preparation - Document Reviews

# COMMAND ----------

df_reviews_end = prepare_silver_table(
    df_raw=df_reviews_raw,
    df_headers=df_headers_reviews,
    source_prefix="GD_DOC_REVISION",
    pk_name=REVIEWS_PK,
)

# COMMAND ----------

# MAGIC %md
# MAGIC ## Preparation - Reference Changes

# COMMAND ----------

df_reference_changes_end = prepare_silver_table(
    df_raw=df_reference_changes_raw,
    df_headers=df_headers_reference_changes,
    source_prefix="GD_DOC_HISTOREF",
    pk_name=REFERENCE_CHANGES_PK,
    pk_source_columns=REFERENCE_CHANGES_PK_COLUMNS,
)

# COMMAND ----------

# MAGIC %md
# MAGIC ## Preparation - Document Links
# MAGIC
# MAGIC Populated in the production source only — see Technical Debt.

# COMMAND ----------

df_links_end = prepare_silver_table(
    df_raw=df_links_raw,
    df_headers=df_headers_links,
    source_prefix="GD_DOC_LIENDOC",
    pk_name=LINKS_PK,
    pk_source_columns=LINKS_PK_COLUMNS,
)

# COMMAND ----------

# MAGIC %md
# MAGIC ## Preparation - External Links

# COMMAND ----------

df_external_links_end = prepare_silver_table(
    df_raw=df_external_links_raw,
    df_headers=df_headers_external_links,
    source_prefix="GD_DOC_LIENEXT",
    pk_name=EXTERNAL_LINKS_PK,
    pk_source_columns=EXTERNAL_LINKS_PK_COLUMNS,
)

# COMMAND ----------

# MAGIC %md
# MAGIC # Data Transformations
# MAGIC
# MAGIC N/A — silver holds no join or multi-dataset operation for these five tables.

# COMMAND ----------

# N/A

# COMMAND ----------

# MAGIC %md
# MAGIC # Quality Checks

# COMMAND ----------

# MAGIC %md
# MAGIC ## Quality Check - Common Function

# COMMAND ----------

def check_primary_key(df, table_name, pk_column):
    """
    Run the standard RED primary key check on one silver table.

    :param df: The DataFrame to validate.
    :type df: DataFrame
    :param table_name: Target table name, used in the event log.
    :type table_name: str
    :param pk_column: Primary key column to validate.
    :type pk_column: str
    :return: None
    :rtype: None
    """
    df_gx = SparkDFDataset(df, persist=False)

    gx_validation.validate_and_log_gx_results(
        quality_check_results=[
            df_gx.expect_column_values_to_not_be_null(column=pk_column),
            df_gx.expect_column_values_to_be_unique(column=pk_column),
        ],
        validation_level="RED",
        info_message=f"Every row of {table_name} is uniquely identified",
        error_message=f"Quality checks failed for {table_name} silver table - PK is not unique or contains nulls",
        pipeline_write_env=PIPELINE_WRITE_ENV,
        table_name=table_name,
        log_to_event_table=True,
        log_successes=True,
    )

# COMMAND ----------

# MAGIC %md
# MAGIC ## Quality Check - Primary keys
# MAGIC
# MAGIC The three constructed keys that include the event date are the ones worth watching: if the
# MAGIC source ever records two identical events on the same day, the key collapses and this check
# MAGIC is what surfaces it.

# COMMAND ----------

check_primary_key(df_form_values_end, FORM_VALUES_TABLE, FORM_VALUES_PK)
check_primary_key(df_reviews_end, REVIEWS_TABLE, REVIEWS_PK)
check_primary_key(
    df_reference_changes_end, REFERENCE_CHANGES_TABLE, REFERENCE_CHANGES_PK
)
check_primary_key(df_links_end, LINKS_TABLE, LINKS_PK)
check_primary_key(df_external_links_end, EXTERNAL_LINKS_TABLE, EXTERNAL_LINKS_PK)

# COMMAND ----------

# MAGIC %md
# MAGIC ## Quality Check - Form values are attached to a document and a field
# MAGIC
# MAGIC A value with no field identifier cannot be labelled when Gold pivots the form fields into
# MAGIC columns, and would be dropped there without trace.

# COMMAND ----------

df_gx_form_values = SparkDFDataset(df_form_values_end, persist=False)

gx_validation.validate_and_log_gx_results(
    quality_check_results=[
        df_gx_form_values.expect_column_values_to_not_be_null(
            column="document_revision_ID"
        ),
        df_gx_form_values.expect_column_values_to_not_be_null(column="form_field_ID"),
    ],
    validation_level="AMBER",
    info_message="Every form value is attached to both a document revision and a form field",
    error_message="Some Intraqual form values are missing their document or field reference and will be lost in Gold",
    pipeline_write_env=PIPELINE_WRITE_ENV,
    table_name=FORM_VALUES_TABLE,
    log_to_event_table=True,
    log_successes=True,
)

# COMMAND ----------

# MAGIC %md
# MAGIC # Outputs

# COMMAND ----------

# MAGIC %md
# MAGIC ## Output - Common Functions

# COMMAND ----------

CATALOG = f"{PIPELINE_WRITE_ENV}_silver"


def save_silver_table(df, destination_table, pk_column):
    """
    Write one silver table and attach its primary key constraint.

    :param df: The DataFrame to write.
    :type df: DataFrame
    :param destination_table: Target table name within the silver schema.
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

    constraint_name = f"silver_{destination_table}_PK"
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

# COMMAND ----------

# MAGIC %md
# MAGIC ## Output - LEAP Tables
# MAGIC
# MAGIC ### Output - Silver Intraqual Form Values

# COMMAND ----------

save_silver_table(df_form_values_end, FORM_VALUES_TABLE, FORM_VALUES_PK)

# COMMAND ----------

# MAGIC %md
# MAGIC ### Output - Silver Intraqual Document Reviews

# COMMAND ----------

save_silver_table(df_reviews_end, REVIEWS_TABLE, REVIEWS_PK)

# COMMAND ----------

# MAGIC %md
# MAGIC ### Output - Silver Intraqual Reference Changes

# COMMAND ----------

save_silver_table(
    df_reference_changes_end, REFERENCE_CHANGES_TABLE, REFERENCE_CHANGES_PK
)

# COMMAND ----------

# MAGIC %md
# MAGIC ### Output - Silver Intraqual Document Links

# COMMAND ----------

save_silver_table(df_links_end, LINKS_TABLE, LINKS_PK)

# COMMAND ----------

# MAGIC %md
# MAGIC ### Output - Silver Intraqual External Links

# COMMAND ----------

save_silver_table(df_external_links_end, EXTERNAL_LINKS_TABLE, EXTERNAL_LINKS_PK)

# COMMAND ----------

# MAGIC %md
# MAGIC ## Output - Files

# COMMAND ----------

# N/A
