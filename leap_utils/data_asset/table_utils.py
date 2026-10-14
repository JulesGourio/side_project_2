import re
import unicodedata
import pyspark
from enum import Enum
from functools import partial
from typing import Dict, Any, List, Optional
from packaging import version

import pandas as pd
from pyspark.sql import Column, DataFrame, Window
from pyspark.sql import functions as f

from leap_utils.common.logger import get_logger, setup_applevel_logger
from leap_utils.config import spark
from leap_utils.common.merge_into import merge_into_spark
from leap_utils.platform.freshness import freshness_spark

# Freshness pilot table of the LEAP datalake (written after each save or merge). Off in this repository:
# it writes to the {env}_bronze catalog, which is not owned by the qualibot project.
FRESHNESS_PILOT_ENABLED = False

setup_applevel_logger()
LOGGER = get_logger(__name__)

PYSPARK_VERSION = version.parse(pyspark.__version__)
HAS_TRY_TO_DATE = PYSPARK_VERSION >= version.parse("4.0.0")
SAP_BUISNESS_HEADER_COLUMNS = ["Table_name", "Field", "Business_name", "Format"]


def safe_to_date(col_expr: Column, date_format: str) -> Column:
    """
    Version-safe date conversion.

    Spark 4+ : uses try_to_date
    Older    : uses to_date (same behaviour in non-ANSI mode)

    :param col_expr: Column expression to convert to date.
    :type col_expr: pyspark.sql.Column
    :param date_format: Date format string.
    :type date_format: str
    :return: Converted date column.
    :rtype: pyspark.sql.Column
    """
    if HAS_TRY_TO_DATE:
        return f.try_to_date(col_expr, date_format)

    return f.to_date(col_expr, date_format)

def add_week_number(df: DataFrame, date_col: str) -> DataFrame:
    """
    Adds a week number column to the dataframe based on the provided date column.

    :param df: Input Spark dataframe.
    :type df: pyspark.sql.DataFrame
    :param date_col: Name of the date column.
    :type date_col: str
    :return: Dataframe with the additional week number column.
    :rtype: pyspark.sql.DataFrame
    """
    # Ensure date column is cast to date
    df = df.withColumn("temp_date", f.col(date_col).cast("date"))

    # Calculate ISO week number
    df = df.withColumn(
        "iso_year", f.year(f.date_format(f.col("temp_date"), "yyyy-MM-dd"))
    )
    df = df.withColumn("iso_week", f.weekofyear(f.col("temp_date")))

    # Adjust for ISO week year (ISO weeks may overlap years)
    df = df.withColumn(
        "iso_year",
        f.when(
            (f.date_format(f.col("temp_date"), "MM") == "01")
            & (f.col("iso_week") >= 52),
            f.col("iso_year") - 1,
        )
        .when(
            (f.date_format(f.col("temp_date"), "MM") == "12")
            & (f.col("iso_week") == 1),
            f.col("iso_year") + 1,
        )
        .otherwise(f.col("iso_year")),
    )
    df = df.withColumn("iso_week", f.format_string("%02d", f.col("iso_week")))
    # Format the week number as 'YYYY-Www'
    df = df.withColumn(
        f"{date_col}_week_number",
        f.concat(
            f.col("iso_year").cast("string"),
            f.lit("-W"),
            f.col("iso_week").cast("string"),
        ),
    )

    # Drop intermediate columns
    df = df.drop("iso_year", "iso_week", "temp_date")

    return df


def add_prefix_to_columns(df: DataFrame, prefix: str) -> DataFrame:
    """
    Add a prefix to every column name in a Spark DataFrame.

    :param df: input Spark dataframe
    :type df: pyspark.sql.DataFrame
    :param prefix: prefix to be added to the column names
    :type prefix: str
    :return: Spark dataframe with prefixed column names
    :rtype: pyspark.sql.DataFrame
    """
    prefixed_column_names = [
        f.col(col_name).alias(f"{prefix}_{col_name}") for col_name in df.columns
    ]
    df = df.select(prefixed_column_names)

    return df


def add_prefix_to_columns_startingwith(
    df: DataFrame, prefix: str, column_startswith=None
) -> DataFrame:
    """
    Rename columns beginning by {column_startswith}, by adding {prefix} + "_" in a Spark DataFrame.

    :param df: input Spark dataframe
    :type df: pyspark.sql.DataFrame
    :param prefix: prefix to be added to the column names
    :type prefix: str
    :param column_startswith: string the column should begin with
    :type column_startswith: str
    :return: Spark dataframe with prefixed column names
    :rtype: pyspark.sql.DataFrame
    """
    if column_startswith:
        new_column_names = [
            (prefix + "_" + col if col.startswith(column_startswith) else col)
            for col in df.columns
        ]
    else:
        new_column_names = [(prefix + "_" + col) for col in df.columns]

    # Utiliser toDF pour renommer les colonnes du DataFrame
    df = df.toDF(*new_column_names)

    return df


def search_columns_to_keep_in_list(
    df: DataFrame, column_name_regex_list: List[str]
) -> List[str]:
    """
    Searches column names in a Spark DataFrame "df" based on a list of regular
    expressions "column_name_regex_list". Returns a list of unique column names
    that matched the regular expressions.

    **WARNING:** This function should not be used in PRODUCTION code because this
    function uses regex `search` and not `match`, meaning it may find other unwanted
    column names, and thus may impact exposed columns if the underlying dataframe
    schema evolves.

    :param df: A Spark DataFrame to search for column names.
    :type df: DataFrame
    :param column_name_regex_list: A list of regular expressions to match with
        column names in the DataFrame.
    :type column_name_regex_list: List[str]
    :return: A list of unique column names that matched the regular expressions.
    :rtype: List[str]
    """
    each_column_match = list()
    for column_name_regex in column_name_regex_list:
        each_column_match += list(
            filter(
                lambda text: re.search(column_name_regex, text) is not None, df.columns
            )
        )

    columns_to_keep = list(pd.unique(each_column_match))
    return columns_to_keep


