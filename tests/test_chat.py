"""Tests for chat endpoints — session logs and feedback (the chat turn itself: test_chat_route.py).

Coverage:
  - GET  /api/chat/sessions        : no DB / with rows
  - GET  /api/chat/sessions/{id}   : not found / messages + sources
  - DELETE /api/chat/sessions/{id} : success
  - POST /api/chat/feedback        : invalid vote / no DB / up / down
"""

import json
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient


# ---------------------------------------------------------------------------
# App fixture — mock lakebase so tests don't need a real DB at startup
# ---------------------------------------------------------------------------

@pytest.fixture(scope='module')
def client():
    with (
        patch('server.app.init_lakebase', new_callable=AsyncMock),
        patch('server.app.shutdown_lakebase', new_callable=AsyncMock),
    ):
        from server.app import app
        with TestClient(app, raise_server_exceptions=True) as c:
            yield c


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_pool(conn):
    """Build a minimal asyncpg pool mock from a connection mock."""
    pool = MagicMock()
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)
    return pool


# ---------------------------------------------------------------------------
# Session / log tests
# ---------------------------------------------------------------------------

def test_list_sessions_no_db_returns_empty(client):
    with patch('server.routers.chat.get_pool', return_value=None):
        r = client.get('/api/chat/sessions')
    assert r.status_code == 200
    data = r.json()
    assert data['sessions'] == []
    assert data['available'] is False


def test_list_sessions_returns_rows(client):
    conn = AsyncMock()
    conn.fetch.return_value = [{
        'id': 'sess-abc',
        'name': 'First question',
        'created_at': datetime(2026, 1, 15, 10, 0),
        'updated_at': datetime(2026, 1, 15, 10, 5),
        'message_count': 4,
    }]
    with (
        patch('server.routers.chat.get_pool', return_value=_make_pool(conn)),
        patch('server.routers.chat.get_user_identity', new=AsyncMock(
            return_value={'user_id': 'u1', 'workspace_id': None})),
    ):
        r = client.get('/api/chat/sessions')
    assert r.status_code == 200
    data = r.json()
    assert data['available'] is True
    assert len(data['sessions']) == 1
    s = data['sessions'][0]
    assert s['id'] == 'sess-abc'
    assert s['message_count'] == 4


def test_get_session_not_found(client):
    conn = AsyncMock()
    conn.fetchrow.return_value = None
    with patch('server.routers.chat.get_pool', return_value=_make_pool(conn)):
        r = client.get('/api/chat/sessions/no-such-id')
    assert r.status_code == 404


def test_get_session_scoped_to_caller(client):
    """A session lookup is always filtered by the caller's own user_id — a
    session_id alone must never be enough to read someone else's conversation."""
    conn = AsyncMock()
    conn.fetchrow.return_value = {
        'id': 'sess-1', 'name': 'NDT query', 'created_at': datetime(2026, 1, 1),
    }
    conn.fetch.return_value = []
    with (
        patch('server.routers.chat.get_pool', return_value=_make_pool(conn)),
        patch('server.routers.chat.get_user_identity', new=AsyncMock(
            return_value={'user_id': 'u1', 'workspace_id': None, 'email': None})),
    ):
        r = client.get('/api/chat/sessions/sess-1')
    assert r.status_code == 200
    args = conn.fetchrow.await_args.args
    assert 'AND user_id = $2' in args[0]
    assert args[1:] == ('sess-1', 'u1')


def test_get_session_returns_messages_with_sources(client):
    """Assistant sources come from the sources_json blob on the message row."""
    conn = AsyncMock()
    conn.fetchrow.return_value = {
        'id': 'sess-1', 'name': 'NDT query', 'created_at': datetime(2026, 1, 1),
    }
    conn.fetch.return_value = [   # messages
        {'id': 1, 'role': 'user', 'content': 'Explain NDT',
         'created_at': datetime(2026, 1, 1), 'sources_json': None},
        {'id': 2, 'role': 'assistant', 'content': 'NDT stands for…',
         'created_at': datetime(2026, 1, 1),
         'sources_json': json.dumps([{'rank': 0, 'title': 'NAS 410', 'url': 'https://docs/nas410'}])},
    ]
    with patch('server.routers.chat.get_pool', return_value=_make_pool(conn)):
        r = client.get('/api/chat/sessions/sess-1')
    assert r.status_code == 200
    data = r.json()
    assert data['id'] == 'sess-1'
    assert len(data['messages']) == 2
    assistant = next(m for m in data['messages'] if m['role'] == 'assistant')
    assert assistant['sources'][0]['title'] == 'NAS 410'
    assert assistant['sources'][0]['url'] == 'https://docs/nas410'


