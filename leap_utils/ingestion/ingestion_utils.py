from typing import List, Tuple, Dict, Any

from databricks.sdk.runtime import dbutils
from pyspark.sql import DataFrame
from pyspark.sql import functions as f

from leap_utils.config import spark
from leap_utils.data_asset import table_utils

DEFAULT_METADATA = [
    {
        "selection_expression": "_metadata.file_name",
        "alias_name":"filename",
        "schema_type":"string"
    },
    {
        "selection_expression": "_metadata.file_path",
        "alias_name":"filepath",
        "schema_type":"string"
    },
    {
        "selection_expression": "_metadata.file_modification_time",
        "alias_name":"modification_timestamp",
        "schema_type":"string"
    },
    {
        "selection_expression": "now()",
        "alias_name":"ingestion_timestamp",
        "schema_type":"string"
    },
]


def copy_into(
    output_stack_database: str,
    output_stack_table_name: str,
    columns_selection: List[str],
    s3_path: str,
    file_format: str = "CSV",
    remove_stack: bool = False,
    empty_stack: bool = False,
    merge_schema: bool = False,
    validate_all: bool = False,
    use_column_mapping: bool = False,
    debug: bool = False,
    metadata: List[Tuple[str, str]] = DEFAULT_METADATA,
    format_options: dict[str, str] = {}
) -> DataFrame:
    """
    The COPY INTO Databricks SQL statement wrapped into python code.
    Loads data from a file location into a Delta table.
    For details on any parameter, please refer to the official documentation of the
    "COPY INTO" statement at
    https://docs.databricks.com/en/sql/language-manual/delta-copy-into.html

    :param output_stack_database: The database of the target table to copy into
                                  databricks.
    :param output_stack_table_name: The table name of the target table to copy into
                                    databricks.
    :param columns_selection: The list of columns to select from the copied source
                              files.
    :param s3_path: The S3 path of the source files to copy.
    :param file_format: The format of the source files to copy.
                        Currently defaults to "CSV" as it is the only format supported
                        by this function.
    :param remove_stack: Flag to determine whether to remove the target table completely
                         (drop) before copying into the table. This can be useful when
                         you need to change the schema of the target table.
    :param empty_stack: Flag to determine whether to empty the target table before
                        copying into the table. This can be useful if you want to keep
                        the current target table schema but want to remove all the data.
    :param merge_schema: Flag to determine whether to allow the target table schema to
                         be merged in case source data has a different schema.
                         This can be useful if you need to change the schema of the
                         table but want to keep the history and the new schema is
                         compatible with the old schema in terms of schema merging.
    :param validate_all: Flag to determine whether to use the "VALIDATE ALL" statement,
                         to validate the COPY INTO command and not write any data to
                         the target table.

    :param use_column_mapping: Flat to determine whether to use delta column mapping.
                               This can be useful when source files have odd header
                               column names (e.g., forbidden characters).
    :param format_options: Options to be passed to the Apache Spark
                               data source reader for the specified format.,
                               defaults to {}
    :param debug: Flag to specify whether debug information should be printed.
                  Defaults to False.
    :return: A spark dataframe with the result information of the "COPY INTO" statement
             that was run.
    """

    if file_format != "CSV":
        raise Exception("Only CSV is supported for now")

    stack_schema = generate_stack_schema(metadata=metadata, columns_selection=columns_selection)
    stack_selection = generate_stack_selection(metadata=metadata, columns_selection=columns_selection)
    
    # Create the chosen database if not exists
    spark.sql(f"CREATE DATABASE IF NOT EXISTS {output_stack_database}")

    # If we should remove the stack, drop the table
    if remove_stack:
        spark.sql(
            f"DROP TABLE IF EXISTS {output_stack_database}.{output_stack_table_name}"
        )

    # Create empty stack table and add Delta "Column Mapping" for odd column names
    create_empty_stack_table_statement = f"""
CREATE TABLE IF NOT EXISTS
{output_stack_database}.{output_stack_table_name} ({stack_schema})
{"TBLPROPERTIES ('delta.columnMapping.mode' = 'name')" if use_column_mapping else ""}
        """

    if debug:
        print(
            f"{create_empty_stack_table_statement=}".split("=")[0] + "=",
            create_empty_stack_table_statement,
            "\n",
        )

    spark.sql(create_empty_stack_table_statement)

    # If we should empty the stack, remove all rows from the table
    if empty_stack:
        spark.sql(f"DELETE FROM {output_stack_database}.{output_stack_table_name}")

    # Use force = true in copy into if stack was removed or emptied
    copy_into_force_copy = remove_stack or empty_stack

    # COPY INTO stack
    copy_into_query = generate_copy_into_sql_statement(
        output_stack_database=output_stack_database,
        output_stack_table_name=output_stack_table_name,
        stack_selection=stack_selection,
        s3_path=s3_path,
        file_format=file_format,
        validate_all=validate_all,
        copy_into_force_copy=copy_into_force_copy,
        merge_schema=merge_schema,
        format_options=format_options
    )

    if debug:
        print(f"{copy_into_query=}".split("=")[0] + "=", copy_into_query, "\n")

    return spark.sql(copy_into_query)

