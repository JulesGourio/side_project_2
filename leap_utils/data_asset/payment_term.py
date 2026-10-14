import functools
from typing import Iterable, List

from pyspark.sql import Column, DataFrame
from pyspark.sql import functions as f


def calculate_payment_term_due_date(
    df: DataFrame,
    df_payment_term: DataFrame,
    df_payment_term_column: Column,
    base_date_column: Column,
    output_column_name: str,
    additional_payment_term_join_condition: Column = f.lit(True),
) -> DataFrame:
    """
    Calculate the payment term due date according to the SAP calculation using the
    payment term table (T052).

    **Technical debts:**
    - This function cleans the bronze payment term dataframe because we don't have a
        proper data asset:
    - We should use a cleaned gold data asset (payment term master data) if we end
        up creating it one day.

    :param df: The dataframe in which to calculate the payment term due date column.
    :param df_payment_term: The reference payment term dataframe to use to calculate
                            the payment term due date.  Can be the bronze "t052_latest"
                            table or the bronze "t052_stack" table.
    :param df_payment_term_column: The payment term code column in `df`, the input
                                   dataframe.
    :param base_date_column: The date column to use to calculate the payment term due
                             date (for exemple `zfbdt` for the `BSID` table).
    :param output_column_name: The column name to use as output of the payment term due
                               date calculation.
    :param additional_payment_term_join_condition: A pyspark column expression for
                                                   additional join condition between
                                                   the input dataframe and the payment
                                                   term table for specific needs.

    :return: The input dataframe with the added payment term due date calculation in the
             `output_column_name` column.
    """
    df_initial = df

    # Clean-up payment term reference table
    # TODO use a payment term master data gold data asset if done one day
    df_t052 = clean_payment_term_table(df_payment_term)

    # Coalesce ZTAGs, ZSMNs and ZSTGs columns for payment term calculation
    df_t052 = coalesce_payment_term_columns(df_t052)

    # Join payment term table to the current dataframe
    payment_term_column_name = df.select(df_payment_term_column).columns[0]
    df = df.join(
        df_t052,
        on=(
            (df[payment_term_column_name] == df_t052["zterm"])
            & additional_payment_term_join_condition
        ),
        how="left",
    ).drop(df_t052["zterm"])

    # Use zfael and zmona to find the reference base date
    df = add_month_and_take_fixed_date(
        df=df,
        output_column_name="_tmp_reference_base_date",
        base_date_column=base_date_column,
        additional_months_column=df_t052["zmona"],
        fixed_day_column=df_t052["zfael"],
    )

    # Offset reference base date with ztag
    df = df.withColumn(
        "_tmp_base_date", f.date_add(f.col("_tmp_reference_base_date"), df_t052["ztag"])
    )

    # Calculate payment term due date
    df = add_month_and_take_fixed_date(
        df=df,
        output_column_name=output_column_name,
        base_date_column=f.col("_tmp_base_date"),
        additional_months_column=df_t052["zsmn"],
        fixed_day_column=df_t052["zstg"],
    )

    # If the payment term code was null, keep the base date only
    df = df.withColumn(
        output_column_name,
        f.when(df[payment_term_column_name].isNull(), base_date_column).otherwise(
            df[output_column_name]
        ),
    )

    # Keep only initial columns and final payment term due date calculation
    df = df.select(*df_initial.columns, output_column_name)

    return df


def clean_payment_term_table(df_payment_term: DataFrame) -> DataFrame:
    """
    Clean the payment term reference table (T052), to be used in payment term due date
    calculation.

    **This is some technical debt, because we should someday use a proper payment term
    data asset table with clean data.**

    :param df_payment_term: The payment term reference table dataframe to clean-up.
    :return: The cleaned-up payment term table for the payment term due date calculation
    """
    df_t052 = df_payment_term

    # Cleanup payment term table
    cols = [
        "ztagg",
        "zfael",
        "zmona",
        "ztag1",
        "ztag2",
        "ztag3",
        "zstg1",
        "zsmn1",
        "zstg2",
        "zsmn2",
        "zstg3",
        "zsmn3",
    ]

    df_t052 = df_t052.withColumns(
        {col_name: f.col(col_name).cast("int") for col_name in cols}
    )

    df_t052 = df_t052.withColumn("zterm", f.trim(f.col("zterm")))

    return df_t052


