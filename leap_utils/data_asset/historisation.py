"""
Module of leap utils that is used to deal with historisation. 
Basically, we use it when collecting data from stack data asset and wanting to optimize the job runs and pick the data only necessary for a run
Eg: we do not want to laucnh a run on all the history of a data asset if only we want to store the last update 
"""

from pyspark.sql import DataFrame, Window
from pyspark.sql import functions as f
from typing import Literal, Tuple, Union

# Leap utils imports
from leap_utils.data_asset import table_utils
from leap_utils.common.logger import get_logger, setup_applevel_logger
from leap_utils.config import spark


setup_applevel_logger()
LOGGER = get_logger(__name__)


def add_weekly_evol_dates(df: DataFrame) -> DataFrame:
    """
    Add useful columns for retrieving date and stacking a dataframe to its
    past evolution

    :param df: any dataframe
    :return: modified dataframe
    """
    new_df = (
        df.withColumn("load_date", f.current_timestamp())
        .withColumn(
            "observation_date_related_sunday",
            f.date_sub(f.next_day(f.col("load_date"), "sunday"), 14),
        )
        .withColumn(
            "observation_date_related_sunday_next_monday",
            f.date_add(f.col("observation_date_related_sunday"), 1),
        )
        .withColumn(
            "year_week_thursday",
            f.date_add(f.col("observation_date_related_sunday"), 4),
        )
        .withColumn(
            "observation_week",
            f.when(
                f.weekofyear(f.col("observation_date_related_sunday_next_monday")) < 10,
                f.concat(
                    f.year(f.col("year_week_thursday")),
                    f.lit("-0"),
                    f.weekofyear(f.col("observation_date_related_sunday_next_monday")),
                ),
            ).otherwise(
                f.concat(
                    f.year(f.col("year_week_thursday")),
                    f.lit("-"),
                    f.weekofyear(f.col("observation_date_related_sunday_next_monday")),
                )
            ),
        )
        .drop("observation_date_related_sunday_next_monday","year_week_thursday")
    )
    return new_df


def add_monthly_evol_dates(df: DataFrame) -> DataFrame:
    """
    Add useful columns for retrieving date and stacking a dataframe to its
    past evolution

    :param df: any dataframe
    :return: modified dataframe
    """
    new_df = (
        df.withColumn("load_date", f.current_timestamp())
        .withColumn("load_date_prev_month", f.add_months(f.col("load_date"), -1))
        .withColumn(
            "observation_month",
            f.when(
                f.month(f.col("load_date_prev_month")) < 10,
                f.concat(
                    f.year(f.col("load_date_prev_month")),
                    f.lit("-M0"),
                    f.month(f.col("load_date_prev_month")),
                ),
            ).otherwise(
                f.concat(
                    f.year(f.col("load_date_prev_month")),
                    f.lit("-M"),
                    f.month(f.col("load_date_prev_month")),
                )
            ),
        )
        .drop("load_date_prev_month")
    )
    return new_df


def partition_weekly(df: DataFrame) -> DataFrame:
    """
    Partition dataframe using a weekly window

    :param df: dataframe to partition
    :type df: DataFrame
    
    :return: partitioned dataframe
    :rtype: DataFrame
    """
    partition_window = Window.partitionBy("observation_week")
    Window_Spec = Window.orderBy("observation_week")

    new_df = df.withColumn(
        "max_load_date_for_this_week",
        f.max("load_date").over(partition_window),
    )

    new_df = (
        new_df.where(f.col("max_load_date_for_this_week") == f.col("load_date"))
        .drop("max_load_date_for_this_week")
        .withColumn("observation_week_rank", f.dense_rank().over(Window_Spec))
    )

    return new_df


def partition_monthly(df: DataFrame) -> DataFrame:
    """
    Partition dataframe using a monthly window

    :param df: dataframe to partition
    :type df: DataFrame

    :return: partitioned dataframe
    :rtype: DataFrame
    """
    partition_window = Window.partitionBy("observation_month")
    Window_Spec = Window.orderBy("observation_month")

    new_df = df.withColumn(
        "max_load_date_for_this_month",
        f.max("load_date").over(partition_window),
    )

    new_df = (
        new_df.where(f.col("max_load_date_for_this_month") == f.col("load_date"))
        .drop("max_load_date_for_this_month")
        .withColumn("observation_month_rank", f.dense_rank().over(Window_Spec))
    )

    return new_df

