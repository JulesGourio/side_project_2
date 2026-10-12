"""Tests for the MLflow trace of a chat turn (server/services/chat/mlflow_turns.py).

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

from server.services.chat import mlflow_turns
from server.services.chat.turn_log import TurnLog


class _Span:
    def __init__(self, name, parent, kwargs, spans):
        self.name, self.parent, self.kwargs, self.ended = name, parent, kwargs, None
        self.trace_id = 'tr-fake'
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
    log = _turn()
    asyncio.run(mlflow_turns.export_turn(log))             # env unset: nothing happens
    assert 'mlflow_trace_id' not in log.data
    monkeypatch.setattr(mlflow_turns, '_experiment', {'chat': None})
    assert mlflow_turns._experiment_id('chat') is None
    mlflow_turns._write_trace(_turn(), 'chat')              # missing experiment: silent no-op


def test_export_never_raises(monkeypatch):
    monkeypatch.setenv('CHAT_MLFLOW_EXPERIMENT', 'chat')

    def boom(log, name):
        raise RuntimeError('mlflow down')
    monkeypatch.setattr(mlflow_turns, '_write_trace', boom)
    log = _turn()
    asyncio.run(mlflow_turns.export_turn(log))
    assert 'mlflow_trace_id' not in log.data


def test_export_records_the_trace_id(monkeypatch, spans):
    monkeypatch.setenv('CHAT_MLFLOW_EXPERIMENT', 'chat')
    log = _turn()
    asyncio.run(mlflow_turns.export_turn(log))
    assert log.data['mlflow_trace_id'] == 'tr-fake'


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


def test_tags_for_the_free_scorers(spans, monkeypatch):
    monkeypatch.setattr(mlflow_turns, '_language_guess', lambda text: 'fr' if text else '')
    log = _turn()
    log.data.update({'passages_sent': 2, 'cost_eur': 0.004, 'rewrite_cost_eur': 0.001})
    mlflow_turns._write_trace(log, 'chat')
    tags = _by_name(spans)['chat_turn'].kwargs['tags']
    assert tags['passages_sent'] == '2' and tags['first_token_ms'] == '1200' and tags['total_ms'] == '4000'
    assert tags['cost_eur'] == '0.005' and tags['question_lang_guess'] == 'fr' and tags['answer_lang_guess'] == 'fr'
    assert 'mlflow.linkedPrompts' not in tags                     # no CHAT_PROMPT_SCHEMA


def test_vote_becomes_an_assessment(monkeypatch):
    import mlflow
    monkeypatch.setenv('CHAT_MLFLOW_EXPERIMENT', 'chat')
    seen = {}
    monkeypatch.setattr(mlflow, 'log_feedback', lambda **kw: seen.update(kw))
    mlflow_turns.log_vote('tr-1', 'down', 'wrong number', 'jane@example.com')
    assert seen['trace_id'] == 'tr-1' and seen['name'] == 'user_vote' and seen['value'] is False
    assert seen['rationale'] == 'wrong number' and 'jane' not in seen['source'].source_id
    seen.clear()
    mlflow_turns.log_vote('', 'up')                                # unknown trace: nothing logged
    assert not seen


def test_vote_never_raises(monkeypatch):
    import mlflow
    monkeypatch.setenv('CHAT_MLFLOW_EXPERIMENT', 'chat')

    def boom(**kw):
        raise RuntimeError('down')
    monkeypatch.setattr(mlflow, 'log_feedback', boom)
    mlflow_turns.log_vote('tr-1', 'up')


@pytest.fixture
def registry(tmp_path, monkeypatch):
    """A real prompt registry on a throw-away SQLite store."""
    pytest.importorskip('sqlalchemy')
    pytest.importorskip('alembic')
    import mlflow
    old = mlflow.get_tracking_uri()
    mlflow.set_tracking_uri(f'sqlite:///{tmp_path}/m.db')
    monkeypatch.setattr(mlflow_turns, '_prompt_cache', {})
    yield mlflow
    mlflow.set_tracking_uri(old)


def _register(mlflow, key, text=None, sha=None, alias='deployed'):
    from server.services.chat import prompts
    name = prompts.registry_name('chat_test', key)
    version = mlflow.genai.register_prompt(
        name=name, template=text or prompts.read(key), tags={'sha': sha or prompts.digest(key)})
    mlflow.genai.set_prompt_alias(name, alias, version.version)
    return name, version.version


def test_registered_prompts_are_linked_to_the_trace(registry, spans, monkeypatch):
    import json
    monkeypatch.setenv('CHAT_PROMPT_SCHEMA', 'chat_test')
    name, version = _register(registry, 'system_base')
    _register(registry, 'rewrite')
    log = _turn()
    log.prompts = ['rewrite', 'system_as', 'system_base']           # system_as was never registered
    mlflow_turns._write_trace(log, 'chat')
    linked = json.loads(_by_name(spans)['chat_turn'].kwargs['tags']['mlflow.linkedPrompts'])
    assert {p['name'] for p in linked} == {name, 'chat_test.chat_rewrite'}
    assert {'name': name, 'version': str(version)} in linked


def test_a_prompt_edited_since_registration_is_not_linked(registry, spans, monkeypatch):
    monkeypatch.setenv('CHAT_PROMPT_SCHEMA', 'chat_test')
    _register(registry, 'system_base', text='an older text', sha='0000000000000000')
    log = _turn()
    log.prompts = ['system_base']
    mlflow_turns._write_trace(log, 'chat')
    assert 'mlflow.linkedPrompts' not in _by_name(spans)['chat_turn'].kwargs['tags']
