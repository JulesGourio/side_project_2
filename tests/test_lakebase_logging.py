"""Tests for what the app records in Lakebase about each request (2026-10-09 schema).

Coverage:
  - every key the chat engine and route put in TurnLog.data / timings_ms is a chat_turns column
    (an unknown key would be dropped silently by store_chat_turn)
  - TurnLog outcome: ok / degraded / error / aborted, issue codes, timers
  - store_chat_turn: one transaction, placeholders match the columns, JSONB casts, an errors row
    per warning/error issue only (not for info), passages with the turn's trace_id
  - /compare/impact audit: status, counts and step durations; partial search and failed
    judge calls give 'warning' errors rows; a failed search gives status 'error'
  - /compare/analyze: outcome and step durations on the llm_requests row
"""

import asyncio
import json
import re
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from server.services import lakebase
from server.services.processors.base import ProcessMetadata, ProcessResult
from server.services.turn_log import INFO, TurnLog

_ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# TurnLog <-> chat_turns columns
# ---------------------------------------------------------------------------

def _written_keys(path: str) -> set:
    """Keys written to log.data (update({...}) and data['...'] = …) in a source file."""
    text = (_ROOT / path).read_text(encoding='utf-8')
    keys = set(re.findall(r"log\.data\['(\w+)'\]\s*=", text))
    for block in re.findall(r'log\.data\.update\(\{(.*?)\}\)', text, flags=re.S):
        keys |= set(re.findall(r"'(\w+)':", block))
    return keys


def test_every_turn_log_key_is_a_chat_turns_column():
    columns = {c for c, _ in lakebase.CHAT_TURN_COLUMNS}
    written = _written_keys('server/services/chat_vsi.py') | _written_keys('server/routers/chat.py')
    assert written and written <= columns, written - columns


def test_every_timed_step_is_a_chat_turns_column():
    text = ''.join((_ROOT / p).read_text(encoding='utf-8')
                   for p in ('server/services/chat_vsi.py', 'server/routers/chat.py'))
    steps = set(re.findall(r"log\.(?:timed|mark)\('(\w+)'\)", text)) | set(re.findall(r"timings_ms\['(\w+)'\]", text))
    assert steps and steps <= set(lakebase.CHAT_TURN_TIMINGS), steps - set(lakebase.CHAT_TURN_TIMINGS)


def test_every_passage_key_is_a_chat_retrieved_chunks_column():
    from server.services.chat_vsi import _passage
    row = {'chunk_id': 'c', 'IDDOC': 3, 'REF': 'R', 'division': 'AS', 'url': 'u', 'semantic_headers': '',
           'chunk_text': 't', '_hits': [{'q': 0, 'via': 'raw', 'rank': 0, 'score': 0.5}]}
    keys = set(_passage(row, 0))
    assert keys <= {c for c, _ in lakebase.CHAT_CHUNK_COLUMNS}, keys - {c for c, _ in lakebase.CHAT_CHUNK_COLUMNS}


def test_turn_log_outcome_and_timers():
    log = TurnLog()
    with log.timed('search'):
        pass
    with pytest.raises(RuntimeError), log.timed('search'):
        raise RuntimeError('timed even when it raises')
    log.mark('first_token')
    log.mark('first_token')                           # first mark kept
    assert set(log.timings_ms) == {'search', 'first_token'}
    assert log.finish('ok') == 'ok' and 'total' in log.timings_ms

    log = TurnLog()
    log.issue('client_disconnected', 'ws', severity=INFO)
    assert log.finish('ok') == 'ok'                   # info issues do not degrade a turn
    log = TurnLog()
    log.issue('rewrite_failed', 'rewrite', 'down')
    log.issue('rewrite_failed', 'rewrite', 'down again')
    assert log.codes == ['rewrite_failed'] and log.finish('ok') == 'degraded'
    log = TurnLog()
    log.fail('search', 'VectorSearchError', 'first')
    log.fail('llm', 'LLMError', 'second')
    assert log.error['stage'] == 'search' and log.codes == [] and log.finish('error') == 'error'


# ---------------------------------------------------------------------------
# store_chat_turn — SQL shape (a real Postgres run is described in docs/lakebase_schema.md)
# ---------------------------------------------------------------------------

def _fake_pool(conn):
    pool = MagicMock()
    acquire = MagicMock()
    acquire.__aenter__ = AsyncMock(return_value=conn)
    acquire.__aexit__ = AsyncMock(return_value=False)
    pool.acquire.return_value = acquire
    tx = MagicMock()
    tx.__aenter__ = AsyncMock(return_value=None)
    tx.__aexit__ = AsyncMock(return_value=False)
    conn.transaction = MagicMock(return_value=tx)
    return pool