def coalesce_payment_term_columns(df_payment_term: DataFrame) -> DataFrame:
    """
    Coalesce the different payment term rules by priority, in the same way these rules
    are used in the SAP's ABAP code for payment term calculation.

    The steps to achieve this are the following:

    - Replace groups of columns with their value if any of the columns are strictly
        positive, else set all of them to null.

    - Then coalesce these columns by priority for the payment term calculation
        (columns "3" overrides columns "2" which overrides columns "1").

    :param df_payment_term: The payment term table that should be coalesced
        (usually T052 which should be cleaned beforehand).
    :return: The coalesced payment term table.
    """
    # Prepare to coalesce by group for payment term calculation
    payment_term_column_name_groups = [
        ("zsmn3", "zstg3", "ztag3"),
        ("zsmn2", "zstg2", "ztag2"),
        ("zsmn1", "zstg1", "ztag1"),
    ]

    payment_term_group_value_expressions = {}
    for column_group in payment_term_column_name_groups:
        # Obtain the values of all columns in the column_group
        # if any column in the list is strictly positive, else set them to None.
        column_group_value_expression = {
            column: f.when(
                any_column_is_strictly_positive(column_group), f.col(column)
            ).otherwise(None)
            for column in column_group
        }

        payment_term_group_value_expressions |= column_group_value_expression

    df_payment_term = df_payment_term.withColumns(payment_term_group_value_expressions)

    # Coalesce ZTAGs, ZSMNs and ZSTGs for payment term calculation
    payment_term_rule_columns = {}
    for payment_term_rule in ["ztag", "zsmn", "zstg"]:
        payment_term_rule_by_priority = [
            f.col(f"{payment_term_rule}{x}") for x in [3, 2, 1]
        ]

        # Coalesce a rule column by priority, or use 0 if all rules are null.
        payment_term_rule_columns[payment_term_rule] = f.coalesce(
            *payment_term_rule_by_priority, f.lit(0)
        )

    df_payment_term = df_payment_term.withColumns(payment_term_rule_columns)

    return df_payment_term


def any_column_is_strictly_positive(column_names: Iterable[str]) -> Column:
    """
    Return the expression to evaluate if any columns in the provided list of column
    names are strictly positive.

    :param column_names: The list of column names to check if any of them is strictly
                         positive.
    :return: The pyspark expression for "any column is strictly positive in the provided
             list".
    """
    return functools.reduce(
        column_or_expression, map(column_is_strictly_positive, column_names)
    )


def column_or_expression(col1: Column, col2: Column) -> Column:
    """
    Return the OR expression of two input columns.

    :param col1: The first column to use in the OR expression.
    :param col2: The second column to use in the OR expression.
    :return: The pyspark OR expression of the two input columns.
    """
    return col1 | col2


def column_is_strictly_positive(column_name: str) -> Column:
    """
    Return the expression for a column to be strictly positive.
    :param column_name: The name of the column to check for strict positivity.
    :return: The expression for the provided column to be strictly positive.
    """
    # noinspection PyTypeChecker
    return f.col(column_name) > 0


