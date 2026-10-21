"""Robustness of the shared server pieces.

Coverage:
  - the catalog scan of the answer runs off the event loop
  - the compare slot is freed only when the message-building thread has ended
  - one HTTP client per service, reused and closed at shutdown
  - history: usage numbers come from the caller's llm_requests row, never from the request body
  - background tasks stay referenced until they end
  - a hung pool close is cut after the timeout
  - capability cache bounded; per-user rate limit answers 429
  - anonymous callers are refused outside development
"""

import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from fastapi import HTTPException

from server.routers import history
from server.routers.compare import analyze
from server.services.chat.engine import retrieval
from server.services.infra import doc_catalog, http_clients, tasks, user
from server.services.infra.lakebase import connection


def test_named_refs_are_looked_up_in_a_worker_thread(monkeypatch):
    seen = {}

    def fake(text):
        seen[text] = threading.current_thread() is not threading.main_thread()
        return ['A-1'] if text == 'q' else ['B-2', 'A-1']
    monkeypatch.setattr(retrieval, 'refs_named_in', fake)
    refs = asyncio.run(asyncio.to_thread(retrieval._refs_named_in_turns, 'q', 'earlier'))
    assert refs == ['A-1', 'B-2'] and seen == {'q': True, 'earlier': True}


def test_catalog_matching_gives_the_same_documents_from_a_thread(monkeypatch):
    monkeypatch.setattr(doc_catalog, '_live', None)
    doc_catalog.set_catalog([{'ref': 'QP-1', 'url': 'u1', 'title': 't', 'base_ref': None},
                             {'ref': 'QP-22', 'url': 'u2', 'title': 't', 'base_ref': None}])
    out = asyncio.run(asyncio.to_thread(doc_catalog.augment_sources, 'see qp-22 and QP-1.', []))
    assert [s['title'] for s in out] == ['QP-22', 'QP-1']
    doc_catalog._live = None


def test_the_compare_slot_waits_for_the_build_thread():
    async def scenario():
        sem = analyze._analyze_semaphore
        free = sem._value
        await sem.acquire()
        gate = threading.Event()
        build = asyncio.ensure_future(asyncio.to_thread(gate.wait, 5))
        analyze._release_slot_when_idle(build)
        await asyncio.sleep(0.05)
        assert sem._value == free - 1            # the thread still runs: the slot stays taken
        gate.set()
        await build
        await asyncio.sleep(0.05)
        assert sem._value == free
        await sem.acquire()                      # no build task: freed at once
        analyze._release_slot_when_idle(None)
        assert sem._value == free
    asyncio.run(scenario())


def test_one_client_per_service_is_reused_and_closed_at_shutdown():
    async def scenario():
        first = http_clients.shared_client('svc')
        assert http_clients.shared_client('svc') is first
        assert http_clients.shared_client('other') is not first
        await http_clients.close_shared_clients()
        assert first.is_closed
        assert http_clients.shared_client('svc') is not first
        await http_clients.close_shared_clients()
    asyncio.run(scenario())


def test_the_model_calls_use_the_shared_clients():
    from server.services.chat import llm as chat_llm
    from server.services.compare import llm as compare_llm

    async def scenario():
        assert chat_llm.http_client() is chat_llm.http_client()
        assert compare_llm.http_client() is compare_llm.http_client()
        assert chat_llm.http_client() is not compare_llm.http_client()
        await http_clients.close_shared_clients()
    asyncio.run(scenario())


def test_spawn_keeps_the_task_until_it_ends():
    async def scenario():
        gate = asyncio.Event()

        async def work():
            await gate.wait()
        task = tasks.spawn(work())
        assert task in tasks._background
        gate.set()
        await task
        await asyncio.sleep(0)
        assert task not in tasks._background

        async def boom():
            raise RuntimeError('x')
        failed = tasks.spawn(boom())
        await asyncio.wait({failed})
        await asyncio.sleep(0)
        assert failed not in tasks._background
    asyncio.run(scenario())


def test_a_pool_that_hangs_on_close_is_terminated():
    class HungPool:
        terminated = False

        async def close(self):
            await asyncio.sleep(60)

        def terminate(self):
            self.terminated = True
    pool = HungPool()
    asyncio.run(connection._retire_pool(pool, grace_s=0, close_timeout_s=0.05))
    assert pool.terminated

    class FastPool(HungPool):
        async def close(self):
            return None
    fast = FastPool()
    asyncio.run(connection._retire_pool(fast, grace_s=0, close_timeout_s=1))
    assert not fast.terminated


def test_the_capability_cache_drops_expired_then_oldest_entries(monkeypatch):
    monkeypatch.setattr(user, '_caps_cache', {})
    monkeypatch.setattr(user, '_CAPS_CACHE_MAX', 3)
    user._remember_caps('a', 0.0, {'v': 1})
    user._remember_caps('b', 0.0, {'v': 2})
    user._remember_caps('c', 10_000.0, {'v': 3})
    user._remember_caps('d', 10_000.0, {'v': 4})          # a and b have expired
    assert list(user._caps_cache) == ['c', 'd']
    user._remember_caps('e', 10_000.0, {'v': 5})
    user._remember_caps('f', 10_000.0, {'v': 6})          # none expired: the oldest goes
    assert list(user._caps_cache) == ['d', 'e', 'f']


