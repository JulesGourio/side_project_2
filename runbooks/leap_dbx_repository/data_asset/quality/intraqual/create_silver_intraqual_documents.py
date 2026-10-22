# Databricks notebook source
# MAGIC %md
# MAGIC
# MAGIC # CREATE SILVER INTRAQUAL DOCUMENTS
# MAGIC
# MAGIC **Description:**
# MAGIC Creates the silver table for Intraqual documents. One row per document revision.
# MAGIC Applies string cleaning, column renaming and casting via the business header table,
# MAGIC boolean conversion of the source flag columns, deduplication and primary key positioning.
# MAGIC No join, no filter, no enrichment — all source columns are retained.
# MAGIC
# MAGIC **Highlighted complexities:**
# MAGIC - Section Prep 1 — source date columns arrive as timestamps and are reduced to dates
# MAGIC - Section Prep 3 — the source flag columns are integers and are converted to booleans
# MAGIC
# MAGIC **Intended Pipeline**
# MAGIC - Data asset - Quality - Intraqual
# MAGIC
# MAGIC **Input Tables Pipeline**
# MAGIC - {pipeline_read_env}_bronze.intraqual.gd_doc_latest
# MAGIC
# MAGIC **Inputs Reference Data**
# MAGIC - {reference_read_env}_bronze.headers_others.headers_intraqual_gd_doc
# MAGIC
# MAGIC **Output Tables (Pipeline)**
# MAGIC - {pipeline_write_env}_silver.quality_system.documents_intraqual

# COMMAND ----------

# MAGIC %md
# MAGIC # Technical Debt
# MAGIC
# MAGIC 1. Item in section Prep 3 — the not-applicable flag can be null. Nulls are kept rather
# MAGIC    than read as false, until the meaning of the flag is confirmed.
# MAGIC 2. Item in section Prep 2 — the lifecycle state code is carried through unlabelled: no
# MAGIC    reference table exists for its values.

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

# COMMAND ----------

# N/A

# COMMAND ----------

# MAGIC %md
# MAGIC ## Config other

# COMMAND ----------

SOURCE_TABLE = "gd_doc"
# The prefix must match Table_name in the header table for the rename to resolve
SOURCE_PREFIX = SOURCE_TABLE.upper()

DESTINATION_SCHEMA = "quality_system"
DESTINATION_TABLE = "documents_intraqual"

PK_COLUMN = "document_revision_ID"

# The source stores every date as a timestamp, at midnight or at the moment of the action.
# Only the calendar day carries business meaning, so dates are reduced to this format.
DATE_FORMAT = "yyyy-MM-dd"

# Source flag columns arriving as 0/1 integers, renamed to is_* by the header table
INTEGER_FLAG_COLUMNS = ["is_not_applicable", "is_hidden"]

# The three states a revision can be in: superseded, current, under revision
REVISION_STATUS_CODES = {0, 1, 2}

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
# MAGIC ### Import Main Table
# MAGIC
# MAGIC **df_documents_raw** is the latest snapshot of the Intraqual document table. It holds one
# MAGIC row per document revision, not per document — a document appears once for every revision
# MAGIC it has ever had.

# COMMAND ----------

df_documents_raw = spark.read.table(
    f"{PIPELINE_READ_ENV}_bronze.intraqual.{SOURCE_TABLE}_latest"
)

# COMMAND ----------

# MAGIC %md
# MAGIC ### Import Headers Table
# MAGIC
# MAGIC **df_headers_documents** is the business header dictionary. It drives both the renaming and
# MAGIC the casting, so any field absent from it keeps its technical name and is excluded from the
# MAGIC exposed view built in Gold.

# COMMAND ----------

df_headers_documents = spark.read.table(
    f"{REFERENCE_READ_ENV}_bronze.headers_others.headers_intraqual_{SOURCE_TABLE}"
)

table_utils.check_schema_of_the_header_file(df_headers_documents)

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
# MAGIC ## Prep 0 - Clean all string columns
# MAGIC
# MAGIC The source exports blanks as empty strings rather than nulls, which would otherwise be
# MAGIC counted as populated values by every downstream completeness check.

# COMMAND ----------

string_columns = [
    c.name for c in df_documents_raw.schema.fields if isinstance(c.dataType, StringType)
]

df_documents_cleaned = df_documents_raw.withColumns(
    {
        col: f.when(f.trim(f.col(col)) == "", None).otherwise(f.trim(f.col(col)))
        for col in string_columns
    }
)

# COMMAND ----------

# MAGIC %md
# MAGIC ## Prep 1 - Reduce timestamps to dates
# MAGIC
# MAGIC The header dictionary decides which fields are dates, so the list is read from it rather
# MAGIC than hardcoded here. Rendering them as text in the target format lets the header casting
# MAGIC step parse them the same way it does for every other asset.

# COMMAND ----------

date_fields = [
    row["Field"]
    for row in df_headers_documents.filter(f.lower(f.col("Format")) == "date")
    .select("Field")
    .collect()
]

timestamp_columns = [
    c.name
    for c in df_documents_cleaned.schema.fields
    if isinstance(c.dataType, TimestampType) and c.name in date_fields
]

df_documents_dated = df_documents_cleaned.withColumns(
    {col: f.date_format(f.col(col), DATE_FORMAT) for col in timestamp_columns}
)