def optimize_and_historize(
    dest_output_table_history: str,
    df_input_history: DataFrame,
    extraction_date_col_name_input_df: str,
    extraction_date_col_name_output_df: str,
    force_db_reinitialization: bool = False,
) -> Tuple[Union[DataFrame, None], Union[None, Literal["append", "overwrite"]]]:
    """
    Function that optimize the run of any notebooks that require historization.
    Basically, we use it when collecting data from stack data asset and wanting to optimize the job runs and pick the data only necessary for a run
    
    NB1: To print the log in the notebook where this function is called, you need to set up the logger in the notebook.
    NB2: Few lines have to be added to the notebook where this function is called:
    ```
        # Quit notebook without any errors
        if MODE is None:
        dbutils.notebook.exit(f"the table `{dest_table_history}` is already up to date")
    ```

    :param dest_output_table_history: path string of the output table of the notebook
    :type dest_output_table_history: str
    :param df_input_history: Input Spark dataframe corresponding to the dataframe to filter (or not)
    :type df_input_history: DataFrame
    :param extraction_date_col_name_input_df: column name corresponding to the a date of extraction of `df_input_history` data asset 
    :type extraction_date_col_name_input_df: str
    :param extraction_date_col_name_output_df: column name corresponding to the a date of extraction of `df_input_history` data asset 
    :type extraction_date_col_name_output_df: str
    :param force_db_reinitialization: parameter related to a widget that indicates if we want to generate totally from the beginning the history of the output data asset
    :type force_db_reinitialization: bool

    :return: a pre-filtered dataframe on a specific date interval nor `df_input_history`.
    :rtype: Tuple[Union[DataFrame, None], Union[None, Literal["append", "overwrite"]]]
    """

    if not table_utils.UnityCatalogCheckTableExists(DbxTablePath=dest_output_table_history):
        LOGGER.info(f"The data table located in {dest_output_table_history} does not exist yet")
    else:
        df_output_history = spark.read.table(dest_output_table_history)

        if extraction_date_col_name_output_df not in df_output_history.columns:
            raise TypeError(f"Dataframe located in {dest_output_table_history} does not have the following column {extraction_date_col_name_input_df}")
  
    if extraction_date_col_name_input_df not in df_input_history.columns:
        raise TypeError(f"Dataframe located in {dest_output_table_history} does not have the following column {extraction_date_col_name_input_df}")

    max_extraction_date_input_silver = df_input_history.select(
        f.max(extraction_date_col_name_input_df)
    ).first()[0]

    # Check if the history table exists and attribute a value nor None
    max_extraction_date_df_output_history = (
        df_output_history.select(f.max(extraction_date_col_name_output_df)).first()[0]
        if table_utils.UnityCatalogCheckTableExists(DbxTablePath=dest_output_table_history)
        else None
    )

    LOGGER.info(
        f"The extraction date of the latest input table is {max_extraction_date_input_silver}"
    )
    LOGGER.info(
        f"The extraction date of the output history table is {max_extraction_date_df_output_history}"
    )

    if max_extraction_date_df_output_history is not None and not force_db_reinitialization:
        if max_extraction_date_input_silver > max_extraction_date_df_output_history:
            # Filter input history data asset
            df_input_history_filtered = df_input_history.filter(
                f.col(extraction_date_col_name_input_df) > max_extraction_date_df_output_history
            )
            MODE="append"

        else:
            # In this scenario, you have to quit the notebook without any errors
            LOGGER.info(f"the table {dest_output_table_history} is already up to date")
            MODE = None
            df_input_history_filtered = None

    else:
        # We get all the data from the silver -> no filters
        df_input_history_filtered = df_input_history.select("*")
        MODE="overwrite"

    LOGGER.info(f"Saving table 'Mode' is equal to {MODE}")

    return df_input_history_filtered, MODE
