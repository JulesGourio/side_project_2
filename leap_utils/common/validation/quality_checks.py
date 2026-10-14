import re
from itertools import chain
from typing import Dict, Iterable, List, Optional

import pandas as pd
from pyspark.sql import DataFrame
from pyspark.sql import functions as f
from pyspark.sql.column import Column

from leap_utils.common.logger import get_logger, setup_applevel_logger
from leap_utils.common.validation import validation_utils
from leap_utils.common.validation import event_logging
from leap_utils.config import spark

setup_applevel_logger()
LOGGER = get_logger(__name__)

# Table history writing operations as defined in
# https://docs.databricks.com/en/delta/history.html
WRITING_OPERATIONS = (
    "WRITE",
    "REPLACE TABLE AS SELECT",
    "COPY INTO",
    "MERGE",
    "CREATE TABLE AS SELECT",
    "CREATE OR REPLACE TABLE AS SELECT",
)
# Column type in the information schema table
INFORMATION_SCHEMA_TABLE_TYPE_COLUMN = "table_type"

# Describe history query limit
HISTORY_LIMIT = 50

# Timezone used for date calculations
TIMEZONE_STR = "Europe/Paris"

# SAP Extraction time
SAP_EXTRACTION_TIME_SUNDAY = 8
SAP_EXTRACTION_TIME_MONDAY_TO_SATURDAY = 22


def collect_column_in_list(df: DataFrame, column: str) -> list:
    """
    Collects values from a specific column  in a Spark DataFrame.

    :param:: df (DataFrame): The Spark DataFrame from which to collect values.
    :param: column (str): The name of the column to extract values from.
    :return: list: A list containing the values from the specified column.
    """
    # Use Spark DataFrame's collect method to retrieve all rows
    # and map to extract values from the specified column
    values = [row[column] for row in df.select(column).collect()]
    return values


def outlier_treatment(
    df: DataFrame,
    column: str,
    outlier_method: str,
    threshold: float = 3.0,
    check_upper_bound: bool = True,
):
    """
    Detects and removes outliers from a PySpark DataFrame using the z-score method or
    the interquartile method.

    :param df: PySpark DataFrame
    :param columns: column name for which outliers should be removed
    :param outlier_method: should be either 'STD' or 'IQR'
    :param threshold: outlier-score threshold to determine outliers (default is 3.0)
    :param check_upper_bound: Whether to apply upper bound check (default True)
    :return: PySpark DataFrame with outliers removed
    """

    if outlier_method == "STD":
        # Calculate mean and standard deviation
        stats = df.select(
            f.mean(f.col(column)).alias("mean"),
            f.stddev(f.col(column)).alias("stddev"),
        ).collect()[0]
        lower_bound = stats["mean"] - threshold * stats["stddev"]
        upper_bound = stats["mean"] + threshold * stats["stddev"]

    elif outlier_method == "IQR":
        q1, median, q3 = df.approxQuantile(column, [0.25, 0.5, 0.75], 0)
        iqr = q3 - q1
        if iqr == 0:  # In case of equality, take 10% of margin
            lower_bound = median * 0.9
            upper_bound = median * 1.1
        else:
            lower_bound = q1 - threshold * iqr
            upper_bound = q3 + threshold * iqr

    else:
        raise ValueError(
            "The method should be either 'STD', "
            f"either 'IQR', here it's {outlier_method}"
        )

    lower_bound = max(lower_bound, 0)

    if check_upper_bound:
        condition = (f.col(column) >= lower_bound) & (f.col(column) <= upper_bound)
    else:
        condition = f.col(column) >= lower_bound

    # Log computed bounds
    LOGGER.debug(
        f"[Outlier Detection] {column}: Lower = {lower_bound:.2f}, Upper = {upper_bound:.2f}"
    )

    # Calculate completness check from lower and upper bounds
    df = df.withColumn("completness_check", condition)
    df = df.withColumn("lower_bound", f.lit(lower_bound))
    df = df.withColumn("upper_bound", f.lit(upper_bound))

    return df


def get_views_list() -> list:
    """
    Retrieves information about tables and views from the Spark SQL system information
    schema.

    :return: list: list of views.
    """
    # Execute Spark SQL query to get information about tables and views
    view_df = (
        spark.sql("SELECT * FROM system.information_schema.tables")
        .filter(f.col(INFORMATION_SCHEMA_TABLE_TYPE_COLUMN) == "VIEW")
        .select(
            f.concat_ws(".", "table_catalog", "table_schema", "table_name").alias(
                "table_path"
            ),
            INFORMATION_SCHEMA_TABLE_TYPE_COLUMN,
        )
    )

    return collect_column_in_list(view_df, "table_path")


