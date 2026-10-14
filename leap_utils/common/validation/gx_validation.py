from great_expectations.core import (
    ExpectationSuiteValidationResult,
    ExpectationValidationResult,
)
from great_expectations.dataset import SparkDFDataset
from pyspark.sql import DataFrame
from typing import Optional

from leap_utils.common.logger import get_logger, setup_applevel_logger
from leap_utils.common.validation import validation_utils
from leap_utils.common.validation import event_logging
from leap_utils.config import spark

setup_applevel_logger()
LOGGER = get_logger(__name__)


def _is_all_gx_results_successful(
    validation_results: list[ExpectationSuiteValidationResult],
) -> bool:
    """
    Return true only if all validation results from great expectation are successful.

    :param validation_results: The list of validation results done by great expectation.
    :return: Return True if all validation result are successful, else False.
    """

    all_validated = all([validation.success for validation in validation_results])

    return all_validated


def validate_and_log_gx_results(
    quality_check_results: list[ExpectationSuiteValidationResult],
    validation_level: str,
    info_message: str,
    error_message: str,
    pipeline_write_env: Optional[str] = "dev",
    table_name: Optional[str] = None,
    pipeline_name: Optional[str] = None,
    log_to_event_table: bool = False,
    log_successes: bool = False,
    dest_table: Optional[str] = None,
) -> None:
    """
    Function for validating gx quality checks, logging errors & optionally log to quality_event_table & potentially raise an
    error for stopping the job.

    :param quality_check_results: A list of GX expectation results to check.
    :param validation_level: "RED" if the job should fail in case of error, "AMBER" if
    just a warning should be logged.
    :param info_message: Message in case all the tests passed.
    :param error_message: Message in case at least one test is KO.
    :param pipeline_write_env: the ENV we are writing to.
    :param table_name: Name of the validated table (required if logging).
    :param pipeline_name: Pipeline or notebook name (required if logging).
    :param log_to_event_table: Whether to log the GX results.
    :param log_successes: Whether to log both successes and failures.
    :param dest_table: Optional fully-qualified table being written (``catalog.schema.table``).
        Used only to route the quality_event_table into the ``data_quality`` schema of the SAME
        datalake — e.g. an HR table (``{env}_*_hr`` catalog) logs into
        ``{env}_bronze_hr.data_quality`` instead of the main ``{env}_bronze.data_quality`` (not
        reachable from the HR workspace). When omitted, defaults to the main lake (backward
        compatible).
    :raises AssertionError: In case of error on a "RED" validation.
    """

    quality_event_table_path = event_logging.resolve_quality_event_table_path(
        pipeline_write_env=pipeline_write_env, dest_table=dest_table
    )
    success = _is_all_gx_results_successful(quality_check_results)

    def log_action():
        dictified_results = [
            r.to_json_dict() if isinstance(r, ExpectationValidationResult) else r
            for r in quality_check_results
        ]
        event_logging._log_expectations_to_table(
            quality_check_results=dictified_results,
            table_name=table_name,
            validation_level=validation_level,
            pipeline_name=pipeline_name,
            spark_session=spark,
            only_failed=not log_successes,
            quality_event_table_path=quality_event_table_path,
        )

    validation_utils.red_amber_validation_logic(
        check=success,
        on_success_message=info_message,
        on_success_action=log_action if log_to_event_table and log_successes else None,
        on_error_message=(
            f"{error_message}\n"
            f"Failed validation, validation results are :\n"
            f"{quality_check_results}"
        ),
        on_error_action=log_action if log_to_event_table else None,
        on_error_validation_level=validation_level,
    )


def gx_check_uniqueness(
    df: DataFrame,
    col: str,
    validation_level: str,
    pipeline_write_env: Optional[str] = "dev",
    table_name: Optional[str] = None,
) -> bool:
    """
    Check the uniqueness of values for a column.

    :param df: The Spark DataFrame to check.
    :type df: SparkDataFrame
    :param col: The name of the column to check for uniqueness.
    :type col: str
    :param validation_level: The level of validation. "RED" if the job should fail on error,
                              "AMBER" if only a warning should be logged.
    :type validation_level: str
    :return: Always returns True or aborts the job if the "RED" check fails.
    :rtype: bool
    """
    df_gx = SparkDFDataset(df)

    quality_check_results = [
        df_gx.expect_column_values_to_be_unique(column=col),
    ]

    validate_and_log_gx_results(
        quality_check_results=quality_check_results,
        validation_level=validation_level,
        info_message=f"All {col}(s) are unique",
        error_message=f"{col}(s) are not unique, check the summary of this run",
        pipeline_write_env=pipeline_write_env,
        table_name=table_name,
        log_to_event_table=True,
        log_successes=True,
    )
    return True


