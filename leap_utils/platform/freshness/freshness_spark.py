from typing import Optional

from delta.exceptions import ConcurrentAppendException
from pyspark.sql import DataFrame
from pyspark.sql import functions as f
from pyspark.sql.types import StructType, StructField, StringType, TimestampType

from leap_utils.common import logger
from leap_utils.config import spark
from leap_utils.platform.freshness import freshness_utils
from leap_utils.common.merge_into import merge_into_spark
from leap_utils.common import retry


LOGGER = logger.get_logger(module_name=__name__)


def _extract_history_df(table_name: str) -> DataFrame:
    """
    Function for extracting the most recent row from delta history with at least one
    operation on a row.

    :param table_name: Unity catalog table name
    :type table_name: str

    :return: A dataframe with the most recent row of the delta history
    :rtype: DataFrame
    """
    df_history = spark.sql(
        freshness_utils.generate_table_history_query(table_name=table_name)
    )
    return df_history


def _transform_history_df_for_freshness_pilot_table(df_history: DataFrame) -> DataFrame:
    """
    Function for filtering and transforming the delta history dataframe

    :param df_history: A dataframe with the most recent row of the delta history
    :type df_history: DataFrame
    :return: The dataframe transformed
    :rtype: DataFrame
    """
    df_history_copy = df_history.withColumn("job_name", f.col("job")["jobName"])
    df_history_copy = df_history_copy.withColumn(
        "schema_name", f.split(f.col("table_name"), r"\.").getItem(1)
    )
    df_history_copy = df_history_copy.select(
        f.col("table_name"),
        f.col("timestamp").alias("last_update_time"),
        f.col("schema_name"),
        f.col("job_name"),
    )

    return df_history_copy


def _is_a_view(table_name: str) -> bool:
    """
    Check if the given table_name is a view.

    :param table_name: Fully qualified table name
    :type table_name: str
    :return: False if it is a table, True if it is a view
    :rtype: bool
    """
    try:
        table_info = spark.sql(f"DESCRIBE EXTENDED {table_name}").collect()
        for row in table_info:
            if row.col_name.lower() == "type" and row.data_type.lower() == "view":
                return True
        return False
    except Exception as e:
        print(f"Failed to check if {table_name} is a table or view: {e}")
        raise


def _is_a_materialized_view(table_name: str) -> bool:
    """
    Check if the given table_name is a view.

    :param table_name: Fully qualified table name
    :type table_name: str
    :return: False if it is a table, True if it is a view
    :rtype: bool
    """
    try:
        table_info = spark.sql(f"DESCRIBE EXTENDED {table_name}").collect()
        for row in table_info:
            if (
                row.col_name.lower() == "type"
                and row.data_type.lower() == "materialized_view"
            ):
                return True
        return False
    except Exception as e:
        print(f"Failed to check if {table_name} is a table or materialized_view: {e}")
        raise


def update_freshness_pilot_table(
    table_name: str,
    pipeline_write_env: Optional[str] = None,
) -> None:
    """
    Function for updating the freshness pilot table from a :param table_name:
    following those steps :
    1. Get the delta history from the table
    2. Transform slighltly the dataframe
    3. Add frequency information if available
    4. Update freshness pilot table with retry in case of concurrency

    :param table_name: Unity catalog table name
    :type table_name: str
    :param pipeline_write_env: defaults set to None
    :type pipeline_write_env: str, optional
    """
    if not pipeline_write_env:
        if table_name.startswith("sd_"):  # sd e.g. sensitive data
            pipeline_write_env = table_name.split("_")[1]
        else:
            pipeline_write_env = table_name.split("_")[0]

    dq_pilot_table = freshness_utils.resolve_pilot_table(
        pipeline_write_env, table_name, freshness_utils.DQ_PILOT_TABLE_PATH
    )
    freshness_pilot_table = freshness_utils.resolve_pilot_table(
        pipeline_write_env, table_name, freshness_utils.FRESHNESS_PILOT_TABLE_PATH
    )

    try:
        LOGGER.info(f"Reading DQ pilot table: {dq_pilot_table}")
        df_dq_pilot = spark.read.table(dq_pilot_table)
    except Exception as e:
        LOGGER.error(f"Failed to read DQ pilot table {dq_pilot_table}: {e}")
        raise

    try:
        if _is_a_view(table_name):
            LOGGER.info(f"Skipping freshness update for view: {table_name}")
            return
    except Exception as e:
        LOGGER.warning(f"Failed to check if {table_name} is a view")
        return

    try:
        if _is_a_materialized_view(table_name):
            # Handle the case where _extract_history_df fails (e.g., on a materialized view)
            LOGGER.warning(
                f"Cannot extract history for {table_name} as it is a materialized_view "
                f"Falling back to using current timestamp."
            )
            # Create a minimal DataFrame with the table name and current timestamp
            schema = StructType(
                [
                    StructField("table_name", StringType(), False),
                    StructField("last_update_time", TimestampType(), True),
                ]
            )
            df_history = spark.createDataFrame(
                [(table_name, None)],
                schema=schema,
            ).withColumn("last_update_time", f.current_timestamp())

            # Add schema_name and job_name columns with default or null values
            df_history = df_history.withColumn(
                "schema_name", f.split(f.col("table_name"), r"\.").getItem(1)
            )
            df_history = df_history.withColumn("job_name", f.lit(None).cast("string"))
        else:
            LOGGER.info(f"Extracting history for table: {table_name}")
            df_history = _extract_history_df(table_name)
            df_history = _transform_history_df_for_freshness_pilot_table(df_history)
    except Exception as e:
        LOGGER.warning(f"Failed to extract history for {table_name}")
        return

    df_history = df_history.join(df_dq_pilot, on="table_name", how="left")

    retry.retry(
        func=merge_into_spark._merge_into_spark_sql,
        exceptions=(ConcurrentAppendException,),
        df_source=df_history,
        target_table=freshness_pilot_table,
        list_id_matching_columns=["table_name"],
    )
    LOGGER.info(f"Freshness pilot table has been updated for table : {table_name}")