def get_materialized_views_list() -> list:
    """
    Retrieves information about tables and views from the Spark SQL system information
    schema.

    :return: list: list of views.
    """
    # Execute Spark SQL query to get information about tables and views
    view_df = (
        spark.sql("SELECT * FROM system.information_schema.tables")
        .filter(f.col(INFORMATION_SCHEMA_TABLE_TYPE_COLUMN) == "MATERIALIZED_VIEW")
        .select(
            f.concat_ws(".", "table_catalog", "table_schema", "table_name").alias(
                "table_path"
            ),
            INFORMATION_SCHEMA_TABLE_TYPE_COLUMN,
        )
    )

    return collect_column_in_list(view_df, "table_path")


def extract_table_name_from_sql(input_sql: str, view_path: str) -> str:
    """
    Extracts the table name from a SQL query by searching for the 'FROM' clause.

    :param: input_sql (str): The SQL query from which the table name needs to be
    :return: str: The extracted table name.
    :param: view_path (str): Path of the view in the Unity Catalog

    Raises:
    :exception: If the SQL query looks different from the expected pattern.

    """
    # Upper SQL query
    input_sql_upper = input_sql.upper()

    # Define a regular expression pattern to match the 'FROM' clause in an SQL query
    pattern = re.compile(r"FROM\s+(\w+(\.\w+)*)")

    # Search for the pattern in the input SQL query
    match = pattern.search(input_sql_upper)

    # Check if a match is found
    if match:
        # Extract the table name from the matched group
        table_name = match.group(1).strip()
        table_name = table_name.lower()
    else:
        # Raise an exception if the SQL query structure is different from the expected
        # pattern
        raise ValueError(
            "SQL query looks a bit different from the pattern defined on view: "
            f"{view_path} with SQL query : {input_sql}"
        )

    return table_name


def get_view_source_table(view_path: str) -> str:
    """
    Retrieves the source table name from the SQL query used to create a Spark SQL view.

    param: view_path (str): The path or name of the Spark SQL view.
    :return: str: The name of the source table extracted from the SQL query.
    """
    # Execute Spark SQL query to get the CREATE TABLE statement for the specified view
    view_df = spark.sql(f"SHOW CREATE TABLE {view_path}")
    # Extract the CREATE TABLE SQL query from the DataFrame
    view_sql_query = collect_column_in_list(view_df, "createtab_stmt")[0]
    # Extract the source table name from the SQL query
    table_source = extract_table_name_from_sql(view_sql_query, view_path)

    return table_source


def _calculate_oldest_acceptable_timestamp_from_frequency(
    df_tables_summary: DataFrame,
) -> DataFrame:
    """
    Function for calculating the oldest acceptable timestamp for freshness checks, it's
    based on the following steps:
    1. Calculate the oldest acceptable date in paris timezone from frequency of update
    2. Calculate the oldest acceptable timestamp depending on SAP extraction time :
        - If sunday, extraction starts at 2PM
        - From monday to saturday, extraction starts at 10PM

    :param df_tables_summary: Dataframe used in "check_tables_freshness" function
    :type oldest_acceptable_date: DataFrame
    :return: Same dataframe with oldest acceptable timestamp added
    :rtype: DataFrame
    """

    # Calculate the oldest acceptable date (lower bound for freshness check calculation)
    df_tables_summary_transformed = df_tables_summary.withColumn(
        "oldest_acceptable_date_(paris_timezone)",
        f.date_sub(f.col("current_timestamp"), f.col("frequency_of_update_(days)")),
    )

    # Calculate the oldest acceptable timestamp based on SAP extraction time
    df_tables_summary_transformed = df_tables_summary_transformed.withColumn(
        "week_day", f.weekday("oldest_acceptable_date_(paris_timezone)")
    )

    df_tables_summary_transformed = df_tables_summary_transformed.withColumn(
        "nb_hours_to_add_to_oldest_acceptable_date",
        f.when(f.col("week_day") == 6, SAP_EXTRACTION_TIME_SUNDAY).otherwise(
            SAP_EXTRACTION_TIME_MONDAY_TO_SATURDAY
        ),
    )

    df_tables_summary_transformed = df_tables_summary_transformed.withColumn(
        "oldest_acceptable_timestamp_(paris_timezone)",
        (
            f.unix_timestamp("oldest_acceptable_date_(paris_timezone)")
            + f.col("nb_hours_to_add_to_oldest_acceptable_date") * 3600
        ).cast("timestamp"),
    )

    # Drop useless columns
    df_tables_summary_transformed = df_tables_summary_transformed.drop(
        "week_day", "nb_hours_to_add_to_oldest_acceptable_date"
    )

    return df_tables_summary_transformed


