from functools import reduce
from typing import List

from pyspark.sql import DataFrame, Window, Column
from pyspark.sql import functions as f

from leap_utils.common import logger
from leap_utils.config import spark
from leap_utils.data_asset import table_utils

logger.setup_applevel_logger()
LOGGER = logger.get_logger(__name__)


def calculate_history_changelog_delta_update(
    df: DataFrame,
    df_existing_history_changelog: DataFrame,
    id_column_list: List,
    sort_column: Column,
    tracked_column_list: list,
    data_extraction_mode: str = "FULL",
    calculate_discontinuities: bool = False,
    allow_duplicate: bool = False,
    duplicate_sorting: List[Column] = None
) -> DataFrame:
    """
    Calculate history changelog table from Dataframe df
        -tracking values changes for an ID represented by id_column_list , for data columns in tracked_column_list, based a timestamp in timestamp_column
        -get all successive values for tracked columns and delete all duplicate consecutive values
        -optionally : find discontinuities (holes) and insert them in the history changelog to fill the gaps

    Args:
        df : The input DataFrame containing new datas to update the history changelog from. Type Spark Dataframe
        df_existing_history_changelog : The input DataFrame containing the previous existing history changelog from. Type Spark Dataframe
        id_column_list : list of columns representing the ID (UniqueKey) of the row. Generally ['ID']. Type List
        sort_column: column name of the timestamp or date column generally 'extraction_timestamp'. Type Column
        tracked_column_list : list of str , representing list of data columns to track data changes. You should exclude metadata columns and the id_column from this list. Type List
        data_extraction_mode: string that indicates if successives extractions are "FULL" or "DELTA". Type str
        calculate_discontinuities: flag to calculate discontinuities, default False. Type bool
        allow_duplicate: Boolean if user wants to allows duplicate in history change log. Generally id_column_list needs to be unique. Type bool
        duplicate_sorting: Additional column to sort duplicates if duplicates. Type str
    Returns:
        DataFrame ordered and filtered
    """

    # Check that columns in id_column_list are columns of Dataframe df
    check_columns_in_dataframe(df, id_column_list, "df", "id_column_list")

    # Check that columns in tracked_column_list are columns of Dataframe df
    check_columns_in_dataframe(df, tracked_column_list, "df", "tracked_column_list")

    # Check sort_column is a column of df
    check_columns_in_dataframe(df, [sort_column], "df")

    # Check that columns in df match those in existing history changelog df_existing_history_changelog
    df_column_list = df.columns
    check_columns_in_dataframe(
        df_existing_history_changelog,
        df_column_list,
        "df_existing_history_changelog",
        "<colonnes du Dataframe df>",
    )

    # First of all Add "Start_Validity" and "End_Validity" columns to df (new_datas)
    df_with_validity_dates = df.withColumns(
        {
            "Start_Validity": f.col(sort_column).cast("date"),
            "End_Validity": f.lit(None).cast("date"),
        },
    ).select(
        *table_utils.get_columns_for_insertion_after_column(
            df,
            search_column_name=sort_column,
            insert_columns=["Start_Validity", "End_Validity"],
        )
    )

    # Append new_datas to history
    df_table_history_appended = df_existing_history_changelog.unionByName(
        df_with_validity_dates, allowMissingColumns=True
    )

    # Find the new discontinuities that appeared with the new datas
    if calculate_discontinuities:
        # Init of all timestamps dataframe based on "df_table_history_appended"
        df_temporary = df_table_history_appended.select(sort_column).dropDuplicates()
        current_timestamp_column = sort_column
        next_timestamp_column = "next_" + sort_column
        df_all_ts = (
            get_all_df_timestamps_with_next_value(
                df_temporary, current_timestamp_column, next_timestamp_column
            )
            .withColumnRenamed(current_timestamp_column, "_ref_timestamp_column")
            .withColumnRenamed(next_timestamp_column, "_next_ref_timestamp_column")
        )

        # With history change log table keep latest f.col(sort_column) per id_column_list
        # Update f.col(sort_column) with value of f.col("_meta_max_extraction_date") to reflect the end of the validity period
        # Then Append new_datas to this history
        window_latest = Window.partitionBy(*id_column_list).orderBy(
            f.col(sort_column).desc()
        )
        df_existing_history_changelog_latest_row_by_id = (
            df_existing_history_changelog.withColumn(
                "_rownum", f.row_number().over(window_latest)
            )
            .filter(f.col("_rownum") == 1)
            .drop("_rownum")
            .withColumn(sort_column, f.col("_meta_max_extraction_date"))
        )
        df_existing_history_changelog_latest_appended = (
            df_existing_history_changelog_latest_row_by_id.unionByName(
                df_with_validity_dates, allowMissingColumns=True
            )
        )

        # With Dataframe df_existing_history_changelog_latest_appended calculate all discontinuities
        df_all_discontinuities = get_all_discontinuities_in_dataframe(
            df_existing_history_changelog_latest_appended,  # df_table_history_appended,
            df_all_ts,
            id_column_list,
            sort_column,
            data_extraction_mode,
        )

        # With all discontinuities remove those already in the history change log table
        # to build "df_discontinuities_new_rows"
        df_temporary_2 = df_existing_history_changelog.select(
            *id_column_list, sort_column
        )

        join_conditions = [
            f.col(f"A.{column}") == f.col(f"B.{column}")
            for column in id_column_list + [sort_column]
        ]
        join_condition = reduce(lambda x, y: x & y, join_conditions)

        df_discontinuities_new_rows = df_all_discontinuities.alias("A").join(
            df_temporary_2.alias("B"), on=join_condition, how="left_anti"
        )
        LOGGER.info(f"Found {df_discontinuities_new_rows.count()} new discontinuities.")

        # Append new discontinuities to datas
        df_with_discontinuites = df_table_history_appended.unionByName(
            df_discontinuities_new_rows, allowMissingColumns=True
        )
    else:
        df_all_discontinuities = None
        df_discontinuities_new_rows = None
        df_with_discontinuites = df_table_history_appended

    # Process the changelog of datas
    df_with_validity_dates = df_with_discontinuites.withColumns(
        {
            "Start_Validity": f.col(sort_column).cast("date"),
            "End_Validity": f.lit(None).cast("date"),
        },
    )

    df_table_history_intermediate = get_all_row_changes_in_dataframe(
        df_with_validity_dates,
        id_column_list,
        tracked_column_list,
        sort_column,
        drop_previous_next_data_columns=True,
        allow_duplicate=allow_duplicate
    )

    df_table_history_with_start_validity_updated = (
        get_all_consecutives_rows_in_dataframe(
            df_table_history_intermediate,
            id_column_list,
            ["Start_Validity"],
            sort_column,
            duplicate_sorting
        )
    )

    # get the first_existing_timestamp_of_extraction
    first_existing_timestamp_of_extraction_in_stack = df_table_history_appended.select(
        f.min(sort_column).alias("MIN")
    ).first()["MIN"]

    # get the last_existing_timestamp_of_extraction
    last_existing_timestamp_of_extraction_in_stack = df_table_history_appended.select(
        f.max(sort_column).alias("MAX")
    ).first()["MAX"]

    # Process the case of the "Start_Validity" for the 1st occurence of each ID
    # When an ID appears for the 1st time, "Start_Validity" is equal to else "1900-01-01" or _meta_min_extraction_date
    df_history_with_origin = df_table_history_with_start_validity_updated.withColumn(
        "Start_Validity",
        f.when(
            f.isnull(f.col("previous_data_Column")),
            f.when(
                f.col("_meta_min_extraction_date")
                > f.to_date(f.lit(first_existing_timestamp_of_extraction_in_stack)),
                f.col("_meta_min_extraction_date").cast("date"),
            ).otherwise(f.lit("1900-01-01").cast("date")),
        ).otherwise(f.col("Start_Validity").cast("date")),
    ).drop("previous_data_Column")

    # Process the case of the "End_Validity" for the last occurence of each ID
    # When an ID appears for the last time, "End_Validity" is equal to else "9999-12-31" or _meta_max_extraction_date + 1day
    df_history_with_infinite = (
        df_history_with_origin.withColumn(
            "End_Validity",
            f.when(
                f.isnull(f.col("next_data_Column")),
                f.when(
                    (
                        f.col("_meta_max_extraction_date")
                        < f.to_date(
                            f.lit(last_existing_timestamp_of_extraction_in_stack)
                        )
                    )
                    & (f.lit(data_extraction_mode) == "FULL"),
                    f.date_add(f.col("_meta_max_extraction_date").cast("date"), 1),
                ).otherwise(f.lit("9999-12-31").cast("date")),
            ).otherwise(f.col("next_data_Column").cast("date")),
        )
        .withColumn(
            "_meta_is_latest",
            f.when(f.isnull(f.col("next_data_Column")), f.lit(True)).otherwise(
                f.lit(False)
            ),
        )
        .withColumn(
            "_meta_is_current",
            f.when(f.col("End_Validity") == f.lit("9999-12-31"), f.lit(True)).otherwise(
                f.lit(False)
            ),
        )
    )

    # Remove rows when "Start_Validity"=="End_Validity" , except when it is the last appearance of an ID
    df_history_final= df_history_with_infinite
    if not allow_duplicate:
        df_history_final = df_history_with_infinite.filter(
            (
                (f.col("Start_Validity") != f.col("End_Validity"))
                | (
                    (f.col("Start_Validity") == f.col("End_Validity"))
                    & (
                        f.col("End_Validity")
                        == f.col("_meta_max_extraction_date").cast("date")
                    )
                )
            )
        )
    return df_history_final.drop("next_data_Column")


