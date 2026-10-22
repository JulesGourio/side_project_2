"""One HTTP client per service, shared by every call so connections are reused."""

import asyncio
import logging
from typing import Dict

import httpx

logger = logging.getLogger(__name__)

_clients: Dict[str, httpx.AsyncClient] = {}


def shared_client(name: str) -> httpx.AsyncClient:
    """The client of a service, created on first use. Each request sets its own timeout.

    :param name: service key, e.g. ``chat_llm``
    :return: the open client (a closed one is replaced)
    """
    client = _clients.get(name)
    if client is None or client.is_closed:
        client = _clients[name] = httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=15.0))
    return client


async def close_shared_clients() -> None:
    """Close every shared client (application shutdown)."""
    clients = list(_clients.values())
    _clients.clear()
    results = await asyncio.gather(*(c.aclose() for c in clients if not c.is_closed), return_exceptions=True)
    for result in results:
        if isinstance(result, Exception):
            logger.warning('http_clients: close failed: %s', result)
