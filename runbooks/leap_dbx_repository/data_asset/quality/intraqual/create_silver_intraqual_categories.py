# Databricks notebook source
# MAGIC %md
# MAGIC
# MAGIC # CREATE SILVER INTRAQUAL CATEGORIES
# MAGIC
# MAGIC **Description:**
# MAGIC Creates the two silver tables carrying the Intraqual category referential: the categories
# MAGIC themselves and the bridge attaching categories to document revisions. Both apply string
# MAGIC cleaning, renaming and casting via their business header table, boolean conversion,
# MAGIC deduplication and a constructed primary key. No join, no filter, no enrichment.
# MAGIC
# MAGIC **Highlighted complexities:**
# MAGIC - Section Config - Common Functions — the two tables share one preparation routine
# MAGIC - Section Preparation - Categories — both keys are constructed, neither table being unique
# MAGIC   on a single column
# MAGIC
# MAGIC **Intended Pipeline**
# MAGIC - Data asset - Quality - Intraqual
# MAGIC
# MAGIC **Input Tables Pipeline**
# MAGIC - {pipeline_read_env}_bronze.intraqual.gd_cat_latest
# MAGIC - {pipeline_read_env}_bronze.intraqual.gd_doc_cat_latest
# MAGIC
# MAGIC **Inputs Reference Data**
# MAGIC - {reference_read_env}_bronze.headers_others.headers_intraqual_gd_cat
# MAGIC - {reference_read_env}_bronze.headers_others.headers_intraqual_gd_doc_cat
# MAGIC
# MAGIC **Output Tables (Pipeline)**
# MAGIC - {pipeline_write_env}_silver.quality_system.categories_intraqual
# MAGIC - {pipeline_write_env}_silver.quality_system.document_categories_intraqual

# COMMAND ----------

# MAGIC %md
# MAGIC # Technical Debt
# MAGIC
# MAGIC 1. Item in section Inputs — categories repeat once per language slot. Silver keeps them
# MAGIC    all; the filter belongs to Gold.
# MAGIC 2. Item in section Quality Checks — a revision can carry more than one main category,
# MAGIC    which the bridge cannot resolve on its own.

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
# MAGIC Both tables in this notebook go through the same silver routine, so it is written once
# MAGIC here and called per table rather than repeated.

# COMMAND ----------

# The source stores every date as a timestamp. Only the calendar day carries business meaning.
DATE_FORMAT = "yyyy-MM-dd"

# The source writes its yes/no flags as French words rather than as 0/1
YES_VALUE = "oui"


def prepare_silver_table(
    df_raw,
    df_headers,
    source_prefix,
    pk_name,
    pk_source_columns,
    yes_no_flag_columns=None,
    integer_flag_columns=None,
):
    """
    Apply the standard Intraqual silver preparation to one source table.

    :param df_raw: The raw bronze DataFrame.
    :type df_raw: DataFrame
    :param df_headers: The business header table driving rename and cast.
    :type df_headers: DataFrame
    :param source_prefix: Prefix matching Table_name in the header table.
    :type source_prefix: str
    :param pk_name: Name of the constructed primary key column.
    :type pk_name: str
    :param pk_source_columns: Business column names the primary key is built from.
    :type pk_source_columns: list[str]
    :param yes_no_flag_columns: Business columns holding the source yes/no words.
    :type yes_no_flag_columns: list[str] | None
    :param integer_flag_columns: Business columns holding 0/1 integers.
    :type integer_flag_columns: list[str] | None
    :return: The prepared DataFrame with the primary key as first column.
    :rtype: DataFrame
    """
    yes_no_flag_columns = yes_no_flag_columns or []
    integer_flag_columns = integer_flag_columns or []

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

    df = df.withColumn(
        pk_name, f.concat_ws("-", *[f.col(c) for c in pk_source_columns])
    )

    # All source columns are retained — the select reorders, it does not filter
    return df.select([pk_name] + [c for c in df.columns if c != pk_name])

# COMMAND ----------

# MAGIC %md
# MAGIC ## Config other

# COMMAND ----------

DESTINATION_SCHEMA = "quality_system"

CATEGORIES_TABLE = "categories_intraqual"
CATEGORIES_PK = "categories_intraqual_ID"

DOCUMENT_CATEGORIES_TABLE = "document_categories_intraqual"
DOCUMENT_CATEGORIES_PK = "document_categories_intraqual_ID"

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
# MAGIC ### Import Categories
# MAGIC
# MAGIC **df_categories_raw** is the category referential. Each category is repeated once per
# MAGIC language slot, so this table is not unique on the category identifier alone.

# COMMAND ----------

df_categories_raw = spark.read.table(
    f"{PIPELINE_READ_ENV}_bronze.intraqual.gd_cat_latest"
)

df_headers_categories = spark.read.table(
    f"{REFERENCE_READ_ENV}_bronze.headers_others.headers_intraqual_gd_cat"
)

# COMMAND ----------

# MAGIC %md
# MAGIC ### Import Document Categories
# MAGIC
# MAGIC **df_document_categories_raw** attaches categories to document revisions. A revision can
# MAGIC carry several categories, of which one is flagged as the main one.