def create_table_view(
    src_full_tablename: str,
    dest_full_tablename: str,
    select_statement: str,
    replace_view: bool = False,
) -> DataFrame:
    """
    Create a Spark SQL view for the given source table. The view can optionally
    overwrite an existing one.

    :param src_full_tablename: The full name (including database and schema) of the
                               source table.
    :type src_full_tablename: str
    :param dest_full_tablename: The full name (including database and schema) of the
                                destination view.
    :type dest_full_tablename: str
    :param select_statement: The SELECT statement to use for creating the view.
    :type select_statement: str
    :param replace_view: Whether to replace an existing view with the same name as the
                         destination.
    :type replace_view: bool
    :return: A DataFrame representing the view.
    :rtype: DataFrame
    """
    try:
        dest_catalog, dest_domain, _ = dest_full_tablename.split(".")

    except ValueError:
        raise ValueError(
            "The destination table format {} is incorrect".format(dest_full_tablename)
        )
    # Create a database if it doesn't exist using the provided environment and zone
    spark.sql(f"CREATE DATABASE IF NOT EXISTS {dest_catalog}.{dest_domain}")

    table_view_sql_statement = f"""
CREATE {"OR REPLACE" if replace_view is True else ""} VIEW {dest_full_tablename}
AS (
    SELECT {select_statement}
    FROM {src_full_tablename}
)
"""

    return spark.sql(table_view_sql_statement)


def create_table_view_generic(
    dest_full_tablename: str, sql_select_statement: str, replace_view: bool = False
) -> DataFrame:
    """
    Create a table view using an arbitrary SQL select statement.
    The view can optionally overwrite an existing one.

    :param dest_full_tablename: The full name (including database and schema) of the
                                destination view.
    :param sql_select_statement: The `SELECT ... FROM ... [...]` statement to use for
                                 creating the view.
    :param replace_view: Whether to replace an existing view with the same name as
                         the destination.
    :return: A DataFrame representing the response of the SQL `CREATE VIEW` statement.
    """
    try:
        dest_catalog, dest_domain, _ = dest_full_tablename.split(".")

    except ValueError as e:
        raise type(e)(
            f"The destination table format {dest_full_tablename} is incorrect."
        ) from e

    # Create a database if it doesn't exist using the provided environment and zone
    spark.sql(f"CREATE DATABASE IF NOT EXISTS {dest_catalog}.{dest_domain}")

    generic_table_view_sql_statement = f"""
CREATE {"OR REPLACE" if replace_view is True else ""} VIEW {dest_full_tablename}
AS (
    {sql_select_statement}
)
"""

    return spark.sql(generic_table_view_sql_statement)


def convert_currency(
    df_base: DataFrame,
    df_currency: DataFrame,
    df_base_source_currency_column_name: str,
    target_currency_value: str,
    df_currency_conversion_rate_type_exp_value: str,
    df_base_conversion_date_column_name: str,
    conversion_output_column_name: str,
) -> DataFrame:
    """
    **DEPRECATED**

    TODO We should not use this function anymore, but use the more generic
        :py:func:`table_utils.currency_conversion` function.

    Function for currency conversion

    :param df_base: Base Dataframe
    :type df_base: pyspark.sql.dataframe.DataFrame

    :param df_currency: Currency DataFrame
    :type df_currency: pyspark.sql.dataframe.DataFrame

    :param df_base_source_currency_column_name: Name of the column in `df_base` that has
                                                source currency value
    :type df_base_source_currency_column_name: str

    :param target_currency_value: Value of the target currency
    :type target_currency_value: str

    :param df_currency_conversion_rate_type_exp_value: Value of the currency conversion
                                                       rate type to use (Spot, Budget,
                                                       Consolidation, etc.)
    :type df_currency_conversion_rate_type_exp_value: str

    :param df_base_conversion_date_column_name: Name of the column in `df_base` that has
                                                the conversion date
    :type df_base_conversion_date_column_name: str

    :param conversion_output_column_name: Name of the new column that will hold the
                                          currency conversion rate
    :type conversion_output_column_name: str

    :return: A DataFrame containing the columns of `df_base` and a new column
             `conversion_output_column_name` representing the currency conversion rate
    :rtype: pyspark.sql.dataframe.DataFrame
    """
    list_columns = df_base.columns
    df_currency_spot = df_currency.filter(
        f.col("Conversion_rate_Type_exp") == df_currency_conversion_rate_type_exp_value
    )

    df = (
        df_base.alias("A")
        .join(
            df_currency_spot.alias("B"),
            (f.col("B.Currency_Target") == target_currency_value)
            & (f.col("B.Currency_Source") == f.col(df_base_source_currency_column_name))
            & (
                f.col("B.Validity_Date_DateFormat")
                == f.col(df_base_conversion_date_column_name)
            ),
            "left",
        )
        .select(
            f.col("A.*"), f.col("B.Exchange_Rate").alias(conversion_output_column_name)
        )
    )

    # By default, since we don't have a conversion rate for target_currency to
    # target_currency, we put 1.0. And we also put 1.0 when there is no conversion rate
    # as a fallback.
    df = df.withColumn(
        conversion_output_column_name,
        f.when(
            f.col(df_base_source_currency_column_name) == f.lit(target_currency_value),
            1.0,
        )
        .when(f.col(conversion_output_column_name).isNull(), 1.0)
        .otherwise(f.col(conversion_output_column_name)),
    )

    list_columns.append(conversion_output_column_name)

    return df.select(list_columns)


