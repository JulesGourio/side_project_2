"""
This module contains functions to update the freshness table, using SQL only
"""

from datetime import datetime
from typing import Optional

import pandas as pd
from databricks.sql.client import Connection
from databricks.sql.exc import ServerOperationError
from delta.exceptions import ConcurrentAppendException

from leap_utils.common import logger
from leap_utils.platform.freshness import freshness_utils
from leap_utils.common.merge_into import merge_into_sql
from leap_utils.common import retry

LOGGER = logger.get_logger(module_name=__name__)

def _extract_history_df(conn: Connection, table_name: str) -> pd.DataFrame:
    """
    Function for extracting the most recent row from delta history with at least one
    operation on a row.

    :param conn: Connection to a Databricks warehouse
    :type conn: Connection
    :param table_name: Unity catalog table name
    :type table_name: str

    :return: A dataframe with the most recent row of the delta history
    :rtype: pandas.DataFrame
    """
    with conn.cursor() as cursor:
        cursor.execute(freshness_utils.generate_table_history_query(table_name=table_name))
        return cursor.fetchall_arrow().to_pandas()


def _transform_history_df_for_freshness_pilot_table(
    df_history: pd.DataFrame,
) -> pd.DataFrame:
    """
    Function for filtering and transforming the delta history dataframe

    :param df_history: A dataframe with the most recent row of the delta history
    :type df_history: pandas.DataFrame

    :return: a new DataFrame, with the following columns : "table_name",
        "last_update_time", "schema_name", "job_name"
    :rtype: pandas.DataFrame
    """
    df = df_history.copy()
    df["job_name"] = df["job"].str.get("jobName")

    df["schema_name"] = df["table_name"].str.split(".").str.get(1)

    df["timestamp"] = df["timestamp"].dt.tz_localize(None)

    return df[["table_name", "timestamp", "schema_name", "job_name"]].rename(
        columns={"timestamp": "last_update_time"}
    )


def _is_a_view(conn: Connection, table_name: str) -> bool:
    """
    Check if the given table_name is a view.

    :param conn: Connection to a Databricks warehouse
    :type conn: Connection
    :param table_name: Fully qualified table name
    :type table_name: str

    :return: False if it is a table, True if it is a view
    :rtype: bool
    """
    try:
        with conn.cursor() as cursor:
            cursor.execute(f"DESCRIBE EXTENDED {table_name}")
            table_info = cursor.fetchall_arrow().to_pandas()
        for _, row in table_info.iterrows():
            if row["col_name"].lower() == "type" and row["data_type"].lower() == "view":
                return True
        return False
    except Exception as e:
        print(f"Failed to check if {table_name} is a table or view: {e}")
        raise


def _is_a_materialized_view(conn: Connection, table_name: str) -> bool:
    """
    Check if the given table_name is a view.

    :param conn: Connection to a Databricks warehouse
    :type conn: Connection
    :param table_name: Fully qualified table name
    :type table_name: str

    :return: False if it is a table, True if it is a view
    :rtype: bool
    """
    try:
        with conn.cursor() as cursor:
            cursor.execute(f"DESCRIBE EXTENDED {table_name}")
            table_info = cursor.fetchall_arrow().to_pandas()
        for _, row in table_info.iterrows():
            if (
                    row["col_name"].lower() == "type"
                    and row["data_type"].lower() == "materialized_view"
            ):
                return True
        return False
    except Exception as e:
        print(f"Failed to check if {table_name} is a table or materialized_view: {e}")
        raise


def update_freshness_pilot_table(
    conn: Connection,
    table_name: str,
    pipeline_write_env: Optional[str] = None,
) -> None:
    """
    Function for updating the freshness pilot table from a :param table_name:
    following those steps :
    1. Get the delta history from the table
    2. Transform slightly the dataframe
    3. Add frequency information if available
    4. Update freshness pilot table with retry in case of concurrency

    :param conn: Connection to a Databricks warehouse
    :type conn: Connection
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
        with conn.cursor() as cursor:
            cursor.execute(f"SELECT * FROM {dq_pilot_table}")
            df_dq_pilot = cursor.fetchall_arrow().to_pandas()
    except Exception as e:
        LOGGER.error(f"Failed to read DQ pilot table {dq_pilot_table}: {e}")
        raise

    try:
        if _is_a_view(conn=conn, table_name=table_name):
            LOGGER.info(f"Skipping freshness update for view: {table_name}")
            return
    except Exception as e:
        LOGGER.warning(f"Failed to check if {table_name} is a view")
        return

    try:
        if _is_a_materialized_view(conn=conn, table_name=table_name):
            # Handle the case where _extract_history_df fails (e.g., on a materialized view)
            LOGGER.warning(
                f"Cannot extract history for {table_name} as it is a materialized_view "
                f"Falling back to using current timestamp."
            )
            df_history = pd.DataFrame(
                [[table_name, datetime.now(), table_name.split(".")[1], ""]],
                columns=["table_name", "last_update_time", "schema_name", "job_name"]
            )
        else:
            LOGGER.info(f"Extracting history for table: {table_name}")
            df_history = _extract_history_df(conn=conn, table_name=table_name)
            df_history = _transform_history_df_for_freshness_pilot_table(df_history)
    except Exception as e:
        LOGGER.warning(f"Failed to extract history for {table_name}")
        return

    df_history = df_history.join(
        other=df_dq_pilot.set_index("table_name"),
        on="table_name",
        how="left",
    )

    retry.retry(
        func=merge_into_sql._merge_into_sql,
        exceptions=(ConcurrentAppendException, ServerOperationError),
        conn=conn,
        source_dataframe=df_history,
        target_table=freshness_pilot_table,
        list_id_matching_columns=["table_name"],
    )
    LOGGER.info(f"Freshness pilot table has been updated for table : {table_name}")
