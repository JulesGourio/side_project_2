
from pyspark.sql import Column, DataFrame
from pyspark.sql import functions as f
from leap_utils.data_asset import table_utils
from leap_utils.common.logger import get_logger, setup_applevel_logger
from leap_utils.config import spark

setup_applevel_logger()
LOGGER = get_logger(__name__)


def add_intercompany_column(
    df: DataFrame,
    company_col: str,
    trading_id_col: str,
    reference_read_env: str = "prod",
) -> DataFrame:
    """
    Define intercompany parameter (intra-co/inter-co/external) based on the trading partner id column
    the initial dataframe and return a new dataframe with the column added.

    intercompany: whether the flow is between two companies of Latecoere or with an external entity. 

    :param df: initial dataframe
    :param reference_read_env: environment for reference table (intercompany config)
    :param trading_id_col: column in the dataframe that contains the trading partner ID from sap
    :return: the initial dataframe with the additionnal column interbranch
    :rtype: DataFrame
    """
    df_interco = spark.read.table(
        f"{reference_read_env}_bronze.manual_input.intercompany_config"
    ).select('trading_partner_sap_id', f.col('company_code').alias('trading_partner_company')).distinct()

    df_interco = df_interco.withColumnsRenamed(
        {col: f"_temp_{col}" for col in df_interco.columns}
    )

    df = table_utils.remove_leading_zeros(df, [trading_id_col])

    df = df.join(
        df_interco,
        df[trading_id_col] == df_interco["_temp_trading_partner_sap_id"],
        how="left",
    )

    df = df.withColumn(
        "intercompany",
        f.when(
            f.col("_temp_trading_partner_company") == df[company_col],
            f.lit("Intra-co"),
        ).when(
            f.col("_temp_trading_partner_company").isNotNull(),
            f.lit("Inter-co"),
        )
        .otherwise(f.lit("External")),
    )
    
    cols_to_keep = [col for col in df.columns if not col.startswith("_temp_")]

    df = df.select(cols_to_keep)

    return df


def add_finance_interbranch(
    df: DataFrame,
    branch_col: Column,
    trading_id_col: Column,
    reference_read_env: str = "prod",
    is_non_sap_internal: bool = True,
) -> DataFrame:
    """
    Define interbranch parameter based on the branch and the trading partner column in
    the initial dataframe and return a new dataframe with the column added.

    interbranch: whether the flow is between two companies of the same branch, between
    two companies of different branches or external

    :param df: initial dataframe
    :param reference_read_env: environment for reference tables (company master data and
                               intercompany config)
    :param branch_col: column in the dataframe that contains the branch information
    :param trading_id_col: column in the dataframe that contains the trading partner ID
                           from sap
    :param is_non_sap_internal: true if flows with non sap companies are considered as
                                internal, false otherwise

    :return: the initial dataframe with the additionnal column interbranch
    :rtype: DataFrame
    """
    df_interco = spark.read.table(
        f"{reference_read_env}_bronze.manual_input.intercompany_config"
    )
    df_interco = df_interco.withColumnsRenamed(
        {col: f"_temp_{col}" for col in df_interco.columns}
    )

    if not is_non_sap_internal:
        df_interco = df_interco.filter(f.col("_temp_IT_platform_SAP") == "SAP")

    df = df.join(
        df_interco,
        trading_id_col == df_interco["_temp_trading_partner_sap_id"],
        how="left",
    )

    df = df.withColumn(
        "interbranch",
        f.when(
            branch_col == f.col("_temp_trading_partner_branch"),
            f.lit("Intra-branch"),
        )
        .when(
            branch_col != f.col("_temp_trading_partner_branch"),
            f.lit("Inter-branch"),
        )
        .otherwise(f.lit("External")),
    )
    cols_to_keep = [col for col in df.columns if not col.startswith("_temp_")]
    df = df.select(cols_to_keep)

    return df


