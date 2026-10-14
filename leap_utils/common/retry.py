"""
This module contains the definition of functions used to retry a call to another function
"""
import random
import time
from typing import Callable

from leap_utils.common.logger import get_logger

LOGGER = get_logger(module_name=__name__)


def retry(
    func: Callable,
    exceptions: tuple[type[Exception], ...],
    max_attempts: int = 6,
    attempt: int = 1,
    *args,
    **kwargs,
) -> None:
    """
    Tries to call ``func``, and, if it raised one of the exceptions in ``exceptions``,
    and retries until ``max_attempts`` was reached.

    :param func: The function to call
    :type func: Callable
    :param exceptions: All the 'expected' exceptions which will cause a retry.
        Exceptions not in this tuple will not be caught
    :type exceptions: tuple[type[Exception], ...]
    :param max_attempts: The maximum attempts to perform. Defaults to 6.
    :type max_attempts: int
    :param attempt: The current attempt
    :type attempt: int
    """
    try:
        func(*args, **kwargs)
    except exceptions as caught_exception:
        LOGGER.debug(
            f"Attempt {attempt} at calling {func.__name__} raised "
            f"{caught_exception.__class__.__name__}, retrying ..."
        )
        if attempt < max_attempts:
            # Make an exponential backoff retry
            delay = 2 ** attempt + random.uniform(0, 1)
            time.sleep(delay)
            retry(
                func=func,
                exceptions=exceptions,
                max_attempts=max_attempts,
                attempt=attempt + 1,
                *args,
                **kwargs,
            )
        else:
            raise ValueError(f"Still have exceptions after {max_attempts} attempts") from caught_exception
