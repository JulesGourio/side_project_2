"""
Ingestion Declarative Pipeline Utilities

This module provides reusable functions for building declarative data ingestion pipelines
(DLT or SDP) that read from files (CSV, PARQUET, XML, EXCEL) or from Delta tables.

Configuration can be loaded from:
- A pilotage table (``get_pilotage_structure``)
- A YAML file deployed with the bundle (``load_pilotage_from_yaml``)
"""

from typing import Dict, List, Any, Optional
import fnmatch
import re
import yaml

from pyspark.sql import DataFrame
from pyspark.sql import functions as f
from pyspark.sql.types import (
    StructType,
    StructField,
    StringType,
    IntegerType,
    LongType,
    DoubleType,
    BooleanType,
    TimestampType,
    TimestampNTZType,
    DateType,
)

from leap_utils.config import spark
from leap_utils.data_asset import table_utils
from leap_utils.common.logger import setup_applevel_logger

# Default logger (can be overridden)
log = setup_applevel_logger()


# =============================================================================
# Constants
# =============================================================================

# Mapping from source database types to Spark/Databricks types
SQL_TO_SPARK_TYPE_MAPPING = {
    "jsonb": "STRING",
    "json": "STRING",
    "clob": "STRING",
    "character varying": "STRING",
    "varchar": "STRING",
    "varchar2": "STRING",
    "nvarchar": "STRING",
    "text": "STRING",
    "ntext": "STRING",
    "char": "STRING",
    "nchar": "STRING",
    "tinyint": "INT",
    "smallint": "INT",
    "int": "INT",
    "integer": "INT",
    "mediumint": "INT",
    "bigint": "BIGINT",
    "long": "BIGINT",
    "decimal": "DOUBLE",
    "numeric": "DOUBLE",
    "real": "DOUBLE",
    "float": "DOUBLE",
    "double": "DOUBLE",
    "double precision": "DOUBLE",
    "number": "DOUBLE",
    "bit": "BOOLEAN",
    "boolean": "BOOLEAN",
    "bool": "BOOLEAN",
    "date": "DATE",
    "datetime": "TIMESTAMP",
    "timestamp": "TIMESTAMP",
    "timestamp without time zone": "TIMESTAMP",
    "timestamp_ntz": "TIMESTAMP",
    "timestamp with time zone": "TIMESTAMP",
    "timestamp_tz": "TIMESTAMP",
    "smalldatetime": "TIMESTAMP",
}


def resolve_spark_type(col_type: str) -> str:
    """Resolve a SQL column type string to a Spark type string.

    Handles parameterized types such as ``decimal(38,10)`` or ``numeric(10,2)``
    by returning them directly as Spark DECIMAL types (preserving precision and scale).
    Falls back to ``SQL_TO_SPARK_TYPE_MAPPING`` for simple types, defaulting to STRING.
    """
    ct = col_type.lower().strip()
    if re.match(r"^(decimal|numeric)\s*\(\s*\d+\s*,\s*\d+\s*\)$", ct):
        return ct.upper()  # e.g. "DECIMAL(38,10)"
    return SQL_TO_SPARK_TYPE_MAPPING.get(ct, "STRING")


# Default metadata columns added to ingested data
DEFAULT_METADATA_COLS = [
    "_meta_id",
    "_meta_extraction_timestamp",
    "_meta_ingestion_timestamp",
    "_meta_file_name",
    "_meta_file_mode",
    "_meta_file_path",
]


# =============================================================================
# Type Conversion Utilities
# =============================================================================


def spark_type_from_str(type_str: str):
    """
    Maps a string representation of a data type to a corresponding PySpark data type.

    :param type_str: A string representing the data type (e.g., "string", "int", "timestamp").
    :type type_str: str
    :return: A PySpark data type corresponding to the input string. Defaults to StringType.
    :rtype: pyspark.sql.types.DataType
    """
    mapping = {
        "string": StringType(),
        "int": IntegerType(),
        "integer": IntegerType(),
        "bigint": LongType(),
        "long": LongType(),
        "double": DoubleType(),
        "float": DoubleType(),
        "boolean": BooleanType(),
        "timestamp": TimestampType(),
        "date": DateType(),
    }
    return mapping.get(type_str.lower(), StringType())


def _validate_datetime_format(fmt: str) -> None:
    """
    Guard a user-supplied datetime format string before interpolating it into a
    Spark SQL expression (``try_to_date`` / ``try_to_timestamp``).

    Formats come from the trusted ``pilotage.yml`` config, but we still reject
    characters that could break out of the single-quoted SQL literal.

    :param fmt: Datetime pattern such as ``"M/d/yyyy"``.
    :raises ValueError: if the format contains quote/escape/terminator characters.
    """
    if any(ch in fmt for ch in ("'", '"', "`", "\\", ";")):
        raise ValueError(
            f"Invalid characters in column_format {fmt!r}: "
            "quotes, backticks, backslashes and semicolons are not allowed."
        )


def get_spark_schema(
    column_types: Dict[str, str], metadatas_cols: List[str]
) -> StructType:
    """
    Builds a Spark StructType schema including metadata columns (first) and user-defined data columns.

    :param column_types: Dictionary mapping column names to their data types as strings.
    :type column_types: Dict[str, str]
    :param metadatas_cols: List of metadata column names to prepend to the schema.
    :type metadatas_cols: List[str]
    :return: Spark StructType schema with metadata fields followed by actual table fields.
    :rtype: StructType
    """
    fields = [
        StructField(meta_col, StringType(), True) for meta_col in metadatas_cols
    ] + [
        StructField(col_name, spark_type_from_str(type_str), True)
        for col_name, type_str in column_types.items()
    ]
    return StructType(fields)


# =============================================================================
# File Utilities
# =============================================================================