# COMMAND ----------

df_document_categories_raw = spark.read.table(
    f"{PIPELINE_READ_ENV}_bronze.intraqual.gd_doc_cat_latest"
)

df_headers_document_categories = spark.read.table(
    f"{REFERENCE_READ_ENV}_bronze.headers_others.headers_intraqual_gd_doc_cat"
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
# MAGIC ## Preparation - Categories

# COMMAND ----------

df_categories_end = prepare_silver_table(
    df_raw=df_categories_raw,
    df_headers=df_headers_categories,
    source_prefix="GD_CAT",
    pk_name=CATEGORIES_PK,
    pk_source_columns=["category_ID", "language_ID"],
)

# COMMAND ----------

# MAGIC %md
# MAGIC ## Preparation - Document Categories

# COMMAND ----------

df_document_categories_end = prepare_silver_table(
    df_raw=df_document_categories_raw,
    df_headers=df_headers_document_categories,
    source_prefix="GD_DOC_CAT",
    pk_name=DOCUMENT_CATEGORIES_PK,
    pk_source_columns=["document_revision_ID", "category_ID"],
    yes_no_flag_columns=["is_main_category"],
)

# COMMAND ----------

# MAGIC %md
# MAGIC # Data Transformations
# MAGIC
# MAGIC N/A — silver holds no join or multi-dataset operation for these two tables.

# COMMAND ----------

# N/A

# COMMAND ----------

# MAGIC %md
# MAGIC # Quality Checks

# COMMAND ----------

# MAGIC %md
# MAGIC ## Quality Check - Categories

# COMMAND ----------

df_gx_categories = SparkDFDataset(df_categories_end, persist=False)

red_categories_results = [
    df_gx_categories.expect_column_values_to_not_be_null(column=CATEGORIES_PK),
    df_gx_categories.expect_column_values_to_be_unique(column=CATEGORIES_PK),
    df_gx_categories.expect_column_values_to_not_be_null(column="category_ID"),
    df_gx_categories.expect_column_values_to_not_be_null(column="language_ID"),
]

gx_validation.validate_and_log_gx_results(
    quality_check_results=red_categories_results,
    validation_level="RED",
    info_message="Every category is uniquely identified by its identifier and language slot",
    error_message="Quality checks failed for Intraqual Categories silver table - PK is not unique or contains nulls",
    pipeline_write_env=PIPELINE_WRITE_ENV,
    table_name=CATEGORIES_TABLE,
    log_to_event_table=True,
    log_successes=True,
)

# COMMAND ----------

# MAGIC %md
# MAGIC ## Quality Check - Document Categories
# MAGIC
# MAGIC The main-category flag is what Gold uses to pick a single category per document revision,
# MAGIC so a revision carrying several main categories has to be visible before Gold silently
# MAGIC picks one of them.

# COMMAND ----------

df_gx_document_categories = SparkDFDataset(df_document_categories_end, persist=False)

red_document_categories_results = [
    df_gx_document_categories.expect_column_values_to_not_be_null(
        column=DOCUMENT_CATEGORIES_PK
    ),
    df_gx_document_categories.expect_column_values_to_be_unique(
        column=DOCUMENT_CATEGORIES_PK
    ),
]

gx_validation.validate_and_log_gx_results(
    quality_check_results=red_document_categories_results,
    validation_level="RED",
    info_message="Every document-to-category attachment is uniquely identified",
    error_message="Quality checks failed for Intraqual Document Categories silver table - PK is not unique or contains nulls",
    pipeline_write_env=PIPELINE_WRITE_ENV,
    table_name=DOCUMENT_CATEGORIES_TABLE,
    log_to_event_table=True,
    log_successes=True,
)

# COMMAND ----------

df_main_category_per_revision = (
    df_document_categories_end.filter(f.col("is_main_category"))
    .groupBy("document_revision_ID")
    .agg(f.count("*").alias("main_category_count"))
)

df_gx_main_category = SparkDFDataset(df_main_category_per_revision, persist=False)

amber_document_categories_results = [
    df_gx_main_category.expect_column_values_to_be_between(
        column="main_category_count", min_value=1, max_value=1
    ),
]

gx_validation.validate_and_log_gx_results(
    quality_check_results=amber_document_categories_results,
    validation_level="AMBER",
    info_message="Every document revision with a category carries exactly one main category",
    error_message="Some Intraqual document revisions carry several main categories - Gold will keep only one of them",
    pipeline_write_env=PIPELINE_WRITE_ENV,
    table_name=DOCUMENT_CATEGORIES_TABLE,
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
# MAGIC ### Output - Silver Intraqual Categories

# COMMAND ----------

save_silver_table(df_categories_end, CATEGORIES_TABLE, CATEGORIES_PK)

# COMMAND ----------

# MAGIC %md
# MAGIC ### Output - Silver Intraqual Document Categories

# COMMAND ----------

save_silver_table(
    df_document_categories_end, DOCUMENT_CATEGORIES_TABLE, DOCUMENT_CATEGORIES_PK
)

# COMMAND ----------

# MAGIC %md
# MAGIC ## Output - Files

# COMMAND ----------

# N/A