def check_tables_freshness(
    read_env: str, tables_dict: Dict[str, int], verbose: bool = False
) -> Dict[str, bool]:
    """
    Check the freshness of specified tables by examining their writing history.
    It takes into consideration the type view/table of the input table_path and its
    frequency of update.
    WARNING: Headers and manual tables are not considered

    The calculation is the following :
    - Get last modification datetime of different tables in Paris timezone
    - Calculate the lower bound by getting the oldest acceptable date from the current
    date and the expected frequency of update
    - Calculate the oldest acceptable timestamp by taking into account SAP extraction
    time
    - Calculate the freshness check in comparing last modification timestamp & the
    oldest acceptable datetime

    TODO: Should improve global freshness checks philosophy by taking into account
    extraction time rather than delta lake update time.

    :param: tables_dict (Dict[str, int]): Dictionary of table names to be checked for
                                          freshness, and there associated frequency
                                          expected {table_name :
                                                    expected_update_frequency}
    :param: verbose (bool, optional): If True, display detailed summary information.
                                      Default is False.
    :return: Dict[str, bool]: Dictionary of freshness checks results :
        {table_name: freshness_flag}
    """
    # check if all tables are tables if it is view get the source table
    all_views = get_views_list()

    # Retrieve table from views and others
    all_tables_dict = {
        (
            get_view_source_table(table) if table in all_views else table
        ): expected_update_frequency
        for table, expected_update_frequency in tables_dict.items()
    }

    # Build a union all query for checking freshness of each table
    # Filter for keeping operations with data insertion (at least one data change).
    # Depending the writing operation (Append, overwrite, merge), the filter is not on
    # the same parameter
    union_all_query = " UNION ALL ".join(
        [
            f"""(SELECT last_update_time AS modification_timestamp, job_name, table_name AS table
            FROM ({read_env}_bronze.data_quality.freshness_pilot_table )
            WHERE table_name=\"{table}\"
            LIMIT 1)"""
            for table in all_tables_dict
        ]
    )

    # Execute the union all query using Spark SQL
    tables_summary = spark.sql(union_all_query)

    # add table update frequency
    mapping_frequency_expr = f.create_map(
        [f.lit(x) for x in chain(*all_tables_dict.items())]
    )
    tables_summary = tables_summary.withColumn(
        "frequency_of_update_(days)", mapping_frequency_expr[f.col("table")]
    )

    # Calculate modification timestamp in Paris Timezone
    tables_summary = tables_summary.withColumn(
        "last_modification_timestamp_(paris_timezone)",
        f.from_utc_timestamp("modification_timestamp", tz=TIMEZONE_STR),
    )

    # Get the current date in Paris Timezone
    tables_summary = tables_summary.withColumn(
        "current_timestamp",
        f.from_utc_timestamp(f.current_timestamp(), tz=TIMEZONE_STR),
    )

    # Calculate the oldest_acceptable_timestamp
    tables_summary = _calculate_oldest_acceptable_timestamp_from_frequency(
        tables_summary
    )

    # Calculate the difference between last modification timestamp of the table and now
    tables_summary = tables_summary.withColumn(
        "days_dff_between_now_and_last_modification_timestamp",
        (
            f.col("current_timestamp").cast("long")
            - f.col("last_modification_timestamp_(paris_timezone)").cast("long")
        )
        / (24.0 * 3600.0),
    )

    # Add a column to check freshness based on timestamp, if jobId is null then it's a
    # manual table
    tables_summary = tables_summary.withColumn(
        "freshness_check",
        (
            f.col("oldest_acceptable_timestamp_(paris_timezone)")
            < f.col("last_modification_timestamp_(paris_timezone)")
        )
        | (f.col("job_name").isNull()),
    ).fillna(False, subset=["freshness_check"])

    # Reorder columns
    tables_summary = tables_summary.select(
        [
            f.col("table"),
            f.col("freshness_check"),
            f.col("frequency_of_update_(days)"),
            f.col("last_modification_timestamp_(paris_timezone)"),
            f.col("oldest_acceptable_timestamp_(paris_timezone)"),
            f.col("days_dff_between_now_and_last_modification_timestamp"),
            f.col("job_name"),
        ]
    )

    # Display the summary if verbose mode is enabled
    if verbose:
        tables_summary.display()

    # Get final results per table
    tables_summary = tables_summary.collect()
    results_freshness = {row["table"]: row["freshness_check"] for row in tables_summary}

    # Return results per table
    return results_freshness


