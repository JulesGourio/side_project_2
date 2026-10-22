# Databricks notebook source
# MAGIC %md
# MAGIC
# MAGIC # CREATE SILVER INTRAQUAL REFERENCES
# MAGIC
# MAGIC **Description:**
# MAGIC Creates the four silver reference tables of the Intraqual asset: document types, users,
# MAGIC form fields and languages. Each applies string cleaning, renaming and casting via its
# MAGIC business header table, boolean conversion, deduplication and primary key positioning.
# MAGIC No join, no filter, no enrichment.
# MAGIC
# MAGIC **Highlighted complexities:**
# MAGIC - Section Config - Common Functions — the four tables share one preparation routine
# MAGIC - Section Preparation - Users — the source credential column is removed before writing
# MAGIC
# MAGIC **Intended Pipeline**
# MAGIC - Data asset - Quality - Intraqual
# MAGIC
# MAGIC **Input Tables Pipeline**
# MAGIC - {pipeline_read_env}_bronze.intraqual.gd_typdoc_latest
# MAGIC - {pipeline_read_env}_bronze.intraqual.gd_utilisateur_latest
# MAGIC - {pipeline_read_env}_bronze.intraqual.gd_champdoc_latest
# MAGIC - {pipeline_read_env}_bronze.intraqual.gu_lang_latest
# MAGIC
# MAGIC **Inputs Reference Data**
# MAGIC - {reference_read_env}_bronze.headers_others.headers_intraqual_gd_typdoc
# MAGIC - {reference_read_env}_bronze.headers_others.headers_intraqual_gd_utilisateur
# MAGIC - {reference_read_env}_bronze.headers_others.headers_intraqual_gd_champdoc
# MAGIC - {reference_read_env}_bronze.headers_others.headers_intraqual_gu_lang
# MAGIC
# MAGIC **Output Tables (Pipeline)**
# MAGIC - {pipeline_write_env}_silver.quality_system.document_types_intraqual
# MAGIC - {pipeline_write_env}_silver.quality_system.users_intraqual
# MAGIC - {pipeline_write_env}_silver.quality_system.form_fields_intraqual
# MAGIC - {pipeline_write_env}_silver.quality_system.languages_intraqual

# COMMAND ----------

# MAGIC %md
# MAGIC # Technical Debt
# MAGIC
# MAGIC 1. Item in section Preparation - Users — the credential column is dropped, the single
# MAGIC    exception to silver keeping every source column.
# MAGIC 2. Item in section Preparation - Languages — the language referential contradicts the data
# MAGIC    it describes and must not be used to drive a language choice.
# MAGIC 3. Item in section Preparation - Form Fields — the deleted flag arrives as an integer here
# MAGIC    and as a word on the other tables, so both conversions are kept.

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
# MAGIC The four tables go through the same silver routine, so it is written once here and called
# MAGIC per table rather than repeated.

# COMMAND ----------

# The source stores every date as a timestamp. Only the calendar day carries business meaning.
DATE_FORMAT = "yyyy-MM-dd"

# The source writes some of its flags as French words rather than as 0/1
YES_VALUE = "oui"


