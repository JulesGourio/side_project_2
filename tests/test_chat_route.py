"""Tests for the chat WebSocket route (server/routers/chat.py), engine mocked.

Coverage:
  - /api/chat/ws calls stream_chat_vsi with the requested division and today's date
  - the 'done' payload carries ⟦n⟧ markers and numbered sources
  - access control (can_chat)
  - CHAT_ENABLED=false -> error event, socket closed
  - an engine error is relayed to the browser and the turn is saved with status=error
  - turns are saved with endpoint_name = vsi-<division>
  - a stream ending without text gives an error, not silence
  - every turn is logged once (store_chat_turn) with its outcome, message ids and trace_id;
    a browser that leaves mid-answer gives an 'aborted' turn
"""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect


@pytest.fixture(scope='module')
def client():
    with (
        patch('server.app.init_lakebase', new_callable=AsyncMock),
        patch('server.app.shutdown_lakebase', new_callable=AsyncMock),
    ):
        from server.app import app
        with TestClient(app, raise_server_exceptions=True) as c:
            yield c


def _engine_stream(*events):
    async def _gen(*args, **kwargs):
        for e in events:
            yield e if e.startswith((': ', 'data: [DONE]')) else f'data: {e}\n\n'
    return _gen


OK_EVENTS = (
    json.dumps({'type': 'response.output_text.delta', 'delta': 'QP-1518 est la référence.'}),
    json.dumps({'type': 'sources', 'sources': [{'title': 'QP-1518', 'url': 'https://intraqual/QP-1518'}],
                'citations': [{'n': 1, 'pos': 25}]}),
    json.dumps({'type': 'metadata', 'trace_id': 'vsi-abc', 'tool_name': 'vector_search'}),
    'data: [DONE]\n\n',
)


def _talk(client, division='ALL', caps=None, pool=None, engine=None, enabled=True):
    """One WebSocket turn with everything external mocked; returns (messages, mocks)."""
    engine = engine or MagicMock(side_effect=_engine_stream(*OK_EVENTS))
    save_turn = AsyncMock(return_value=(41, 42))
    store_turn = AsyncMock(return_value=7)
    with (
        patch('server.routers.chat.CHAT_ENABLED', enabled),
        patch('server.routers.chat.TRANSLATE_BRIDGE_ENABLED', False),
        patch('server.routers.chat.stream_chat_vsi', engine),
        patch('server.routers.chat.get_capabilities', new=AsyncMock(return_value=caps or {'can_chat': True})),
        patch('server.routers.chat.get_user_identity', new=AsyncMock(
            return_value={'user_id': 'u1', 'workspace_id': None, 'email': 'u1@example.com'})),
        patch('server.routers.chat._get_chat_credentials', return_value=('https://host', 'tok')),
        patch('server.routers.chat.get_pool', return_value=pool),
        patch('server.routers.chat._save_turn', save_turn),
        patch('server.routers.chat.store_chat_turn', store_turn),
        patch('server.routers.chat.augment_sources', side_effect=lambda content, sources: sources),
    ):
        received = []
        with client.websocket_connect('/api/chat/ws') as ws:
            ws.send_json({'messages': [{'role': 'user', 'content': 'Qui fait la qualification CND ?'}],
                          'session_id': 's1', 'division': division})
            try:
                while True:
                    m = ws.receive_json()
                    received.append(m)
                    if m['type'] in ('done', 'error'):
                        break
            except WebSocketDisconnect:
                pass
    return received, {'engine': engine, 'save_turn': save_turn, 'store_turn': store_turn}


def test_route_calls_the_engine_with_the_division(client):
    received, mocks = _talk(client, division='as')
    assert received[-1]['type'] == 'done'
    mocks['engine'].assert_called_once()
    host, token, division, messages = mocks['engine'].call_args.args
    assert (host, token, division) == ('https://host', 'tok', 'AS')
    assert messages[-1]['content'].startswith('[Date: ')


def test_done_has_markers_and_numbered_sources(client):
    received, _ = _talk(client)
    deltas = [m['delta'] for m in received if m['type'] == 'delta']
    done = received[-1]
    assert deltas == ['QP-1518 est la référence.']
    assert done['content'] == 'QP-1518 est la référence.⟦1⟧'
    assert done['sources'] == [{'title': 'QP-1518', 'url': 'https://intraqual/QP-1518', 'n': 1}]


