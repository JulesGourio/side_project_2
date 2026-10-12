"""Tests for the MLflow trace of a chat turn (server/services/mlflow_turns.py).

Coverage:
  - off without CHAT_MLFLOW_EXPERIMENT, or when the experiment does not exist
  - the span tree: steps nested as documented, at their recorded offsets and durations
  - the retriever span returns the passages sent, in prompt order; dropped ones are only counted
  - tags, session and hashed user on the root; failed turn -> ERROR; no raw user id leaves the app
"""

import asyncio
from types import SimpleNamespace

import pytest

pytest.importorskip('mlflow')

from server.services import mlflow_turns
from server.services.turn_log import TurnLog


class _Span:
    def __init__(self, name, parent, kwargs, spans):
        self.name, self.parent, self.kwargs, self.ended = name, parent, kwargs, None
        spans.append(self)

    def end(self, outputs=None, attributes=None, status=None, end_time_ns=None):
        self.ended = {'outputs': outputs, 'attributes': attributes or {}, 'status': status, 'end': end_time_ns}


@pytest.fixture
def spans(monkeypatch):
    import mlflow
    recorded = []
    monkeypatch.setattr(mlflow, 'start_span_no_context',
                        lambda name, **kw: _Span(name, kw.get('parent_span'), kw, recorded))
    monkeypatch.setattr(mlflow_turns, '_experiment', {'chat': '7'})
    return recorded


def _turn(status='ok'):
    log = TurnLog()
    log.started_at_ns = 1_000_000_000
    log.data.update({'question': 'Q ?', 'division': 'AS', 'session_id': 's1', 'user_id': 'jane@example.com',
                     'fr_query': 'q', 'en_query': 'q en', 'rewrite_ok': True, 'rewrite_endpoint': 'luna',
                     'input_tokens': 100, 'output_tokens': 50, 'answer_endpoint': 'luna',
                     'llm_attempts': [{'endpoint': 'a', 'outcome': 'rate', 'status': 429, 's': 1.0},
                                      {'endpoint': 'b', 'outcome': 'ok', 's': 2.0}]})
    log.timings_ms.update({'retrieval': 800, 'rewrite': 300, 'search': 400, 'queue_wait': 50, 'generation': 3000,
                           'first_token': 1200, 'persist': 20, 'total': 4000})
    log.starts_ms.update({'retrieval': 10, 'rewrite': 10, 'search': 320, 'answer': 900, 'persist': 3950})
    log.passages = [
        {'kept': True, 'prompt_rank': 1, 'ref': 'B-2', 'chunk_text': 'second', 'doc_number': 2},
        {'kept': True, 'prompt_rank': 0, 'ref': 'A-1', 'chunk_text': 'first', 'doc_number': 1, 'cited': True},
        {'kept': False, 'drop_reason': 'over_cap', 'ref': 'C-3', 'chunk_text': 'dropped'}]
    log.answer = 'The answer'
    log.status = status
    return log


def _by_name(spans):
    return {s.name: s for s in spans}


def test_off_without_experiment_or_when_missing(monkeypatch):
    monkeypatch.delenv('CHAT_MLFLOW_EXPERIMENT', raising=False)
    mlflow_turns.export_turn(_turn())                       # no loop, no env: nothing happens
    monkeypatch.setattr(mlflow_turns, '_experiment', {'chat': None})
    assert mlflow_turns._experiment_id('chat') is None
    mlflow_turns._write_trace(_turn(), 'chat')              # missing experiment: silent no-op


def test_export_never_raises(monkeypatch):
    monkeypatch.setenv('CHAT_MLFLOW_EXPERIMENT', 'chat')

    def boom(log, name):
        raise RuntimeError('mlflow down')
    monkeypatch.setattr(mlflow_turns, '_write_trace', boom)

    async def run():
        mlflow_turns.export_turn(_turn())
        await asyncio.gather(*list(mlflow_turns._pending))
    asyncio.run(run())


def test_span_tree_and_timings(spans):
    mlflow_turns._write_trace(_turn(), 'chat')
    s = _by_name(spans)
    assert [x.name for x in spans if x.parent is None] == ['chat_turn']
    for name, parent in (('retrieval', 'chat_turn'), ('answer', 'chat_turn'), ('persist', 'chat_turn'),
                         ('rewrite', 'retrieval'), ('search', 'retrieval'),
                         ('queue_wait', 'answer'), ('call_1', 'answer'), ('call_2', 'answer')):
        assert s[name].parent is s[parent], name
    assert 'translate_in' not in s and 'ref_lookup' not in s            # steps that did not run
    t0 = 1_000_000_000
    assert s['search'].kwargs['start_time_ns'] == t0 + 320_000_000
    assert s['search'].ended['end'] == t0 + 720_000_000
    assert s['answer'].ended['end'] == t0 + (900 + 50 + 3000) * 1_000_000
    assert s['call_1'].ended['status'] == 'ERROR' and s['call_2'].ended['status'] == 'OK'
    assert s['call_2'].kwargs['start_time_ns'] == s['call_1'].ended['end']
    assert s['chat_turn'].ended['end'] == t0 + 4000 * 1_000_000
    assert s['answer'].ended['attributes']['mlflow.chat.tokenUsage'] == {
        'input_tokens': 100, 'output_tokens': 50, 'total_tokens': 150}


def test_retriever_returns_the_passages_sent_in_prompt_order(spans):
    mlflow_turns._write_trace(_turn(), 'chat')
    retrieval = _by_name(spans)['retrieval']
    docs = retrieval.ended['outputs']
    assert [d.metadata['ref'] for d in docs] == ['A-1', 'B-2']
    assert docs[0].page_content == 'first' and docs[0].metadata['cited'] is True
    assert '"over_cap"' in retrieval.ended['attributes']['dropped']


def test_root_tags_user_hash_and_status(spans):
    mlflow_turns._write_trace(_turn('error'), 'chat')
    root = _by_name(spans)['chat_turn']
    assert root.kwargs['tags']['status'] == 'error' and root.kwargs['tags']['division'] == 'AS'
    assert root.kwargs['metadata']['mlflow.trace.session'] == 's1'
    assert 'jane' not in root.kwargs['metadata']['mlflow.trace.user']
    assert root.ended['status'] == 'ERROR' and root.ended['outputs'] == 'The answer'
