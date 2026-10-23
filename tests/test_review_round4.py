"""Tests for the history session-folder rule, the preview rate limit, the 500 headers and the feedback comment cap."""

from unittest.mock import AsyncMock, MagicMock, patch

import asyncio

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from server.routers import history
from server.routers.compare.common import user_session_root

VOLUME = '/Volumes/c/s/v'


def _run(fn, *args):
    """Run a coroutine function to completion."""
    return asyncio.run(fn(*args))


def _conn(owned: bool) -> MagicMock:
    """A connection whose ownership lookup answers ``owned``."""
    conn = MagicMock()
    conn.fetchval = AsyncMock(return_value=1 if owned else None)
    return conn


def test_foreign_session_folder_is_not_stored() -> None:
    foreign = f'{VOLUME}/2026-01-01_120000'
    with patch.dict('os.environ', {'COMPARE_VOLUME_PATH': VOLUME}):
        assert _run(history._accepted_session_path, _conn(False), 'alice', foreign) is None
        other = f'{user_session_root(VOLUME, "bob")}/s1'
        assert _run(history._accepted_session_path, _conn(False), 'alice', other) is None


def test_own_root_folder_is_stored() -> None:
    own = f'{user_session_root(VOLUME, "alice")}/s1'
    with patch.dict('os.environ', {'COMPARE_VOLUME_PATH': VOLUME}):
        assert _run(history._accepted_session_path, _conn(False), 'alice', own) == own


def test_legacy_folder_already_in_own_history_is_stored() -> None:
    legacy = f'{VOLUME}/2026-01-01_120000'
    with patch.dict('os.environ', {'COMPARE_VOLUME_PATH': VOLUME}):
        assert _run(history._accepted_session_path, _conn(True), 'alice', legacy) == legacy


def test_climbing_path_under_own_root_is_refused() -> None:
    sneaky = f'{user_session_root(VOLUME, "alice")}/../u_other/s1'
    with patch.dict('os.environ', {'COMPARE_VOLUME_PATH': VOLUME}):
        assert _run(history._accepted_session_path, _conn(False), 'alice', sneaky) is None


@pytest.fixture(scope='module')
def client():
    with (
        patch('server.app.init_lakebase', new_callable=AsyncMock),
        patch('server.app.shutdown_lakebase', new_callable=AsyncMock),
    ):
        from server.app import app
        with TestClient(app, raise_server_exceptions=False) as c:
            yield c


def test_preview_routes_are_rate_limited(client) -> None:
    with patch('server.services.infra.user.check_rate_limit', return_value=True):
        for path in ('/api/preview', '/api/preview/pdf'):
            response = client.post(path, files={'file': ('a.txt', b'x', 'text/plain')})
            assert response.status_code == 429


def test_500_response_carries_security_headers() -> None:
    from server.app import global_exception_handler
    request = MagicMock()
    request.method, request.url.path = 'GET', '/x'
    with patch('server.app.store_error'):
        response = asyncio.run(global_exception_handler(request, RuntimeError('boom')))
    assert response.status_code == 500
    assert response.headers['X-Content-Type-Options'] == 'nosniff'
    assert 'Content-Security-Policy' in response.headers


def test_feedback_comment_over_the_cap_is_rejected(client) -> None:
    response = client.post('/api/feedback', json={'vote': 'up', 'comment': 'x' * 2001})
    assert response.status_code == 422


def test_check_rate_limit_counts_per_user() -> None:
    from server.services.infra import user
    with patch.object(user, 'RATE_LIMIT_PER_MINUTE', 1), patch.dict(user._rate_hits, {}, clear=True):
        assert user.check_rate_limit('u1') is False
        assert user.check_rate_limit('u1') is True
        assert user.check_rate_limit('u2') is False