def test_route_applies_chat_access_control(client):
    received, mocks = _talk(client, caps={'can_chat': False})
    assert received == [{'type': 'error', 'error': 'Chat access not granted'}]
    mocks['engine'].assert_not_called()


def test_disabled_gives_error_and_closes(client):
    received, mocks = _talk(client, enabled=False)
    assert received == [{'type': 'error', 'error': 'Chat is disabled'}]
    mocks['engine'].assert_not_called()


def test_engine_error_is_relayed_and_saved_as_error(client):
    engine = MagicMock(side_effect=_engine_stream(
        json.dumps({'type': 'error', 'error': 'Document search failed (Vector Search returned 403).',
                    'error_type': 'VectorSearchError', 'http_status': 403}),
        'data: [DONE]\n\n'))
    received, mocks = _talk(client, pool=MagicMock(), engine=engine)
    assert received[-1] == {'type': 'error', 'error': 'Document search failed (Vector Search returned 403).'}
    mocks['save_turn'].assert_awaited_once()
    assert mocks['save_turn'].await_args.kwargs['status'] == 'error'
    assert mocks['save_turn'].await_args.kwargs['endpoint_name'] == 'vsi-all'


@pytest.mark.parametrize('division,label', [('ALL', 'vsi-all'), ('AS', 'vsi-as'), ('IS', 'vsi-is'), ('XX', 'vsi-all')])
def test_turn_saved_with_division_endpoint_name(client, division, label):
    _, mocks = _talk(client, division=division, pool=MagicMock())
    mocks['save_turn'].assert_awaited_once()
    kwargs = mocks['save_turn'].await_args.kwargs
    assert kwargs['endpoint_name'] == label
    assert kwargs['status'] == 'ok'
    assert mocks['save_turn'].await_args.args[7] == [{'title': 'QP-1518', 'url': 'https://intraqual/QP-1518', 'n': 1}]


def test_stream_ending_without_text_gives_an_error_not_silence(client):
    empty = MagicMock(side_effect=_engine_stream('data: [DONE]\n\n'))
    received, mocks = _talk(client, pool=MagicMock(), engine=empty)
    assert received == [{'type': 'error', 'error': 'No answer was produced. Please try again.'}]
    assert mocks['save_turn'].await_args.kwargs['status'] == 'error'


def _logged_turn(mocks):
    mocks['store_turn'].assert_awaited_once()
    return mocks['store_turn'].await_args


def test_answered_turn_is_logged_with_its_messages(client):
    received, mocks = _talk(client, division='as', pool=MagicMock())
    assert received[-1]['type'] == 'done' and received[-1]['message_id'] == 42
    call = _logged_turn(mocks)
    log = call.args[0]
    assert log.status == 'ok' and call.kwargs == {'endpoint': '/api/chat/ws', 'user_message_id': 41,
                                                  'assistant_message_id': 42}
    assert mocks['engine'].call_args.kwargs['log'] is log            # the engine fills the same log
    assert mocks['save_turn'].await_args.args[8] == log.trace_id     # chat_messages.trace_id = join key
    assert log.data['division'] == 'AS' and log.data['question'] == 'Qui fait la qualification CND ?'
    assert log.data['sources_count'] == 1 and 'persist' in log.timings_ms and 'total' in log.timings_ms


def test_engine_error_turn_is_logged_as_error(client):
    engine = MagicMock(side_effect=_engine_stream(
        json.dumps({'type': 'error', 'error': 'busy', 'error_type': 'LLMUnavailable', 'http_status': 429}),
        'data: [DONE]\n\n'))
    _, mocks = _talk(client, pool=MagicMock(), engine=engine)
    log = _logged_turn(mocks).args[0]
    assert log.status == 'error'
    assert log.error == {'stage': 'engine', 'type': 'LLMUnavailable', 'message': 'busy'}


def test_browser_leaving_mid_answer_is_an_aborted_turn(client):
    async def _slow(*args, **kwargs):
        yield f"data: {json.dumps({'type': 'response.output_text.delta', 'delta': 'début'})}\n\n"
        raise WebSocketDisconnect(1001)
    _, mocks = _talk(client, pool=MagicMock(), engine=MagicMock(side_effect=_slow))
    log = _logged_turn(mocks).args[0]
    assert log.status == 'aborted' and log.codes == ['client_disconnected']
    assert mocks['save_turn'].await_args.kwargs['status'] == 'aborted'
    assert mocks['save_turn'].await_args.args[6] == 'début'