def is_any_file(folder_path: str, pattern: str, dbutils) -> bool:
    """
    Check if any file in the specified folder matches the given pattern.

    :param folder_path: The path of the folder to check for files.
    :type folder_path: str
    :param pattern: A glob pattern to match file names (e.g., '*MES-tool-*.parquet').
    :type pattern: str
    :param dbutils: Databricks dbutils object.
    :return: True if at least one matching file is found, False otherwise.
    :rtype: bool
    """
    try:
        file_list = dbutils.fs.ls(folder_path)
        for file in file_list:
            if fnmatch.fnmatch(file.name, pattern):
                log.debug(
                    f"File found in {folder_path} matching {pattern}: {file.name}"
                )
                return True
    except Exception as e:
        log.debug(f"No files found in {folder_path} with pattern {pattern}: {str(e)}")
    return False


# =============================================================================
# Unity Catalog Tag Utilities
# =============================================================================


def normalize_table_tags_for_uc(table_tags: Optional[dict]) -> Dict[str, str]:
    """
    Normalize table-level tags into Unity Catalog table properties.

    Converts a dictionary of tags ``{key: value | None}`` into UC-compatible
    table properties ``{"tag.<key>": "<value>"}``.

    Tags with None values (key-only tags) are ignored and must be applied
    later using ``ALTER TABLE ... SET TAGS ('key')``.

    :param table_tags: Dictionary of table tags where values may be strings or None.
    :type table_tags: dict | None
    :return: Dictionary of Unity Catalog table properties prefixed with "tag.".
    :rtype: dict[str, str]
    """
    if not table_tags:
        return {}
    props = {}
    for k, v in table_tags.items():
        if v is not None:
            props[f"tag.{k}"] = str(v)
    return props


# =============================================================================
# Pilotage Table Functions
# =============================================================================


def get_pilotage_structure(
    pipeline_name: str,
    pilotage_table: str,
    reference_read_env: str,
    pipeline_write_env: str,
) -> Dict[str, Dict[str, Any]]:
    """
    Retrieves the table structure for processing in the pipeline from the pilotage table.

    :param pipeline_name: Name of the pipeline to filter.
    :type pipeline_name: str
    :param pilotage_table: Full name of the pilotage table (catalog.schema.table).
    :type pilotage_table: str
    :param reference_read_env: Environment for reference/input data reads.
    :type reference_read_env: str
    :param pipeline_write_env: Environment for pipeline writes.
    :type pipeline_write_env: str
    :return: A structured dictionary with table configurations.
    :rtype: Dict[str, Dict[str, Any]]

    Example return format:
    {
        "1_tablename": {
            "database_name": "source_db",
            "table_name": "tablename",
            "stream_format": "file",
            "stacked_stream": True,
            "file_format": "CSV",  # or "PARQUET", "XML", or "EXCEL"
            "file_keyword_simplified": "KEYWORD",
            "file_regex": "regex_pattern",
            "bucket_name": "s3-bucket",
            "file_prefix": "apps/source/",
            "csv_delimiter": ";",
            "csv_header": True,
            # XML-specific options (if file_format == "XML"):
            "xml_row_tag": "row",
            "xml_attribute_prefix": "_",
            "xml_value_tag": "_VALUE",
            "xml_exclude_attributes": False,
            # Excel-specific options (if file_format == "EXCEL"):
            "excel_data_address": "'Sheet1'!A1",
            "excel_use_yaml_schema": True,
            ...
        },
    }
    """
    if pilotage_table is None:
        raise ValueError("The pilotage table must be provided.")

    try:
        df_pilotage: DataFrame = spark.read.table(pilotage_table)
    except Exception as e:
        log.error(f"Error reading pilotage table {pilotage_table}: {e}")
        raise

    df_pilotage = df_pilotage.filter(f.col("pipeline_name") == pipeline_name)
    if df_pilotage.isEmpty():
        log.warning(
            f"No records found for pipeline: {pipeline_name}. "
            "Please populate the pilotage table correctly."
        )

    files_attrs_dict: Dict[str, Dict[str, Any]] = {}

    for row in df_pilotage.collect():
        row_dict = row.asDict()

        id_val: str = row["id"]
        table_name: str = row["table_name"]

        # Environment substitution for bucket names
        bucket_name = row["bucket_name"].format(REFERENCE_READ_ENV=reference_read_env)
        archive_bucket_name = row["archive_bucket_name"].format(
            PIPELINE_WRITE_ENV=pipeline_write_env
        )

        # Extract column details
        columns = row["columns"]
        file_expected_cols = [
            col["column_name"] for col in columns if col["is_expected"]
        ]
        primary_key_cols = [
            col["column_name"] for col in columns if col["is_primary_key"]
        ]
        notnull_cols = [
            col["column_name"] for col in columns if not col["null_allowed"]
        ]

        # Map column types
        column_types = {
            col["column_name"]: resolve_spark_type(col["column_type"])
            for col in columns
        }

        # Build configuration dictionary
        key = f"{id_val}_{table_name}"
        files_attrs_dict[key] = {
            "database_name": row["database_name"],
            "table_name": table_name,
            "stream_format": row["stream_format"],
            "stacked_stream": row["stacked_stream"],
            "optional_bronze_stack": row["optional_bronze_stack"],
            "file_format": row["file_format"].upper(),
            "file_keyword_simplified": row["file_keyword_simplified"],
            "file_regex": row["file_regex"],
            "file_mode": row["file_mode"],
            "bucket_name": bucket_name,
            "file_prefix": row["file_prefix"],
            "archive_bucket_name": archive_bucket_name,
            "archive_file_prefix": row["archive_file_prefix"],
            "csv_delimiter": row_dict.get("csv_delimiter", ";"),
            "csv_header": row["csv_header"],
            "csv_escape_char": row_dict.get("csv_escape_char", '"'),
            "csv_quote_char": row_dict.get("csv_quote_char", '"'),
            "frequency": row["frequency"],
            "file_expected_cols": file_expected_cols,
            "metadatas_cols": DEFAULT_METADATA_COLS.copy(),
            "primary_key_cols": primary_key_cols,
            "notnull_cols": ",".join(notnull_cols),
            "column_types": column_types,
            "table_tags": row_dict.get("table_tags", {}),
            "column_tags": row_dict.get("column_tags", {}),
            # XML-specific options
            "xml_row_tag": row["xml_row_tag"] or "row",
            "xml_attribute_prefix": row["xml_attribute_prefix"] or "_",
            "xml_value_tag": row["xml_value_tag"] or "_VALUE",
            "xml_exclude_attributes": row["xml_exclude_attributes"] or False,
            # Excel-specific options
            "excel_data_address": row_dict.get("excel_data_address"),
            "excel_use_yaml_schema": row_dict.get("excel_use_yaml_schema", False),
        }

    return files_attrs_dict


