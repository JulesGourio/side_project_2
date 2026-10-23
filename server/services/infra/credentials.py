"""Databricks host and token the app uses to call serving endpoints and Vector Search."""

import asyncio
import logging
import os
from functools import lru_cache
from typing import Any

logger = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def _sdk_config() -> Any:
    """The SDK configuration, built once: it keeps its own token and renews it when it expires.

    Building it for every request would repeat the host discovery and a token request on the event loop.

    :return: a ``databricks.sdk.core.Config``
    :raises Exception: when the SDK finds no way to authenticate (the failure is not cached)
    """
    from databricks.sdk.core import Config
    return Config()


def get_credentials(request) -> tuple[str, str]:
    """(host, token): the environment first, then the SDK's own auth, then the caller's forwarded token.

    ``request`` is a Request or a WebSocket (anything with ``.headers``).

    :param request: ``Request`` or ``WebSocket``, read for its headers
    :return: ``(host, token)``, empty strings when none is found
    """
    host = os.environ.get('DATABRICKS_HOST', '').rstrip('/')
    if host and not host.startswith('http'):
        host = f'https://{host}'
    token = os.environ.get('DATABRICKS_TOKEN', '')
    if not host or not token:
        try:
            cfg = _sdk_config()
            if not host:
                host = (cfg.host or '').rstrip('/')
            if not token:
                auth_value = cfg.authenticate().get('Authorization', '')
                if auth_value.startswith('Bearer '):
                    token = auth_value[len('Bearer '):]
        except Exception as exc:
            logger.debug('SDK auth unavailable: %s', exc)
    if not token:
        token = request.headers.get('x-forwarded-access-token', '')
    return host, token


async def get_credentials_async(request) -> tuple[str, str]:
    """``get_credentials`` run in a worker thread: the SDK's token refresh is a blocking HTTP call.

    :param request: ``Request`` or ``WebSocket``, read for its headers
    :return: ``(host, token)``, empty strings when none is found
    """
    return await asyncio.to_thread(get_credentials, request)