def test_delete_session_soft_deletes_messages_and_session(client):
    conn = AsyncMock()
    conn.fetchval.return_value = 1  # session owned by the caller
    with patch('server.routers.chat.get_pool', return_value=_make_pool(conn)):
        r = client.delete('/api/chat/sessions/sess-1')
    assert r.status_code == 200
    assert r.json()['success'] is True
    # Two UPDATE … SET deleted = TRUE statements: chat_messages + chat_sessions.
    # Rows are kept for audit, never physically removed.
    assert conn.execute.call_count == 2
    statements = ' '.join(str(c.args[0]) for c in conn.execute.await_args_list)
    assert 'UPDATE chat_messages' in statements
    assert 'UPDATE chat_sessions' in statements
    assert 'DELETE' not in statements


def test_delete_session_not_owner_returns_404(client):
    """A session_id that isn't the caller's own must never be deletable."""
    conn = AsyncMock()
    conn.fetchval.return_value = None
    with patch('server.routers.chat.get_pool', return_value=_make_pool(conn)):
        r = client.delete('/api/chat/sessions/sess-1')
    assert r.status_code == 404
    conn.execute.assert_not_called()


# ---------------------------------------------------------------------------
# Share / shared-view / duplicate tests
# ---------------------------------------------------------------------------

def test_share_session_generates_token_for_first_share(client):
    conn = AsyncMock()
    conn.fetchrow.return_value = {'share_token': None}
    with (
        patch('server.routers.chat.get_pool', return_value=_make_pool(conn)),
        patch('server.routers.chat.get_user_identity', new=AsyncMock(
            return_value={'user_id': 'u1', 'workspace_id': None, 'email': None})),
    ):
        r = client.post('/api/chat/sessions/sess-1/share')
    assert r.status_code == 200
    token = r.json()['share_token']
    assert token
    # The generated token is persisted via UPDATE chat_sessions SET share_token = ...
    conn.execute.assert_awaited_once()
    assert 'UPDATE chat_sessions' in conn.execute.await_args.args[0]
    assert conn.execute.await_args.args[1:] == ('sess-1', token)


def test_share_session_is_idempotent(client):
    """Re-sharing an already-shared session returns the existing token unchanged."""
    conn = AsyncMock()
    conn.fetchrow.return_value = {'share_token': 'already-shared-token'}
    with (
        patch('server.routers.chat.get_pool', return_value=_make_pool(conn)),
        patch('server.routers.chat.get_user_identity', new=AsyncMock(
            return_value={'user_id': 'u1', 'workspace_id': None, 'email': None})),
    ):
        r = client.post('/api/chat/sessions/sess-1/share')
    assert r.status_code == 200
    assert r.json()['share_token'] == 'already-shared-token'
    conn.execute.assert_not_called()


def test_share_session_not_owner_returns_404(client):
    conn = AsyncMock()
    conn.fetchrow.return_value = None
    with (
        patch('server.routers.chat.get_pool', return_value=_make_pool(conn)),
        patch('server.routers.chat.get_user_identity', new=AsyncMock(
            return_value={'user_id': 'someone-else', 'workspace_id': None, 'email': None})),
    ):
        r = client.post('/api/chat/sessions/sess-1/share')
    assert r.status_code == 404


def test_get_shared_session_not_found(client):
    conn = AsyncMock()
    conn.fetchrow.return_value = None
    with patch('server.routers.chat.get_pool', return_value=_make_pool(conn)):
        r = client.get('/api/chat/shared/no-such-token')
    assert r.status_code == 404


