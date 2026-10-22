"""Background tasks that outlive the request that started them."""

import asyncio
import logging
from typing import Any, Coroutine, Set

logger = logging.getLogger(__name__)

# The event loop keeps only weak references to tasks: a task nobody holds can be collected before it finishes.
_background: Set[asyncio.Task] = set()


def _finished(task: asyncio.Task) -> None:
    """Forget a finished background task and log its failure, if any.

    :param task: the task that just ended
    """
    _background.discard(task)
    if not task.cancelled() and task.exception() is not None:
        logger.warning('background task %s failed: %s', task.get_name(), task.exception())


def spawn(coro: Coroutine[Any, Any, Any]) -> asyncio.Task:
    """Run ``coro`` in the background, keeping a strong reference until it ends.

    :param coro: the coroutine to run; it must handle its own errors, a failure is only logged
    :return: the task
    """
    task = asyncio.get_running_loop().create_task(coro)
    _background.add(task)
    task.add_done_callback(_finished)
    return task
