"""One gate for every Vector Search query of the app (chat and impact search).

Measured 2026-10-08 on the DEV endpoint (``load_test_vector_search.py``, operations_dev.md V2):
HYBRID queries are served at most ~25 per second; past ~8 queries in flight the latency rises,
and past ~16 the endpoint answers 429. The reranker does not lower that ceiling (it only makes
each query slower: 0.5 s instead of 0.2 s). One chat question sends 6 to 8 queries at once, so
two or three questions searching at the same moment were enough to get refusals — the chat
load test failed from 20 questions in progress.

So the app never sends more than ``VS_MAX_CONCURRENT_QUERIES`` queries at once (default 8): the
others wait their turn, a few hundred milliseconds, instead of being refused. A refusal that
still happens (429, 5xx, timeout) is retried with a growing pause that honours ``Retry-After``
(``VS_QUERY_RETRIES`` times, default 5: about 15 s in all). The slot is held only during the
HTTP call, never during a pause.

The limit is per instance of the app; several instances share the endpoint, so lower it if the
app runs on more than one.
"""

import asyncio
import logging
import os
import random
from typing import Any, Dict, Optional, Tuple

import httpx

logger = logging.getLogger(__name__)

_RETRYABLE = {429, 500, 502, 503, 504}
_semaphores: Dict[int, Tuple[int, asyncio.Semaphore]] = {}


def max_concurrent_queries() -> int:
    try:
        return max(0, int(os.getenv('VS_MAX_CONCURRENT_QUERIES', '8') or 8))
    except ValueError:
        return 8


def query_retries() -> int:
    try:
        return max(0, int(os.getenv('VS_QUERY_RETRIES', '5') or 5))
    except ValueError:
        return 5


def _semaphore() -> Optional[asyncio.Semaphore]:
    """One semaphore per event loop (the eval notebooks run each question in its own loop)."""
    limit = max_concurrent_queries()
    if limit <= 0:
        return None
    loop_id = id(asyncio.get_running_loop())
    current = _semaphores.get(loop_id)
    if current is None or current[0] != limit:
        current = (limit, asyncio.Semaphore(limit))
        _semaphores[loop_id] = current
    return current[1]


def _pause(attempt: int, resp: Optional[httpx.Response]) -> float:
    """0.5 s, 1 s, 2 s, 4 s, 8 s… (+ up to 50 % jitter), or the server's Retry-After (≤ 10 s)."""
    if resp is not None:
        try:
            return min(10.0, float(resp.headers.get('Retry-After', '')))
        except ValueError:
            pass
    base = min(8.0, 0.5 * 2 ** attempt)
    return base + random.random() * base * 0.5


async def post(host: str, token: str, index: str, payload: Dict[str, Any], timeout_s: float,
               retries: Optional[int] = None) -> httpx.Response:
    """POST one query to ``index``, through the gate, retried on 429 / 5xx / timeout / network
    error. Returns the last response (the caller checks its status); raises the last network
    error when every attempt failed without a response."""
    attempts = query_retries() if retries is None else retries
    url = f'{host}/api/2.0/vector-search/indexes/{index}/query'
    headers = {'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'}
    for attempt in range(attempts + 1):
        resp: Optional[httpx.Response] = None
        sem = _semaphore()
        try:
            if sem is not None:
                await sem.acquire()
            try:
                async with httpx.AsyncClient(timeout=timeout_s) as client:
                    resp = await client.post(url, json=payload, headers=headers)
            finally:
                if sem is not None:
                    sem.release()
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            if attempt == attempts:
                raise
            logger.warning('vs_gate: Vector Search %s: %s, retry %d/%d', index, type(exc).__name__, attempt + 1, attempts)
        else:
            if resp.status_code not in _RETRYABLE or attempt == attempts:
                return resp
            logger.warning('vs_gate: Vector Search %s returned %d, retry %d/%d', index, resp.status_code,
                           attempt + 1, attempts)
        await asyncio.sleep(_pause(attempt, resp))
    raise RuntimeError('unreachable')
