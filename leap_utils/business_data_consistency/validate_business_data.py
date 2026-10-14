"""
Data Quality Check Module

This module provides a set of quality check functions to apply validation rules to DataFrame columns in PySpark.
Each check function returns a PySpark condition based on the specified rule, which can then be applied 
to a DataFrame to flag rows that do not meet certain data quality standards.
"""
import pyspark.sql.functions as f
from pyspark.sql import DataFrame, Column
from typing import Optional, Dict

from leap_utils.common import logger

log = logger.setup_applevel_logger(log_level=logger.LOGGER_MAPPING["info"])

def check_less_than_or_equal(column: str, value: str) -> Column:
    """
    Checks if the values in a specified column are less than or equal to a given value.

    :param column: The column name to check.
    :type column: str
    :param value: The value to compare against.
    :type value: str
    :return: A PySpark condition indicating if column values are less than or equal to the given value.
    :rtype: Column
    """
    return f.col(column) <= value

def check_more_than_or_equal(column: str, value: str) -> Column:
    """
    Checks if the values in a specified column are more than or equal to a given value.

    :param column: The column name to check.
    :type column: str
    :param value: The value to compare against.
    :type value: str
    :return: A PySpark condition indicating if column values are more than or equal to the given value.
    :rtype: Column
    """
    return f.col(column) >= value

def check_equal(column: str, values: str) -> Column:
    """
    Checks if the values in a specified column are equal to any of the provided values.

    :param column: The column name to apply the equality check on.
    :type column: str
    :param values: A comma-separated string of values to compare against.
    :type values: str
    :return: A PySpark condition indicating if column values match any in the list.
    :rtype: Column
    """
    value_list = values.split(',')

    condition = f.lit(False)

    for value in value_list:
        value = value.strip()
        condition = condition | (f.col(column) == value)

    return condition

def check_between(column: str, min_val: str, max_val: str) -> Column:
    """
    Checks if the values in a specified column fall within a given range.

    :param column: The column name to check.
    :type column: str
    :param min_val: Minimum value of the range.
    :type min_val: str
    :param max_val: Maximum value of the range.
    :type max_val: str
    :return: A PySpark condition indicating if column values are within the range.
    :rtype: Column
    """
    return (f.col(column) >= min_val) & (f.col(column) <= max_val)

def check_not_between(column: str, min_val: str, max_val: str) -> Column:
    """
    Checks if the values in a specified column fall outside a given range.

    :param column: The column name to check.
    :type column: str
    :param min_val: Minimum value of the range.
    :type min_val: str
    :param max_val: Maximum value of the range.
    :type max_val: str
    :return: A PySpark condition indicating if column values are outside the range.
    :rtype: Column
    """
    return (~((f.col(column) >= min_val) & (f.col(column) <= max_val)))

def check_not_equal(column: Column, values: str) -> Column:
    """
    Checks if the values in a specified column are not equal to any provided values.

    :param column: The column name to apply the inequality check on.
    :type column: str
    :param values: A comma-separated string of values to compare against.
    :type values: str
    :return: A PySpark Column indicating rows where the column values are not equal to any in the list.
    :rtype: Column
    """
    value_list = values.split(',')

    condition = f.lit(True)

    for value in value_list:
        value = value.strip()
        condition = condition & (f.col(column) != value)

    return condition

def check_starts_with(column: str, value: str) -> Column:
    """
    Checks if the values in a specified column start with a given substring.

    :param column: The column name to check.
    :type column: str
    :param value: The substring to match at the beginning.
    :type value: str
    :return: A PySpark Column indicating rows where the column value starts with the substring.
    :rtype: Column
    """
    return f.col(column).startswith(value)

def check_not_starts_with(column: str, value: str) -> Column:
    """
    Checks if the values in a specified column does not start with a given substring.

    :param column: The column name to check.
    :type column: str
    :param value: The substring to not match at the beginning.
    :type value: str
    :return: A PySpark Column indicating rows where the column value does not starts with the substring.
    :rtype: Column
    """
    return ~check_starts_with(column, value)

def check_ends_with(column: str, value: str) -> Column:
    """
    Checks if the values in a specified column end with a given substring.

    :param column: The column name to check.
    :type column: str
    :param value: The substring to match at the end.
    :type value: str
    :return: A PySpark Column indicating rows where the column value ends with the substring.
    :rtype: Column
    """
    return f.col(column).endswith(value)

def check_not_ends_with(column: str, value: str) -> Column:
    """
    Checks if the values in a specified column does not end with a given substring.

    :param column: The column name to check.
    :type column: str
    :param value: The substring to not match at the end.
    :type value: str
    :return: A PySpark Column indicating rows where the column value does not end with the substring.
    :rtype: Column
    """
    return ~check_not_ends_with(column, value)

