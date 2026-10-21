"""Lakebase connection: a database that is not reachable at startup is retried, and the health check says so."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

from server.services.infra.lakebase import connection


def test_the_connection_is_retried_until_lakebase_answers(monkeypatch):
    connect = AsyncMock(side_effect=[ConnectionError('waking up'), ConnectionError('still waking'), 3000.0])
    start_refresh = MagicMock()
    sleeps = AsyncMock()
    monkeypatch.setattr(connection, '_connect', connect)
    monkeypatch.setattr(connection, '_start_refresh', start_refresh)
    monkeypatch.setattr(connection.asyncio, 'sleep', sleeps)
    cfg = {'project_id': 'p', 'branch': 'b', 'endpoint': 'e', 'database': 'd'}
    asyncio.run(connection._connect_until_ready(cfg))
    assert connect.await_count == 3
    start_refresh.assert_called_once_with(cfg, 3000.0)
    waits = [call.args[0] for call in sleeps.await_args_list]
    assert waits == [10.0, 20.0, 40.0]            # the wait doubles up to the cap


def test_the_wait_between_attempts_is_capped(monkeypatch):
    connect = AsyncMock(side_effect=[ConnectionError()] * 8 + [10.0])
    monkeypatch.setattr(connection, '_connect', connect)
    monkeypatch.setattr(connection, '_start_refresh', MagicMock())
    sleeps = AsyncMock()
    monkeypatch.setattr(connection.asyncio, 'sleep', sleeps)
    asyncio.run(connection._connect_until_ready({'project_id': 'p'}))
    assert max(call.args[0] for call in sleeps.await_args_list) == 300.0


def test_a_failed_startup_leaves_a_reconnect_task_and_the_health_check_reports_it(monkeypatch):
    monkeypatch.setenv('LAKEBASE_PROJECT_ID', 'project')
    monkeypatch.setattr(connection, '_pool', None)
    monkeypatch.setattr(connection, '_connect', AsyncMock(side_effect=ConnectionError('down')))

    async def scenario():
        await connection.init_lakebase()
        task = connection._reconnect_task
        assert task is not None and not task.done()
        assert connection.status() == 'connecting'
        await connection.shutdown_lakebase()
        assert task.cancelled()

    asyncio.run(scenario())


def test_status_is_disabled_without_a_project_and_ready_with_a_pool(monkeypatch):
    monkeypatch.delenv('LAKEBASE_PROJECT_ID', raising=False)
    assert connection.status() == 'disabled'
    monkeypatch.setenv('LAKEBASE_PROJECT_ID', 'project')
    monkeypatch.setattr(connection, '_pool', object())
    assert connection.status() == 'ready'