def identify_DES_activities(
    df: DataFrame,
    company_col: Column,
    profit_center_col: Column,
    output_col_name: str,
    reference_read_env: str = "prod",
) -> DataFrame:
    """
    Identify DES flow in a dataframe based on the company and the profit center.
    Business rule: DES is only for french company/plant.

    :param df: initial dataframe
    :param reference_read_env: env for reference tables (company master data and
                               intercompany config)
    :param company_col: column in the dataframe that contains the company code
    :param profit_center_col: column in the dataframe that contains the profit center
    :param output_col_name: name of the output column that is a coalesce between the
                            company and DES

    :return: the initial dataframe with the additionnal column
    """

    df_pc = (
        spark.read.table(f"{reference_read_env}_gold.finance.profit_center_exposed")
        .select("Profit_Center_short", "Branch")
        .withColumnsRenamed(
            {
                "Profit_Center_short": "_temp_profit_center",
                "Branch": "_temp_profit_center_branch",
            }
        )
    )

    df = df.join(df_pc, profit_center_col == df_pc["_temp_profit_center"], how="left")

    df = df.withColumn(
        output_col_name,
        f.when(
            (f.col("_temp_profit_center_branch") == f.lit("IS"))
            & (company_col == f.lit("LATE")),
            f.lit("LFR-DES"),
        )
        .when(profit_center_col.isin(["DD1", "DD3"]), f.lit("LFR-DES"))
        .otherwise(company_col),
    )

    cols_to_keep = [col for col in df.columns if not col.startswith("_temp_")]
    df_with_des = df.select(cols_to_keep)

    return df_with_des


def add_site_region_division(
    df: DataFrame,
    site_col: str,
    domain: str,
    conventional_site_col: str = None,
    region_col: str = "region",
    division_col: str = "division",
    check_null: bool = False,
    reference_read_env: str = "prod"
):
    """
    Add conventional site names, region and division columns to a dataframe.
    Manage 2 error cases from dealing with history and erasing the current site_col:
    Case 1: Union the current data with the history can lead to having a site_col mixing previous and 
    new site names.
    Case 2: Our dataframe has already been updated with the new site names, 
    so we will join the mapping sites table on the specific domain sites (supply, finance, etc.) 
    with the conventional ones.

    :param df: Initial dataframe.
    :param site_col: Column name containing the current site names.
    :param conventional_site_col: Column name for the new conventional site names. Default None, 
        we add the conventional site names directly to site_col without creating a new column.
    :param domain: Can be "supply", "rh", "enc", "manuf", "qn", "qim". If None, we are considering site_col as a trigram:
        (LMXA, LCZ, LAC1, ...). Use "qim" to join on the `site_qim` column of mapping_sites (QIM system site codes).
    :param region_col: Column name for the new region column. Default "region".
    :param division_col: Column name for the new division column. Default "division".
    :param check_null: If True, the function will check if there are null values in conventional_site_col
        and log a warning showing which site does not have its corresponding conventional name. This implies 
        a Spark action decreasing the performances.
    :param reference_read_env: Env for reference tables (mapping_sites table).

    :return: The initial dataframe with the conventional site name column, the region column, and the division column.
    """
    # Overwrite the current the site names of site_col with the corresponding conventional site name
    if conventional_site_col is None:
        conventional_site_col = site_col

    # Check in the data if we have already added new site names, region & division columns (case we work with history)
    # We look on region_col, division and not on conventional_site_col for managing the case where 
    # conventional_site_col is site_col (erasing the current site col with the new one).
    if all([col in df.columns for col in [conventional_site_col, region_col, division_col]]):
        LOGGER.info((
            "The dataframe already has rows with new site names, regions & divisions. "
            "We will only enrich the rows with old site names, no region & division values."
        ))
        df_enriched = df.filter(
            (f.col(region_col).isNotNull()) & (f.col(division_col).isNotNull())
        )
        df_to_enrich = df.where(
            (f.col(region_col).isNull()) | (f.col(division_col).isNull())
        )
        df_to_enrich = add_conventional_site_names(
            df=df_to_enrich,
            site_col=site_col,
            conventional_site_col=conventional_site_col,
            domain=domain,
            check_null=check_null,
            reference_read_env=reference_read_env
        )
        df_to_enrich = add_region_division(
            df=df_to_enrich,
            conventional_site_col=conventional_site_col,
            region_col=region_col,
            division_col=division_col,
            check_null=check_null,
            reference_read_env=reference_read_env
        )
        return df_enriched.unionByName(df_to_enrich, allowMissingColumns=True)

    LOGGER.info("Adding new site names, region & division columns.")
    df = add_conventional_site_names(
        df=df,
        site_col=site_col,
        conventional_site_col=conventional_site_col,
        domain=domain,
        check_null=check_null,
        reference_read_env=reference_read_env
    )
    df = add_region_division(
        df=df,
        conventional_site_col=conventional_site_col,
        region_col=region_col,
        division_col=division_col,
        check_null=check_null,
        reference_read_env=reference_read_env
    )
    return df