# =============================================================================
# Data Loading Functions
# =============================================================================


def get_raw_data(
    attr_dict: Dict[str, Any],
    table_id: str,
    lowercase_columns: bool = True,
    dbutils=None,
) -> DataFrame:
    """
    Generic function to load and transform data based on the table configuration.

    Supports CSV, PARQUET, XML, and EXCEL file formats with Auto Loader (cloudFiles)
    for streaming or batch read for non-streaming scenarios.

    :param attr_dict: A dictionary containing attributes for data loading.
    :type attr_dict: Dict[str, Any]
    :param table_id: The identifier of the table being processed.
    :type table_id: str
    :param lowercase_columns: Whether to convert all column names to lowercase.
    :type lowercase_columns: bool
    :param dbutils: Databricks dbutils object (required for file existence check).
    :return: A DataFrame with transformed data including metadata columns.
    :rtype: DataFrame
    """
    table_name = attr_dict["table_name"]
    glob_filter = attr_dict["file_keyword_simplified"]
    stream_format = attr_dict["stream_format"]
    stacked_stream = attr_dict["stacked_stream"]
    # Auto Loader column-name matching against the tracked schema. Default "true"
    # preserves native (case-sensitive) behaviour; set reader_case_sensitive: false
    # in pilotage to make a source resilient to header-casing drift — otherwise
    # columns differing only by case land in _rescued_data and the schema columns
    # come out null.
    reader_case_sensitive = str(attr_dict.get("reader_case_sensitive", True)).lower()

    # Column configuration
    primary_key_cols = [col.lower() for col in attr_dict["primary_key_cols"]]
    expected_cols = [col.lower() for col in attr_dict["file_expected_cols"]]
    metadatas_cols = [col.lower() for col in attr_dict["metadatas_cols"]]
    not_null_cols = [col.lower() for col in attr_dict["notnull_cols"].split(",") if col]
    column_types = attr_dict["column_types"]
    column_formats = attr_dict.get("column_formats", {})
    column_timezones = attr_dict.get("column_timezones", {})

    if stream_format != "file":
        raise ValueError(
            "Invalid stream format. Only 'file' stream format is currently supported."
        )

    # File path configuration
    bucket_name = attr_dict["bucket_name"]
    file_prefix = attr_dict["file_prefix"]
    file_format = attr_dict["file_format"].lower()
    source_volume = attr_dict.get("source_volume", "")
    if source_volume:
        # UC Volume path
        landingzone_catalog = spark.conf.get("landingzone_catalog")
        input_path = f"/Volumes/{landingzone_catalog}/{source_volume}"
    else:
        input_path = f"s3://{bucket_name}/{file_prefix}"
    log.info(f"Input path: {input_path}")

    # Map format name to file extension for glob pattern.
    # "excel" matches both .xls and .xlsx via "xls*" glob.
    # by default file_ext = file_format
    file_ext = {"excel": "xls*"}.get(file_format, file_format)

    # Build glob filter pattern
    path_glob_filter = f"*{glob_filter}*.{file_ext}"
    log.info(f"Filtering files with pattern: {path_glob_filter}")
    file_regex = attr_dict["file_regex"]

    # =================================================================
    # Format-specific configuration
    # =================================================================

    if file_format == "csv":
        csv_delimiter = attr_dict["csv_delimiter"]
        csv_header = "true" if attr_dict["csv_header"] else "false"
        csv_escape_char = attr_dict["csv_escape_char"]
        csv_quote_char = attr_dict["csv_quote_char"]
    elif file_format == "parquet":
        csv_header = "false"
    elif file_format == "xml":
        # XML-specific options
        xml_row_tag = attr_dict.get("xml_row_tag", "row")
        xml_attribute_prefix = attr_dict.get("xml_attribute_prefix", "_")
        xml_value_tag = attr_dict.get("xml_value_tag", "_VALUE")
        xml_exclude_attributes = attr_dict.get("xml_exclude_attributes", False)
        csv_header = "false"  # Not applicable for XML

        if not xml_row_tag:
            raise ValueError("xml_row_tag is required for XML format")
        log.info(
            f"XML options: rowTag={xml_row_tag}, attributePrefix={xml_attribute_prefix}"
        )
    elif file_format == "excel":
        # Excel-specific options (native format, requires DBR 17.1+)
        excel_data_address = attr_dict.get("excel_data_address")
        excel_use_yaml_schema = attr_dict.get("excel_use_yaml_schema")
        header_rows = 0 if excel_use_yaml_schema else 1

        log.info(
            f"Excel options: "
            f"dataAddress={excel_data_address}, header={header_rows}"
        )
    else:
        raise ValueError(
            f"Invalid file format '{file_format}'. "
            "Supported formats: 'csv', 'parquet', 'xml', 'excel'."
        )

    # Check if files exist
    files_exist = False
    if dbutils:
        files_exist = is_any_file(input_path, path_glob_filter, dbutils)

    # =================================================================
    # Build DataFrame reader
    # =================================================================

    if stacked_stream:
        # Streaming mode with Auto Loader
        df_raw = (
            spark.readStream.format("cloudFiles")
            .option("cloudFiles.format", file_format)
            .option("readerCaseSensitive", reader_case_sensitive)
            .option("cloudFiles.includeExistingFiles", "true")
            .option("cloudFiles.useIncrementalListing", "true")
            .option("cloudFiles.allowOverwrites", "true")
            .option("pathGlobfilter", path_glob_filter)
        )

        if file_format == "csv":
            df_raw = df_raw.option("header", csv_header)
        elif file_format == "excel":
            # Excel format does not support schema evolution via addNewColumns
            df_raw = df_raw.option("cloudFiles.schemaEvolutionMode", "none")
    else:
        # Batch mode
        df_raw = spark.read.format(file_format).option(
            "pathGlobfilter", path_glob_filter
        )

        if file_format == "csv":
            df_raw = df_raw.option("header", csv_header)

    # =================================================================
    # Add format-specific options
    # =================================================================

    if file_format == "csv":
        df_raw = (
            df_raw.option("delimiter", csv_delimiter)
            .option("escape", csv_escape_char)
            .option("quote", csv_quote_char)
            .option("multiLine", "true")
        )
    elif file_format == "xml":
        df_raw = (
            df_raw.option("rowTag", xml_row_tag)
            .option("attributePrefix", xml_attribute_prefix)
            .option("valueTag", xml_value_tag)
            .option("excludeAttribute", str(xml_exclude_attributes).lower())
            .option("mode", "PERMISSIVE")
            .option("nullValue", "")
        )
    elif file_format == "excel":
        # headerRows: number of header rows (1 = first row is header, 0 = no header)
        # When excel_use_yaml_schema is set, schema provides column names — no header needed.
        header_rows = 0 if excel_use_yaml_schema else 1
        df_raw = df_raw.option("headerRows", str(header_rows)).option("dataAddress", excel_data_address)

    # =================================================================
    # Schema handling
    # =================================================================

    if file_format == "excel":
        if not excel_use_yaml_schema:
            log.info(f"{table_name} - Excel format with header: letting reader use Excel header names")
        else:
            # No header row → positional mapping. Provide explicit schema so
            # the reader knows column names and types.
            log.info(f"{table_name} - Excel format without header: using defined schema (positional)")
            excel_schema = StructType([
                StructField(col_name, spark_type_from_str(type_str), True)
                for col_name, type_str in column_types.items()
            ])
            df_raw = df_raw.schema(excel_schema)
    elif files_exist:
        log.info(f"{table_name} - Files exist, inferring schema")
        df_raw = df_raw.option("inferSchema", "true").option("mergeSchema", "true")
    else:
        log.info(f"{table_name} - No files found, using defined schema")
        df_raw = (
            df_raw.schema(get_spark_schema(column_types, metadatas_cols))
            .option("inferSchema", "false")
            .option("mergeSchema", "true")
        )

    # Load the data
    df_raw = df_raw.load(input_path)

    # =================================================================
    # Column cleanup and case handling
    # =================================================================

    rename_dict = {col: table_utils._cleanup_column_name_unicode(col) for col in df_raw.columns}
    for old_name, new_name in rename_dict.items():
        df_raw = df_raw.withColumnRenamed(old_name, new_name)

    if lowercase_columns:
        df_raw = df_raw.toDF(*[c.lower() for c in df_raw.columns])
    else:
        # Map lowercase names to actual column names
        colmap_real = {c.lower(): c for c in df_raw.columns}

        def resolve_col_name(lower_name: str) -> str:
            return colmap_real.get(lower_name, lower_name)

        primary_key_cols = [resolve_col_name(c) for c in primary_key_cols]
        expected_cols = [resolve_col_name(c) for c in expected_cols]
        metadatas_cols = [resolve_col_name(c) for c in metadatas_cols]
        not_null_cols = [resolve_col_name(c) for c in not_null_cols]
        column_types = {resolve_col_name(k.lower()): v for k, v in column_types.items()}

    log.info(f"Expected columns: {', '.join(expected_cols)}.")
    log.info(f"Not null columns: {', '.join(not_null_cols)}.")

    # =================================================================
    # Excel cleanup: drop empty columns/rows from broad data address
    # required if the schema is not specified, all the empy cells
    # wihtin the cell address range would stay in the table otherwise
    # =================================================================

    if file_format == "excel" and not excel_use_yaml_schema:
        # Keep only columns declared in expected_cols (+ _metadata for Auto Loader)
        all_cols = set(df_raw.columns)
        cols_to_keep = [c for c in expected_cols if c in all_cols]
        if "_metadata" in all_cols:
            cols_to_keep.append("_metadata")
        dropped_cols = all_cols - set(cols_to_keep)
        if dropped_cols:
            log.info(f"{table_name} - Dropping {len(dropped_cols)} unexpected columns from Excel range")
        df_raw = df_raw.select(cols_to_keep)

        # Drop rows where ALL expected columns are null (empty rows from broad range)
        not_all_null_expr = f.lit(False)
        for c in [c for c in cols_to_keep if c != "_metadata"]:
            not_all_null_expr = not_all_null_expr | f.col(c).isNotNull()
        df_raw = df_raw.filter(not_all_null_expr)

    # =================================================================
    # Validity checks
    # =================================================================

    existing_cols = set(df_raw.columns)
    log.info(f"Data columns: {', '.join(existing_cols)}.")

    missing_cols = set(expected_cols) - existing_cols
    missing_cols_expr = f.lit(False) if missing_cols else f.lit(True)
    if missing_cols:
        log.error(f"Some expected columns are missing: {', '.join(missing_cols)}.")

    # Type casting (see get_raw_data_from_table for column_format / column_timezone notes)
    for col_name, col_type in column_types.items():
        target_col = col_name if not lowercase_columns else col_name.lower()
        if target_col not in existing_cols:
            continue
        col_fmt = column_formats.get(col_name)
        col_tz = column_timezones.get(col_name)
        if col_type == "TIMESTAMP":
            if col_fmt:
                _validate_datetime_format(col_fmt)
                ts_col = f.expr(f"try_to_timestamp(`{target_col}`, '{col_fmt}')")
            else:
                ts_col = f.to_timestamp(f.col(target_col))
            if col_tz:
                ts_col = f.to_utc_timestamp(ts_col, col_tz)
            df_raw = df_raw.withColumn(target_col, ts_col)
        elif col_type == "DATE":
            if col_fmt:
                _validate_datetime_format(col_fmt)
                df_raw = df_raw.withColumn(target_col, f.expr(f"try_to_date(`{target_col}`, '{col_fmt}')"))
            else:
                df_raw = df_raw.withColumn(target_col, f.to_date(f.col(target_col)))
        else:
            df_raw = df_raw.withColumn(target_col, f.col(target_col).cast(col_type))

    # Build validity check expression
    valid_not_null_cols = [col for col in not_null_cols if col in existing_cols]
    not_null_expr = (
        f.expr(" AND ".join([f"{col} IS NOT NULL" for col in valid_not_null_cols]))
        if valid_not_null_cols
        else f.lit(True)
    )
    validity_check_expr = missing_cols_expr & not_null_expr

    # =================================================================
    # Add metadata columns
    # =================================================================

    meta_id_expr = (
        f.concat_ws("-", *[f.col(c) for c in primary_key_cols])
        if primary_key_cols
        else f.sha2(f.concat_ws("-", *df_raw.columns), 256)
    )

    # File extension pattern for metadata extraction (supports csv, parquet, xml, xlsx, xls)
    file_ext_pattern = r"-(\w+)\.(csv|parquet|xml|xlsx|xls)$"

    df_meta = (
        df_raw.withColumn("_meta_id", meta_id_expr)
        .withColumn(
            "_meta_extraction_timestamp",
            f.to_timestamp(
                f.regexp_extract(f.col("_metadata.file_path"), r"(\d{8}_\d{6})", 1),
                "yyyyMMdd_HHmmss",
            ),
        )
        .withColumn("_meta_ingestion_timestamp", f.current_timestamp())
        .withColumn(
            "_meta_file_name",
            f.regexp_extract(f.col("_metadata.file_path"), r"([^/]+)$", 1),
        )
        .withColumn(
            "_meta_file_mode",
            f.when(
                f.regexp_extract(f.col("_meta_file_name"), file_ext_pattern, 1) == "F",
                "FULL",
            )
            .when(
                f.regexp_extract(f.col("_meta_file_name"), file_ext_pattern, 1) == "D",
                "DELTA",
            )
            .otherwise(f.lit(attr_dict["file_mode"])),
        )
        .withColumn("_meta_file_path", f.col("_metadata.file_path"))
        .withColumn("_meta_validity_check", validity_check_expr)
    )

    # Reorder columns: metadata first, then data
    data_cols = [col for col in df_meta.columns if col not in metadatas_cols]
    df_reordered = df_meta.select(*(metadatas_cols + data_cols))

    # Filter by file regex
    df_filtered = df_reordered.filter(f.col("_meta_file_name").rlike(file_regex))
    log.info(f"Filtering files with regex: {file_regex}")

    return df_filtered