def _params(sql: str) -> int:
    return len(set(re.findall(r'\$(\d+)', sql)))


def test_store_chat_turn_writes_turn_passages_and_issues():
    conn = AsyncMock()
    conn.fetchval.return_value = 12
    log = TurnLog()
    log.data.update({'session_id': 's', 'config': {'rerank_top_k': 12}, 'named_refs': ['MI-1'], 'vs_calls_ok': 6})
    log.passages = [{'position': 0, 'kept': True, 'chunk_id': 'c1', 'hits': [{'q': 0}], 'semantic_headers': ['H']}]
    log.issue('vs_partial', 'search', '1/6 failed', http_status=503)
    log.issue('client_disconnected', 'ws', severity=INFO)
    log.finish('ok')
    with patch.object(lakebase, 'get_pool', return_value=_fake_pool(conn)):
        assert asyncio.run(lakebase.store_chat_turn(log, endpoint='/api/chat/ws', assistant_message_id=5)) == 12

    sql, *values = conn.fetchval.await_args.args
    assert sql.startswith('INSERT INTO chat_turns') and _params(sql) == len(values)
    cols = re.search(r'\((.*?)\) VALUES', sql).group(1).split(', ')
    row = dict(zip(cols, values))
    assert row['status'] == 'degraded' and row['warnings'] == ['vs_partial', 'client_disconnected']
    assert row['assistant_message_id'] == 5 and row['trace_id'] == log.trace_id
    assert json.loads(row['config']) == {'rerank_top_k': 12} and row['named_refs'] == ['MI-1']
    assert row['app_version'] and row['total_ms'] is not None
    assert f"${cols.index('config') + 1}::jsonb" in sql

    (chunk_sql, chunk_rows), (error_sql, error_rows) = (c.args for c in conn.executemany.await_args_list)
    assert chunk_sql.startswith('INSERT INTO chat_retrieved_chunks') and _params(chunk_sql) == len(chunk_rows[0])
    assert chunk_rows[0][0] == 12 and chunk_rows[0][1] == log.trace_id
    assert error_sql.strip().startswith('INSERT INTO errors') and len(error_rows) == 1   # info issue not written
    assert _params(error_sql) == len(error_rows[0]) and 'warning' in error_rows[0]


def test_store_chat_turn_without_database_is_a_no_op():
    with patch.object(lakebase, 'get_pool', return_value=None):
        assert asyncio.run(lakebase.store_chat_turn(TurnLog())) is None


# ---------------------------------------------------------------------------
# /compare/impact and /compare/analyze audits
# ---------------------------------------------------------------------------

@pytest.fixture(scope='module')
def app():
    with (
        patch('server.app.init_lakebase', new_callable=AsyncMock),
        patch('server.app.shutdown_lakebase', new_callable=AsyncMock),
    ):
        from server.app import app as fastapi_app
        yield fastapi_app


@pytest.fixture
def client(app, monkeypatch):
    from server.services.user import require_compare
    for key, value in {
        'COMPARE_ENABLED': 'true', 'COMPARE_ANALYSIS_ENDPOINT': 'analysis', 'COMPARE_IMPACT_ENDPOINT': 'judge',
        'COMPARE_IMPACT_INDEX': 'cat.sch.chunks_index', 'COMPARE_VOLUME_PATH': '',
        'DATABRICKS_HOST': 'https://example.test', 'DATABRICKS_TOKEN': 'token',
    }.items():
        monkeypatch.setenv(key, value)
    app.dependency_overrides[require_compare] = lambda: None
    with (
        patch('server.routers.compare.get_user_identity', new=AsyncMock(return_value={'user_id': 'u', 'workspace_id': 'w'})),
        patch('server.routers.compare.get_cached_impact_result', new=AsyncMock(return_value=None)),
        patch('server.routers.compare.store_impact_cache', new=AsyncMock()),
        TestClient(app) as c,
    ):
        yield c
    app.dependency_overrides.clear()


CHANGES = json.dumps([{'section': '4.3 Torque', 'type': 'Value changed', 'criticality': 'High',
                       'before': 'couple de serrage 35 Nm', 'after': 'couple de serrage 40 Nm', 'rationale': ''}])