def initialize_history_changelog(
    df: DataFrame,
    id_column_list: List,
    sort_column: Column,
    tracked_column_list: list,
    data_extraction_mode: str = "FULL",
    calculate_discontinuities: bool = False,
    allow_duplicate: bool = False,
    duplicate_sorting: List[Column] = None
) -> DataFrame:
    """
    Calculate the entire history changelog table from Dataframe df
        -tracking values changes for an ID represented by id_column_list , for data columns in tracked_column_list, based a timestamp in timestamp_column
        -get all successive values for tracked columns and delete all duplicate consecutive values
        -optionally : find discontinuities (holes) and insert them in the history changelog to fill the gaps

    Args:
        df : The input DataFrame containing datas to build the changelog from. Type Spark Dataframe
        id_column_list : list of columns representing the ID (UniqueKey) of the row. Generally ['ID']. Type List
        sort_column: column name of the timestamp or date column generally 'extraction_timestamp' . Type Column
        tracked_column_list : list of str , representing list of data column names to track data changes from. You should exclude metadata columns and the id_column from this list. Type List
        data_extraction_mode: string that indicates if successives extractions are "FULL" or "DELTA". Type str
        calculate_discontinuities: flag to calculate discontinuities, default False. Type bool
        allow_duplicate: Boolean if user wants to allows duplicate in history change log. Generally id_column_list needs to be unique. Type bool
        duplicate_sorting: Additional column to sort duplicates if duplicates. Type Column

    Returns:
        DataFrame ordered and filtered
    """

    # Check that columns in id_column_list are columns of Dataframe df
    check_columns_in_dataframe(df, id_column_list, "df", "id_column_list")

    # Check that columns in tracked_column_list are columns of Dataframe df
    check_columns_in_dataframe(df, tracked_column_list, "df", "tracked_column_list")

    # Check sort_column is a column of df
    check_columns_in_dataframe(df, [sort_column], "df")

    # Find the discontinuities : an ID that disappear and reappear with time
    if calculate_discontinuities:
        # Init of all timestamps dataframe
        current_timestamp_column = sort_column
        next_timestamp_column = "next_" + sort_column
        df_all_ts = (
            get_all_df_timestamps_with_next_value(
                df, current_timestamp_column, next_timestamp_column
            )
            .withColumnRenamed(current_timestamp_column, "_ref_timestamp_column")
            .withColumnRenamed(next_timestamp_column, "_next_ref_timestamp_column")
        )

        # Find discontinuities
        df_discontinuities = get_all_discontinuities_in_dataframe(
            df,
            df_all_ts,
            id_column_list,
            sort_column,
            data_extraction_mode,
        )
        LOGGER.info(f"Found {df_discontinuities.count()} discontinuities.")

        df_with_discontinuites = df.unionByName(
            df_discontinuities, allowMissingColumns=True
        )
    else:
        df_discontinuities = None
        df_with_discontinuites = df

    # Process the changelog of datas
    df_with_validity_dates = df_with_discontinuites.withColumns(
        {
            "Start_Validity": f.col(sort_column).cast("date"),
            "End_Validity": f.lit(None).cast("date"),
            "_meta_min_extraction_date": f.lit(None),  # .cast("date"),
            "_meta_max_extraction_date": f.lit(None),  # .cast("date"),
            "_meta_is_latest": f.lit(False),
            "_meta_is_current": f.lit(False),
        },
    ).select(
        *table_utils.get_columns_for_insertion_after_column(
            df_with_discontinuites,
            search_column_name=sort_column,
            insert_columns=[
                "Start_Validity",
                "End_Validity",
                "_meta_min_extraction_date",
                "_meta_max_extraction_date",
                "_meta_is_latest",
                "_meta_is_current",
            ],
        )
    )

    df_table_history_intermediate = get_all_row_changes_in_dataframe(
        df_with_validity_dates,
        id_column_list,
        tracked_column_list,
        sort_column,
        drop_previous_next_data_columns=True,
        allow_duplicate=allow_duplicate
    )

    df_table_history_with_start_validity_updated = (
        get_all_consecutives_rows_in_dataframe(
            df_table_history_intermediate,
            id_column_list,
            ["Start_Validity"],
            sort_column,
            duplicate_sorting
        )
    )

    # get the first_existing_timestamp_of_extraction
    first_existing_timestamp_of_extraction_in_stack = df.select(
        f.min(sort_column).alias("MIN")
    ).first()["MIN"]

    # get the last_existing_timestamp_of_extraction
    last_existing_timestamp_of_extraction_in_stack = df.select(
        f.max(sort_column).alias("MAX")
    ).first()["MAX"]

    # Process the case of the "Start_Validity" for the 1st occurence of each ID
    # When an ID appears for the 1st time, "Start_Validity" is equal to else "1900-01-01" or _meta_min_extraction_date
    df_history_with_origin = df_table_history_with_start_validity_updated.withColumn(
        "Start_Validity",
        f.when(
            f.isnull(f.col("previous_data_Column")),
            f.when(
                f.col("_meta_min_extraction_date")
                > f.to_date(f.lit(first_existing_timestamp_of_extraction_in_stack)),
                f.col("_meta_min_extraction_date").cast("date"),
            ).otherwise(f.lit("1900-01-01").cast("date")),
        ).otherwise(f.col("Start_Validity").cast("date")),
    ).drop("previous_data_Column")

    # Process the case of the "End_Validity" for the last occurence of each ID
    # When an ID appears for the last time, "End_Validity" is equal to else "9999-12-31" or _meta_max_extraction_date + 1day
    df_history_with_infinite = (
        df_history_with_origin.withColumn(
            "End_Validity",
            f.when(
                f.isnull(f.col("next_data_Column")),
                f.when(
                    (
                        f.col("_meta_max_extraction_date")
                        < f.to_date(
                            f.lit(last_existing_timestamp_of_extraction_in_stack)
                        )
                    )
                    & (f.lit(data_extraction_mode) == "FULL"),
                    f.date_add(f.col("_meta_max_extraction_date").cast("date"), 1),
                ).otherwise(f.lit("9999-12-31").cast("date")),
            ).otherwise(f.col("next_data_Column").cast("date")),
        )
        .withColumn(
            "_meta_is_latest",
            f.when(f.isnull(f.col("next_data_Column")), f.lit(True)).otherwise(
                f.lit(False)
            ),
        )
        .withColumn(
            "_meta_is_current",
            f.when(f.col("End_Validity") == f.lit("9999-12-31"), f.lit(True)).otherwise(
                f.lit(False)
            ),
        )
    )

    # Remove rows when "Start_Validity"=="End_Validity" , except when it is the last appearance of an ID
    df_history_final = df_history_with_infinite
    if not allow_duplicate:
        df_history_final = df_history_with_infinite.filter(
            (
                (f.col("Start_Validity") != f.col("End_Validity"))
                | (
                    (f.col("Start_Validity") == f.col("End_Validity"))
                    & (
                        f.col("End_Validity")
                        == f.col("_meta_max_extraction_date").cast("date")
                    )
                )
            )
        )

    return df_history_final.drop("next_data_Column")