def prepare_silver_table(
    df_raw,
    df_headers,
    source_prefix,
    pk_name,
    pk_source_columns=None,
    yes_no_flag_columns=None,
    integer_flag_columns=None,
    drop_columns=None,
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
    :param yes_no_flag_columns: Business columns holding the source yes/no words.
    :type yes_no_flag_columns: list[str] | None
    :param integer_flag_columns: Business columns holding 0/1 integers.
    :type integer_flag_columns: list[str] | None
    :param drop_columns: Technical column names to remove before writing.
    :type drop_columns: list[str] | None
    :return: The prepared DataFrame with the primary key as first column.
    :rtype: DataFrame
    """
    yes_no_flag_columns = yes_no_flag_columns or []
    integer_flag_columns = integer_flag_columns or []
    drop_columns = drop_columns or []

    table_utils.check_schema_of_the_header_file(df_headers)

    df = df_raw.drop(*drop_columns)

    # Blanks arrive as empty strings and would otherwise count as populated values
    string_columns = [
        c.name for c in df.schema.fields if isinstance(c.dataType, StringType)
    ]
    df = df.withColumns(
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

    # The header table cannot declare a boolean format, so flags are converted here to match
    # their naming. Nulls stay null: a missing flag is not the same statement as an unset one.
    df = df.withColumns(
        {
            col: f.when(f.col(col).isNull(), None).otherwise(
                f.lower(f.col(col)) == YES_VALUE
            )
            for col in yes_no_flag_columns
            if col in df.columns
        }
    )
    df = df.withColumns(
        {
            col: f.when(f.col(col).isNull(), None).otherwise(f.col(col) == 1)
            for col in integer_flag_columns
            if col in df.columns
        }
    )

    # The source is re-extracted twice a day, so an unchanged row can arrive twice per load
    df = df.dropDuplicates()

    if pk_source_columns:
        df = df.withColumn(
            pk_name, f.concat_ws("-", *[f.col(c) for c in pk_source_columns])
        )

    # All retained source columns are kept — the select reorders, it does not filter
    return df.select([pk_name] + [c for c in df.columns if c != pk_name])

# COMMAND ----------

# MAGIC %md
# MAGIC ## Config other

# COMMAND ----------

DESTINATION_SCHEMA = "quality_system"

DOCUMENT_TYPES_TABLE = "document_types_intraqual"
DOCUMENT_TYPES_PK = "document_types_intraqual_ID"

USERS_TABLE = "users_intraqual"
USERS_PK = "user_ID"

FORM_FIELDS_TABLE = "form_fields_intraqual"
FORM_FIELDS_PK = "form_field_ID"

LANGUAGES_TABLE = "languages_intraqual"
LANGUAGES_PK = "language_ID"

# Credential column carried by the user export. It has no analytical use and must not be
# written to a table the data team can read.
USER_CREDENTIAL_COLUMN = "pass"

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
# MAGIC ### Import Document Types
# MAGIC
# MAGIC **df_document_types_raw** is the document type referential. Like the categories, each type
# MAGIC is repeated once per language slot.

# COMMAND ----------

df_document_types_raw = spark.read.table(
    f"{PIPELINE_READ_ENV}_bronze.intraqual.gd_typdoc_latest"
)

df_headers_document_types = spark.read.table(
    f"{REFERENCE_READ_ENV}_bronze.headers_others.headers_intraqual_gd_typdoc"
)

# COMMAND ----------

# MAGIC %md
# MAGIC ### Import Users
# MAGIC
# MAGIC **df_users_raw** is the user referential, used to name the writer, the issuer and the
# MAGIC reviewer of a document. It covers every account ever created, not only the active ones.

# COMMAND ----------

df_users_raw = spark.read.table(
    f"{PIPELINE_READ_ENV}_bronze.intraqual.gd_utilisateur_latest"
)

df_headers_users = spark.read.table(
    f"{REFERENCE_READ_ENV}_bronze.headers_others.headers_intraqual_gd_utilisateur"
)

# COMMAND ----------

# MAGIC %md
# MAGIC ### Import Form Fields
# MAGIC
# MAGIC **df_form_fields_raw** defines the custom fields a document can carry. It is the dictionary
# MAGIC for the key-value table pivoted in Gold.

# COMMAND ----------

df_form_fields_raw = spark.read.table(
    f"{PIPELINE_READ_ENV}_bronze.intraqual.gd_champdoc_latest"
)

df_headers_form_fields = spark.read.table(
    f"{REFERENCE_READ_ENV}_bronze.headers_others.headers_intraqual_gd_champdoc"
)

# COMMAND ----------

# MAGIC %md
# MAGIC ### Import Languages
# MAGIC
# MAGIC **df_languages_raw** declares the language slots. It is carried for completeness only —
# MAGIC see Technical Debt, it contradicts the data it describes.

# COMMAND ----------

df_languages_raw = spark.read.table(
    f"{PIPELINE_READ_ENV}_bronze.intraqual.gu_lang_latest"
)

df_headers_languages = spark.read.table(
    f"{REFERENCE_READ_ENV}_bronze.headers_others.headers_intraqual_gu_lang"
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
# MAGIC ## Preparation - Document Types

# COMMAND ----------

df_document_types_end = prepare_silver_table(
    df_raw=df_document_types_raw,
    df_headers=df_headers_document_types,
    source_prefix="GD_TYPDOC",
    pk_name=DOCUMENT_TYPES_PK,
    pk_source_columns=["document_type_ID", "language_ID"],
    yes_no_flag_columns=["is_deleted"],
)

# COMMAND ----------

# MAGIC %md
# MAGIC ## Preparation - Users
# MAGIC
# MAGIC The credential column is the single exception to silver keeping every source column.

# COMMAND ----------

df_users_end = prepare_silver_table(
    df_raw=df_users_raw,
    df_headers=df_headers_users,
    source_prefix="GD_UTILISATEUR",
    pk_name=USERS_PK,
    yes_no_flag_columns=["is_deleted"],
    integer_flag_columns=["is_blocked"],
    drop_columns=[USER_CREDENTIAL_COLUMN],
)

# COMMAND ----------

# MAGIC %md
# MAGIC ## Preparation - Form Fields

# COMMAND ----------

df_form_fields_end = prepare_silver_table(
    df_raw=df_form_fields_raw,
    df_headers=df_headers_form_fields,
    source_prefix="GD_CHAMPDOC",
    pk_name=FORM_FIELDS_PK,
    integer_flag_columns=["is_deleted"],
)

# COMMAND ----------

# MAGIC %md
# MAGIC ## Preparation - Languages

# COMMAND ----------

df_languages_end = prepare_silver_table(
    df_raw=df_languages_raw,
    df_headers=df_headers_languages,
    source_prefix="GU_LANG",
    pk_name=LANGUAGES_PK,
    integer_flag_columns=["is_active"],
)

# COMMAND ----------

# MAGIC %md
# MAGIC # Data Transformations
# MAGIC
# MAGIC N/A — silver holds no join or multi-dataset operation for these four tables.

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

# COMMAND ----------

check_primary_key(df_document_types_end, DOCUMENT_TYPES_TABLE, DOCUMENT_TYPES_PK)
check_primary_key(df_users_end, USERS_TABLE, USERS_PK)
check_primary_key(df_form_fields_end, FORM_FIELDS_TABLE, FORM_FIELDS_PK)
check_primary_key(df_languages_end, LANGUAGES_TABLE, LANGUAGES_PK)

# COMMAND ----------

# MAGIC %md
# MAGIC ## Quality Check - Credential column removed
# MAGIC
# MAGIC The drop happens before the header rename, so the column would survive under its prefixed
# MAGIC technical name if it ever slipped through. Both spellings are therefore refused.

# COMMAND ----------

# The check runs over the column names themselves, so it states an absence rather than the
# presence every schema expectation asserts
df_users_column_names = spark.createDataFrame(
    [(column,) for column in df_users_end.columns], ["column_name"]
)

df_gx_users_columns = SparkDFDataset(df_users_column_names, persist=False)

gx_validation.validate_and_log_gx_results(
    quality_check_results=[
        df_gx_users_columns.expect_column_values_to_not_be_in_set(
            column="column_name",
            value_set=[
                USER_CREDENTIAL_COLUMN,
                f"GD_UTILISATEUR_{USER_CREDENTIAL_COLUMN}",
            ],
        )
    ],
    validation_level="RED",
    info_message="The Intraqual user credential column is absent from the silver table",
    error_message="The Intraqual user credential column reached the silver table and must not be written",
    pipeline_write_env=PIPELINE_WRITE_ENV,
    table_name=USERS_TABLE,
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
# MAGIC ### Output - Silver Intraqual Document Types

# COMMAND ----------

save_silver_table(df_document_types_end, DOCUMENT_TYPES_TABLE, DOCUMENT_TYPES_PK)

# COMMAND ----------

# MAGIC %md
# MAGIC ### Output - Silver Intraqual Users

# COMMAND ----------

save_silver_table(df_users_end, USERS_TABLE, USERS_PK)

# COMMAND ----------

# MAGIC %md
# MAGIC ### Output - Silver Intraqual Form Fields

# COMMAND ----------

save_silver_table(df_form_fields_end, FORM_FIELDS_TABLE, FORM_FIELDS_PK)

# COMMAND ----------

# MAGIC %md
# MAGIC ### Output - Silver Intraqual Languages

# COMMAND ----------

save_silver_table(df_languages_end, LANGUAGES_TABLE, LANGUAGES_PK)

# COMMAND ----------

# MAGIC %md
# MAGIC ## Output - Files

# COMMAND ----------

# N/A
