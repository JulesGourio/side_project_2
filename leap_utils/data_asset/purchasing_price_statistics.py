import pyspark.sql.functions as f
from pyspark.sql import Window


def weighted_average_price_by_period(
    df,
    agg_cols,
    group_cols,
    rel_periods=[0, 1, 2],
    period_col="year"):
    """
    Aggregates Volume and Value from a PySpark DataFrame by specified columns and relative periods of time.
    Then calcultes the weighted average price. Note: If expected period of calculation is month, year column should be
    part of group_cols.

    Args:
        df: The input PySpark DataFrame.
        agg_cols: A list with 1st Volume and 2nd Value columns to aggregate (e.g., ['Volume', 'Value']).
        group_cols: A list of column names to group by.
        rel_periods: A list of relative periods: 0 is current period, 1 is previous period... Defaults to [0, 1, 2].
        period_col: The name of the period column in the DataFrame. It should be integer. Defaults to "year".

    Returns:
        A new PySpark DataFrame with the weighted average prices calculated.
    """
    # Identify prefix for naming from value name
    value_name_splits = agg_cols[1].split("_", 2)
    prefix = value_name_splits[0] + "_" + value_name_splits[1] if len(value_name_splits) > 1 else value_name_splits[0]

    # Initialise empty list
    new_cols = []

    # Create a window specification
    for rel_p in rel_periods:

        window_spec = Window().partitionBy(*group_cols).orderBy(f.col(period_col)).rangeBetween(-rel_p, -rel_p)

        for agg_col in agg_cols:
            df = df.withColumn(f"{agg_col}_p{rel_p}", f.sum(agg_col).over(window_spec))
            new_cols.append(f"{agg_col}_p{rel_p}")

        # Calculate average price: agg_cols[0] must be volume and agg_cols[1] must be value
        df = df.withColumn(
            f"{prefix}_weighted_average_price_p{rel_p}",
            f.col(f"{agg_cols[1]}_p{rel_p}") / f.col(f"{agg_cols[0]}_p{rel_p}")
        )
        new_cols.append(f"{prefix}_weighted_average_price_p{rel_p}")

    # Select columns to return and drop duplicated
    df = df.select(*group_cols, period_col, *new_cols)
    df = df.dropDuplicates()

    return df


def median_price_by_period(
    df,
    group_cols,
    agg_col,
    rel_periods=[0, 1, 2],
    period_col="year"):
    """
    Aggregates Volume and Value from a PySpark DataFrame by specified columns and relative periods of time.
    Then calcultes the median price. Note: If expected period of calculation is month, year column should be
    part of group_cols.

    Args:
        df: The input PySpark DataFrame.
        group_cols: A list of column names to group by.
        agg_col: The name of the column to aggregate.
        rel_periods: A list of relative periods: 0 is current period, 1 is previous period... Defaults to [0, 1, 2].
        period_col: The name of the period column in the DataFrame. It should be integer. Defaults to "year".

    Returns:
        A new PySpark DataFrame with the median prices calculated.
    """
    # Identify prefix for naming from value name
    value_name_splits = agg_col.split("_", 2)
    prefix = value_name_splits[0] + "_" + value_name_splits[1] if len(value_name_splits) > 1 else value_name_splits[0]

    # Initialise empty list
    new_cols = []

    # Create a window specification
    for rel_p in rel_periods:

        window_spec = Window().partitionBy(*group_cols).orderBy(f.col(period_col)).rangeBetween(-rel_p, -rel_p)

        # Calculate median price
        df = df.withColumn(
            f"{prefix}_median_price_p{rel_p}",
            f.percentile_approx(agg_col, 0.5).over(window_spec)
        )
        new_cols.append(f"{prefix}_median_price_p{rel_p}")

    # Select columns to return and drop duplicated
    df = df.select(*group_cols, period_col, *new_cols)
    df = df.dropDuplicates()

    return df


def benchmark_price_by_period(
    df,
    bm_selected="EUR_budget_weighted_average_price",
    bm_order_pref_periods=[1, 2, 3, 0]):
    """
    Generate the benchmark price from initial average price values prioritized. By default, benchmark is calculated from
    weighted average price in EUR budget, from 1 year before or 2 year before if no value, of 3 year before if no value,
    or the average price from current year if still no value.

    Args:
        df: The input PySpark DataFrame.
        bm_selected: The name of the initial statistics on which the benchmark price calculation will be based. \
        It is not directly a column name but corresponds to the root name of columns to use. \
        Default to "EUR_budget_weighted_average_price"
        bm_order_pref_periods: A list of relative periods to use in priority as benchmark value. \
        E.g, if [1, 2], benchmark would be bm_selected variable from previous period (p1) or the period before (p2) \
        Defaults to [1, 2, 3, 0].

    Returns:
        A new PySpark DataFrame with benchmark price and the actual value from which this benchmark price is coming.
    """
    # Identify prefix for naming from value name
    value_name_splits = bm_selected.split("_", 2)
    prefix = value_name_splits[0] + "_" + value_name_splits[1] if len(value_name_splits) > 1 else value_name_splits[0]

    bm_cols = []

    # List columns to use for benchmark price in order of preference
    for rel_period in bm_order_pref_periods:
        bm_cols.append(f"{bm_selected}_p{rel_period}")

    # Create a new column that coalesces columns by preference
    df = df.withColumn(f"{prefix}_benchmark_price", f.coalesce(*bm_cols))

    # Identify the source column for the coalesced value
    bm_source = None
    for c in bm_cols:
        if bm_source is None:
            bm_source = f.when(f.col(f"{prefix}_benchmark_price") == f.col(c), f.lit(c))
        else:
            bm_source = bm_source.when(f.col(f"{prefix}_benchmark_price") == f.col(c), f.lit(c))

    bm_source = bm_source.otherwise(None)
    df = df.withColumn("benchmark_source", bm_source)

    return df


