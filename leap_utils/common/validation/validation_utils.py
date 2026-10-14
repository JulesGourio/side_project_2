from typing import Callable, Optional

from leap_utils.common.logger import get_logger

LOGGER = get_logger(__name__)


def _get_validation_level_log(validation_level: str) -> Callable:
    """
    Check that the validation level

    :param validation_level: The validation level to use for logging amber or red errors.
    :return: The log function to use depending on the validation_level chosen.
    """

    if validation_level == "AMBER":
        validation_level_log = LOGGER.warning
    elif validation_level == "RED":
        validation_level_log = LOGGER.critical
    else:
        raise ValueError(
            "validation_level variable should be 'AMBER' or 'RED', here"
            f"it's {validation_level}"
        )

    return validation_level_log


def red_amber_validation_logic(
    check: bool,
    on_success_message: str,
    on_success_action: Optional[Callable],
    on_error_message: str,
    on_error_action: Optional[Callable],
    on_error_validation_level: str,
    message_prefix: str = None,
) -> None:
    """
    Perform a check on the `check` parameter, to log information if the check is successful,
        or log a warning/error depending on the type of validation to be done.
        For red validation level checks, it will also raise an exception on error.
    This is the reference function that centralizes the red amber check logic.

    :param check: The value to perform a check on. If the check is True, it will log an information message.
        If it's False, it will log an error message (and raise an error for RED validation level)
    :param on_success_message: The information message to log when the check is successful.
    :param on_success_action: The action to perform when the check is successful.
        For example, you can use a reference to the display function of a dataframe.
    :param on_error_message: The error message to log when the check is not successful.
    :param on_error_action: The action to perform when the check is not successful.
    :param on_error_validation_level: The validation level to use, between "AMBER" and "RED".
    :param message_prefix: An optional prefix to use for the various messages that will be logged.
    """

    # Get the right logging level function for errors and make sure validation_level is valid
    validation_level_log = _get_validation_level_log(on_error_validation_level)

    if message_prefix:
        message_prefix = f"{message_prefix}: "
    else:
        message_prefix = ""

    if check:
        LOGGER.info(message_prefix + on_success_message)
        if on_success_action:
            on_success_action()
    else:
        validation_level_log(message_prefix + on_error_message)
        if on_error_action:
            on_error_action()

        if on_error_validation_level == "RED":
            raise AssertionError(message_prefix + on_error_message)
