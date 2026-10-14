"""
Implementation of an SQL-only merge-into
"""

from typing import Optional

import pandas as pd
from databricks.sql.client import Connection

from leap_utils.common import logger
from leap_utils.common.merge_into import merge_into_utils

LOGGER = logger.get_logger(module_name=__name__)


def _get_table_columns(
    conn: Connection,
    table_name: str,
) -> list[str]:
    """
    Get the list of all column names in the table.

    :param conn: Connection to a remote Databricks
    :type conn: Connection
    :param table_name: Fully qualified name of the table (catalog.schema.name)
    :type table_name: str

    :return: list of all column names
    :rtype: list[str]
    """
    with conn.cursor() as cursor:
        cursor.execute(f"SHOW COLUMNS IN {table_name}")
        results = cursor.fetchall_arrow()
    return results.column("col_name").to_pylist()


def _create_temporary_view_from_pandas_dataframe(
    conn: Connection,
    df: pd.DataFrame,
    temp_view_name: str,
) -> None:
    """
    Create (or replace) a temporary view with the specified name from the provided
    DataFrame, using the provided sql warehouse connection.

    :param conn: Connection to a remote SQL warehouse.
    :type conn: Connection
    :param df: DataFrame to save in a temporary table
    :type df: pd.DataFrame
    :param temp_view_name: name of the temporary view
    :type temp_view_name: str
    """
    columns = ", ".join(df.columns)
    rows = list(df.itertuples(index=False))
    values = ",".join(
        [
            "("
            + ",".join((repr(item) if not pd.isnull(item) else "null" for item in row))
            + ")"
            for row in rows
        ]
    )
    with conn.cursor() as cursor:
        cursor.execute(
            f"CREATE OR REPLACE TEMPORARY VIEW {temp_view_name} ({columns}) "
            f"AS VALUES {values}"
        )
    LOGGER.debug(f"Created temporary view {temp_view_name}")


def _merge_into_sql(
    conn: Connection,
    source_dataframe: pd.DataFrame,
    target_table: str,
    list_id_matching_columns: list[str],
    do_when_matched: bool = True,
    matched_clause: Optional[str] = None,
    do_when_notmatched: bool = True,
    do_when_notmatchedbysource: bool = False,
) -> None:
    """
    Merge the source table or view into the target table, using the provided
    Databricks SQL connection

    :param conn: Databricks SQL connection to a remote SQL warehouse
    :type conn: Connection
    :param source_dataframe: The DataFrame to merge into the target table.
    :type source_dataframe: pd.DataFrame
    :param target_table: Table name of the table to MERGE INTO
    :type target_table: str
    :param list_id_matching_columns: list of matching column name between two dataframes
                                     that are merged
    :type list_id_matching_columns: list[str]
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
    """
    source_columns = source_dataframe.columns.to_list()
    target_columns = _get_table_columns(conn=conn, table_name=target_table)

    source_view = "TEMP_SOURCE_VIEW_FOR_MERGE_INTO"
    _create_temporary_view_from_pandas_dataframe(
        conn=conn,
        df=source_dataframe,
        temp_view_name=source_view,
    )

    with conn.cursor() as cursor:
        cursor.execute(
            merge_into_utils.generate_merge_into_statement(
                source_table_or_view=source_view,
                source_columns=source_columns,
                target_columns=target_columns,
                target_table=target_table,
                list_id_matching_columns=list_id_matching_columns,
                do_when_matched=do_when_matched,
                matched_clause=matched_clause,
                do_when_notmatched=do_when_notmatched,
                do_when_notmatchedbysource=do_when_notmatchedbysource,
            )
        )