def add_month_and_take_fixed_date(
    df: DataFrame,
    output_column_name: str,
    base_date_column: Column,
    additional_months_column: Column,
    fixed_day_column: Column,
) -> DataFrame:
    """
    Add a certain number of months to a given date and then take a fixed day in that
    resulting year-month date.
    For example, `2023-02-10` with 2 more months and with the fixed day of the 15th
    gives the date `2023-04-15`.

    :param df: The input dataframe in which to calculate the new date.
    :param output_column_name: The name of the column in which to calculate the new date
    :param base_date_column: The base date column on which to perform the calculation.
    :param additional_months_column: The column with the number of months to add to the
                                     base date.
    :param fixed_day_column: The column with the fixed day to use for the calculation.
    :return: The dataframe with the new date calculated.
    """
    # Actual payment term year-month before fixed date is used
    payment_term_year_month_date = f.add_months(
        base_date_column, additional_months_column
    )

    payment_term_year = f.year(payment_term_year_month_date)
    payment_term_month = f.month(payment_term_year_month_date)

    # Theoretical payment term due date with fixed day expression
    payment_term_fixed_day_due_date = f.make_date(
        payment_term_year, payment_term_month, fixed_day_column
    )

    # Check that fixed day due date won't overflow actual year-month
    # (if the make_date() is null) noinspection PyTypeChecker
    df = df.withColumn(
        output_column_name,
        # Fixed day overshot the last day of the month, so we use the last day of the
        # month
        f.when(
            (payment_term_fixed_day_due_date.isNull() & (fixed_day_column > f.lit(28))),
            f.last_day(payment_term_year_month_date),
        )
        # No fixed day was used, so we keep the year month-date.
        .when(fixed_day_column < f.lit(1), payment_term_year_month_date)
        # Nominal case, we use the fixed day date
        .otherwise(payment_term_fixed_day_due_date),
    )

    return df


def get_payment_term_due_date_from_existing_columns(
    df: DataFrame,
    base_date_column: Column,
    zbd123t_column_names: List[str],
    output_column_name: str,
):
    """
    Obtain the payment term due date using the already calculated number of days to add
    to a base date from SAP columns.

    The columns containing the number of days to add are usually named `zbd1t`, `zbd2t`
    and `zbd3t`. They are then taken into account from a priority order: `zbd3t`
    overrides `zbd2t`, that overrides `zbd1t`.

    :param df: The dataframe in which to calculate the existing payment term due date.
    :param base_date_column: The date column to use to calculate the payment term due
                             date (for exemple `zfbdt` for the `BSID` table).
    :param zbd123t_column_names: The three zbdxt column names used to calculate the
                                 existing payment term due date. There must be exactly
                                 three column names in the list,
                                 or raise an :py:class:`AssertionError`.
    :param output_column_name: The column name to use as output of the existing payment
                               term due date calculation.

    :return: The input dataframe with the added existing payment term due date
             calculation in the `output_column_name` column.

    :raise AssertionError: If the `zbd123t_column_names` param doesn't contain exactly
                           three column names.
    """
    # Expecting three "zbd" columns, raise an error if a different amount was given
    zbd123t_column_names_length = len(zbd123t_column_names)
    assert zbd123t_column_names_length == 3, (
        f"There should be exactly 3 column names given as argument to the "
        "`zbd123t_column_names` param, "
        f"but {zbd123t_column_names_length} were given ({zbd123t_column_names=})"
    )

    df_initial = df

    # Cleanup zbd123t columns just in case
    df = df.withColumns(
        {f"_tmp_{column}": f.col(column).cast("int") for column in zbd123t_column_names}
    )

    # Prepare zbd123t columns for coalescing if they are equal to zero
    df = df.withColumns(
        {
            f"_tmp_{column}": f.when(
                f.col(f"_tmp_{column}") != f.lit(0), f.col(f"_tmp_{column}")
            ).otherwise(None)
            for column in zbd123t_column_names
        }
    )

    df = df.withColumns(
        {
            # Coalesce zbd123t by priority order to make the "refe" column,
            #  aka the number of days to add to the base date.
            "_tmp_refe": f.coalesce(
                *[f"_tmp_{column}" for column in zbd123t_column_names], f.lit(0)
            ),
            # Add the number of days to add to the base date to get the payment term due
            # date value
            output_column_name: f.date_add(base_date_column, f.col("_tmp_refe")),
        }
    )

    # Keep only initial columns and final payment term due date value
    df = df.select(*df_initial.columns, output_column_name)

    return df
