"""
This module contains utility functions to perform a merge into, either using
a SQL statement executed by a spark session, or using pyspark's python API
"""

from typing import List, Optional

from delta.tables import DeltaTable
from pyspark.sql import DataFrame

from leap_utils.common import logger
from leap_utils.common.merge_into import merge_into_utils
from leap_utils.config import spark

LOGGER = logger.get_logger(__name__)


def _merge_into_spark_sql(
    df_source: DataFrame,
    target_table: str,
    list_id_matching_columns: List[str],
    do_when_matched: bool = True,
    matched_clause: Optional[str] = None,
    do_when_notmatched: bool = True,
    do_when_notmatchedbysource: bool = False,
) -> DataFrame:
    """
    Generate an SQL statement to merge a DataFrame into a table based on a list of
    columns. It is a SCD1 MERGE INTO ( Slowly Changing Dimension Type1 )

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
    # Create a temporary view of the source dataframe to be used in the SQL command
    source_view = "TEMP_SOURCE_VIEW_FOR_MERGE_INTO"
    df_source.createOrReplaceTempView(source_view)

    return spark.sql(
        merge_into_utils.generate_merge_into_statement(
            source_table_or_view=source_view,
            source_columns=df_source.columns,
            target_columns=spark.read.table(target_table).columns,
            target_table=target_table,
            list_id_matching_columns=list_id_matching_columns,
            do_when_matched=do_when_matched,
            matched_clause=matched_clause,
            do_when_notmatched=do_when_notmatched,
            do_when_notmatchedbysource=do_when_notmatchedbysource,
        )
    )


def _merge_into_python(
    df_source: DataFrame,
    target_table: str,
    list_id_matching_columns: List[str],
    do_when_matched: bool = True,
    matched_clause: Optional[str] = None,
    do_when_notmatched: bool = True,
    do_when_notmatchedbysource: bool = False,
) -> None:
    """
    Merge a DataFrame into a table based on a list of columns, using the Python / spark
    API. It is a SCD1 MERGE INTO ( Slowly Changing Dimension Type1 )

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
    """
    if not (matched_clause or do_when_notmatched or do_when_notmatchedbysource):
        raise ValueError(
            "MERGE INTO must have at least one MERGE clause among 'WHEN MATCHED', "
            "'WHEN NOT MATCHED', 'WHEN NOT MATCHED BY SOURCE' "
        )

    target_cols = spark.read.table(target_table).columns
    source_cols = df_source.columns

    merge_into_utils._validate_merge_into_columns(
        source_cols, target_cols, list_id_matching_columns
    )

    # Build the join condition for the merge
    join_condition = " AND ".join(
        [f"target.{col} = source.{col}" for col in list_id_matching_columns]
    )

    delta_table = DeltaTable.forName(spark, target_table)

    # Execute merge_into with python api
    # Initialize the merge
    merge_builder = delta_table.alias("target").merge(
        df_source.alias("source"), join_condition
    )

    # WHEN MATCHED
    if do_when_matched:
        if matched_clause:
            merge_builder = merge_builder.whenMatchedUpdate(
                condition=matched_clause, set={"*": "*"}
            )
        else:
            merge_builder = merge_builder.whenMatchedUpdateAll()

    # WHEN NOT MATCHED
    if do_when_notmatched:
        merge_builder = merge_builder.whenNotMatchedInsertAll()

    # WHEN NOT MATCHED BY SOURCE
    if do_when_notmatchedbysource:
        merge_builder = merge_builder.whenNotMatchedBySourceDelete()

    # Exécuter le MERGE
    merge_builder.execute()

    LOGGER.info(f"MERGE INTO {target_table} executed successfully.")
    LOGGER.debug(f"Join condition: {join_condition}")
    return None