def test_get_shared_session_returns_messages_without_ownership_check(client):
    """Anyone with the token sees the conversation — no identity lookup involved."""
    conn = AsyncMock()
    conn.fetchrow.return_value = {'id': 'sess-1', 'name': 'NDT query', 'created_at': datetime(2026, 1, 1)}
    conn.fetch.return_value = [
        {'id': 1, 'role': 'user', 'content': 'Explain NDT',
         'created_at': datetime(2026, 1, 1), 'sources_json': None,
         'feedback_vote': None, 'feedback_comment': None},
        {'id': 2, 'role': 'assistant', 'content': 'NDT stands for…',
         'created_at': datetime(2026, 1, 1),
         'sources_json': json.dumps([{'rank': 0, 'title': 'NAS 410', 'url': 'https://docs/nas410'}]),
         'feedback_vote': 'up', 'feedback_comment': 'Spot on'},
    ]
    with patch('server.routers.chat.get_pool', return_value=_make_pool(conn)):
        r = client.get('/api/chat/shared/tok-abc')
    assert r.status_code == 200
    data = r.json()
    assert data['id'] == 'sess-1'
    assert len(data['messages']) == 2
    assert data['messages'][0]['feedback'] is None
    assert data['messages'][1]['feedback'] == {'vote': 'up', 'comment': 'Spot on'}


def test_duplicate_shared_session_copies_messages(client):
    conn = AsyncMock()
    conn.fetchrow.return_value = {'id': 'sess-1', 'name': 'NDT query'}
    conn.fetch.return_value = [
        {'role': 'user', 'content': 'Explain NDT', 'sources_json': None},
        {'role': 'assistant', 'content': 'NDT stands for…', 'sources_json': None},
    ]
    with (
        patch('server.routers.chat.get_pool', return_value=_make_pool(conn)),
        patch('server.routers.chat.get_user_identity', new=AsyncMock(
            return_value={'user_id': 'viewer', 'workspace_id': None, 'email': None})),
    ):
        r = client.post('/api/chat/shared/tok-abc/duplicate')
    assert r.status_code == 200
    new_id = r.json()['session_id']
    assert new_id and new_id != 'sess-1'
    statements = ' '.join(str(c.args[0]) for c in conn.execute.await_args_list)
    # One upsert_user, one _upsert_session, two chat_messages inserts (one per copied turn)
    assert statements.count('INSERT INTO chat_messages') == 2
    assert 'INSERT INTO chat_sessions' in statements
    # Copied messages belong to the viewer, not the original session owner
    insert_calls = [c for c in conn.execute.await_args_list if 'INSERT INTO chat_messages' in str(c.args[0])]
    assert all(c.args[1] == new_id and c.args[2] == 'viewer' for c in insert_calls)


def test_duplicate_shared_session_not_found(client):
    conn = AsyncMock()
    conn.fetchrow.return_value = None
    with (
        patch('server.routers.chat.get_pool', return_value=_make_pool(conn)),
        patch('server.routers.chat.get_user_identity', new=AsyncMock(
            return_value={'user_id': 'viewer', 'workspace_id': None, 'email': None})),
    ):
        r = client.post('/api/chat/shared/no-such-token/duplicate')
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Feedback tests
# ---------------------------------------------------------------------------

def test_feedback_rejects_invalid_vote(client):
    r = client.post('/api/chat/feedback', json={'vote': 'meh'})
    assert r.status_code == 422


def test_feedback_no_db_returns_503(client):
    with patch('server.routers.chat.get_pool', return_value=None):
        r = client.post('/api/chat/feedback', json={'vote': 'up', 'message_id': 1, 'session_id': 'sess-1'})
    assert r.status_code == 503


def test_feedback_up_saved(client):
    conn = AsyncMock()
    conn.fetchrow.return_value = {'id': 42, 'vote': 'up'}
    with (
        patch('server.routers.chat.get_pool', return_value=_make_pool(conn)),
        patch('server.routers.chat.get_user_identity', new=AsyncMock(
            return_value={'user_id': 'u1', 'workspace_id': None})),
        patch('server.routers.chat.get_workspace_url', return_value='https://test.azuredatabricks.net'),
    ):
        r = client.post('/api/chat/feedback', json={
            'vote': 'up', 'message_id': 1, 'session_id': 'sess-1',
        })
    assert r.status_code == 200
    assert r.json() == {'success': True, 'id': 42, 'vote': 'up'}