def gx_check_compound_uniqueness(
    df: DataFrame,
    cols: str,
    validation_level: str,
    pipeline_write_env: Optional[str] = "dev",
    table_name: Optional[str] = None,
) -> bool:
    """
    Check the uniqueness of values for several columns.

    :param df: The Spark DataFrame to check.
    :type df: SparkDataFrame
    :param cols: A comma-separated string of column names to check for uniqueness.
    :type cols: str
    :param validation_level: The level of validation. "RED" if the job should fail on error,
                              "AMBER" if only a warning should be logged.
    :type validation_level: str
    :return: Always returns True or aborts the job if the "RED" check fails.
    :rtype: bool
    """
    df_gx = SparkDFDataset(df)
    cols_list = cols.replace(" ", "").split(",")

    quality_check_results = [
        df_gx.expect_compound_columns_to_be_unique(column_list=cols_list),
    ]

    validate_and_log_gx_results(
        quality_check_results=quality_check_results,
        validation_level=validation_level,
        info_message=f"Compound columns {cols} are unique",
        error_message=f"Compound columns {cols} are not unique, check the summary of this run",
        pipeline_write_env=pipeline_write_env,
        table_name=table_name,
        log_to_event_table=True,
        log_successes=True,
    )
    return True


def gx_check_not_null(
    df: DataFrame,
    col: str,
    validation_level: str,
    pipeline_write_env: Optional[str] = "dev",
    table_name: Optional[str] = None,
) -> bool:
    """
    Check that all values for a column are not null.

    :param df: The Spark DataFrame to check.
    :type df: SparkDataFrame
    :param col: The name of the column to check for null values.
    :type col: str
    :param validation_level: The level of validation. "RED" if the job should fail on error,
                              "AMBER" if only a warning should be logged.
    :type validation_level: str
    :return: Always returns True or aborts the job if the "RED" check fails.
    :rtype: bool
    """
    df_gx = SparkDFDataset(df)

    quality_check_results = [df_gx.expect_column_values_to_not_be_null(column=col)]

    validate_and_log_gx_results(
        quality_check_results=quality_check_results,
        validation_level=validation_level,
        info_message=f"All {col}(s) are not null",
        error_message=f"{col} contains null(s), check the summary of this run",
        pipeline_write_env=pipeline_write_env,
        table_name=table_name,
        log_to_event_table=True,
        log_successes=True,
    )
    return True


def gx_check_max_value(
    df: DataFrame,
    col: str,
    max_value: float,
    validation_level: str,
    pipeline_write_env: Optional[str] = "dev",
    table_name: Optional[str] = None,
) -> bool:
    """
    Check that all values in a column are less than or equal to a specified maximum value.

    :param df: The Spark DataFrame to check.
    :type df: SparkDataFrame
    :param col: The name of the column to check values from.
    :type col: str
    :param max_value: The maximum allowed value for the column.
    :type max_value: float
    :param validation_level: The level of validation. "RED" if the job should fail on error,
                              "AMBER" if only a warning should be logged.
    :type validation_level: str
    :return: Always returns True or aborts the job if the "RED" check fails.
    :rtype: bool
    """

    df_gx = SparkDFDataset(df)

    quality_check_results = [
        df_gx.expect_column_values_to_be_between(
            column=col,
            min_value=None,
            max_value=max_value,
            strict_min=False,
            strict_max=False,
        )
    ]

    validate_and_log_gx_results(
        quality_check_results=quality_check_results,
        validation_level=validation_level,
        info_message=f"All {col}(s) are <= {max_value}",
        error_message=f"{col} contains values greater than {max_value}, check the summary of this run",
        pipeline_write_env=pipeline_write_env,
        table_name=table_name,
        log_to_event_table=True,
        log_successes=True,
    )
    return True