def inflation_calculation_by_year(df, rel_years, price_refs=["weighted_average_price"], year_col="year"):
    """
    Calculates inflation per year from selected benchmark prices and value impact due to inflation.

    Args:
        df: The input PySpark DataFrame.
        rel_years: A list of relative years (e.g., [1, 2]): 1 is previous period, 2 is two periods ago...
        price_refs: The referencial or benchmark prices to use to calculate inflation.
        year_col: The name of the year column in the DataFrame. Defaults to "year".

    Returns:
        A new PySpark DataFrame with the inflation and inflation impact columns calculated.
    """

    # Loop on referencial relative year (rel_years) & benchmark price types (price_refs) to calculate inflation & effect
    for rel_year in rel_years:
        for price_ref in price_refs:
            df = (
                df
                .withColumn(
                    f"inflation_{price_ref}_y{rel_year}",
                    f.col(f"{price_ref}_y0") / f.col(f"{price_ref}_y{rel_year}") - 1
                )
                .withColumn(
                    f"inflation_effect_{price_ref}_y{rel_year}",
                    (f.col(f"{price_ref}_y0") - f.col(f"{price_ref}_y{rel_year}")) * f.col("volume_y0")
                )
                .withColumn(
                    f"effect_type_{price_ref}_y{rel_year}",
                    f.when(
                        f.col(f"inflation_effect_{price_ref}_y{rel_year}") < 0,
                        "deflation impact"
                    ).otherwise("inflation impact")
                )
            )

    return df


def inflation_total_effect(df, rel_years, price_refs=["weighted_average_price"], group_cols=["material_number"]):
    """
    Calculates total inflation effect over years, for selected types of price and relative referencial years.

    Args:
        df: The input PySpark DataFrame.
        rel_years: A list of relative years (e.g., [1, 2]): 1 is previous period, 2 is two periods ago...
        price_refs: The referencial or benchmark prices to use to calculate inflation.
        group_cols: The name of the group by columns for total calculation. Defaults to ["material_number"].

    Returns:
        A new PySpark DataFrame with the inflation total impact.
    """

    # Define window for sum grouped aggregation
    window_spec = Window().partitionBy(*group_cols)

    # Loop on referencial relative year (rel_years) & benchmark price types (price_refs) to calculate inflation & effect
    for rel_year in rel_years:
        for price_ref in price_refs:
            agg_col = f"inflation_effect_{price_ref}_y{rel_year}"
            df = df.withColumn(f"total_effect_{price_ref}_y{rel_year}", f.sum(agg_col).over(window_spec))

    return df


def define_inflation_classes(df, inflation_cols, inflation_classes):
    """
    Adds an inflation class column to a PySpark DataFrame without UDFs.

    Args:
    df: A PySpark DataFrame with inflation percentage columns.
    inflation_cols: Columns names with inflation percentage to class
    inflation_classes: A list of inflation thresholds (e.g., [-0.1, 0, 0.1, 0.2, 0.5]).

    Returns:
    A new PySpark DataFrame with an additional Inflation Class column.
    """

    for inflation_col in inflation_cols:
        # Create a when-otherwise expression for each class
        infl_min = round(inflation_classes[0] * 100)
        class_name = f"01| {infl_min}% to Less"
        expr = f.when(f.col(inflation_col) < inflation_classes[0], class_name)

        for i in range(1, len(inflation_classes)):
            infl_min = round(inflation_classes[i - 1] * 100)
            infl_max = round(inflation_classes[i] * 100)

            class_num = "0" + str(i + 1)
            class_name = f"{class_num[-2:]}| {infl_min}% to {infl_max}%"

            expr = expr.when(f.col(inflation_col) < inflation_classes[i], class_name)

        class_num = "0" + str(len(inflation_classes) + 1)
        class_name = f"{class_num[-2:]}| {infl_max}% to More"
        expr = expr.otherwise(class_name)

        df = df.withColumn(f"class_{inflation_col}", expr)

    return df