# =============================================================================
# View/Table Helper Functions
# =============================================================================


def get_latest_view_from_stack(
    df_stack: DataFrame,
    primary_key_cols: List[str],
    file_mode: str,
) -> DataFrame:
    """
    Get the latest view from a stack DataFrame based on the file mode.

    For FULL mode: Returns rows from the most recent extraction timestamp.
    For DELTA mode: Deduplicates by primary key, keeping the latest version.

    :param df_stack: The stack DataFrame containing historical data.
    :type df_stack: DataFrame
    :param primary_key_cols: List of primary key column names.
    :type primary_key_cols: List[str]
    :param file_mode: The file mode, either "FULL" or "DELTA".
    :type file_mode: str
    :return: A DataFrame with the latest view of the data.
    :rtype: DataFrame
    """
    if file_mode == "FULL":
        # For FULL mode, get all rows from the latest file
        max_ts_ext = df_stack.agg(
            f.max("_meta_extraction_timestamp").alias("max_extraction_timestamp")
        )
        max_ts_ing = df_stack.agg(
            f.max("_meta_ingestion_timestamp").alias("max_ingestion_timestamp")
        )
        df_latest = (
            df_stack.join(
                max_ts_ext,
                df_stack["_meta_extraction_timestamp"]
                == max_ts_ext["max_extraction_timestamp"],
                "inner",
            )
            .join(
                max_ts_ing,
                df_stack["_meta_ingestion_timestamp"]
                == max_ts_ing["max_ingestion_timestamp"],
                "inner",
            )
            .drop("max_extraction_timestamp", "max_ingestion_timestamp")
        )
    else:
        # For DELTA mode, deduplicate by primary key keeping the latest
        df_latest = table_utils.drop_duplicates_by_order(
            df=df_stack,
            columns=primary_key_cols,
            order_col=["_meta_extraction_timestamp", "_meta_ingestion_timestamp"],
            keep="last",
        )
    return df_latest


