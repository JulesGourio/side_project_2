"""Databricks host and token the app uses to call serving endpoints and Vector Search."""

import logging
import os

logger = logging.getLogger(__name__)


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
            from databricks.sdk.core import Config
            cfg = Config()
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