def check_starts_ends_with(column, start, end) -> Column:
    """
    Checks if the values in a specified column start with one substring and end with another.

    :param column: The column name to check.
    :type column: str
    :param start: The substring to match at the beginning.
    :type start: str
    :param end: The substring to match at the end.
    :type end: str
    :return: A PySpark Column indicating rows where the column value starts with `start` and ends with `end`.
    :rtype: Column
    """
    return f.col(column).startswith(start) & f.col(column).endswith(end)

def check_not_starts_ends_with(column, start, end) -> Column:
    """
    Checks if the values in a specified column does not: start with one substring and end with another.

    :param column: The column name to check.
    :type column: str
    :param start: The substring to not match at the beginning.
    :type start: str
    :param end: The substring to not match at the end.
    :type end: str
    :return: A PySpark Column indicating rows where the column value does not start with `start` and ends with `end`.
    :rtype: Column
    """
    return ~(check_starts_ends_with(column, start, end))

def check_contains(column: str, value: str) -> Column:
    """
    Checks if the values in a specified column contain a given substring.

    :param column: The column name to check.
    :type column: str
    :param value: The substring to search for.
    :type value: str
    :return: A PySpark Column indicating rows where the column value contains the substring.
    :rtype: Column
    """
    return f.col(column).contains(value)

def check_not_contains(column: str, value: str) -> Column:
    """
    Checks if the values in a specified column do not contain a given substring.

    :param column: The column name to check.
    :type column: str
    :param value: The substring to exclude.
    :type value: str
    :return: A PySpark Column indicating rows where the column value does not contain the substring.
    :rtype: Column
    """
    return ~check_contains(column, value)

def check_length(column: str, length_val) -> Column:
    """
    Checks if the values in a specified column have a specific length.

    :param column: The column name to check.
    :type column: str
    :param length_val: The required length.
    :type length_val: int
    :return: A PySpark Column indicating rows where the column value length matches `length_val`.
    :rtype: Column
    """
    return f.length(f.col(column)) == int(length_val)

def check_regex(column: str, regex_pattern: str) -> Column:
    """
    Checks if the values in a specified column match a given regular expression pattern.

    :param column: The column name to check.
    :type column: str
    :param regex_pattern: The regular expression pattern to match.
    :type regex_pattern: str
    :return: A PySpark Column indicating rows where the column value matches the regex pattern.
    :rtype: Column
    """
    return f.col(column).rlike(regex_pattern)

def check_has_decimal(column: str) -> Column:
    """
    Checks if the values in a specified column contain decimal points.

    :param column: The column name to check.
    :type column: str
    :return: A PySpark Column indicating rows where the column value has a decimal.
    :rtype: Column
    """
    return f.col(column).cast("float") != f.col(column).cast("int")

def check_has_not_decimal(column: str) -> Column:
    """
    Checks if the values in a specified column do not contain decimal points.

    :param column: The column name to check.
    :type column: str
    :return: A PySpark Column indicating rows where the column value does not have a decimal.
    :rtype: Column
    """
    return f.col(column).cast("float") == f.col(column).cast("int")