def get_all_row_changes_in_dataframe(
    df: DataFrame,
    id_column_list: List,
    tracked_column_list: list,
    sort_column: Column,
    allow_duplicate: bool = False,
    drop_previous_next_data_columns: bool = True,
) -> DataFrame:
    """
    Get all row changes by ID, based on "data_column" column which is a concat of all columns in the list "tracked_column_list"
    In other words, get all successive values and delete all duplicate consecutive values for an ID based on a "tracked_column_list"

    Args:
        df : DataFrame to filter from
        id_column_list : list of columns representing the ID (UniqueKey) of the row. Generally ['ID']. Type List
        tracked_column_list : list of str , representing list of data columns excluding metadata and ID. Type List
        sort_column : column name of the sort column generally 'extraction_timestamp' . Type Column
        drop_previous_next_data_columns : wheter to drop or not "previous_data_column" and "next_data_column" columns, default is True. Type bool
                                          if False, output DataFrame is enriched with "previous_data_column" and "next_data_column" columns
        allow_duplicate: Boolean if user wants to allows duplicate in history change log. Generally id_column_list needs to be unique. Type bool

    Returns:
        DataFrame ordered and filtered
    """

    # Check that columns in id_column_list are columns of Dataframe df
    check_columns_in_dataframe(df, id_column_list, "df", "id_column_list")

    # Check that columns in tracked_column_list are columns of Dataframe df
    check_columns_in_dataframe(df, tracked_column_list, "df", "tracked_column_list")

    # Check sort_column is a column of df
    check_columns_in_dataframe(df, [sort_column], "df")

    # Build an unique column "data_column" by concat of all columns in tracked_column_list
    df_global = df.withColumn("data_column", f.concat_ws("_", *tracked_column_list))

    # Select only necessary columns for the compact DataFrame
    if not allow_duplicate:
        df_global = df_global.select(
            *id_column_list,
            sort_column,
            "_meta_min_extraction_date",
            "_meta_max_extraction_date",
            "data_column",
        )        

    # Add a column named "previous_data_column" with previous value of "data_column", for each ID when sorting by "sort_column"
    # Add two columns named "_meta_min_extraction_date" and "_meta_max_extraction_date" for each ID
    #     these columns are initialized from the stack and must be kept in the table for delta update
    #     these columns must be kept in same type as {sort_column}
    # Below when the history_change_log is updated in delta mode
    if (
        "_meta_min_extraction_date" in df_global.columns
        and "_meta_max_extraction_date" in df_global.columns
    ):
        df_global = (
            df_global.withColumn(
                "previous_data_column",
                f.lag("data_column").over(
                    Window.partitionBy(*id_column_list).orderBy(sort_column)
                ),
            )
            .withColumn(
                "_meta_min_extraction_date",
                f.when(
                    f.col("_meta_min_extraction_date").isNull(),
                    f.min(sort_column).over(Window.partitionBy(*id_column_list)),
                    # .cast("date"),
                ).otherwise(
                    f.least(
                        f.col("_meta_min_extraction_date"),
                        f.min(sort_column).over(Window.partitionBy(*id_column_list)),
                        # .cast("date"),
                    )
                ),
            )
            .withColumn(
                "_meta_max_extraction_date",
                f.when(
                    f.col("_meta_max_extraction_date").isNull(),
                    f.max(sort_column).over(Window.partitionBy(*id_column_list)),
                    # .cast("date"),
                ).otherwise(
                    f.greatest(
                        f.col("_meta_max_extraction_date"),
                        f.max(sort_column).over(Window.partitionBy(*id_column_list)),
                        # .cast("date"),
                    )
                ),
            )
        )
    # Below when the history_change_log is initialized
    else:
        df_global = (
            df_global.withColumn(
                "previous_data_column",
                f.lag("data_column").over(
                    Window.partitionBy(*id_column_list).orderBy(sort_column)
                ),
            )
            .withColumn(
                "_meta_min_extraction_date",
                f.min(sort_column).over(Window.partitionBy(*id_column_list)),
                # .cast("date"),
            )
            .withColumn(
                "_meta_max_extraction_date",
                f.max(sort_column).over(Window.partitionBy(*id_column_list)),
                # .cast("date"),
            )
        )

    # Cache the DataFrame to optimize performance.
    # Best-effort: persist() is not supported on serverless compute — there we skip caching
    # (correctness is unaffected, the DataFrame is simply recomputed when needed).
    try:
        df_global = df_global.persist()
    except Exception as e:
        LOGGER.info(f"persist() skipped (likely serverless compute): {e}")

    # Filter to keep lines where "data_column" is different from "previous_data_column" or lines when "previous_data_column" is null
    df_filtered = df_global.filter(
        (f.col("data_column") != f.col("previous_data_column"))
        | f.col("previous_data_column").isNull()
    )

    # Add a column named "next_data_column" with next value of "data_column", for each ID when sorting by "sort_column"
    # /!\ it's mandatory to do this after the filter and not before
    df_filtered_with_next_data_column = df_filtered.withColumn(
        "next_data_column",
        f.lead("data_column").over(
            Window.partitionBy(*id_column_list).orderBy(sort_column)
        ),
    )

    # Drop temporary column "data_column"
    df_filtered_final = df_filtered_with_next_data_column.drop("data_column")

    # Wheter to drop or not the 2 columns "previous_data_column" and "next_data_column" based on boolean parameter "drop_previous_next_data_columns"
    # before returning the Dataframe
    if drop_previous_next_data_columns:
        df_filtered_final = df_filtered_final.drop("previous_data_column").drop(
            "next_data_column"
        )

    df_final = df_filtered_final
    
    # Join the filtered results back with the original DataFrame to restore all columns
    if not allow_duplicate:
        df = df.drop("_meta_min_extraction_date", "_meta_max_extraction_date")
        df_final = df_final.join(df, on=id_column_list + [sort_column], how="left")
    
    return df_final