def currency_conversion(
    df: DataFrame,
    document_currency_column: Column,
    rate_types: List[str],
    rate_dates: List[Column],
    target_currency_value: str,
    document_amounts: List[Column],
    df_currency: DataFrame = None,
    reference_read_env: str = "prod",
) -> DataFrame:
    """
    Convert all documents amounts from a document currency column to a specific target
    currency, for every date and rate type specified.

    :param df: The dataframe in which to calculate all amount's conversions requested.
    :param document_currency_column: The currency column of the amounts to convert.
    :param rate_types: A list of rate type column names to use for conversion.
                       Values should be picked in the existing values of the
                       `Conversion_rate_Type_exp` column, in the currency exchange
                       gold table.
    :param rate_dates: A list of date columns to use to convert documents at specific
                       dates.
    :param target_currency_value: The currency value in which to convert the documents.
    :param document_amounts: The list of document amounts columns to convert.
    :param df_currency: The currency exchange table dataframe to use to convert the
                        documents' amounts. If the dataframe is not provided, it
                        defaults to
                        `{reference_read_env}_gold.finance.currency_exchange`,
                        which is the recommended usage.
    :param reference_read_env: The reference read env value to use to read the currency
                               exchange table if no currency table is provided.
                               Defaults to "prod".
                               The value of the corresponding dbutils widget should be
                               used if the default should not be static.

    :return: The input dataframe with all rate values and document conversion columns
             added.
    """

    # Read currency if not provided by the user
    if df_currency is None:
        df_currency = spark.read.table(
            f"{reference_read_env}_gold.finance.currency_exchange"
        )

    # Get the list of existing conversion rate types explanation
    df_unique_rate_types = (
        df_currency.groupBy("Conversion_rate_Type_exp")
        .count()
        .select("Conversion_rate_Type_exp")
    )
    existing_rate_types = df_unique_rate_types.toPandas()[
        "Conversion_rate_Type_exp"
    ].to_list()

    # Check if chosen rate types exist in the existing rate types list
    pdf_rate_types = pd.Series(rate_types)
    unknown_rates = pdf_rate_types[~pdf_rate_types.isin(existing_rate_types)]
    assert unknown_rates.count() == 0, (
        f"Some chosen rate types are unknown: {unknown_rates.to_list()}, "
        f"available rate types from the currency dataframe are: {existing_rate_types}"
    )

    # Filter currency to keep only values that will be used
    df_currency_filtered = df_currency.where(
        f.col("Conversion_rate_Type_exp").isin(rate_types)
    ).where(f.col("Currency_Target") == f.lit(target_currency_value))

    # Pivot currency for all selected rate types
    df_currency_pivot = (
        df_currency_filtered.groupBy(
            "Validity_Date_DateFormat",
            "Currency_Source",
            "Currency_Target",
        )
        .pivot(pivot_col="Conversion_rate_Type_exp")
        .agg(f.first("Exchange_Rate"))
    )

    # Rename all selected rate types
    rename_rates = {rate_name: f"{rate_name}_rate" for rate_name in rate_types}
    df_currency_renamed_rates = df_currency_pivot.withColumnsRenamed(rename_rates)

    renamed_rates = rename_rates.values()
    document_currency_name = df.select(document_currency_column).columns[0]

    # Get the rates for a single target currency, a single source currency column, at a
    # list of currency rate types
    created_rate_names = []
    for index, rate_date_column in enumerate(rate_dates):
        current_currency = f"df_currency_{index}"
        df_currency_rate_date = df_currency_renamed_rates.alias(current_currency)

        rate_date_name = df.select(rate_date_column).columns[0]

        # Get all rates for the current rate date column for the corresponding currency
        df = df.join(
            df_currency_rate_date,
            on=(
                (
                    f.col(f"{current_currency}.Currency_Source")
                    == df[document_currency_name]
                )
                & (
                    f.col(f"{current_currency}.Validity_Date_DateFormat")
                    == df[rate_date_name]
                )
            ),
            how="left",
        )

        rename_rates_for_date = {
            rate_name: f"{document_currency_name}_to_{target_currency_value}_in_{rate_name}_at_{rate_date_name}"  # noqa: E501
            for rate_name in renamed_rates
        }

        # Rename rates and remove left-over columns from the currency join, to join
        # again during the current for loop
        df = df.withColumnsRenamed(rename_rates_for_date).drop(
            *[
                f.col(f"{current_currency}.{column}")
                for column in df_currency_rate_date.columns
            ]
        )

        created_rate_names += rename_rates_for_date.values()

    # If any rate is null, use 1.0 instead
    df = df.fillna(1.0, created_rate_names)

    # Convert a list of document amounts columns to all rates created
    for document_amount_column in document_amounts:
        document_amount_name = df.select(document_amount_column).columns[0]

        document_amount_conversions = {
            f"{document_amount_name}_from_{document_rate_name}": document_amount_column
            * f.col(document_rate_name)
            for document_rate_name in created_rate_names
        }

        df = df.withColumns(document_amount_conversions)

    return df


def remove_start_and_end_spaces_from_column_names(df: DataFrame) -> DataFrame:
    """
    Remove spaces at the beginning and at the end of all columns

    :param df: the DataFrame
    :return: DataFrame with column names without spaces
    """
    columns = df.columns
    cleaned_columns = [f.col(column).alias(column.strip()) for column in columns]

    cleaned_df = df.select(*cleaned_columns)
    return cleaned_df


def _cleanup_column_name(column_name: str) -> str:
    """
    **For internal module use only**

    Remove special characters except underscores and spaces, and replace spaces with
    underscores. For example, `Release date (actual)` would become `Release_date_actual`

    :param column_name: The column name to clean up.
    :return: The cleaned-up column name.
    """
    return re.sub(" ", "_", str.strip(re.sub(r"[^a-zA-Z0-9_ ]*", "", column_name)))


def _cleanup_column_name_unicode(column_name: str) -> str:
    """
    **For internal module use only**

    Like _cleanup_column_name but transliterates accented characters to ASCII first,
    and replaces apostrophes/hyphens with underscores to preserve word boundaries.

    Examples::

        Désignation complémentaire  -> Designation_complementaire
        Date d'officialisation      -> Date_d_officialisation
        Désignation - Opération     -> Designation_Operation
        Sérialisable ?              -> Serialisable

    :param column_name: The column name to clean up.
    :return: The cleaned-up column name with accents transliterated.
    """
    # Decompose unicode chars (é -> e + combining accent), then drop combining marks
    nfkd = unicodedata.normalize("NFKD", column_name)
    ascii_approx = "".join(c for c in nfkd if not unicodedata.combining(c))
    # Replace apostrophes and hyphens with spaces (to become underscores later)
    ascii_approx = re.sub(r"['\-]", " ", ascii_approx)
    # Remove remaining special characters (keep alphanumeric, underscores, spaces)
    ascii_approx = re.sub(r"[^a-zA-Z0-9_ ]+", "", ascii_approx)
    # Collapse multiple spaces into one, strip, then replace spaces with underscores
    ascii_approx = re.sub(r" +", " ", ascii_approx).strip()
    return re.sub(" ", "_", ascii_approx)


def rename_columns_with_sap_business_header(
    df: DataFrame, df_sap_business_header: DataFrame
) -> DataFrame:
    """
    Rename given dataframe's columns (`df`) using the given "sap business headers file"
    (`df_sap_business_header`). Column names in `df` should be in the format of
    `<table_name>_<sap_field_name>`.

    :param df: The dataframe in which columns should be renamed according to the
               business header file.
    :param df_sap_business_header: The sap business header file to use to rename the
                                   given dataframe.

    :return: The input dataframe with columns renamed according to the sap business
             header file.
    """
    dfp_sap_business_header = df_sap_business_header.toPandas()
    dfp_sap_business_header["full_table_field_name"] = (
        dfp_sap_business_header["Table_name"] + "_" + dfp_sap_business_header["Field"]
    )

    rename_dict = {}
    for _, row in dfp_sap_business_header.iterrows():
        old_column_name = row["full_table_field_name"]
        new_column_name = _cleanup_column_name(row["Business_name"])

        rename_dict[old_column_name] = new_column_name

    if rename_dict:
        df = df.withColumnsRenamed(rename_dict)

    return df


