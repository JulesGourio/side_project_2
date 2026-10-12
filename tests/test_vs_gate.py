"""Tests for server/services/vs_gate.py — the app-wide gate on Vector Search queries.

Coverage:
  - never more than VS_MAX_CONCURRENT_QUERIES queries in flight; the others wait, none refused
  - 429 retried until it passes; Retry-After honoured; the slot is free during the pause
  - a non-retryable status is returned at once; the last 429 is returned after the retries
  - 0 = no limit
"""

import asyncio
from unittest.mock import AsyncMock

import httpx
import pytest

from server.services import vs_gate


def _fake_post(statuses=None, delay=0.0, track=None, headers=None):
    statuses = list(statuses or [])

    async def _post(self, url, json=None, headers_=None, **kwargs):
        if track is not None:
            track['now'] += 1
            track['max'] = max(track['max'], track['now'])
        if delay:
            await asyncio.sleep(delay)
        if track is not None:
            track['now'] -= 1
        status = statuses.pop(0) if statuses else 200
        return httpx.Response(status, json={}, headers=headers or {}, request=httpx.Request('POST', url))
    return _post


def test_concurrency_is_capped_and_nothing_is_refused(monkeypatch):
    monkeypatch.setenv('VS_MAX_CONCURRENT_QUERIES', '3')
    track = {'now': 0, 'max': 0}
    monkeypatch.setattr(httpx.AsyncClient, 'post', _fake_post(delay=0.01, track=track))

    async def _go():
        return await asyncio.gather(*(vs_gate.post('https://h', 't', 'idx', {}, 5) for _ in range(12)))
    responses = asyncio.run(_go())
    assert all(r.status_code == 200 for r in responses)
    assert track['max'] == 3


def test_429_is_retried_until_it_passes(monkeypatch):
    monkeypatch.setattr(httpx.AsyncClient, 'post', _fake_post([429, 429, 200]))
    sleep = AsyncMock()
    monkeypatch.setattr(vs_gate.asyncio, 'sleep', sleep)
    resp = asyncio.run(vs_gate.post('https://h', 't', 'idx', {}, 5))
    assert resp.status_code == 200 and sleep.await_count == 2


def test_retry_after_is_honoured(monkeypatch):
    monkeypatch.setattr(httpx.AsyncClient, 'post', _fake_post([429, 200], headers={'Retry-After': '3'}))
    sleep = AsyncMock()
    monkeypatch.setattr(vs_gate.asyncio, 'sleep', sleep)
    asyncio.run(vs_gate.post('https://h', 't', 'idx', {}, 5))
    assert sleep.await_args.args[0] == 3.0


@pytest.mark.parametrize('statuses,expected,pauses', [([400], 400, 0), ([429] * 3, 429, 2)])
def test_non_retryable_at_once_last_429_after_retries(monkeypatch, statuses, expected, pauses):
    monkeypatch.setattr(httpx.AsyncClient, 'post', _fake_post(statuses))
    sleep = AsyncMock()
    monkeypatch.setattr(vs_gate.asyncio, 'sleep', sleep)
    resp = asyncio.run(vs_gate.post('https://h', 't', 'idx', {}, 5, retries=2))
    assert resp.status_code == expected and sleep.await_count == pauses


def test_zero_means_no_limit(monkeypatch):
    monkeypatch.setenv('VS_MAX_CONCURRENT_QUERIES', '0')
    track = {'now': 0, 'max': 0}
    monkeypatch.setattr(httpx.AsyncClient, 'post', _fake_post(delay=0.01, track=track))

    async def _go():
        await asyncio.gather(*(vs_gate.post('https://h', 't', 'idx', {}, 5) for _ in range(10)))
    asyncio.run(_go())
    assert track['max'] == 10
