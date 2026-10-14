import ast
from typing import Any

from databricks.sdk.runtime import dbutils


def dbutils_get_python_literal(
    widget_param_name: str, default_value: Any = None
) -> Any:
    """
    Parse a dbutils widget into a python literal.

    :param widget_param_name: The name of the dbutils widget to get.
    :param default_value: Optional default value to return if the widget value is empty.
        By default, this value is None.
    :return: The corresponding value of the dbutils widget parsed into a python literal.
    """
    widget_value = dbutils.widgets.get(widget_param_name)

    if not widget_value:
        return default_value

    widget_value_parsed = ast.literal_eval(widget_value)

    return widget_value_parsed