log.info(f"Date fields reduced from timestamp: {timestamp_columns}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Prep 2 - Rename and cast columns
# MAGIC
# MAGIC Fields absent from the header dictionary deliberately keep the source prefix. That is what
# MAGIC lets Gold separate the documented business columns from the technical ones when it builds
# MAGIC the exposed view.

# COMMAND ----------

df_documents_prefixed = table_utils.add_prefix_to_columns(
    df_documents_dated, SOURCE_PREFIX
)

df_documents_renamed = table_utils.rename_columns_with_sap_business_header(
    df_documents_prefixed, df_headers_documents
)

df_documents_casted = table_utils.cast_columns_with_sap_business_headers(
    df_documents_renamed, df_headers_documents, date_format=DATE_FORMAT
)

# COMMAND ----------

# MAGIC %md
# MAGIC ## Prep 3 - Convert source flags to booleans
# MAGIC
# MAGIC The header table cannot declare a boolean format, so the flags arrive as integers and are
# MAGIC converted here to match their naming. Nulls are preserved rather than read as false: on
# MAGIC this source a missing flag is not the same statement as an unset one.

# COMMAND ----------

df_documents_prep = df_documents_casted.withColumns(
    {
        col: f.when(f.col(col).isNull(), None).otherwise(f.col(col) == 1)
        for col in INTEGER_FLAG_COLUMNS
        if col in df_documents_casted.columns
    }
)

# COMMAND ----------

# MAGIC %md
# MAGIC # Data Transformations

# COMMAND ----------

# MAGIC %md
# MAGIC ## Tr 1 - Deduplication
# MAGIC
# MAGIC The source is re-extracted twice a day, so an unchanged revision can be delivered more
# MAGIC than once within the same load.

# COMMAND ----------

df_documents_dedup = df_documents_prep.dropDuplicates()

# COMMAND ----------

# MAGIC %md
# MAGIC ## Tr 2 - Primary key positioning
# MAGIC
# MAGIC The revision identifier is already unique at source, so it keeps its own business name
# MAGIC rather than being rebuilt as a constructed key.

# COMMAND ----------

# All source columns are retained — the select reorders, it does not filter
column_order = [PK_COLUMN] + [c for c in df_documents_dedup.columns if c != PK_COLUMN]
df_documents_end = df_documents_dedup.select(column_order)

# COMMAND ----------

# MAGIC %md
# MAGIC # Quality Checks

# COMMAND ----------

df_gx = SparkDFDataset(df_documents_end, persist=False)

red_quality_check_results = [
    df_gx.expect_column_values_to_not_be_null(column=PK_COLUMN),
    df_gx.expect_column_values_to_be_unique(column=PK_COLUMN),
    df_gx.expect_column_values_to_not_be_null(column="document_ID"),
]

gx_validation.validate_and_log_gx_results(
    quality_check_results=red_quality_check_results,
    validation_level="RED",
    info_message="Every revision is uniquely identified and carries a document identity",
    error_message="Quality checks failed for Intraqual Documents silver table - PK is not unique, contains nulls, or a revision has no document identity",
    pipeline_write_env=PIPELINE_WRITE_ENV,
    table_name=DESTINATION_TABLE,
    log_to_event_table=True,
    log_successes=True,
)

# COMMAND ----------

# MAGIC %md
# MAGIC ## Quality Check - Revision status and revision number
# MAGIC
# MAGIC These two columns drive the current-revision logic in Gold. A value outside the known set
# MAGIC would silently change which row Gold treats as the live version of a document.

# COMMAND ----------

amber_quality_check_results = [
    df_gx.expect_column_distinct_values_to_be_in_set(
        column="revision_status_code", value_set=REVISION_STATUS_CODES
    ),
    df_gx.expect_column_values_to_not_be_null(column="revision_number"),
    df_gx.expect_column_values_to_be_between(column="revision_number", min_value=1),
]

gx_validation.validate_and_log_gx_results(
    quality_check_results=amber_quality_check_results,
    validation_level="AMBER",
    info_message="Revision status codes are within the known set and every revision is numbered",
    error_message="Intraqual Documents silver table carries an unknown revision status code or an unnumbered revision",
    pipeline_write_env=PIPELINE_WRITE_ENV,
    table_name=DESTINATION_TABLE,
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

# N/A

# COMMAND ----------

# MAGIC %md
# MAGIC ## Output - LEAP Tables
# MAGIC
# MAGIC ### Output - Silver Intraqual Documents

# COMMAND ----------

CATALOG = f"{PIPELINE_WRITE_ENV}_silver"
DESTINATION_TABLE_FULL_PATH = f"{CATALOG}.{DESTINATION_SCHEMA}.{DESTINATION_TABLE}"

table_utils.save_table(
    dest_table=DESTINATION_TABLE_FULL_PATH,
    df=df_documents_end,
    overwrite_schema=True,
    mode="overwrite",
)

# COMMAND ----------

# MAGIC %md
# MAGIC ### Adding constraint primary key in DBX

# COMMAND ----------

TABLE_CONSTRAINT_NAME = f"silver_{DESTINATION_TABLE}_PK"

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
# MAGIC ## Output - Files

# COMMAND ----------

# N/A