def cast_columns_with_sap_business_headers(
    df: DataFrame,
    df_sap_business_header: DataFrame,
    date_format: str | None = "yyyyMMdd",
) -> DataFrame:
    """
    Cast dataframe's columns with provided format from the provided sap business header
    file. Applies casting to business column names only, so renaming of the dataframe's
    columns needs to be done first.

    :param df: The dataframe in which columns should be cast according to the business
               header file.
    :param df_sap_business_header: The sap business header file to use to cast columns
                                   of the given dataframe.
    :param date_format: The date format specifier to use to cast dates.
        Defaults to "yyyyMMdd" for SAP. Can be None.
    :return: The input dataframe with columns cast according to the sap business header
             file.
    """
    dfp_sap_business_header = df_sap_business_header.toPandas()

    cast_dict = {}
    for _, row in dfp_sap_business_header.iterrows():
        column_name = _cleanup_column_name(row["Business_name"])

        if column_name in df.columns:
            cast_format = row["Format"].lower()
            cast_expression = None

            # TODO could be done using the match statement, but when we will globally
            # use Python 3.10+ (DBX runtime 13.3+)
            # TODO long term we should turn these cast format literals into enumerations
            if cast_format == "string":
                pass
            elif cast_format == "date":
                cast_expression = safe_to_date(f.col(column_name), date_format)
            elif cast_format in ["float", "int", "bigint", "double"]:
                cast_expression = f.col(column_name).cast(cast_format)
            elif cast_format == "timestamp":
                cast_expression = f.to_timestamp(f.col(column_name), date_format)
            else:
                raise Exception(
                    f"Format provided in business header is incorrect: {cast_format}"
                )

            if cast_expression is not None:
                cast_dict[column_name] = cast_expression

    if cast_dict:
        df = df.withColumns(cast_dict)

    return df


def transform_float_column(df: DataFrame, col_name: str) -> DataFrame:
    """
    In some tables, SAP places the minus sign at the end so we need to move
    it to the beginning if there is one, then cast to double

    :param df: dataframe to alter
    :param col_name: name of the column to modify
    :return: altered dataframe
    """
    new_df = df.withColumn(
        col_name,
        f.when(
            f.col(col_name).endswith("-"),
            -f.regexp_replace(col_name, "-", "").try_cast("double"),
        ).otherwise(f.col(col_name).try_cast("double")),
    )
    return new_df


def cast_sap_float_column(df: DataFrame, col_name: str) -> DataFrame:
    """
    In some tables, SAP sets points for spaces and comas for points in floats
    we need to change it to be able to cast values as double then

    :param df: dataframe to alter
    :param col_name: name of the column to modify
    :return: altered dataframe
    """
    new_df = df.withColumn(
        col_name,
        f.regexp_replace(f.regexp_replace(col_name, r"\.", ""), ",", "."),
    )
    new_df = transform_float_column(new_df, col_name)
    return new_df


def cast_column(df: DataFrame, col_name: str, cast_type: str) -> DataFrame:
    """
    Cast dataframe column to expected type : type should be a spark authorized
    format given as a string, no check is made to assert type exists

    :param df: dataframe to use
    :param col_name: name of the column to cast
    :param cast_type: type to cast
    :return: dataframe with casted column
    """
    new_df = df.withColumn(col_name, f.col(col_name).cast(cast_type))
    return new_df


def get_columns_for_insertion_after_column(
    df: DataFrame, search_column_name: str, insert_columns: List[str]
) -> List[str]:
    """
    Return the columns of the given dataframe after insertion of more column names after
    a given column.

    :param df: The dataframe to use to insert a column name.
    :param search_column_name: The column name to search in the given dataframe.
    :param insert_columns: The list of column names to insert after the searched column
                           name in the dataframe.

    :return: The column names of the dataframe after insertion.
    """
    df_columns = df.columns

    search_column_index = df_columns.index(search_column_name)
    insert_after_search_column_index = search_column_index + 1

    df_inserted_columns = (
        df.columns[:insert_after_search_column_index]
        + insert_columns
        + df.columns[insert_after_search_column_index:]
    )

    return df_inserted_columns


def save_table(
    dest_table: str,
    df: DataFrame,
    mode: str,
    overwrite_schema: bool = True,
    cluster_cols: str = None,
    partition_col: str = None,
    merge_schema: bool = False,
    replace_where: str = None,
) -> None:
    """
    Create or overwrite or append a Delta table in Spark SQL if it doesn't already
    exist.
    For more details on the `mode` or `overwrite_schema` parameters see the
    :class:`pyspark.sql.readwriter.DataFrameWriter` class.
    Update freshness pilot table once the dest_table has been saved

    :param dest_table: The destination table where to save the DataFrame.
    :type dest_table: str
    :param df: The DataFrame containing the data to be written to the Delta table.
    :type df: DataFrame
    :param mode: Saving mode (see available options in :meth:
        `pyspark.sql.readwriter.DataFrameWriter.mode`)
    :type mode: str
    :param overwrite_schema: Additional option to overwrite schema when saving if true.
    :type overwrite_schema: bool
    :param cluster_cols: Additional option to clusterize table with a list of specific
        column.
    :type cluster_cols: str
    :param partition_col: Additional option to partition table with one specific column.
    :type partition_col: str

    :param merge_schema: Option to be set to `True` when mode="append" and the schema of
        the dataframe added to the existent table is different (types or column name)
    :type merge_schema: bool

    :return: None
    """
    try:
        catalog, domain, _ = dest_table.split(".")

    except ValueError:
        raise ValueError(
            "The destination table format {} is incorrect".format(dest_table)
        )
    # Create a database if it doesn't exist using the provided environment and zone
    spark.sql(f"CREATE DATABASE IF NOT EXISTS {catalog}.{domain}")
    # Write the DataFrame to a Delta table with the specified options

    # Parsing options , if {cluster_cols} is not None check that each column in
    # {cluster_cols} is in the columns of df
    if cluster_cols:
        cluster_cols_list = cluster_cols.replace(" ", "").split(",")
        for col in cluster_cols_list:
            if col.strip() not in df.columns:
                raise ValueError(
                    f"Cluster column '{col.strip()}' is not defined in the columns of "
                    "Dataframe."
                )

    # Parsing options , if {partition_col} is not None check that column {partition_col}
    # is in the columns of df
    if partition_col and partition_col not in df.columns:
        raise ValueError(
            f"Partition column '{partition_col}' is not defined in the columns of "
            "Dataframe."
        )
    options = {"mergeSchema": str(merge_schema).lower()}
    if replace_where:
        options["replaceWhere"] = replace_where
    if cluster_cols:
        writer = (
            df.write.mode(mode)
            .clusterBy(cluster_cols_list)
            .option("overwriteSchema", str(overwrite_schema).lower())
        )
    elif partition_col:
        writer = (
            df.write.mode(mode)
            .partitionBy(partition_col)
            .option("overwriteSchema", str(overwrite_schema).lower())
        )
    else:
        writer = df.write.mode(mode).option(
            "overwriteSchema", str(overwrite_schema).lower()
        )
    for k, v in options.items():
        writer = writer.option(k, v)

    writer.saveAsTable(dest_table)

    # Update freshness pilot table
    if FRESHNESS_PILOT_ENABLED:
        freshness_spark.update_freshness_pilot_table(table_name=dest_table)