def _request(headers=None):
    return SimpleNamespace(headers=headers or {})


def test_the_rate_limit_answers_429_past_the_limit(monkeypatch):
    monkeypatch.setattr(user, '_rate_hits', {})
    monkeypatch.setattr(user, 'RATE_LIMIT_PER_MINUTE', 2)

    async def scenario():
        req = _request({'x-forwarded-user': '1@2'})
        await user.rate_limit(req)
        await user.rate_limit(req)
        with pytest.raises(HTTPException) as caught:
            await user.rate_limit(req)
        assert caught.value.status_code == 429
        await user.rate_limit(_request({'x-forwarded-user': '3@2'}))     # another user is not affected
    asyncio.run(scenario())


def test_the_rate_limit_window_slides_and_zero_turns_it_off(monkeypatch):
    hits = {}
    assert not user._rate_limited('u', 0.0, 1)
    assert user._rate_limited('u', 1.0, 1)
    assert not user._rate_limited('u', 61.0, 1)
    monkeypatch.setattr(user, 'RATE_LIMIT_PER_MINUTE', 0)
    monkeypatch.setattr(user, '_rate_hits', hits)
    asyncio.run(user.rate_limit(_request()))
    asyncio.run(user.rate_limit(_request()))
    assert hits == {}
    user._rate_hits.clear()


def test_anonymous_callers_are_refused_outside_development(monkeypatch):
    monkeypatch.setattr(user, '_is_dev', lambda: False)
    monkeypatch.setattr(user, '_CAPS_BYPASS', False)
    with pytest.raises(HTTPException) as caught:
        user.reject_anonymous({'user_id': ''})
    assert caught.value.status_code == 401
    user.reject_anonymous({'user_id': '42'})
    monkeypatch.setattr(user, '_is_dev', lambda: True)
    user.reject_anonymous({'user_id': ''})
    monkeypatch.setattr(user, '_is_dev', lambda: False)
    monkeypatch.setattr(user, '_CAPS_BYPASS', True)
    user.reject_anonymous({'user_id': ''})


def test_history_refuses_a_request_without_identity(monkeypatch):
    monkeypatch.setattr(user, '_is_dev', lambda: False)
    monkeypatch.setattr(user, '_CAPS_BYPASS', False)
    monkeypatch.setattr(history, 'get_pool', lambda: object())
    monkeypatch.setattr(history, 'get_user_identity',
                        AsyncMock(return_value={'user_id': '', 'workspace_id': None, 'email': None}))
    with pytest.raises(HTTPException) as caught:
        asyncio.run(history.list_comparisons(_request()))
    assert caught.value.status_code == 401


def _history_pool(llm_row):
    captured = {}
    conn = MagicMock()

    async def fetchrow(sql, *args):
        if 'FROM llm_requests' in sql:
            captured['lookup'] = args
            return llm_row
        captured['insert'] = args
        return {'id': 9, 'created_at': None, 'old_filename': 'a', 'new_filename': 'b', 'volume_session_path': None}
    conn.fetchrow = fetchrow
    pool = MagicMock()
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)
    return pool, captured


def _save(monkeypatch, llm_row):
    pool, captured = _history_pool(llm_row)
    monkeypatch.setattr(history, 'get_pool', lambda: pool)
    monkeypatch.setattr(history, 'upsert_user', AsyncMock())
    monkeypatch.setattr(history, 'get_user_identity',
                        AsyncMock(return_value={'user_id': 'u1', 'workspace_id': 'w', 'email': None}))
    body = history.SaveComparisonRequest(
        old_filename='a', new_filename='b', llm_request_id=5, input_tokens=1, output_tokens=2,
        thinking_tokens=3, total_tokens=999999, cost_eur=123.0)
    asyncio.run(history.save_comparison(body, _request()))
    return captured


def test_history_stores_the_usage_of_the_callers_llm_request_not_the_clients(monkeypatch):
    row = {'endpoint_name': 'ep', 'input_tokens': 10, 'output_tokens': 20, 'thinking_tokens': 0,
           'total_tokens': 30, 'cost_eur': 0.5}
    captured = _save(monkeypatch, row)
    assert captured['lookup'] == (5, 'u1')
    args = captured['insert']
    assert args[15:20] == (10, 20, 0, 30, 0.5)
    assert args[21] == 5 and args[22] == 'ep'


def test_history_stores_null_usage_when_the_request_is_not_the_callers(monkeypatch):
    captured = _save(monkeypatch, None)
    args = captured['insert']
    assert args[15:20] == (None, None, None, None, None)
    assert args[21] is None and args[22] is None


def test_the_model_routes_carry_the_rate_limit():
    from server.routers.compare import impact, summarize

    limited = {route.path for module in (analyze, impact, summarize) for route in module.router.routes
               if any(d.dependency is user.rate_limit for d in route.dependencies)}
    assert limited == {'/compare/analyze', '/compare/impact', '/compare/summarize'}
