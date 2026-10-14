"""
In SAP, amounts have two decimals. However, amounts in some currencies have 3 digits.
In such case, the output table gives the wrong amount as it treats like if all amounts
have 2 digits. For example, an amount of 10,242TND is written 102,42 in the table we
extract from SAP. The number of digits per currency are in the table TCURX in SAP.
The aim of this function is to correct a column with amounts based on its currency
associated column and TCURX.
"""

import pyspark.sql.functions as f
from pyspark.sql import DataFrame

from leap_utils.config import spark


def create_corrected_amounts(
    df: DataFrame, amount_col: str, currency_col: str
) -> DataFrame:
    """
    Create a new column amountXX_corrected which is the column amount_col with the
    correction of SAP mistake

    :param df: Original dataframe
    :type df: DataFrame
    :param amount_col: Name of the amount column in df
    :type amount_col: str
    :param currency_col: Name of the column in df that represents the currency of the
                         column amount_col
    :type currency_col: str

    :return: df_new
    :type: DataFrame 
    """
    df_tcurx = spark.read.table("prod_bronze.sap_latecoere_ecc6.tcurx_latest")

    # Technical debt : select just the table columns for not having ingestion metadata
    df_tcurx = df_tcurx.select(["currkey", "currdec"])

    df_tcurx = df_tcurx.withColumn("Exposant", 2 - f.col("currdec"))
    df_tcurx = df_tcurx.withColumn("Coefficient", (10 ** f.col("Exposant")))

    df_join = df.join(df_tcurx, df[currency_col] == df_tcurx.currkey, how="left")

    df_join = df_join.withColumn(
        "Coefficient",
        f.when(f.col("Coefficient").isNull(), f.lit(1)).otherwise(f.col("Coefficient")),
    )

    df_new = df_join.withColumn(
        f"{amount_col}_corrected", f.col("Coefficient") * f.col(f"{amount_col}")
    ).drop("currkey", "currdec", "Coefficient", "Exposant")

    return df_new