def get_tables_nb_rows_evolution_by_extraction_timestamp(
    tables_iterable: Iterable[str],
    history_limit: int,
    filter_condition: Optional[Column] = None,
) -> Dict[str, DataFrame]:
    """
    Get for all tables the evolution of number of rows appended at each extraction
    timestamp. Return a dictionary with the corresponding Spark Dataframe

    :param tables_iterable: Iterable of unity catalog tables
    :type tables_iterable: Iterable[str]
    :history_limit: number of rows to take into account for calculating statistics
    :type history_limit: int
    :param filter_condition: Optional Spark Column condition to filter DataFrame before analysis
    :type filter_condition: Column, optional
    :return: Dictionary {unity_catalog_table : Spark Dataframe evolution}
    :rtype: Dict[str, DataFrame]
    """

    # Initalize the dictionary
    tables_aggregated_dictionary = {}

    # Check if all tables are tables; if it is a view, get the source table
    all_views = get_views_list()

    # Retrieve table from views and others
    all_tables = [
        get_view_source_table(table) if table in all_views else table
        for table in tables_iterable
    ]

    for table in all_tables:
        df_table = spark.read.table(table)
        df_table = df_table.withColumn("table_name", f.lit(table))

        # Apply filter if provided
        if filter_condition is not None:
            df_table = df_table.filter(filter_condition)

        df_agg = (
            df_table.groupby(["table_name", "extraction_timestamp"])
            .agg(f.count("*").alias("num_rows_appended"))
            .orderBy(f.desc("extraction_timestamp"))
        )

        # Take just a certain amount of rows and persist the dataframe as next function
        # will apply multiple spark actions on it (it makes the query faster)
        df_agg = df_agg.limit(history_limit).persist()
        tables_aggregated_dictionary[table] = df_agg

    return tables_aggregated_dictionary


def check_tables_completeness(
    tables_to_check: List[str],
    tables_aggregated_dictionary: Dict[str, DataFrame],
    verbose: bool = False,
    outlier_method="iqr",
    threshold=1.5,
    check_upper_bound: bool = True,
) -> Dict[str, bool]:
    """
    Check the completeness of specified tables by examining their history. It's based
    on the number of rows evolution on a limited history, a statistical method is then
    use to detect outliers.

    :param tables_to_check: List of unity catalog tables to check
    :type tables_to_check: List[str]
    :param tables_aggregated_dictionary: Dictionary {unity_catalog_table :
                                                     Spark Dataframe evolution}
    :type tables_aggregated_dictionary: Dict[str, DataFrame]
    :param verbose: If True, display detailed summary information, defaults to False
    :type verbose: bool, optional
    :param outlier_method: should be either 'IQR' or 'STD' : classical outlier detection
                           methods, defaults to "iqr"
    :type outlier_method: str, optional
    :param threshold: threshold associated to the oulier method detection,
                      defaults to 1.5
    :type threshold: float, optional
    :param check_upper_bound: Whether to apply upper bound check in outlier detection,
                              defaults to True
    :type check_upper_bound: bool, optional
    :return: Completeness checks results : {table_name: completeness_flag}
    :rtype: Dict[str, bool]
    """

    results_completness = {}
    for table in tables_to_check:
        df_agg = tables_aggregated_dictionary[table]

        # Apply outlier treatment to the summary data
        tables_summary = outlier_treatment(
            df_agg,
            column="num_rows_appended",
            outlier_method=outlier_method,
            threshold=threshold,
            check_upper_bound=check_upper_bound,
        )

        # Extract and store the result of the integrity check for the current table
        try:
            completness_flag = (
                tables_summary.select("completness_check").limit(1).collect()[0]
            )["completness_check"]
            results_completness[table] = completness_flag
            if verbose or (not completness_flag):
                tables_summary.limit(10).display()
        except IndexError:
            LOGGER.warning(
                f"{table} has either a very short history or has no writing operation"
            )

    # Return completeness results of all tables
    return results_completness