class DropOrder(Enum):
    first = partial(f.asc)
    last = partial(f.desc)


def drop_duplicates_by_order(
    df: DataFrame, columns: List | str, order_col: List | str, keep: str = "first"
) -> DataFrame:
    """
    Drop first/last occurence of columns while respecting a certain order of order_col
    :param df: The dataframe in which columns should dropped
    :param columns: The columns to consider for identifying duplicates
    :param order_col: The column to order before removing dupplicates
    :param keep: {‘first’, ‘last’}, default ‘first’ Determines which duplicates (if any)
                 to keep after ordering
                 ‘first’ : Drop duplicates except for the first occurrence.
                 ‘last’ : Drop duplicates except for the last occurrence.
    :return: The input dataframe with dupplicates removed.
    """
    if isinstance(order_col, str):
        order_col = [order_col]
    drop_order = DropOrder[keep].value
    window = Window.partitionBy(columns).orderBy(*[drop_order(c) for c in order_col])
    df = (
        df.select("*", f.row_number().over(window).alias("row_number"))
        .where("row_number==1")
        .drop("row_number")
    )
    return df


def set_table_column_not_null(table_path: str, column: str) -> None:
    """
    Function to add primary key on table

    :param table_path: path of the table
    :type table_path: str
    :param column: column name to set not null
    :type column: str
    :return: None
    """
    spark.sql(f"ALTER TABLE {table_path} ALTER COLUMN {column} SET NOT NULL")


def add_table_primary_key(table_path: str, pk_name: str, columns: str) -> None:
    """
    Function to add primary key on table

    :param table_path: path of the table
    :type table_path: str
    :param pk_name: primary key name
    :type pk_name: str
    :param columns: column list
    :type columns: str
    :return: None
    """
    spark.sql(
        f"ALTER TABLE {table_path} ADD CONSTRAINT {pk_name} PRIMARY KEY({columns})"
    )


def get_latest_version_for_each_id_column(
    df: DataFrame, id_col: str, extraction_timestamp_col: str
) -> DataFrame:
    """
    Extract the latest version of an SAP table by retrieving latest value for each ID

    :param df: Stacked dataframe to filter
    :type df: DataFrame
    :param id_col: Specify ID column name
    :type id_col: str
    :param extraction_timestamp_col: Sepcify timestamp column name
                                     (column for ordering data)
    :type extraction_timestamp_col: str
    :return: Dataframe filtered on latest values
    :rtype: DataFrame
    """
    window = Window.partitionBy(id_col).orderBy(f.col(extraction_timestamp_col).desc())
    df_latest = (
        df.withColumn("row", f.row_number().over(window))
        .filter(f.col("row") == 1)
        .drop("row")
    )
    return df_latest


def UnityCatalogCheckTableExists(DbxTablePath: str) -> bool:
    """
    Check if table Exist or not in Unity Catalog as a MANAGED or an EXTERNAL table ,
    not a VIEW

    @TODO - TECHNICALDEBT: format this function and one's parameter to a function format:
    - `UnityCatalogCheckTableExists` -> `unity_catalog_check_table_exists`
    - `DbxTablePath` -> `dbx_pable_path`
    When using it, it seems like a class ....

    :param DbxTablePath: Path of Unity Catalog Table as  CATALOG.SCHEMA.TABLE_NAME
    :type DbxTablePath: str
    :return: True or False
    :rtype: bool
    """
    PathSplit = DbxTablePath.lower().split(".")
    if len(PathSplit) != 3:
        print("Must have 3 sections [CatalogName].[SchemaName].[TableName]")
        return False
    if len(PathSplit) == 3:
        df_tables = spark.read.table(f"{PathSplit[0]}.information_schema.tables")
        true_false = bool(
            df_tables.filter(f.col("table_catalog") == PathSplit[0])
            .filter(f.col("table_schema") == PathSplit[1])
            .filter(f.col("table_name") == PathSplit[2])
            .filter(
                f.col("table_type").isin(
                    "MANAGED", "EXTERNAL", "STREAMING_TABLE", "MATERIALIZED_VIEW"
                )
            )
            .count()
        )
        return true_false


def merge_into(
    df_source: DataFrame,
    target_table: str,
    list_id_matching_columns: List[str],
    do_when_matched: bool = True,
    matched_clause: Optional[str] = None,
    do_when_notmatched: bool = True,
    do_when_notmatchedbysource: bool = False,
) -> DataFrame:
    """
    Use _merge_into_spark_sql function and update freshness pilot table once the target
    table has been updated

    :param df_source: DataFrame to merge
    :type df_source: DataFrame
    :param target_table: Table name of the table to MERGE INTO
    :type target_table: str
    :param list_id_matching_columns: List of matching column name between two dataframes
                                     that are merged
    :type list_id_matching_columns: List[str]
    :param matched_clause: Optional statement added to the "WHEN MATCHED".
                           Must be on the form "source.COLXX = target.COLXX" or
                           "source.COLXX <> target.COLXX", COLXX not being part of
                           list_id_matching_columns.
    :type matched_clause: str
    :param do_when_matched: Optional statement to do UPDATE when MATCHED
    :type do_when_matched: bool. Default True
    :param do_when_notmatched: Optional statement to do INSERT when NOT MATCHED
    :type do_when_notmatched: bool. Default True
    :param do_when_notmatchedbysource: Optional statement to do DELETE when
                                       NOT MATCHED BY SOURCE
    :type do_when_notmatchedbysource: bool. Default False
    :return: Generated SQL statement executed
    :rtype: DataFrame
    """
    df = merge_into_spark._merge_into_spark_sql(
        df_source=df_source,
        target_table=target_table,
        list_id_matching_columns=list_id_matching_columns,
        do_when_matched=do_when_matched,
        matched_clause=matched_clause,
        do_when_notmatched=do_when_notmatched,
        do_when_notmatchedbysource=do_when_notmatchedbysource,
    )

    # Update freshness pilot table
    if FRESHNESS_PILOT_ENABLED:
        freshness_spark.update_freshness_pilot_table(table_name=target_table)

    return df