def update_freshness_pilot_table_python(
    table_name: str,
    pipeline_write_env: Optional[str] = None,
    job_name: Optional[str] = None,
) -> None:
    """
    Function for updating the freshness pilot table from a :param table_name:
    following those steps :
    1. Get the delta history from the table
    2. Transform slighltly the dataframe
    3. Add frequency information if available
    4. Update freshness pilot table with retry in case of concurrency

    :param table_name: Unity catalog table name
    :type table_name: str
    :param pipeline_write_env: defaults set to None
    :type pipeline_write_env: str, optional
    """
    if not pipeline_write_env:
        if table_name.startswith("sd_"):  # sd e.g. sensitive data
            pipeline_write_env = table_name.split("_")[1]
        else:
            pipeline_write_env = table_name.split("_")[0]

    dq_pilot_table = freshness_utils.resolve_pilot_table(
        pipeline_write_env, table_name, freshness_utils.DQ_PILOT_TABLE_PATH
    )
    freshness_pilot_table = freshness_utils.resolve_pilot_table(
        pipeline_write_env, table_name, freshness_utils.FRESHNESS_PILOT_TABLE_PATH
    )

    try:
        LOGGER.info(f"Reading DQ pilot table: {dq_pilot_table}")
        df_dq_pilot = spark.read.table(dq_pilot_table)
    except Exception as e:
        LOGGER.error(f"Failed to read DQ pilot table {dq_pilot_table}: {e}")
        raise

    try:
        if _is_a_view(table_name):
            LOGGER.info(f"Skipping freshness update for view: {table_name}")
            return
    except Exception as e:
        LOGGER.warning(f"Failed to check if {table_name} is a view")
        return

    try:
        if _is_a_materialized_view(table_name):
            # Handle the case where _extract_history_df fails (e.g., on a materialized view)
            LOGGER.warning(
                f"Cannot extract history for {table_name} as it is a materialized_view "
                f"Falling back to using current timestamp."
            )
            # Create a minimal DataFrame with the table name and current timestamp
            schema = StructType(
                [
                    StructField("table_name", StringType(), False),
                    StructField("last_update_time", TimestampType(), True),
                ]
            )
            df_history = spark.createDataFrame(
                [(table_name, None)],
                schema=schema,
            ).withColumn("last_update_time", f.current_timestamp())

            # Add schema_name and job_name columns with default or null values
            df_history = df_history.withColumn(
                "schema_name", f.split(f.col("table_name"), r"\.").getItem(1)
            )
            df_history = df_history.withColumn(
                "job_name", f.lit(job_name).cast("string")
            )
        else:
            LOGGER.info(f"Extracting history for table: {table_name}")
            df_history = _extract_history_df(table_name)
            df_history = _transform_history_df_for_freshness_pilot_table(df_history)
    except Exception as e:
        LOGGER.warning(f"Failed to extract history for {table_name}")
        return

    df_history = df_history.join(df_dq_pilot, on="table_name", how="left")

    retry.retry(
        func=merge_into_spark._merge_into_python,
        exceptions=(ConcurrentAppendException,),
        df_source=df_history,
        target_table=freshness_pilot_table,
        list_id_matching_columns=["table_name"],
    )
    LOGGER.info(f"Freshness pilot table has been updated for table : {table_name}")