def apply_quality_checks_by_rule(
    target_df: DataFrame, 
    quality_checks_df: DataFrame, 
    column_mapping: Optional[Dict[str, str]]
) -> DataFrame:
    """
    Applies multiple quality check rules defined in a DataFrame to another DataFrame's columns.
    Each rule is applied based on the specified conditions in `quality_checks_df`, 
    and the target DataFrame is augmented with pass/fail results and error messages for each rule.

    :param target_df: The DataFrame to apply the quality checks to.
    :type target_df: DataFrame
    :param quality_checks_df: The DataFrame containing quality check rules, with each row defining a rule for a column.
    :type quality_checks_df: DataFrame
    :param column_mapping: An optional dictionary mapping rule column names to target DataFrame column names.
    :type column_mapping: Optional[Dict[str, str]]
    :return: The target DataFrame with additional columns for quality check results and error messages.
    :rtype: DataFrame
    """
    for row in quality_checks_df.collect():
        rule_idx = row["rule_idx"]
        error_type = row["error_type"]
        conditions = []
        error_msgs = []
        columns = [
            col_name.lower()
            for col_name in quality_checks_df.columns
            if col_name
            not in [
                "rule_idx",
                "object",
                "description",
                "error_type",
                "file_name",
                "extraction_date",
            ]
        ]
        if column_mapping:
            columns = [
                column_mapping[col_name]
                for col_name in row.asDict()
                if col_name
                not in [
                    "rule_idx",
                    "object",
                    "description",
                    "error_type",
                    "file_name",
                    "extraction_date",
                ]
            ]
        target_columns = [col_name.lower() for col_name in target_df.columns]
        for column in columns:
            if column not in target_columns:
                log.error(f"Column {column} not found in dataframe")
                continue
            rule = row[column]
            if rule and rule not in ["N/A", None]:
                checks = rule.split("AND")
                column_condition = None
                column_errors = []
                for check in checks:
                    subchecks = check.split("OR")
                    subcondition = None

                    for subcheck in subchecks:
                        if subcheck == "HAS_DECIMAL":
                            new_condition = check_has_decimal(column)
                            error_msg = f"{column} should contains decimal"
                        elif subcheck == "HAS_NOT_DECIMAL":
                            new_condition = check_has_not_decimal(column)
                            error_msg = f"{column} should not contains decimal"
                        else:
                            keyword, param = subcheck.split(":")
                            if keyword == "EQUAL":
                                new_condition = check_equal(column, param)
                                error_msg = f"{column} should be equal to {param}"
                            elif keyword == "BETWEEN":
                                min_val, max_val = param.split(",")
                                new_condition = check_between(column, min_val, max_val)
                                error_msg = f"{column} should be between {min_val} and {max_val}"
                            elif keyword == "NOT_BETWEEN":
                                min_val, max_val = param.split(",")
                                new_condition = check_not_between(column, min_val, max_val)
                                error_msg = f"{column} should not be between {min_val} and {max_val}"
                            elif keyword == "NOT_EQUAL":
                                new_condition = check_not_equal(column, param)
                                error_msg = f"{column} should not be equal to {param}"
                            elif keyword == "MORE_THAN_OR_EQUAL":
                                new_condition = check_more_than_or_equal(column, param)
                                error_msg = f"{column} should start with {param}"
                            elif keyword == "LESS_THAN_OR_EQUAL":
                                new_condition = check_less_than_or_equal(column, param)
                                error_msg = f"{column} should start with {param}"
                            elif keyword == "STARTS_WITH":
                                new_condition = check_starts_with(column, param)
                                error_msg = f"{column} should start with {param}"
                            elif keyword == "NOT_STARTS_WITH":
                                new_condition = check_not_starts_with(column, param)
                                error_msg = f"{column} should not start with {param}"
                            elif keyword == "ENDS_WITH":
                                new_condition = check_ends_with(column, param)
                                error_msg = f"{column} should end with {param}"
                            elif keyword == "NOT_ENDS_WITH":
                                new_condition = check_not_ends_with(column, param)
                                error_msg = f"{column} should not end with {param}"
                            elif keyword == "STARTS_ENDS_WITH":
                                start, end = param.split("*")
                                new_condition = check_starts_ends_with(
                                    column, start, end
                                )
                                error_msg = f"{column} should start with {start} and end with {end}"
                            elif keyword == "NOT_STARTS_ENDS_WITH":
                                start, end = param.split("*")
                                new_condition = check_not_starts_ends_with(
                                    column, start, end
                                )
                                error_msg = f"{column} should not start with {start} and end with {end}"
                            elif keyword == "CONTAINS":
                                new_condition = check_contains(column, param)
                                error_msg = f"{column} should contain {param}"
                            elif keyword == "NOT_CONTAINS":
                                new_condition = check_not_contains(column, param)
                                error_msg = f"{column} should not contain {param}"
                            elif keyword == "LENGTH":
                                new_condition = check_length(column, param)
                                error_msg = f"{column} should have length {param}"
                            elif keyword == "REGEX":
                                new_condition = check_regex(column, param)
                                error_msg = f"{column} should match regex {param}"

                        column_errors.append(f.when(new_condition, error_msg))

                        if subcondition is None:
                            subcondition = new_condition
                        else:
                            subcondition = subcondition | new_condition

                    if column_condition is None:
                        column_condition = subcondition
                    else:
                        column_condition = column_condition & subcondition
                conditions.append(column_condition)
                error_msgs.append(f.concat_ws(", ", *column_errors))   
        if conditions:
            combined_condition = conditions[0]
            for condition in conditions[1:]:
                combined_condition = combined_condition & condition

            combined_errors = f.concat_ws(", ", *error_msgs)
            target_df = target_df.withColumn(
                f"quality_check_{rule_idx}",
                f.when(combined_condition, f.lit("FAIL")).otherwise(f.lit("PASS")),
            )
            target_df = target_df.withColumn(
                f"errors_{rule_idx}",
                f.when(combined_condition, combined_errors).otherwise(f.lit(None)),
            )
            target_df = target_df.withColumn(
                f"errors_name_{rule_idx}",
                f.when(combined_condition, f.lit(error_type)).otherwise(f.lit(None)),
            )
    error_columns = [
        f.col(c) for c in target_df.columns if c.startswith("errors_name_")
    ]
    target_df = target_df.withColumn("errors_name", f.concat_ws(",", *error_columns))
    target_df = target_df.drop(
        *[c for c in target_df.columns if c.startswith("errors_name_")]
    )
    return target_df