def get_all_consecutives_rows_in_dataframe(
    df: DataFrame,
    id_column_list: List,
    tracked_column_list: list,
    sort_column: Column,
    duplicate_sorting: Column = None
) -> DataFrame:
    """
    Get all consecutives row values by ID, based on "data_column" column which is a concat of all columns in the list "tracked_column_list"
    DO NOT delete all duplicate consecutive values for an ID based on a "tracked_column_list"

    Args:
        df : DataFrame to process
        id_column_list : list of columns representing the ID (UniqueKey) of the row. Generally ['ID']. Type List
        tracked_column_list : list of str , representing list of data columns excluding metadata and ID. Type List
        sort_column : column name of the sort column generally 'extraction_timestamp' . Type Column
        duplicate_sorting : optional column name to help sort duplicates. Type Column

    Returns:
        DataFrame ordered and enriched with "previous_data_column" and "next_data_column" columns
    """

    # Check that columns in id_column_list are columns of Dataframe df
    check_columns_in_dataframe(df, id_column_list, "df", "id_column_list")

    # Check that columns in tracked_column_list are columns of Dataframe df
    check_columns_in_dataframe(df, tracked_column_list, "df", "tracked_column_list")

    # Check sort_column is a column of df
    check_columns_in_dataframe(df, [sort_column], "df")
    
    # Include duplicate_sorting column in checks if provided
    if duplicate_sorting:
        check_columns_in_dataframe(df, duplicate_sorting, "df", "duplicate_sorting")

    # Build an unique column "data_column" by concat of all columns in tracked_column_list
    df_with_data_column = df.withColumn(
        "data_column", f.concat_ws("_", *tracked_column_list)
    )
    
    # Define the window specification for partitioning and ordering
    order_columns = [sort_column]
    if duplicate_sorting:
        order_columns.extend(duplicate_sorting)

    window_spec = Window.partitionBy(*id_column_list).orderBy(*order_columns)
    
    # Add a column named "previous_data_column" with previous value of "data_column", for each ID when sorting by "sort_column"
    # Add a column named "next_data_column" with next value of "data_column", for each ID when sorting by "sort_column"
    df_with_previous_next = df_with_data_column.withColumn(
        "previous_data_column",
        f.lag("data_column").over(
            window_spec
        ),
    ).withColumn(
        "next_data_column",
        f.lead("data_column").over(
            window_spec
        ),
    )

    # Drop temporary column "data_column"
    df_final = df_with_previous_next.drop("data_column")

    return df_final