def test_feedback_down_with_comment(client):
    conn = AsyncMock()
    conn.fetchrow.return_value = {'id': 43, 'vote': 'down'}
    with (
        patch('server.routers.chat.get_pool', return_value=_make_pool(conn)),
        patch('server.routers.chat.get_user_identity', new=AsyncMock(
            return_value={'user_id': 'u1', 'workspace_id': None})),
        patch('server.routers.chat.get_workspace_url', return_value='https://test.azuredatabricks.net'),
    ):
        r = client.post('/api/chat/feedback', json={
            'vote': 'down', 'message_id': 2, 'session_id': 'sess-1', 'comment': 'Not relevant',
        })
    assert r.status_code == 200
    assert r.json()['vote'] == 'down'


# ---------------------------------------------------------------------------
# Division directive must not leak into thread titles
# ---------------------------------------------------------------------------

def test_strip_division_removes_directive():
    from server.routers.chat import _strip_division
    prefixed = (
        '[Division: AS] The user works in the AS (Aerostructures) division. '
        'Restrict your search.\n\nWhat changed in the bonding spec?'
    )
    assert _strip_division(prefixed) == 'What changed in the bonding spec?'
    # ALL / un-prefixed questions are left untouched
    assert _strip_division('Plain question?') == 'Plain question?'


def test_detect_division():
    from server.routers.chat import _detect_division
    assert _detect_division('[Division: AS] …\n\nQ?') == 'AS'
    assert _detect_division('[Division: IS] …\n\nQ?') == 'IS'
    assert _detect_division('Plain question, no scope') == 'ALL'
    assert _detect_division('') == 'ALL'


def test_save_turn_strips_division_and_registers_user():
    """Thread name is derived from the clean question and the user is upserted."""
    import asyncio

    from server.routers import chat

    conn = AsyncMock()
    conn.fetchval.side_effect = [98, 99]          # user message id, assistant message id
    pool = _make_pool(conn)

    with patch('server.routers.chat.upsert_user', new=AsyncMock()) as mock_upsert:
        ids = asyncio.run(chat._save_turn(
            pool, 'sess-1', 'u1', 'ws1', 'https://test',
            '[Division: IS] The user works in the IS division. Restrict.\n\nHow do I wire it?',
            'Here is how.',
            trace_id='vsi-abc',
            email='jules@latecoere.aero',
            endpoint_name='vsi-is',
        ))

    assert ids == (98, 99)
    # User registered in the shared table during the chat flow
    mock_upsert.assert_awaited_once()
    assert mock_upsert.await_args.kwargs['email'] == 'jules@latecoere.aero'

    # Session upsert (first conn.execute) receives the stripped, truncated name
    # Arg order: …, name, share_token (token is generated fresh on every call).
    first_execute_args = conn.execute.await_args_list[0].args
    session_name = first_execute_args[-2]
    assert session_name == 'How do I wire it?'
    assert 'Division' not in session_name

    # Both rows record the division scope ('IS' here) and the turn's trace_id (join key
    # with chat_turns). Arg order: …, division, question_lang.
    user_insert, assistant_insert = (c.args for c in conn.fetchval.await_args_list)
    assert user_insert[-2] == 'IS' and assistant_insert[-2] == 'IS'
    assert 'vsi-abc' in user_insert and 'vsi-abc' in assistant_insert
    # The Knowledge Assistant columns are no longer written.
    assert 'tool_name' not in assistant_insert[0] and 'reasoning_steps' not in assistant_insert[0]


def test_save_turn_records_error_status():
    """A failed turn is persisted with status='error' and the failure reason."""
    import asyncio

    from server.routers import chat

    conn = AsyncMock()
    conn.fetchval.side_effect = [4, 5]
    pool = _make_pool(conn)

    with patch('server.routers.chat.upsert_user', new=AsyncMock()):
        asyncio.run(chat._save_turn(
            pool, 'sess', 'u1', None, 'https://t',
            'why does it fail?', '',  # no assistant text — the turn crashed
            status='error', error_msg='Agent failure',
        ))

    user_insert, assistant_insert = (c.args for c in conn.fetchval.await_args_list)
    # The assistant INSERT ends with …, status, error_msg, division, question_lang
    assert assistant_insert[-4] == 'error'          # status
    assert assistant_insert[-3] == 'Agent failure'  # error_msg
    assert assistant_insert[-2] == 'ALL'            # division (no prefix in this question)

    # The user message row is also flagged 'error' so the whole turn is hidden
    # on reload (get_session filters status='ok') while staying in the DB.
    # User INSERT ends with …, status, division, question_lang.
    assert user_insert[-3] == 'error'
    assert user_insert[-2] == 'ALL'