def _search(documents, queries_failed=0):
    async def _gen(**kwargs):
        yield {'type': 'plan', 'changes': kwargs['extracted']['changes'], 'source': 'structured', 'queries_used': 2,
               'queries_failed': queries_failed, 'chunks_returned': 3, 'candidates': len(documents),
               'excluded_refs': [], 'not_judged': [{'ref': 'X-1'}]}
        for doc in documents:
            yield {'type': 'document', 'document': doc}
        yield {'type': 'done', 'usage': {'input_tokens': 10, 'output_tokens': 5, 'total_tokens': 15, 'cost_eur': 0.0}}
    return _gen


def _impact(client, search):
    audit, errors = AsyncMock(return_value=7), AsyncMock()
    with (
        patch('server.routers.compare.run_impact_search', new=search),
        patch('server.routers.compare.store_impact_request', new=audit),
        patch('server.routers.compare.store_error', new=errors),
    ):
        res = client.post('/api/compare/impact', json={'changes_text': CHANGES})
    return [json.loads(line) for line in res.text.splitlines() if line.strip()], audit, errors


def test_impact_audit_has_status_counts_and_durations(client):
    docs = [{'ref': 'PR-1', 'status': 'impacted', 'change_ids': ['C1'], 'judge_ms': 40, 'judge_attempts': 1},
            {'ref': 'PR-2', 'status': 'error', 'reason': 'Judgment failed: timeout', 'error_type': 'ReadTimeout',
             'change_ids': ['C1'], 'judge_attempts': 3}]
    events, audit, errors = _impact(client, _search(docs, queries_failed=1))
    assert events[-1]['type'] == 'done' and events[-1]['impact_request_id'] == 7
    kw = audit.await_args.kwargs
    assert (kw['status'], kw['index_name'], kw['queries_failed'], kw['candidates'], kw['not_judged'],
            kw['judge_failed']) == ('ok', 'cat.sch.chunks_index', 1, 2, 1, 1)
    assert all(isinstance(kw[k], int) for k in ('extract_ms', 'search_ms', 'judge_ms', 'queries_used'))
    warnings = [c.kwargs for c in errors.call_args_list]
    assert [(w['severity'], w['stage'], w['error_type']) for w in warnings] == [
        ('warning', 'search', 'VectorSearchPartial'), ('warning', 'judge', 'ReadTimeout')]
    assert warnings[1]['context'] == {'ref': 'PR-2', 'judge_attempts': 3}


def test_impact_search_failure_is_an_error_audit(client):
    async def _boom(**kwargs):
        raise RuntimeError('All 2 Vector Search queries failed')
        yield  # pragma: no cover — makes this an async generator
    events, audit, errors = _impact(client, _boom)
    assert events[-1]['type'] == 'error'
    assert audit.call_args.kwargs['status'] == 'error' and audit.call_args.kwargs['http_status'] == 502
    assert errors.call_args.kwargs['stage'] == 'search' and errors.call_args.kwargs['upstream'] == 'cat.sch.chunks_index'


def test_analyze_records_outcome_and_durations(client):
    processor = MagicMock()
    processor.build_messages.return_value = ProcessResult(
        messages=[{'role': 'user', 'content': 'diff'}], metadata=ProcessMetadata('pdf', 'visual'))

    async def _llm(*args, **kwargs):
        yield f"data: {json.dumps({'type': 'response.output_text.delta', 'delta': 'rapport'})}\n\n"
        yield f"data: {json.dumps({'type': 'usage', 'input_tokens': 3, 'output_tokens': 2})}\n\n"
        yield 'data: [DONE]\n\n'

    run = AsyncMock()
    with (
        patch('server.routers.compare._get_cached_analysis', new=AsyncMock(return_value=None)),
        patch('server.routers.compare.get_processor', return_value=processor),
        patch('server.routers.compare.store_llm_request', new=AsyncMock(return_value=11)),
        patch('server.routers.compare.update_llm_request_usage', new=AsyncMock()),
        patch('server.routers.compare.update_llm_request_run', new=run),
        patch('server.routers.compare.stream_analysis', new=_llm),
    ):
        res = client.post('/api/compare/analyze', files={
            'old_file': ('a.pdf', b'%PDF-1.4 old', 'application/pdf'),
            'new_file': ('b.pdf', b'%PDF-1.4 new', 'application/pdf')})
    assert 'rapport' in res.text
    args, kw = run.call_args
    assert args == (11,) and kw['status'] == 'ok' and kw['chunked_parts'] == 0
    assert all(isinstance(kw[k], int) for k in ('queue_wait_ms', 'build_ms', 'first_token_ms', 'generation_ms',
                                                 'total_ms'))