def get_all_discontinuities_in_dataframe(
    df: DataFrame,
    df_all_timestamps: DataFrame,
    id_column_list: List,
    timestamp_column: Column,
    data_extraction_mode: str = "FULL",
) -> DataFrame:
    """
    Get all discontinuities in Dataframe for each id designated by List of Columns "id_column_list", based on reference timestamp column designated by "timestamp_column"

    Args:
        df : DataFrame to find discontinuities from
        df_all_timestamps : DataFrame with 2 columns, containing all the timestamps in the table with their next value
                column names attended :   "_ref_timestamp_column" , "_next_ref_timestamp_column"
        id_column_list : list of columns representing the ID (UniqueKey) of the row. Generally ['ID']. Type List
        timestamp_column : column representing the timestamp column generally 'extraction_timestamp' . Type Column

    Returns:
        Dataframe containing the "holes", with for each row, the "id_column_list" and the "timestamp_column" at which the hole begins
        Columns of this Dataframe are :  *id_column_list and timestamp_column
    """

    # Check that columns in id_column_list are columns of Dataframe df
    check_columns_in_dataframe(df, id_column_list, "df", "id_column_list")

    # Check timestamp_column is a column of df
    check_columns_in_dataframe(df, [timestamp_column], "df")

    # Filter out DELTA rows and collect the first row after filtering
    if "file_mode" in df.columns:
        delta_row = df.filter(f.col("file_mode") == "DELTA").first()
        # If there are DELTA rows, return an empty DataFrame as we cannot discover discontinuities across DELTA loads
        if delta_row:
            return df.sparkSession.createDataFrame(
                [], df.select(*id_column_list, timestamp_column).schema
            )
    if data_extraction_mode != "FULL":
        return df.sparkSession.createDataFrame(
            [], df.select(*id_column_list, timestamp_column).schema
        )

    # Get reference timestamp columns in df_all_timestamps
    # Verify df_all_timestamps has only 2 columns and catch them as reference_timestamp_column and reference_next_timestamp_column
    if len(df_all_timestamps.columns) == 2:
        reference_timestamp_column = df_all_timestamps.columns[0]
        reference_next_timestamp_column = df_all_timestamps.columns[1]
    else:
        raise Exception("The df_all_timestamps Dataframe doesn't have 2 columns")

    next_timestamp_column = "next_" + timestamp_column

    # Join df to df_all_timestamps
    df_joined = df.join(
        df_all_timestamps,
        (f.col(timestamp_column) == f.col(reference_timestamp_column)),
        "inner",
    ).drop(reference_timestamp_column)

    windowSpecNextTS = Window.partitionBy(*id_column_list).orderBy(timestamp_column)

    # Add column : "next_" + timestamp_column
    # Add column : "_is_gap_detected"
    df_joined_enriched = df_joined.withColumn(
        next_timestamp_column, f.lead(f.col(timestamp_column)).over(windowSpecNextTS)
    ).withColumn(
        "_is_gap_detected",
        f.col(next_timestamp_column) != f.col(reference_next_timestamp_column),
    )

    # Filter out discontinuities
    discontinuities_df = df_joined_enriched.filter(
        (f.col("_is_gap_detected") == "true")
        & (f.col(timestamp_column) != f.col(next_timestamp_column))
    ).select(
        *id_column_list, f.col(reference_next_timestamp_column).alias(timestamp_column)
    )

    return discontinuities_df