# =============================================================================
# YAML-based Configuration Loading
# =============================================================================


def _normalize_foreign_keys(foreign_keys: list) -> list:
    """
    Normalize foreign key entries from YAML format to the format expected by
    ``apply_pk_fk_tags_uc``.

    YAML format (flat):
        {"columns": [...], "ref_table": "...", "ref_columns": [...]}

    Expected format (nested):
        {"columns": [...], "references": {"table": "...", "columns": [...]}}

    Entries already in the nested format are returned unchanged.
    """
    normalized = []
    for fk in foreign_keys:
        if "references" in fk:
            normalized.append(fk)
        else:
            normalized.append({
                "columns": fk.get("columns", []),
                "references": {
                    "table": fk.get("ref_table", ""),
                    "columns": fk.get("ref_columns", []),
                },
                "constraint_name": fk.get("constraint_name"),
            })
    return normalized


def load_pilotage_from_yaml(
    yaml_path: str,
    reference_read_env: str = "",
    pipeline_write_env: str = "",
) -> Dict[str, Dict[str, Any]]:
    """
    Load pipeline configuration from a YAML file.

    Returns the same dictionary structure as ``get_pilotage_structure``, so both
    functions are interchangeable as configuration sources.

    The YAML file must have a top-level ``tables`` list. Each entry maps to one
    table processed by the pipeline.  Two ``stream_format`` values are supported:

    - ``file``: reads from S3/object-storage files via Auto Loader (same as the
      existing file-based pipeline).
    - ``table``: reads from an existing Delta table in the landingzone catalog
      (no file reading).  Use ``source_has_stack_suffix`` to indicate whether
      the source table name already carries the ``_stack`` suffix.

    Column-level tags can be defined inline under each column entry as
    ``column_tags: {tag_key: tag_value_or_null}``.  Tags with a ``null`` value
    are applied as valueless governed tags
    (``ALTER TABLE ... SET TAGS ('key')``).

    :param yaml_path:
        Absolute path to the YAML configuration file.
        Can be a workspace path (``/Workspace/...``) or a local DBFS path
        (``/dbfs/...``).  ``dbfs:`` scheme is automatically converted.
    :type yaml_path: str
    :param reference_read_env:
        Environment string used to substitute ``{REFERENCE_READ_ENV}``
        placeholders in ``bucket_name`` fields (file mode only).
    :type reference_read_env: str
    :param pipeline_write_env:
        Environment string used to substitute ``{PIPELINE_WRITE_ENV}``
        placeholders in ``archive_bucket_name`` fields (file mode only).
    :type pipeline_write_env: str
    :return:
        Dictionary keyed by ``"<id>_<table_name>"`` with table configuration
        dictionaries.  Same structure as ``get_pilotage_structure``.
    :rtype: Dict[str, Dict[str, Any]]

    YAML example::

        pipeline_name: hr_ingestion

        tables:
          - id: 1
            table_name: employee
            database_name: hr
            stream_format: table          # "file" or "table"
            source_has_stack_suffix: false
            optional_bronze_stack: false
            file_mode: FULL              # FULL or DELTA
            frequency: 1                 # freshness refresh frequency in DAYS (1 = daily)
            source_timestamp_col: update_ts  # optional: column used as extraction timestamp
            table_tags:
              domain: hr
            primary_key: [employee_id]
            foreign_keys: []
            columns:
              - column_name: employee_id
                column_type: string
                is_primary_key: true
                is_expected: true
                null_allowed: false
                column_tags:
                  abac_hr_pii_standard:   # null = valueless governed tag
                  abac_hr_country:
              - column_name: first_name
                column_type: string
                is_expected: true
                null_allowed: true
                column_tags:
                  abac_hr_pii_rgpd:
    """
    actual_path = yaml_path.replace("dbfs:", "/dbfs") if yaml_path.startswith("dbfs:") else yaml_path

    with open(actual_path, "r", encoding="utf-8") as fh:
        config = yaml.safe_load(fh)

    tables = config.get("tables", [])
    files_attrs_dict: Dict[str, Dict[str, Any]] = {}

    for tbl in tables:
        id_val = tbl.get("id", tbl.get("table_name"))
        table_name = tbl["table_name"]
        key = f"{id_val}_{table_name}"

        columns = tbl.get("columns", [])

        file_expected_cols = [
            c["column_name"] for c in columns if c.get("is_expected", True)
        ]
        primary_key_cols = [
            c["column_name"] for c in columns if c.get("is_primary_key", False)
        ]
        notnull_cols = [
            c["column_name"] for c in columns if not c.get("null_allowed", True)
        ]
        column_types = {
            c["column_name"]: resolve_spark_type(c.get("column_type", "string"))
            for c in columns
        }
        # Aggregate per-column column_tags into {col_name: {tag: value}} dict
        column_tags = {
            c["column_name"]: c["column_tags"]
            for c in columns
            if c.get("column_tags")
        }
        # Aggregate per-column comments into {col_name: comment_str} dict
        column_comments = {
            c["column_name"]: c["comment"]
            for c in columns
            if c.get("comment")
        }
        # Optional per-column datetime parsing format (for DATE/TIMESTAMP columns
        # whose source is a string not in ISO format, e.g. "M/d/yyyy").
        column_formats = {
            c["column_name"]: c["column_format"]
            for c in columns
            if c.get("column_format")
        }
        # Optional per-column source timezone (TIMESTAMP columns only): the wall-clock
        # string is interpreted in this zone and converted to UTC via to_utc_timestamp.
        column_timezones = {
            c["column_name"]: c["column_timezone"]
            for c in columns
            if c.get("column_timezone")
        }

        # Environment substitution for file-mode bucket paths
        raw_bucket = tbl.get("bucket_name") or ""
        bucket_name = (
            raw_bucket.format(
                REFERENCE_READ_ENV=reference_read_env,
                PIPELINE_WRITE_ENV=pipeline_write_env,
            )
            if raw_bucket
            else raw_bucket
        )
        raw_archive = tbl.get("archive_bucket_name") or ""
        archive_bucket_name = (
            raw_archive.format(
                REFERENCE_READ_ENV=reference_read_env,
                PIPELINE_WRITE_ENV=pipeline_write_env,
            )
            if raw_archive
            else raw_archive
        )

        files_attrs_dict[key] = {
            "table_name": table_name,
            # Table-mode source designator (analogous to file-mode source_volume /
            # file_keyword_simplified / file_regex). Defaults to table_name so
            # existing bundles keep their current behaviour (source name = destination name).
            "source_table_name": tbl.get("source_table_name", table_name),
            "database_name": tbl.get("database_name", ""),
            "stream_format": tbl.get("stream_format", "file"),
            "source_has_stack_suffix": tbl.get("source_has_stack_suffix", False),
            "stacked_stream": tbl.get("stacked_stream", True),
            "optional_bronze_stack": tbl.get("optional_bronze_stack", False),
            "file_format": (tbl.get("file_format") or "").upper() or None,
            "file_keyword_simplified": tbl.get("file_keyword_simplified", ""),
            "file_regex": tbl.get("file_regex", ".*"),
            "file_mode": tbl.get("file_mode", "FULL"),
            "bucket_name": bucket_name,
            "source_volume": tbl.get("source_volume"),
            "file_prefix": tbl.get("file_prefix", ""),
            "archive_bucket_name": archive_bucket_name,
            "archive_file_prefix": tbl.get("archive_file_prefix", ""),
            "csv_delimiter": tbl.get("csv_delimiter", ";"),
            "csv_header": tbl.get("csv_header", True),
            "csv_escape_char": tbl.get("csv_escape_char", '"'),
            "csv_quote_char": tbl.get("csv_quote_char", '"'),
            # Auto Loader case-sensitivity for column matching (file mode only).
            # Default True = native behaviour; set false for sources with unstable
            # header casing so values do not fall through to _rescued_data.
            "reader_case_sensitive": tbl.get("reader_case_sensitive", True),
            "frequency": tbl.get("frequency", 1),
            "file_expected_cols": file_expected_cols,
            "metadatas_cols": DEFAULT_METADATA_COLS.copy(),
            "primary_key_cols": primary_key_cols,
            "notnull_cols": ",".join(notnull_cols),
            "column_types": column_types,
            "table_tags": tbl.get("table_tags") or {},
            "column_tags": column_tags,
            "column_formats": column_formats,
            "column_timezones": column_timezones,
            "table_comment": tbl.get("table_comment") or "",
            "column_comments": column_comments,
            "primary_key": tbl.get("primary_key") or primary_key_cols,
            "foreign_keys": _normalize_foreign_keys(tbl.get("foreign_keys") or []),
            "source_timestamp_col": tbl.get("source_timestamp_col", "_meta_extraction_timestamp"),
            # XML options (file mode only)
            "xml_row_tag": tbl.get("xml_row_tag", "row"),
            "xml_attribute_prefix": tbl.get("xml_attribute_prefix", "_"),
            "xml_value_tag": tbl.get("xml_value_tag", "_VALUE"),
            "xml_exclude_attributes": tbl.get("xml_exclude_attributes", False),
            # Excel options (file mode only)
            "excel_data_address": tbl.get("excel_data_address", "A1:AZ999"),
            "excel_use_yaml_schema": tbl.get("excel_use_yaml_schema", False),
            "excel_max_cell": tbl.get("excel_max_cell", "AZ9999"),
            "excel_skip_rows": tbl.get("excel_skip_rows", 0),
            "excel_col_stop_marker": tbl.get("excel_col_stop_marker", None),
            "excel_row_stop_marker": tbl.get("excel_row_stop_marker", None),
            "excel_identification_cols": tbl.get("excel_identification_cols", []),
            "excel_day_header_format": tbl.get("excel_day_header_format", ""),
            "excel_day_filename_pattern": tbl.get("excel_day_filename_pattern", ""),
        }

    return files_attrs_dict