def add_conventional_site_names(
    df: DataFrame,
    site_col: str,
    domain: str,
    conventional_site_col: str = None,
    check_null: bool = True,
    reference_read_env: str = "prod"
) -> DataFrame:
    """
    Create a new site name column respecting the conventional site naming.

    :param df: initial dataframe.
    :param site_col: column name containing the current site names.
    :param conventional_site_col: column name for the new conventional site names. Default None, 
     we add the conventional site names directly to site_col without creating a new column.
    :param domain: can be "supply", "rh", "enc", "manuf", "qn", "qim". If None, we are considering site_col as a trigram:
    (LMXA, LCZ, LAC1,....). Use "qim" to join on the `site_qim` column of mapping_sites (QIM system site codes).
    :param check_null: if True, the function will check if there are null values in conventional_site_col
     and log a warning showing which site does not have its corresponding conventional name. This implies 
     a Spark action decreasing the performances.
    :param reference_read_env: env for reference tables (mapping_sites table).

    :return: the initial dataframe with the additionnal conventional site name column.
    """
    df_mapping_sites = spark.read.table(
        f"{reference_read_env}_bronze.manual_input.mapping_sites"
    )
    df = df.withColumn('_temp_base_site', f.col(site_col))

    # Overwrite the current the site names of site_col with the corresponding conventional site name
    if conventional_site_col is None:
        conventional_site_col = site_col
    if domain is None:
        site_col_from_mapping = "site_trigram"
    elif domain == "qim":
        site_col_from_mapping = "site_qim"
    elif domain in ["supply", "rh", "enc", "manuf", "qn"]:
        site_col_from_mapping = f'site_from_{domain}'
    else:
        raise ValueError(
            f"Invalid domain {domain}. "
            f"Please provide a valid domain among ['supply', 'rh', 'enc', 'manuf', 'qn', 'qim'] or None (for trigram)."
        )

    # Drop the conventional site name column from the df if it already exists
    # avoiding potential conflicts or missing values from the history.
    df = df.drop(conventional_site_col)
    df = df.alias("base").join(
        # Temporary names for avoiding conflicts with site_col and conventional_site_col
        df_mapping_sites.select(
            f.col("site").alias("_map_site"), 
            f.col(site_col_from_mapping).alias("_map_key"))
        .alias('map'),
        f.col(f"base._temp_base_site") == f.col(f"map._map_key"),
        how='left'
    )
    df = df.withColumn(conventional_site_col, f.col("_map_site")).drop("_map_site", "_map_key")

    if check_null:
        df_with_null_conventional_sites = (
            df.filter(f.col(conventional_site_col).isNull())
            .select(f.col('_temp_base_site').alias('original_site')).distinct()
        )
        if df_with_null_conventional_sites.count() > 0:
            LOGGER.warning(
                f"Missing mapping for sites {df_with_null_conventional_sites.display()}\n"
                f"Please verify that the current domain parameter {domain} is correctly associated to the same {site_col} domain.\n"
                f"If the domain is None, the current {site_col} column should be a trigram (LMXA, LCZ, LAC1,....)."
            )
    df = df.drop('_temp_base_site')
                       
    return df


def add_region_division(
    df: DataFrame,
    conventional_site_col: str,
    region_col: str = "region",
    division_col: str = "division",
    check_null: bool = True,
    reference_read_env: str = "prod"
) -> DataFrame:
    """
    Add 'region' and 'division' columns based on the conventional site name.

    :param df: initial dataframe.
    :param conventional_site_col: column name containing the conventional site names.
    :param region_col: column name for the new region column.
    :param division_col: column name for the new division column.
    :param check_null: if True, the function will check if there are null values in the region col
     and log a warning showing which conventional site does not have its corresponding region. This implies 
     a Spark action decreasing the performances.
    :param reference_read_env: env for reference tables (mapping_site_division_region table).

    :return: the initial dataframe with the additionnal region and division columns.
    """
    df_mapping_site_division_region = spark.read.table(
        f"{reference_read_env}_bronze.manual_input.mapping_site_division_region"
    )
    # Drop region & division columns from the df if they already exist
    # avoiding potential conflicts or missing values from history.
    df = df.drop(region_col, division_col)
    df = df.alias("base").join(
        df_mapping_site_division_region
        .select(
            # Temporary name for avoiding potential conflict with conventional_site_col
            f.col('site').alias('_map_site'),
            f.col('region').alias(region_col),
            f.col('division').alias(division_col)
        )
        .alias("map"),
        f.col(f'base.{conventional_site_col}') == f.col("map._map_site"),
        how="left"
    ).drop(f.col("_map_site"))

    if check_null:
        df_with_null_regions = (
            df.filter(f.col(region_col).isNull())
            .select(conventional_site_col).distinct()
        )
        if df_with_null_regions.count() > 0:
            LOGGER.warning(
                f"Missing region and division for sites: {df_with_null_regions.display()}\n"
                f"Please verify that the current '{conventional_site_col}' column is correctly associated "
                "to the mapping_site_division_region table 'site' column."
            )
        
    return df