def get_all_df_timestamps_with_next_value(
    df: DataFrame,
    timestamp_column: Column,
    next_timestamp_column: Column,
) -> DataFrame:
    """
    Get all timestamps in Dataframe based on reference timestamp column designated by "timestamp_column" variable
    Return a Dataframe containing the "timestamp_column" and the "next_timestamp_column".

    Args:
        df : DataFrame to filter from
        timestamp_column : column representing the timestamp column generally 'extraction_timestamp' . Type Column
        next_timestamp_column : column representing the next timestamp column generally 'next_extraction_timestamp' . Type Column

    Returns:
        DataFrame with 2 columns:  timestamp_column and next_timestamp_column

    Examples:
        1. "timestamp_column" could be  "extraction_timestamp" and "next_timestamp_column" could be "next_extraction_timestamp", both in timestamp format
        2. "timestamp_column" could be  "extraction_date" and "next_timestamp_column" could be "next_extraction_date", both in date format.
        Other configurations are possible.

    """

    # Check timestamp_column is a column of df
    check_columns_in_dataframe(df, [timestamp_column], "df")

    df_all_ts_values = df.select(timestamp_column).distinct()
    windowTsSpec = Window.orderBy(timestamp_column)
    df_all_ts_values_with_next = df_all_ts_values.withColumn(
        next_timestamp_column, f.lead(timestamp_column).over(windowTsSpec)
    )
    return df_all_ts_values_with_next