# =============================================================================
# Table-to-Table Data Loading
# =============================================================================


def get_raw_data_from_table(
    attr_dict: Dict[str, Any],
    source_table: str,
    lowercase_columns: bool = True,
) -> DataFrame:
    """
    Load and normalize data from an existing Delta table (stack).

    Used for table-to-table ingestion (e.g. landingzone_hr -> bronze_hr) where
    the source is already a Delta table rather than a set of files.

    The function:

    1. Reads the source table with ``spark.read.table``.
    2. Normalises column names (unicode cleanup, optional lowercase).
    3. Casts columns to the types defined in ``column_types``.
    4. Adds ``_meta_*`` columns if they are absent from the source:

       - ``_meta_id`` -- derived from primary key columns (or SHA-256 hash of all
         columns when no PK is defined).
       - ``_meta_ingestion_timestamp`` -- ``current_timestamp()``.
       - ``_meta_extraction_timestamp`` -- copied from ``source_timestamp_col``
         if present in the source, otherwise same as ``_meta_ingestion_timestamp``.
         Used by ``get_latest_view_from_stack`` for FULL-mode deduplication.
       - ``_meta_file_mode`` -- literal value from ``file_mode`` attribute.
       - ``_meta_validity_check`` -- ``True`` when all expected columns exist and
         all not-null columns are non-null.

    .. note::
        When the source table does not contain ``_meta_extraction_timestamp`` and
        no ``source_timestamp_col`` is configured, all rows in a given pipeline
        run receive the same synthetic timestamp.  FULL mode will correctly return
        all rows (single snapshot).  DELTA mode deduplication is still safe but
        the ordering between rows sharing the same PK is arbitrary.

    :param attr_dict:
        Table configuration dictionary (as returned by ``load_pilotage_from_yaml``
        or ``get_pilotage_structure``).
    :type attr_dict: Dict[str, Any]
    :param source_table:
        Fully qualified source Delta table name
        (e.g. ``dev_landingzone_hr.hr.employee``).
    :type source_table: str
    :param lowercase_columns:
        Whether to convert all column names to lowercase.
    :type lowercase_columns: bool
    :return:
        Normalised DataFrame ready for ``get_latest_view_from_stack``.
    :rtype: DataFrame
    """
    table_name = attr_dict["table_name"]
    primary_key_cols = list(attr_dict["primary_key_cols"])
    column_types = dict(attr_dict["column_types"])
    notnull_cols = [c for c in attr_dict["notnull_cols"].split(",") if c]
    file_expected_cols = list(attr_dict["file_expected_cols"])
    file_mode = attr_dict.get("file_mode", "FULL")
    source_timestamp_col = attr_dict.get("source_timestamp_col", "_meta_extraction_timestamp")
    column_formats = dict(attr_dict.get("column_formats", {}))
    column_timezones = dict(attr_dict.get("column_timezones", {}))

    log.info(f"{table_name} - Reading source table: {source_table}")
    df = spark.read.table(source_table)

    # ------------------------------------------------------------------
    # Cast TIMESTAMP_NTZ -> TIMESTAMP: source datetime columns (e.g.
    # pe_fec_ini, pe_fec_fin in periodo) are inferred as TIMESTAMP_NTZ
    # in the landingzone. Delta MV creation requires the timestampNtz
    # table feature which SDP does not auto-enable.
    # ------------------------------------------------------------------
    for _field in df.schema.fields:
        if isinstance(_field.dataType, TimestampNTZType):
            df = df.withColumn(_field.name, f.col(_field.name).cast("timestamp"))
            log.info(f"{table_name} - Cast {_field.name} from TIMESTAMP_NTZ to TIMESTAMP")

    # ------------------------------------------------------------------
    # Column name normalisation
    # ------------------------------------------------------------------
    rename_dict = {
        col: table_utils._cleanup_column_name_unicode(col) for col in df.columns
    }
    for old_name, new_name in rename_dict.items():
        if old_name != new_name:
            df = df.withColumnRenamed(old_name, new_name)

    if lowercase_columns:
        df = df.toDF(*[c.lower() for c in df.columns])
        primary_key_cols = [c.lower() for c in primary_key_cols]
        column_types = {k.lower(): v for k, v in column_types.items()}
        notnull_cols = [c.lower() for c in notnull_cols]
        file_expected_cols = [c.lower() for c in file_expected_cols]
        column_formats = {k.lower(): v for k, v in column_formats.items()}
        column_timezones = {k.lower(): v for k, v in column_timezones.items()}
        if source_timestamp_col:
            source_timestamp_col = source_timestamp_col.lower()

    existing_cols = set(df.columns)

    # ------------------------------------------------------------------
    # Type casting
    #
    # DATE/TIMESTAMP columns may carry an optional ``column_format`` (e.g.
    # "M/d/yyyy") when the source string is not in ISO format — common with
    # Airbyte-loaded landingzone tables where every value is a STRING. Parsing
    # uses ``try_to_*`` so empty strings / malformed values become NULL instead
    # of raising (safe under ANSI mode). TIMESTAMP columns may also carry a
    # ``column_timezone`` to convert a wall-clock string from a source zone to
    # UTC. DATE values are timezone-agnostic, so ``column_timezone`` is ignored
    # for them.
    # ------------------------------------------------------------------
    for col_name, col_type in column_types.items():
        if col_name not in existing_cols:
            continue
        col_fmt = column_formats.get(col_name)
        col_tz = column_timezones.get(col_name)
        if col_type == "TIMESTAMP":
            if col_fmt:
                _validate_datetime_format(col_fmt)
                ts_col = f.expr(f"try_to_timestamp(`{col_name}`, '{col_fmt}')")
            else:
                ts_col = f.to_timestamp(f.col(col_name))
            if col_tz:
                ts_col = f.to_utc_timestamp(ts_col, col_tz)
            df = df.withColumn(col_name, ts_col)
        elif col_type == "DATE":
            if col_fmt:
                _validate_datetime_format(col_fmt)
                df = df.withColumn(col_name, f.expr(f"try_to_date(`{col_name}`, '{col_fmt}')"))
            else:
                df = df.withColumn(col_name, f.to_date(f.col(col_name)))
        else:
            df = df.withColumn(col_name, f.col(col_name).cast(col_type))

    # ------------------------------------------------------------------
    # Metadata columns (add only if absent in source)
    # ------------------------------------------------------------------
    if "_meta_id" not in existing_cols:
        meta_id_expr = (
            f.concat_ws("-", *[f.col(c) for c in primary_key_cols])
            if primary_key_cols
            else f.sha2(f.concat_ws("-", *df.columns), 256)
        )
        df = df.withColumn("_meta_id", meta_id_expr)
        existing_cols = existing_cols | {"_meta_id"}

    if "_meta_ingestion_timestamp" not in existing_cols:
        df = df.withColumn("_meta_ingestion_timestamp", f.current_timestamp())
        existing_cols = existing_cols | {"_meta_ingestion_timestamp"}

    if "_meta_extraction_timestamp" not in existing_cols:
        if source_timestamp_col and source_timestamp_col in existing_cols:
            log.info(
                f"{table_name} - Using '{source_timestamp_col}' as _meta_extraction_timestamp"
            )
            df = df.withColumn(
                "_meta_extraction_timestamp",
                f.to_timestamp(f.col(source_timestamp_col)),
            )
        else:
            if file_mode == "DELTA":
                log.warning(
                    f"{table_name} - No extraction timestamp found (source_timestamp_col="
                    f"'{source_timestamp_col}'). DELTA mode deduplication will use "
                    "_meta_ingestion_timestamp; row ordering within the same PK is arbitrary."
                )
            df = df.withColumn(
                "_meta_extraction_timestamp", f.col("_meta_ingestion_timestamp")
            )
        existing_cols = existing_cols | {"_meta_extraction_timestamp"}

    if "_meta_file_mode" not in existing_cols:
        df = df.withColumn("_meta_file_mode", f.lit(file_mode))

    # ------------------------------------------------------------------
    # Validity check
    # ------------------------------------------------------------------
    missing_cols = set(file_expected_cols) - existing_cols
    if missing_cols:
        log.warning(f"{table_name} - Expected columns missing from source: {missing_cols}")
    missing_cols_expr = f.lit(False) if missing_cols else f.lit(True)

    valid_notnull = [c for c in notnull_cols if c in existing_cols]
    not_null_expr = (
        f.expr(" AND ".join([f"`{c}` IS NOT NULL" for c in valid_notnull]))
        if valid_notnull
        else f.lit(True)
    )
    df = df.withColumn("_meta_validity_check", missing_cols_expr & not_null_expr)

    return df