def check_schema_of_the_header_file(df_header: DataFrame):
    """Check if we have the right columns in the manual added header file

    :param: df_header: sap header table dataframe
    :raise: Exception if the header schema is not respected
    """
    error_message = (
        "Bad Table schema. Expecting 4 columns, Table_name, Field, Business_name and "
        "Format"
    )
    if df_header.columns == SAP_BUISNESS_HEADER_COLUMNS:
        LOGGER.info("Correct schema")
    else:
        LOGGER.critical(error_message)
        raise ValueError(error_message)


def create_unity_catalog_table(
    catalog: str,
    domain: str,
    table: str,
    force_reinitialization: bool,
    columns: Dict[str, str],
    cluster_cols: str = None,
    partition_col: str = None,
    primary_key_cols: str = None,
    force_drop: bool = False,
) -> None:
    """
    Function for creating a unity catalog if it does not exist. Also possible to
    reinitialize the table if :param force_reinitialization: is set to True.
    If force_drop is set to True, the table will be dropped before creating a new one.

    :param catalog: Unity catalog
    :type catalog: str
    :param domain: Unity catalog domain or schema
    :type domain: str
    :param table: Unity catalog table name
    :type table: str
    :param force_reinitialization: Whether to reinitialize the table or not
    :type force_reinitialization: bool
    :param columns: Dictionary defining columns and their types , example :
                    {"stack_row_id": "BIGINT", "mdkp_ID": "STRING",
                    "extraction_timestamp": "TIMESTAMP", "extraction_date": "DATE"}
    :type columns: Dict[str, str]
    :param cluster_cols: Names of the columns to cluster by
    :type cluster_cols: str, optional
    :param partition_cols: Name of the column to partition by
    :type partition_col: str, optional
    :param primary_key_cols: Names of the columns to be set as primary key separated by
                             commas
    :type primary_key_cols: str, optional
    :param force_drop: Whether to drop the table before creating a new one
    :type force_drop: bool, optional
    : output : none
    """
    if not columns:
        raise ValueError("Column dictionary is empty.")

    # Parsing options , if {partition_col} is not None check that column {partition_col}
    # is in the columns list
    if partition_col and partition_col not in columns:
        raise ValueError(
            f"Partition column '{partition_col}' is not defined in the column "
            "dictionary."
        )

    # Parsing options , if {cluster_cols} is not None check that each column in
    # {cluster_cols} is in the columns list
    if cluster_cols:
        cluster_cols_list = cluster_cols.split(",")
        for col in cluster_cols_list:
            if col.strip() not in columns:
                raise ValueError(
                    f"Cluster column '{col.strip()}' is not defined in the column "
                    "dictionary."
                )

    # Parsing options , if {primary_key_cols} is not None check that each column in
    # {primary_key_cols} is in the columns list
    if primary_key_cols:
        primary_key_cols_list = primary_key_cols.split(",")
        for col in primary_key_cols_list:
            if col.strip() not in columns:
                raise ValueError(
                    f"Primary key column '{col.strip()}' is not defined in the column "
                    "dictionary."
                )

    # Drop table if {force_drop} is True
    if force_drop:
        sqlcmd = f"DROP TABLE IF EXISTS {catalog}.{domain}.{table}"
        LOGGER.info(sqlcmd)
        spark.sql(sqlcmd)

    # Parsing options, take by precedence {cluster_cols} if not None, else
    # {partition_col} if not None, else no partitioning
    if cluster_cols:
        clust_part_sentence = f"CLUSTER BY ({cluster_cols})"
    elif partition_col is not None:
        clust_part_sentence = f"PARTITIONED BY ({partition_col})"
    else:
        clust_part_sentence = ""

    # Parsing options , generate primary key sentence if {primary_key_cols} is not None
    if primary_key_cols:
        pk_sentence = f", PRIMARY KEY ({primary_key_cols})"
    else:
        pk_sentence = ""

    schema = [f"{col_name} {col_type}" for col_name, col_type in columns.items()]
    table_schema = ", ".join(schema)

    # Create database if it does not exist
    sqlcmd = f"CREATE DATABASE IF NOT EXISTS {catalog}.{domain}"
    LOGGER.info(sqlcmd)
    spark.sql(sqlcmd)

    # Create table if it does not exist
    sqlcmd = (
        f"CREATE TABLE IF NOT EXISTS {catalog}.{domain}.{table} "
        f"( {table_schema} {pk_sentence} ) USING DELTA {clust_part_sentence} "
    )
    LOGGER.info(sqlcmd)
    spark.sql(sqlcmd)

    # Force reinitialization is param is True
    if force_reinitialization:
        spark.sql(f"DELETE FROM {catalog}.{domain}.{table} ")
        LOGGER.info(f"Table {catalog}.{domain}.{table} has been reinitialized")


def array_position_custom(col: Column | str, value: Column | str) -> Column:
    """
    A custom wrapper of the `f.array_position` function, to make the `value` parameter
    compatible with using a column expression and not just a column name reference.
    For more detail on usage of `f.array_position()` see
    :meth:`pyspark.sql.functions.array_position`

    :param col: The column in which a list is located, to find the position of a value.
    :param value: The value to find in the provided column list.
    :return: The `f.array_position()` expression.
    """
    # if value is a string, one transform to column
    if isinstance(value, str):
        value = f.col(value)
    # if it's a simple python value use lit()
    elif not isinstance(value, Column):
        value = f.lit(value)

    return f.array_position(col, value)


def remove_leading_zeros(df: DataFrame, column_names: list[str]) -> DataFrame:
    """
    Function for removing leading zeros from specified columns in a DataFrame.

    :param df: DataFrame from which to remove leading zeros
    :type df: DataFrame
    :param column_names: List of column names to process for removing leading zeros
    :type column_names: list[str]
    :return: DataFrame with leading zeros removed from specified columns
    :rtype: DataFrame
    """
    remove_dict = {
        column_name: f.regexp_replace(df[column_name], "^0+", "")
        for column_name in column_names
    }
    return df.withColumns(remove_dict)


def trim_dataframe(df: DataFrame) -> DataFrame:
    """
    Trim the data contained in `df` in an optimized way

    :param df: input Spark dataframe
    :type df: pyspark.sql.DataFrame
    :return: Spark dataframe with nan values replaced
    :rtype: pyspark.sql.DataFrame
    """

    return df.select(
        [
            f.when(f.trim(f.col(column)) == "", f.lit(None))
            .otherwise(f.trim(f.col(column)))
            .alias(column)
            for column in df.columns
        ]
    )