def add_version_history_to_dataframe(
    df: DataFrame,
    id_column_list: List,
    sort_column: Column,
    version_history_column: Column,
) -> DataFrame:
    """
    Add a 'version_history_column' based on 'timestamp' column

    Args:
        df : DataFrame to add 'version history' column to
        id_column_list : list of columns representing the ID (UniqueKey) of the row. Generally ['ID']. Type List
        sort_column: column name of the timestamp or date column generally 'extraction_timestamp'. Type Column
        version_history : name of the 'version history' column to add

    Returns:
        DataFrame:  Input DataFrame enriched with new column version_history_column
    """

    w = (
        Window().partitionBy(*id_column_list).orderBy(sort_column)
    )  # OrderBy string "A" so that it maintains the original order
    df_final = df.withColumn(version_history_column, f.row_number().over(w) - 1)

    return df_final


def check_columns_in_dataframe(
    df: DataFrame,
    column_list: List,
    df_name: str = None,
    list_name: str = None,
):
    """
    Raise an exception if columns in column_list are not in dataframe df

    Args:
        df : DataFrame to check
        column_list : list of columns to check. Type List
        df_name: optional parameter to name the Dataframe when raising the exception. Type str
        list_name : optional parameter to name the list of columns when raising the exception. Type str

    Returns:
        None
    """
    missing_columns = [column for column in column_list if column not in df.columns]
    if missing_columns:
        if df_name:
            df_name_str = f"{df_name}"
        else:
            df_name_str = ""

        if list_name:
            list_name_str = f"de la liste {list_name}"
        else:
            list_name_str = ""

        if len(missing_columns) == 1:
            column_str = "La colonne"
            verb_str = "ne correspond pas"
        else:
            column_str = "Les colonnes"
            verb_str = "ne correspondent pas"

        raise Exception(
            f"{column_str} {', '.join(missing_columns)} {list_name_str} {verb_str} au Dataframe {df_name_str}."
        )