def test_save_turn_failure_is_a_persist_issue_of_the_turn():
    import asyncio

    from server.routers import chat
    from server.services.turn_log import TurnLog

    conn = AsyncMock()
    conn.fetchval.side_effect = RuntimeError('connection lost')
    log = TurnLog()
    with patch('server.routers.chat.upsert_user', new=AsyncMock()):
        ids = asyncio.run(chat._save_turn(_make_pool(conn), 'sess', 'u1', None, 'https://t', 'q', 'a', log=log))
    assert ids == (None, None)
    assert log.codes == ['persist_failed'] and 'connection lost' in log.issues[0]['message']


def test_trim_history_bounds_prompt_and_keeps_latest_question():
    from server.routers.chat import _trim_history, CHAT_MAX_HISTORY

    # 13 alternating messages, last one is the user's newest question
    msgs = [
        {'role': 'user' if i % 2 == 0 else 'assistant', 'content': str(i)}
        for i in range(13)
    ]
    trimmed = _trim_history(msgs)
    assert len(trimmed) <= CHAT_MAX_HISTORY
    assert trimmed[-1] == msgs[-1]          # newest question never dropped
    assert trimmed[0]['role'] == 'user'     # window never opens on an assistant turn
    # A short conversation is returned unchanged
    assert _trim_history(msgs[:3]) == msgs[:3]


# ---------------------------------------------------------------------------
# Feature access control (capabilities + 403 guards)
# ---------------------------------------------------------------------------

def test_get_capabilities_reads_user_flags_from_db():
    """Capabilities come from the Lakebase users table (per-user booleans)."""
    import asyncio
    from types import SimpleNamespace

    from server.services import user as user_mod

    req = SimpleNamespace(headers={'x-forwarded-user': 'jules@latecoere.aero'})

    def run_with_row(row):
        user_mod._caps_cache.clear()
        conn = AsyncMock()
        conn.fetchrow.return_value = row
        pool = _make_pool(conn)
        with patch.object(user_mod.lakebase, 'get_pool', return_value=pool):
            return asyncio.run(user_mod.get_capabilities(req))

    chat_only = run_with_row({'can_chat': True, 'can_compare': False})
    assert chat_only['can_chat'] is True
    assert chat_only['can_compare'] is False

    all_caps = run_with_row({'can_chat': True, 'can_compare': True})
    assert all_caps['can_chat'] and all_caps['can_compare']

    none = run_with_row({'can_chat': False, 'can_compare': False})
    assert not (none['can_chat'] or none['can_compare'])

    user_mod._caps_cache.clear()


def test_get_capabilities_fails_open_without_db_or_row():
    """DB down or user not yet synced → grant all (fail-open by design)."""
    import asyncio
    from types import SimpleNamespace

    from server.services import user as user_mod

    req = SimpleNamespace(headers={'x-forwarded-user': 'new.user@latecoere.aero'})

    user_mod._caps_cache.clear()
    with patch.object(user_mod.lakebase, 'get_pool', return_value=None):
        caps = asyncio.run(user_mod.get_capabilities(req))
    assert caps['can_chat'] and caps['can_compare']

    user_mod._caps_cache.clear()
    conn = AsyncMock()
    conn.fetchrow.return_value = None  # not in the users table yet
    with patch.object(user_mod.lakebase, 'get_pool', return_value=_make_pool(conn)):
        caps = asyncio.run(user_mod.get_capabilities(req))
    assert caps['can_chat'] and caps['can_compare']

    user_mod._caps_cache.clear()


def test_chat_route_returns_403_without_capability(client):
    """The require_chat dependency blocks chat endpoints for non-chat users."""
    with patch('server.services.user.get_capabilities',
               new=AsyncMock(return_value={'can_chat': False, 'can_compare': True, 'groups': []})):
        r = client.get('/api/chat/sessions')
    assert r.status_code == 403
