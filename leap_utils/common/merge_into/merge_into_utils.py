from leap_utils.common import logger

LOGGER = logger.get_logger(__name__)


def _validate_merge_into_columns(
    source_cols: list[str], target_cols: list[str], list_id_matching_columns: list[str]
) -> None:
    """
    Validate source columns and target columns match the specified
    list_id_matching_columns

    :param source_cols: Columns of the source dataframe
    :type source_cols: list[str]
    :param target_cols: Columns of the target dataframe
    :type target_cols: list[str]
    :param list_id_matching_columns: Columns on which to merge
    :type list_id_matching_columns: list[str]
    :raises ValueError: In case source and target are not the same
    :raises ValueError: In case at least one column of list_id_matching_columns is not
                        present in target table
    """
    if set(source_cols) != set(target_cols):
        error_message = (
            "The source table and the target table don't have the same columns, "
            f"source_cols : {set(source_cols)}, target_cols: {set(target_cols)}"
        )
        LOGGER.error(error_message)
        raise ValueError(error_message)

    # Check IDs are in target
    if not set(list_id_matching_columns).issubset(target_cols):
        id_not_in_target = set(list_id_matching_columns).difference(set(target_cols))
        error_message = (
            "At least one column used as key of the merge into is not present in "
            f"the target table : {id_not_in_target}"
        )
        LOGGER.error(error_message)
        raise ValueError(error_message)


def _create_on_statement(id_matching_columns: list[str]) -> str:
    """
    Create "ON" statement for MERGE INTO sql command.

    :param id_matching_columns: list of matching column name
                                between two dataframe that
                                are merged.
    :type id_matching_columns: list[str]
    :return: Part of MERGE INTO sql statement, part of
            column reference to merge.
    :rtype: str
    """
    return " AND ".join(
        [
            f"((source.{x} IS NULL AND target.{x} IS NULL) OR (source.{x} = target.{x}))"  # noqa: E501
            for x in id_matching_columns
        ]
    )


def generate_merge_into_statement(
    source_table_or_view: str,
    source_columns: list[str],
    target_columns: list[str],
    target_table: str,
    list_id_matching_columns: list[str],
    do_when_matched: bool = True,
    matched_clause: str | None = None,
    do_when_notmatched: bool = True,
    do_when_notmatchedbysource: bool = False,
) -> str:
    """
    Generate an SQL statement to merge a DataFrame into a table based on a list of
    columns. It is a SCD1 MERGE INTO ( Slowly Changing Dimension Type1 )

    :param source_table_or_view: Full name (catalog.schema.name) of the table or view
                                 to merge into the target.
    :type source_table_or_view: str
    :param source_columns: List of columns in the source table/view.
    :type source_columns: list[str]
    :param target_columns: List of columns in the target table/view.
    :type target_columns: list[str]
    :param target_table: Table name of the table to MERGE INTO
    :type target_table: str
    :param list_id_matching_columns: list of matching column name between two dataframes
                                     that are merged
    :type list_id_matching_columns: list[str]
    :param matched_clause: Optional statement added to the "WHEN MATCHED".
                           Must be on the form "source.COLXX = target.COLXX" or
                           "source.COLXX <> target.COLXX", COLXX not being part of
                           list_id_matching_columns.
    :type matched_clause: str | None
    :param do_when_matched: Optional statement to do UPDATE when MATCHED
    :type do_when_matched: bool. Default True
    :param do_when_notmatched: Optional statement to do INSERT when NOT MATCHED
    :type do_when_notmatched: bool. Default True
    :param do_when_notmatchedbysource: Optional statement to do DELETE when
                                       NOT MATCHED BY SOURCE
    :type do_when_notmatchedbysource: bool. Default False
    :return: The generated SQL statement, ready to be executed
    :rtype: str
    """
    if not (matched_clause or do_when_notmatched or do_when_notmatchedbysource):
        raise ValueError(
            "MERGE INTO must have at least one MERGE clause among 'WHEN MATCHED', "
            "'WHEN NOT MATCHED', 'WHEN NOT MATCHED BY SOURCE' "
        )

    _validate_merge_into_columns(
        source_columns, target_columns, list_id_matching_columns
    )

    match_clause_str, notmatch_clause_str, notmatchbysource_clause_str = "", "", ""

    if do_when_matched:
        if matched_clause:
            match_clause_str = "AND " + matched_clause
        match_clause_str = f"WHEN MATCHED {match_clause_str} THEN UPDATE SET *"

    if do_when_notmatched:
        notmatch_clause_str = "WHEN NOT MATCHED THEN INSERT *"

    if do_when_notmatchedbysource:
        notmatchbysource_clause_str = "WHEN NOT MATCHED BY SOURCE THEN DELETE"

    return f"""
    MERGE INTO {target_table} AS target
    USING {source_table_or_view} AS source
    ON {_create_on_statement(list_id_matching_columns)}
    {match_clause_str}
    {notmatch_clause_str}
    {notmatchbysource_clause_str}
    """
