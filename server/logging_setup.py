"""Logging of the app: one setup, one correlation id, no noise.

Rules (see ``server/README.md``):
- INFO: one summary line per chat turn (id, user, status, durations) and per request that changes something (POST/PUT/DELETE),
  is slow or fails. A successful GET is DEBUG. Nothing else per call.
- WARNING: something degraded that is worth reading. A normal fallback or retry is DEBUG.
- ERROR: always with the stack trace (``logger.exception`` or ``exc_info=True``).
- No user question, prompt or token in a log (the question is in the MLflow trace).
- ``LOG_LEVEL`` (env, default INFO) sets the level of the app; chatty libraries stay at WARNING.
"""

import contextvars
import logging
import os

# Id of the current request or chat turn, added to every log line of the code it runs.
request_id: contextvars.ContextVar[str] = contextvars.ContextVar('request_id', default='-')

# Paths that are polled or static: no log line for them when they succeed.
# A successful GET slower than this is logged at INFO.
SLOW_REQUEST_S = 1.0

QUIET_PATHS = ('/api/health', '/images/', '/logos/', '/videos/', '/content/', '/assets/', '/favicon')

_NOISY_LOGGERS = ('uvicorn', 'uvicorn.error', 'httpx', 'httpcore', 'urllib3', 'uvicorn.access', 'mlflow', 'databricks', 'azure', 'asyncio',
                  'PIL', 'fsevents', 'matplotlib')


class _RequestIdFilter(logging.Filter):
    """Adds ``record.request_id`` (from the ``request_id`` context variable) to every record."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id.get()
        return True


def is_quiet_path(path: str) -> bool:
    """True for a health check or a static file, which are not worth a log line.

    :param path: URL path of the request
    """
    return path.startswith(QUIET_PATHS)


def setup_logging() -> None:
    """Configure the root logger once: level from ``LOG_LEVEL``, one format with the request id, quiet libraries."""
    level_name = os.getenv('LOG_LEVEL', 'INFO').upper()
    level = getattr(logging, level_name, logging.INFO)
    handler = logging.StreamHandler()
    handler.addFilter(_RequestIdFilter())
    handler.setFormatter(logging.Formatter(
        '%(asctime)s UTC - %(levelname)s - [%(request_id)s] %(name)s - %(message)s', '%Y-%m-%d %H:%M:%S'))
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)
    # The libraries stay at WARNING, unless the whole app runs at DEBUG.
    lib_level = logging.DEBUG if level <= logging.DEBUG else logging.WARNING
    for name in _NOISY_LOGGERS:
        logging.getLogger(name).setLevel(lib_level)
