"""
Data Quality Validation Module

This module provides functions to validate and standardize quality rules applied to data validation.
"""
import re

from pyspark.sql import functions as f
from pyspark.sql import DataFrame

from leap_utils.common import logger

log = logger.setup_applevel_logger(log_level=logger.LOGGER_MAPPING["info"])


def validate_rules(rules_df: DataFrame, columns_information_rules: list[str]) -> DataFrame:
    """
    Validate if each cell in the DataFrame contains a keyword:value format for quality rules.

    The function checks each cell in the DataFrame to ensure it adheres to the format 'keyword:value'.
    It supports logical operators 'AND' and 'OR' within conditions.

    :param rules_df: DataFrame containing quality rules with columns representing conditions.
    :type rules_df: pyspark.sql.DataFrame
    :return: List of validation errors, if any, indicating cells that do not conform to the expected format.
    :rtype: list
    """
    valid_patterns = [
        r"^EQUAL:(\S+(,\S+)*)+$",
        r"^NOT_EQUAL:(\S+(,\S+)*)+$",
        r"^BETWEEN:\d+,\d+$",
        r"^NOT_BETWEEN:\d+,\d+$",
        r"^STARTS_WITH:\S+$",
        r"^NOT_STARTS_WITH:\S+$",
        r"^ENDS_WITH:\S+$",
        r"^NOT_ENDS_WITH:\S+$",
        r"^STARTS_ENDS_WITH:\S+\*\S+$",
        r"^NOT_STARTS_ENDS_WITH:\S+\*\S+$",
        r"^CONTAINS:\S+$",
        r"^NOT_CONTAINS:\S+$",
        r"^LENGTH:\S+$",
        r"^REGEX:.+$",
        r"^HAS_DECIMAL$",
        r"^HAS_NOT_DECIMAL$",
        r"^LESS_THAN_OR_EQUAL:\S+$",
        r"^MORE_THAN_OR_EQUAL:\S+$",
    ]
    pattern_regex = re.compile("|".join(valid_patterns))

    columns = rules_df.columns
    columns_rules = [column for column in columns if column not in columns_information_rules]

    for col_name in columns_rules:
        values = rules_df.select(col_name).collect()
        values = [row[0] for row in values if row[0] is not None]
        log.info(f"Checking column '{col_name}'...")
        for value in values:
            log.info(f"Checking condition '{value}'...")
            if value == "N/A" or not value:
                continue
            else:
                transformed_value = value.replace(" ", "_").upper()
                conditions = re.split(
                    r"\s+OR\s+|\s+AND\s+", transformed_value, flags=re.IGNORECASE
                )

                for condition in conditions:
                    if not re.match(pattern_regex, condition.strip()):
                        raise Exception(
                            f"Erreur dans la colonne '{col_name}': '{value}' n'est pas au format mot-clé:valeur."
                        )

def clean_keywords(value: str) -> str:
    """
    Clean and transform keywords by removing spaces and converting them to uppercase.
    This function processes each keyword:value pair, ensuring that all keywords have no spaces and are in uppercase.
    
    :param value: The condition string to clean.
    :type value: str
    :return: The cleaned condition string with keywords in uppercase and spaces removed.
    :rtype: str
    """
    
    conditions = re.split(r"\s+OR\s+|\s+AND\s+", value, flags=re.IGNORECASE)

    cleaned_conditions = []
    for condition in conditions:
        
        if ':' in condition:
            keyword, keyword_value = condition.split(":", 1)
            cleaned_keyword = keyword.strip().replace(" ", "_").upper()
            cleaned_condition = f"{cleaned_keyword}:{keyword_value.strip()}"
        else:
            cleaned_condition = condition.strip()

        cleaned_conditions.append(cleaned_condition)

    logical_operators = re.findall(r"(?<=\s)(AND|OR)(?=\s)", value, flags=re.IGNORECASE)
    
    final_string = cleaned_conditions[0]

    for i, operator in enumerate(logical_operators):
        final_string += f" {operator.upper()} {cleaned_conditions[i + 1]}"

    return final_string