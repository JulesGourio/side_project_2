"""Factorization of global variables and functions for the freshness tools"""


WRITING_OPERATIONS = (
    "WRITE",
    "REPLACE TABLE AS SELECT",
    "COPY INTO",
    "MERGE",
    "CREATE OR REPLACE TABLE AS SELECT",
)

DQ_PILOT_TABLE_PATH = "bronze.data_quality.data_quality_pilot_table"
FRESHNESS_PILOT_TABLE_PATH = "bronze.data_quality.freshness_pilot_table"
FRESHNESS_DASHBOARD_VIEW_PATH = "bronze.data_quality.freshness_checks_table"

# Secondary "datalakes" whose catalogs carry an explicit suffix after the zone
# (e.g. dev_bronze_hr). Use an ALLOW-LIST, not a positional heuristic: the main lake has
# multi-token catalogs that are NOT datalakes (e.g. {env}_bronze_cleaned) and must keep
# resolving to {env}_bronze.data_quality. Add new datalakes here as they appear.
_KNOWN_DATALAKES = ("hr",)


def datalake_suffix(table_name: str) -> str:
    """
    Return the *datalake* suffix carried by a table's catalog, or "" for the main lake.

    Only catalogs ending with a known datalake suffix are matched, so main-lake catalogs
    such as ``{env}_bronze_cleaned`` are correctly treated as the main lake ("").
      - ``dev_bronze.x.y``          -> ""    (main lake)
      - ``dev_bronze_cleaned.x.y``  -> ""    (main lake — NOT a datalake)
      - ``dev_bronze_hr.x.y``       -> "hr"  (HR datalake)
      - ``dev_gold_hr.x.y``         -> "hr"
      - ``sd_dev_bronze_hr.x.y``    -> "hr"

    :param table_name: fully qualified table name (``catalog.schema.table``)
    :return: the datalake suffix (e.g. "hr") or "" if none
    """
    catalog = table_name.split(".")[0]
    for datalake in _KNOWN_DATALAKES:
        if catalog.endswith(f"_{datalake}"):
            return datalake
    return ""


def resolve_pilot_table(pipeline_write_env: str, table_name: str, path_suffix: str) -> str:
    """
    Build a freshness/DQ pilot table path in the **bronze catalog of the same datalake** as
    ``table_name`` — so HR tables track freshness in ``{env}_bronze_hr.data_quality`` instead
    of the main ``{env}_bronze.data_quality`` (not accessible from the HR Datalake workspace).

    Backward compatible: for the main lake the result is identical to ``{env}_{path_suffix}``.

    :param pipeline_write_env: env prefix already resolved by the caller (e.g. "dev")
    :param table_name: the table being written (used to detect the datalake suffix)
    :param path_suffix: a constant like ``"bronze.data_quality.data_quality_pilot_table"``
    :return: e.g. ``dev_bronze.data_quality.data_quality_pilot_table`` (main) or
             ``dev_bronze_hr.data_quality.data_quality_pilot_table`` (HR)
    """
    suffix = datalake_suffix(table_name)
    _, schema_table = path_suffix.split(".", 1)  # drop the "bronze" zone, keep schema.table
    catalog = f"{pipeline_write_env}_bronze" + (f"_{suffix}" if suffix else "")
    return f"{catalog}.{schema_table}"


def generate_table_history_query(table_name: str)-> str:
    """
    Generate a query to retrieve the latest changes on the specified table

    :param table_name: the fully qualified table name
    :type table_name: str

    :return: The query to be executed
    :rtype: str
    """
    return f"""(SELECT '{table_name}' AS table_name, *
            FROM (DESCRIBE HISTORY {table_name} LIMIT 10)
            WHERE operation IN {WRITING_OPERATIONS} AND (
                cast(operationMetrics.numFiles as INT)> 0  OR
                cast(operationMetrics.numTargetFilesAdded as INT)> 0)
            LIMIT 1
            )"""