def check_tables_increase(
    tables_to_check: List[str],
    tables_aggregated_dictionary: Dict[str, DataFrame],
    verbose: bool = False,
) -> Dict[str, bool]:
    """
    Check the increase of specified tables by examining their history. It's based
    on the number of rows evolution on a limited history.

    :param tables_to_check: List of unity catalog tables to check
    :type tables_to_check: List[str]
    :param tables_aggregated_dictionary: Dictionary {unity_catalog_table :
                                                     Spark Dataframe evolution}
    :type tables_aggregated_dictionary: Dict[str, DataFrame]
    :param verbose: If True, display detailed summary information, defaults to False
    :type verbose: bool, optional
    :return: Increase checks results : {table_name: increase_flag}
    :rtype: Dict[str, bool]
    """
    results_expect_increase = {}
    for table in tables_to_check:
        df_agg = tables_aggregated_dictionary[table]

        # Extract and store the result of the integrity check for the current table
        try:
            last_num_rows_appended = (
                df_agg.select("num_rows_appended").limit(1).collect()[0]
            )["num_rows_appended"]
            max_num_rows_appended = df_agg.agg(f.max("num_rows_appended")).first()[0]

            expect_increase_flag = last_num_rows_appended == max_num_rows_appended
            results_expect_increase[table] = expect_increase_flag

            if verbose or (not expect_increase_flag):
                df_agg.limit(10).display()
        except IndexError:
            LOGGER.warning(
                f"{table} has either a very short history or has no writing operation"
            )

    # Return increase results of all tables
    return results_expect_increase


def validate_and_log_quality_check_results(
    results_dict: Dict[str, bool],
    validation_level: str,
    info_message: str,
    error_message: str,
    pipeline_write_env: Optional[str] = "dev",
    pipeline_name: Optional[str] = None,
    log_to_event_table: bool = False,
    log_successes: bool = False,
    expectation_type: str = None,
    raw_result: Optional[Dict[str, Dict]] = None,
    datalake: str | None = None
) -> None:
    """
    Function for validating quality checks, logging errors & potentially raise an error
    for stopping the job.

    :param results_dict: Dictionary corresponding to tables results
                         {table : result_flag}
    :type results_dict: Dict[str, bool]
    :param validation_level: "RED" if the job should fail in case of error, "AMBER" if
                             just a warning should be logged
    :type validation_level: str
    :param info_message: Message in case all the tests passed
    :type info_message: str
    :param error_message: Message in case at least one test is KO
    :type error_message: str
    :param pipeline_write_env: the ENV we are writing to.
    :param table_name: Name of the validated table (required if logging).
    :param pipeline_name: Pipeline or notebook name (required if logging).
    :param log_to_event_table: Whether to log the GX results.
    :param log_successes: Whether to log both successes and failures.
    :param raw_result: Dict with optional information to log by table_name
    :param datalake: Optionnal affix to the catalog, such as 'hr', defaults to None 
    (standard Leap catalogs)
    :raises ValueError: In case of error on a "RED" validation level a ValueError is
                        raised
    """

    # Validate all results
    all_results_validated = all([result_flag for result_flag in results_dict.values()])

    layer, schema, table = event_logging.QUALITY_EVENT_TABLE.split('.') 
    quality_event_table_path = (
        f"{pipeline_write_env}_{layer}{f'_{datalake}' if datalake else ''}.{schema}.{table}"
    )

    display_results_on_error = pd.DataFrame(
        results_dict.items(), columns=["table_name", "flag"]
    ).display

    def log_action():
        event_logging._log_to_quality_event_table(
            quality_check_results=results_dict,
            validation_level=validation_level,
            pipeline_name=pipeline_name,
            spark_session=spark,
            only_failed=not log_successes,
            quality_event_table_path=quality_event_table_path,
            expectation_type=expectation_type,
            raw_result=raw_result,
        )

    def combined_on_error_action():
        display_results_on_error()
        if log_to_event_table:
            log_action()

    validation_utils.red_amber_validation_logic(
        check=all_results_validated,
        on_success_message=info_message,
        on_success_action=log_action if log_to_event_table and log_successes else None,
        on_error_message=error_message,
        on_error_action=combined_on_error_action,
        on_error_validation_level=validation_level,
    )
