"""Tests for the engine switch in server/routers/chat.py — plan step 3.

Coverage (engines mocked):
  - /api/chat/ws calls stream_chat, never stream_chat_vsi
  - /api/chat-vsi/ws calls stream_chat_vsi with the requested division
  - VSI route: the 'done' payload carries ⟦n⟧ markers and numbered sources (same post-processing as the KA)
  - VSI route: same access control (can_chat) as the KA route
  - CHAT_VSI_ENABLED=false -> error event, socket closed
  - a VSI engine error is relayed to the browser and the turn is saved with status=error
  - VSI turns are saved with endpoint_name = vsi-<division>
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


def _talk(client, path, division='ALL', caps=None, pool=None, ka=None, vsi=None, vsi_enabled=True):
    """One WebSocket turn with everything external mocked; returns (messages, mocks)."""
    ka = ka or MagicMock(side_effect=_engine_stream(*OK_EVENTS))
    vsi = vsi or MagicMock(side_effect=_engine_stream(*OK_EVENTS))
    save_turn = AsyncMock(return_value=42)
    with (
        patch('server.routers.chat.CHAT_ENABLED', True),
        patch('server.routers.chat.CHAT_VSI_ENABLED', vsi_enabled),
        patch('server.routers.chat.CHAT_ENDPOINT', 'ka-test-endpoint'),
        patch('server.routers.chat.TRANSLATE_BRIDGE_ENABLED', False),
        patch('server.routers.chat.stream_chat', ka),
        patch('server.routers.chat.stream_chat_vsi', vsi),
        patch('server.routers.chat.get_capabilities', new=AsyncMock(return_value=caps or {'can_chat': True})),
        patch('server.routers.chat.get_user_identity', new=AsyncMock(
            return_value={'user_id': 'u1', 'workspace_id': None, 'email': 'u1@example.com'})),
        patch('server.routers.chat._get_chat_credentials', return_value=('https://host', 'tok')),
        patch('server.routers.chat.get_pool', return_value=pool),
        patch('server.routers.chat._save_turn', save_turn),
        patch('server.routers.chat.augment_sources', side_effect=lambda content, sources: sources),
    ):
        received = []
        with client.websocket_connect(path) as ws:
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
    return received, {'ka': ka, 'vsi': vsi, 'save_turn': save_turn}


def test_ka_route_uses_the_ka_only(client):
    received, mocks = _talk(client, '/api/chat/ws')
    assert received[-1]['type'] == 'done'
    mocks['ka'].assert_called_once()
    mocks['vsi'].assert_not_called()
    assert mocks['ka'].call_args.args[2] == 'ka-test-endpoint'


def test_vsi_route_uses_the_vsi_engine_with_the_division(client):
    received, mocks = _talk(client, '/api/chat-vsi/ws', division='as')
    assert received[-1]['type'] == 'done'
    mocks['ka'].assert_not_called()
    mocks['vsi'].assert_called_once()
    host, token, division, messages = mocks['vsi'].call_args.args
    assert (host, token, division) == ('https://host', 'tok', 'AS')
    assert messages[-1]['content'].startswith('[Date: ')      # same _with_today_date as the KA path


def test_vsi_route_done_has_markers_and_numbered_sources(client):
    received, _ = _talk(client, '/api/chat-vsi/ws')
    deltas = [m['delta'] for m in received if m['type'] == 'delta']
    done = received[-1]
    assert deltas == ['QP-1518 est la référence.']
    assert done['content'] == 'QP-1518 est la référence.⟦1⟧'
    assert done['sources'] == [{'title': 'QP-1518', 'url': 'https://intraqual/QP-1518', 'n': 1}]


def test_vsi_route_applies_chat_access_control(client):
    received, mocks = _talk(client, '/api/chat-vsi/ws', caps={'can_chat': False})
    assert received == [{'type': 'error', 'error': 'Chat access not granted'}]
    mocks['vsi'].assert_not_called()


def test_vsi_disabled_gives_error_and_closes(client):
    received, mocks = _talk(client, '/api/chat-vsi/ws', vsi_enabled=False)
    assert received == [{'type': 'error', 'error': 'Chat VSI is disabled'}]
    mocks['vsi'].assert_not_called()


def test_vsi_disabled_does_not_affect_the_ka_route(client):
    received, mocks = _talk(client, '/api/chat/ws', vsi_enabled=False)
    assert received[-1]['type'] == 'done'
    mocks['ka'].assert_called_once()


def test_vsi_engine_error_is_relayed_and_saved_as_error(client):
    vsi = MagicMock(side_effect=_engine_stream(
        json.dumps({'type': 'error', 'error': 'Document search failed (Vector Search returned 403).',
                    'error_type': 'VectorSearchError', 'http_status': 403}),
        'data: [DONE]\n\n'))
    received, mocks = _talk(client, '/api/chat-vsi/ws', pool=MagicMock(), vsi=vsi)
    assert received[-1] == {'type': 'error', 'error': 'Document search failed (Vector Search returned 403).'}
    mocks['save_turn'].assert_awaited_once()
    assert mocks['save_turn'].await_args.kwargs['status'] == 'error'
    assert mocks['save_turn'].await_args.kwargs['endpoint_name'] == 'vsi-all'


@pytest.mark.parametrize('division,label', [('ALL', 'vsi-all'), ('AS', 'vsi-as'), ('IS', 'vsi-is'), ('XX', 'vsi-all')])
def test_vsi_turn_saved_with_vsi_endpoint_name(client, division, label):
    _, mocks = _talk(client, '/api/chat-vsi/ws', division=division, pool=MagicMock())
    mocks['save_turn'].assert_awaited_once()
    kwargs = mocks['save_turn'].await_args.kwargs
    assert kwargs['endpoint_name'] == label
    assert kwargs['status'] == 'ok'
    assert mocks['save_turn'].await_args.args[7] == [{'title': 'QP-1518', 'url': 'https://intraqual/QP-1518', 'n': 1}]


@pytest.mark.parametrize('path', ['/api/chat/ws', '/api/chat-vsi/ws'])
def test_stream_ending_without_text_gives_an_error_not_silence(client, path):
    empty = MagicMock(side_effect=_engine_stream('data: [DONE]\n\n'))
    received, mocks = _talk(client, path, pool=MagicMock(), ka=empty, vsi=empty)
    assert received == [{'type': 'error', 'error': 'No answer was produced. Please try again.'}]
    assert mocks['save_turn'].await_args.kwargs['status'] == 'error'