def escape_sql_value(value: Any) -> str:
    """
    Convert a value to string and escape single quotes for safe SQL literal usage.

    This helper casts the input to ``str`` and replaces any single quote ``'`` with
    a doubled single quote ``''`` so it can be embedded in SQL string literals
    (e.g., ``... 'O''Reilly' ...``).

    .. note::
       - This function **does not** add surrounding quotes; it only escapes the
         internal single quotes of the value's string representation.
       - This is a minimal escaping helper for SQL **string literals**. It is **not**
         a general-purpose SQL sanitization routine and does **not** protect against
         SQL injection. Prefer parameterized queries whenever possible.
       - ``None`` is converted to the literal string ``"None"`` because the value is
         cast with ``str(value)``. If you need SQL ``NULL`` instead, handle that
         upstream/downstream (e.g., conditional logic before calling this function).

    :param value:
        The value to escape for inclusion inside a SQL string literal.
    :type value: Any

    :return:
        The string representation of ``value`` with single quotes doubled.
    :rtype: str
    """
    return str(value).replace("'", "''")


def _normalize_column_name(table_path: str, column_name: str) -> str:
    """
    Given a table path and a column name, returns the exact column name as defined in the table schema,
    preserving the correct case as stored in Unity Catalog or Hive Metastore.

    This is useful for handling case-sensitivity issues, especially when working with Delta tables or
    Unity Catalog, where the actual column name's case may differ from the input. The function queries
    the table schema, builds a mapping of lowercased column names to their actual names, and returns
    the correct version if found. If the column does not exist, returns the input column name.

    :param table_path: Full table path (catalog.schema.table)
    :type table_path: str
    :param column_name: Column name to normalize (case-insensitive)
    :type column_name: str
    :return: The column name as it exists in the table schema (with correct case), or the input name if not found
    :rtype: str
    """
    try:
        # Get the table schema as a list of Row objects
        table_desc = spark.sql(f"DESCRIBE TABLE {table_path}").collect()

        # Build a mapping from lower-case column names to their actual case in the schema
        column_mapping = {row.col_name.lower(): row.col_name for row in table_desc}

        # Return the normalized column name if found, else return the original input
        normalized = column_mapping.get(column_name.lower(), column_name)

        if normalized != column_name:
            LOGGER.info(f"[NORMALIZE] {table_path}.{column_name} -> {normalized}")

        return normalized

    except Exception as e:
        LOGGER.warning(
            f"[WARN] Could not normalize column {column_name} for {table_path}: {e}"
        )
        return column_name


def apply_table_tags_uc(
    full_table_name: str, table_tags: Optional[dict], dry_run: bool = False
):
    """
    Apply Unity Catalog table-level tags using ``ALTER TABLE ... SET TAGS``.

    Parameters
    ----------
    full_table_name : str
        Fully qualified table name (e.g., ``catalog.schema.table``).
    table_tags : dict or None
        Dictionary of tags to apply on the table.
        Keys are tag names. Values are tag values or ``None`` for valueless tags.
    dry_run : bool, optional
        If ``True``, the function prints the SQL statements instead of executing them.
        Default is ``False``.

    Notes
    -----
    - When ``val`` is ``None``, the tag is applied as ``('key')``.
    - When ``val`` is not ``None``, the value is escaped and applied as ``('key' = 'value')``.
    - SQL statements are executed through ``spark.sql`` unless ``dry_run`` is enabled.

    Examples
    --------
    >>> apply_table_tags_uc(
    ...     "main.sales.orders",
    ...     {"owner": "data-team", "pii": None},
    ...     dry_run=True
    ... )
    [DRY-RUN] ALTER TABLE main.sales.orders SET TAGS ('owner' = 'data-team')
    [DRY-RUN] ALTER TABLE main.sales.orders SET TAGS ('pii')
    """
    if not table_tags:
        return
    for key, val in table_tags.items():
        if val is None:
            stmt = f"ALTER TABLE {full_table_name} SET TAGS ('{key}')"
        else:
            sval = escape_sql_value(val)
            stmt = f"ALTER TABLE {full_table_name} SET TAGS ('{key}' = '{sval}')"
        if dry_run:
            print("[DRY-RUN] " + stmt)
        else:
            spark.sql(stmt)


def apply_column_tags_uc(
    full_table_name: str, column_tags: Optional[dict], dry_run: bool = False
):
    """
    Apply Unity Catalog column-level tags using
    ``ALTER TABLE ... ALTER COLUMN ... SET TAGS``.

    Parameters
    ----------
    full_table_name : str
        Fully qualified table name.
    column_tags : dict or None
        Dictionary mapping column names to dictionaries of tags.
        Example::
            {
                "col1": {"sensitivity": "high", "pii": None},
                "col2": {"quality": "verified"}
            }
    dry_run : bool, optional
        If ``True``, SQL statements are printed instead of executed.

    Notes
    -----
    - Each column may have multiple tags.
    - Tags with ``None`` values become valueless tags.
    - Tag values are escaped with ``escape_sql_value``.
    - SQL is executed through ``spark.sql`` unless in dry-run mode.

    Examples
    --------
    >>> apply_column_tags_uc(
    ...     "main.sales.orders",
    ...     {"customer_id": {"pii": None, "classification": "sensitive"}},
    ...     dry_run=True
    ... )
    [DRY-RUN] ALTER TABLE main.sales.orders ALTER COLUMN customer_id SET TAGS ('pii')
    [DRY-RUN] ALTER TABLE main.sales.orders ALTER COLUMN customer_id SET TAGS ('classification' = 'sensitive')
    """
    if not column_tags:
        return
    for col, tags in column_tags.items():
        normalized_col = _normalize_column_name(full_table_name, col)
        for key, val in tags.items():
            if val is None:
                stmt = f"ALTER TABLE {full_table_name} ALTER COLUMN `{normalized_col}` SET TAGS ('{key}')"
            else:
                sval = escape_sql_value(val)
                stmt = f"ALTER TABLE {full_table_name} ALTER COLUMN `{normalized_col}` SET TAGS ('{key}' = '{sval}')"
            if dry_run:
                print("[DRY-RUN] " + stmt)
            else:
                spark.sql(stmt)