def generate_copy_into_sql_statement(
    output_stack_database: str,
    output_stack_table_name: str,
    stack_selection: List[str],
    s3_path: str,
    file_format: str,
    validate_all: bool,
    copy_into_force_copy: bool,
    merge_schema: bool,
    format_options: dict[str, str]
):
    """
    generate_copy_into_sql_statement generates databricks COPY INTO sql statement.

    :param output_stack_database: The database of the target table to copy into databricks.
    :type output_stack_database: str
    :param output_stack_table_name: The table name of the target table to copy into databricks.
    :type output_stack_table_name: str
    :param stack_selection:  list containing
        metadata and columns with their types, according to
        SQL syntax.
    :type stack_selection: List[str]
    :param s3_path: The S3 path of the source files to copy.
    :type s3_path: str
    :param file_format: The format of the source files to copy.
        Currently defaults to "CSV" as it is the only format supported by this function.
    :type file_format: str
    :param validate_all: Flag to determine whether to use the "VALIDATE ALL" statement,
        to validate the COPY INTO command and not write any data to the target table.
    :type validate_all: bool
    :param format_options: Options to be passed to the Apache Spark
                               data source reader for the specified format.,
                               defaults to {}
    :type format_options: Dict[str, str], optional
    :param copy_into_force_copy:
    :type copy_into_force_copy: bool
    :param merge_schema: Flag to determine whether to allow the target table schema to be merged
        in case source data has a different schema.
    :type merge_schema: bool
    """
    copy_into_query = f"""
COPY INTO {output_stack_database}.{output_stack_table_name}
FROM (
    SELECT {stack_selection}
    FROM '{s3_path}'
)
FILEFORMAT = {file_format}
{"VALIDATE ALL" if validate_all else ""}
"""

    if format_options:
        copy_into_query+=f"""
FORMAT_OPTIONS (
    {dictionnary_into_sql_statement(dictionnary=format_options)}
)
"""
    copy_into_query += f"""
COPY_OPTIONS (
"force" = "{str(copy_into_force_copy).lower()}",
"mergeSchema" = "{str(merge_schema).lower()}"
)
        """
    return copy_into_query

def generate_stack_schema(
    metadata: List[dict[str, str]],
    columns_selection: List[str],   
) -> str:
    """
    generate_raw_and_stack_selection generates a list containing
    metadata and columns with their types, according to databricks
    unity catalog syntax.

    :param metadata: List of dictionnary containing metadata info, for each metadata a dictionnary
    contains 'selection_expression', 'alias_name' and 'schema_type'
        Example:
        [{
            'selection_expression': 'current_timestamp()',
            'alias_name': 'ingestion_timestamp',
            'schema_type': 'timestamp'
        }]
    :type metadata: List[dict[str, str]]
    :param columns_selection: The list of columns to select from the copied source files.
    :type columns_selection: List[str]
    :return: List containing stack schema
        Example:
        "filename string, filepath string, ..."
    :rtype: str
    """
    metadata_schema = [f"{dict_info['alias_name']} {dict_info['schema_type']}" for dict_info in metadata]
    rawdata_schema = [
        f"`{table_utils._cleanup_column_name(column_name)}` string"
        for column_name in columns_selection
    ]

    stack_schema = ", ".join(metadata_schema + rawdata_schema)
    return stack_schema


def generate_stack_selection(
    metadata: List[dict[str, str]],
    columns_selection: List[str],
) -> str:
    """
    generate_stack_selection generates a list containing
    metadata and columns with their types, according to
    SQL syntax.
    

    :param metadata: List of dictionnary containing metadata info, for each metadata a dictionnary
    contains 'selection_expression', 'alias_name' and 'schema_type'
    :type metadata: List[dict[str, str]]
        Example:
        [{
            'selection_expression': 'current_timestamp()',
            'alias_name': 'ingestion_timestamp',
            'schema_type': 'timestamp'
        }]
    :param columns_selection: List of columns
    :type columns_selection: List[str]
    :return: List containing metadata and columns with their types.
        Example:
        "_metadata.file_name AS filename, _metadata.file_path AS filepath,"
    :rtype: str
    """
    metadata_selection = [f"{dict_info['selection_expression']} AS {dict_info['alias_name']}" for dict_info in metadata]
    rawdata_selection = [
        f"`{column_name}` AS {table_utils._cleanup_column_name(column_name)}"
        for column_name in columns_selection
    ]
    stack_selection = ", ".join(metadata_selection + rawdata_selection)
    return stack_selection

def create_dbutils_ls_dataframe(s3_path: str) -> DataFrame:
    """
    Create a dbutils.fs.ls dataframe with proper modification timestamp based on field
    `modificationTime`.
    :param s3_path: S3 path of folder to `ls`.
    :return: The list directory command of the provided S3 path in a spark dataframe.
    """
    dbutils_ls_result = dbutils.fs.ls(s3_path)

    dbutils_ls_schema = None
    if not dbutils_ls_result:
        dbutils_ls_schema = (
            "path string, name string, size bigint, modificationTime bigint"
        )

    return (
        spark.createDataFrame(dbutils_ls_result, schema=dbutils_ls_schema)
        .withColumn(
            "modification_timestamp", f.from_unixtime(f.col("modificationTime") / 1000)
        )
        .drop("modificationTime")
    )

def dictionnary_into_sql_statement(dictionnary: Dict[str, str]) -> str:
    """
    Transform dictionnary into sql statement, keys of dictionnary contains
    identifier and values contains corresponding value.

    :param dictionnary: dictionnary with statement
    :type dictionnary: Dict[str, str]
    :return: sql statement
    :rtype: str
    """
    statement = ", \r\n".join(
        [
            f"'{option_identifier}' = '{option_value}'"
            for option_identifier, option_value in dictionnary.items()
        ]
    )
    return statement