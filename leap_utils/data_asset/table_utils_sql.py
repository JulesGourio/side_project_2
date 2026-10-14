"""
This module contains utilitary functions to perform operation on tables, using SQL only.
"""

from typing import Optional

import pandas as pd
from databricks.sql.client import Connection

from leap_utils.common.logger import get_logger
from leap_utils.common.merge_into import merge_into_sql
from leap_utils.platform.freshness import freshness_sql

log = get_logger(module_name=__name__)


def merge_into(
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
    Use _merge_into_sql function, and update freshness pilot table once the target
    table has been updated

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
    log.debug(f"Merging data into table {target_table} ...")
    merge_into_sql._merge_into_sql(
        conn=conn,
        source_dataframe=source_dataframe,
        target_table=target_table,
        list_id_matching_columns=list_id_matching_columns,
        do_when_matched=do_when_matched,
        matched_clause=matched_clause,
        do_when_notmatched=do_when_notmatched,
        do_when_notmatchedbysource=do_when_notmatchedbysource
    )

    # Update freshness pilot table
    log.debug(f"Updating freshness for table {target_table} ...")
    freshness_sql.update_freshness_pilot_table(
        conn=conn,
        table_name=target_table
    )