def apply_pk_fk_tags_uc(
    full_table_name: str,
    primary_key: Optional[List[str]],
    foreign_keys: Optional[List[dict]],
    dry_run: bool = False,
):
    """
    Apply Unity Catalog logical PK/FK tagging conventions to a table and its columns.

    The function sets Databricks Unity Catalog tags on both the table and the
    relevant columns to describe primary and foreign keys:

    - **Primary key (PK)**:
      - On each PK column: set the tag key ``'primary_key'`` (boolean-like, no value).
      - On the table: set ``'primary_key_columns'`` to a comma-separated list
        of PK column names.

    - **Foreign keys (FK)**:
      - On each local FK column: set ``'foreign_key'`` with the referenced target
        in the form ``<ref_table>(col1, col2, ...)``.
      - If provided, also set ``'foreign_key_constraint_name'`` on the local column.
      - On the table: set ``'foreign_keys'`` to a multi-line summary where each line
        has the form ``local_col1, local_col2 -> <ref_table>(ref_col1, ref_col2, ...)``.


    :param full_table_name:
        Fully qualified table name (e.g., ``catalog.schema.table``) on which tags
        will be applied.
    :type full_table_name: str

    :param primary_key:
        List of primary key column names. If ``None`` or empty, no PK tags are applied.
    :type primary_key: Optional[List[str]]

    :param foreign_keys:
        List of foreign key descriptors. Each item **must** be a ``dict`` with:
          - ``"columns"`` (List[str]): local (child) column names.
          - ``"references"`` (dict): must contain:
              - ``"table"`` (str): fully qualified referenced table name.
              - ``"columns"`` (List[str]): referenced (parent) column names.
          - Optional ``"constraint_name"`` (str): the FK constraint name.
        If ``None`` or empty, no FK tags are applied.
    :type foreign_keys: Optional[List[dict]]

    :param dry_run:
        If ``True``, print SQL statements instead of executing them.
        Default is ``False``.
    :type dry_run: bool

    :return:
        None. The function executes (or prints in dry-run) ``ALTER TABLE`` /
        ``ALTER COLUMN`` statements to set tags.
    :rtype: None

    :notes:
        - Tag strings are sanitized with ``escape_sql_value`` to avoid malformed SQL.
        - Only the specified tag keys are updated; other existing tags remain unchanged.
        - Uses Databricks Unity Catalog tag DDL, e.g.:
          ``ALTER TABLE <tbl> SET TAGS ('k' = 'v')`` and
          ``ALTER TABLE <tbl> ALTER COLUMN <col> SET TAGS ('k' = 'v')``.

    :raises KeyError:
        If required keys are missing in any of the provided ``foreign_keys`` entries.

    :raises Exception:
        Any exception raised by the underlying ``spark.sql`` execution.

    :example:
        >>> apply_pk_fk_tags_uc(
        ...     full_table_name="main.sales.orders",
        ...     primary_key=["order_id"],
        ...     foreign_keys=[
        ...         {
        ...             "columns": ["customer_id"],
        ...             "references": {
        ...                 "table": "main.crm.customers",
        ...                 "columns": ["id"],
        ...             },
        ...             "constraint_name": "fk_orders_customers",
        ...         }
        ...     ],
        ...     dry_run=True,
        ... )
        [DRY-RUN] ALTER TABLE main.sales.orders ALTER COLUMN customer_id SET TAGS ('foreign_key' = 'main.crm.customers(id)')
        [DRY-RUN] ALTER TABLE main.sales.orders ALTER COLUMN customer_id SET TAGS ('foreign_key_constraint_name' = 'fk_orders_customers')
        [DRY-RUN] ALTER TABLE main.sales.orders ALTER COLUMN order_id SET TAGS ('primary_key')
        [DRY-RUN] ALTER TABLE main.sales.orders SET TAGS ('primary_key_columns' = 'order_id')
        [DRY-RUN] ALTER TABLE main.sales.orders SET TAGS ('foreign_keys' = 'customer_id -> main.crm.customers(id)')
    """
    # =======================
    #        PRIMARY KEY
    # =======================
    if primary_key:
        pk_cols = [c for c in primary_key if c]
        normalized_pk_cols = [
            _normalize_column_name(full_table_name, c) for c in pk_cols
        ]

        # Tag sur les colonnes PK
        for c in normalized_pk_cols:
            stmt = (
                f"ALTER TABLE {full_table_name} "
                f"ALTER COLUMN `{c}` SET TAGS ('primary_key')"
            )
            if dry_run:
                print("[DRY-RUN] " + stmt)
            else:
                spark.sql(stmt)

        # Tag table : résumé des PK
        pk_list = ", ".join(normalized_pk_cols)
        stmt_view = (
            f"ALTER TABLE {full_table_name} "
            f"SET TAGS ('primary_key_columns' = '{escape_sql_value(pk_list)}')"
        )
        if dry_run:
            print("[DRY-RUN] " + stmt_view)
        else:
            spark.sql(stmt_view)

    # =======================
    #        FOREIGN KEYS
    # =======================
    fk_summaries = []

    if foreign_keys:
        for fk in foreign_keys:

            # Chaque fk est un Row Spark
            cols = fk["columns"] or []
            ref = fk["references"]

            if ref is None:
                continue  # sécurité

            ref_table = ref["table"]
            ref_cols = ref["columns"]
            normalized_cols = [_normalize_column_name(full_table_name, c) for c in cols]

            ref_str = f"{ref_table}({', '.join(ref_cols)})"

            # Tag sur colonnes locales
            for c in normalized_cols:
                stmt = (
                    f"ALTER TABLE {full_table_name} "
                    f"ALTER COLUMN `{c}` SET TAGS ('foreign_key' = '{escape_sql_value(ref_str)}')"
                )
                if dry_run:
                    print("[DRY-RUN] " + stmt)
                else:
                    spark.sql(stmt)

                # ----- constraint_name -----
                cname = (
                    fk.get("constraint_name")
                    if isinstance(fk, dict)
                    else fk["constraint_name"]
                )
                if cname:
                    stmt2 = (
                        f"ALTER TABLE {full_table_name} ALTER COLUMN `{c}` "
                        f"SET TAGS ('foreign_key_constraint_name' = '{escape_sql_value(cname)}')"
                    )
                    if dry_run:
                        print("[DRY-RUN] " + stmt2)
                    else:
                        spark.sql(stmt2)

            # Texte résumé au niveau table
            fk_summaries.append(f"{', '.join(normalized_cols)} -> {ref_str}")

    # Tag table : résumé FK
    if fk_summaries:
        fk_summary_txt = "\n".join(fk_summaries)
        stmt = (
            f"ALTER TABLE {full_table_name} "
            f"SET TAGS ('foreign_keys' = '{escape_sql_value(fk_summary_txt)}')"
        )

        if dry_run:
            print("[DRY-RUN] " + stmt)
        else:
            spark.sql(stmt)
