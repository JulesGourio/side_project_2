"""Every finished chat turn as one MLflow trace — the live view of what the chatbot does.

``chat.py`` calls ``export_turn`` once the answer is sent and the turn is saved to Lakebase. The
trace is rebuilt from the turn's ``TurnLog`` (``turn_log.py``): the same steps, durations, passages
and issues as in ``chat_turns``, at their real start offsets, so nothing is measured twice and the
user never waits for it. Off unless ``CHAT_MLFLOW_EXPERIMENT`` names an existing experiment; a
failure is logged and never reaches the chat.

    chat_turn                       the question → the answer shown (tags: division, status, warnings…)
    ├─ translate_in                 non-French/English question → English (translation bridge)
    ├─ retrieval                    RETRIEVER: output = the passages sent to the model, prompt order
    │   ├─ rewrite                  LLM: FR + EN search queries (model, tokens, cost)
    │   ├─ search                   3 hybrid queries; calls expected / ok, reranker ok
    │   └─ ref_lookup               REFs named in the conversation
    ├─ answer                       LLM: tokens, cost, first-token delay, truncation, fallback
    │   ├─ queue_wait               wait for a free answer slot
    │   └─ call_1, call_2…          one span per model call (outcome, status, seconds)
    ├─ translate_back               answer translated back
    └─ persist                      messages saved to Lakebase

The user's vote is not attached here: ``utils/evaluation/score_chat_traces.py`` joins it from Lakebase by ``chat_trace_id``.
"""

import asyncio
import hashlib
import json
import logging
import os
import threading
from typing import Any, Dict, List, Optional, Set

logger = logging.getLogger(__name__)

_NS_PER_MS = 1_000_000
_MAX_DOC_CHARS = 20_000          # passage text kept per document in the retriever span output
_MAX_ISSUE_CHARS = 500
_lock = threading.Lock()
_experiment: Dict[str, Optional[str]] = {}       # experiment name -> id (None: missing, warned once)
_pending: Set['asyncio.Task[None]'] = set()      # strong references until each export is done


def experiment_name() -> str:
    return os.getenv('CHAT_MLFLOW_EXPERIMENT', '').strip()


def _experiment_id(name: str) -> Optional[str]:
    """Id of the experiment ``name``, looked up once. Never created here: the experiment is bound to
    its Unity Catalog trace location by hand first (OPERATIONS.md), a default one would not be."""
    with _lock:
        if name in _experiment:
            return _experiment[name]
    import mlflow
    if os.getenv('DATABRICKS_WAREHOUSE_ID'):             # only read by a Unity Catalog trace location
        os.environ.setdefault('MLFLOW_TRACING_SQL_WAREHOUSE_ID', os.environ['DATABRICKS_WAREHOUSE_ID'])
    try:
        found = mlflow.get_experiment_by_name(name)
    except Exception as exc:  # noqa: BLE001 — retried on the next turn
        logger.warning('mlflow_turns: experiment lookup %s failed: %s', name, exc)
        return None
    with _lock:
        _experiment[name] = found.experiment_id if found else None
    if not found:
        logger.warning('mlflow_turns: experiment %s does not exist, chat traces are off (see OPERATIONS.md)', name)
    return _experiment[name]


def export_turn(log: Any) -> None:
    """Schedules the export of the finished turn ``log`` (never blocks, never raises)."""
    name = experiment_name()
    if not name or not log.status:
        return
    try:
        task = asyncio.get_running_loop().create_task(_export(log, name))
    except RuntimeError:
        return
    _pending.add(task)
    task.add_done_callback(_pending.discard)


async def _export(log: Any, name: str) -> None:
    try:
        await asyncio.wait_for(asyncio.to_thread(_write_trace, log, name), timeout=60)
    except Exception as exc:  # noqa: BLE001 — telemetry must not disturb the chat
        logger.warning('mlflow_turns: trace of %s not written: %s', log.trace_id, exc)


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode('utf-8')).hexdigest()[:12]


def _document(passage: Dict[str, Any]) -> Any:
    from mlflow.entities import Document
    return Document(page_content=(passage.get('chunk_text') or '')[:_MAX_DOC_CHARS],
                    metadata={'doc_uri': passage.get('url') or passage.get('ref') or '', 'ref': passage.get('ref'),
                              'doc_number': passage.get('doc_number'), 'cited': bool(passage.get('cited')),
                              'source': passage.get('source'), 'score': passage.get('best_score'),
                              'semantic_headers': passage.get('semantic_headers') or ''})


def _write_trace(log: Any, name: str) -> None:
    import mlflow
    from mlflow.entities import SpanType
    from mlflow.tracing.constant import SpanAttributeKey

    experiment_id = _experiment_id(name)
    if not experiment_id:
        return
    data, timings, starts = log.data, log.timings_ms, log.starts_ms
    t0 = log.started_at_ns
    total_ms = timings.get('total') or log.elapsed_ms()
    failed = log.status == 'error'

    def at(ms: int) -> int:
        return t0 + int(ms) * _NS_PER_MS

    def child(parent: Any, step: str, span_type: str, start_ms: int, duration_ms: int, *,
              inputs: Any = None, outputs: Any = None, attributes: Optional[Dict[str, Any]] = None,
              status: Optional[str] = None, label: Optional[str] = None) -> Any:
        span = mlflow.start_span_no_context(label or step, span_type=span_type, parent_span=parent, inputs=inputs,
                                            start_time_ns=at(start_ms))
        span.end(outputs=outputs, attributes={k: v for k, v in (attributes or {}).items() if v is not None},
                 status=status, end_time_ns=at(start_ms + max(0, duration_ms)))
        return span

    issues = [{'code': i['code'], 'stage': i['stage'], 'severity': i['severity'],
               'message': (i['message'] or '')[:_MAX_ISSUE_CHARS], 'at_ms': i['at_ms']} for i in log.issues]
    tags = {
        'chat_trace_id': log.trace_id, 'division': str(data.get('division') or ''), 'status': log.status,
        'question_lang': str(data.get('question_lang') or ''), 'answer_endpoint': str(data.get('answer_endpoint') or ''),
        'llm_fallback': str(bool(data.get('llm_fallback'))).lower(), 'warnings': ','.join(log.codes),
        'app_version': str(data.get('app_version') or ''), 'index_name': str(data.get('index_name') or ''),
        'cited_count': str(data.get('citations_count') or 0),
    }
    metadata = {k: v for k, v in {'mlflow.trace.session': data.get('session_id'),
                                  'mlflow.trace.user': _hash(str(data['user_id'])) if data.get('user_id') else None
                                  }.items() if v}
    root = mlflow.start_span_no_context(
        'chat_turn', span_type=SpanType.CHAIN, inputs={'question': data.get('question') or '',
                                                       'division': data.get('division')},
        tags=tags, metadata=metadata, experiment_id=experiment_id, start_time_ns=t0)

    if 'translate_in' in timings:
        child(root, 'translate_in', SpanType.CHAIN, starts.get('translate_in', 0), timings['translate_in'],
              inputs=data.get('question'), outputs=data.get('question_en'),
              attributes={'question_lang': data.get('question_lang')})

    if 'retrieval' in timings:
        start = starts.get('retrieval', 0)
        sent = sorted((p for p in log.passages if p.get('kept')), key=lambda p: p.get('prompt_rank') or 0)
        dropped: List[Dict[str, Any]] = [{'ref': p.get('ref'), 'reason': p.get('drop_reason')}
                                         for p in log.passages if not p.get('kept')]
        retrieval = mlflow.start_span_no_context(
            'retrieval', span_type=SpanType.RETRIEVER, parent_span=root,
            inputs={'query': data.get('fr_query') or data.get('question'), 'en_query': data.get('en_query')},
            start_time_ns=at(start))
        if 'rewrite' in timings:
            child(retrieval, 'rewrite', SpanType.LLM, starts.get('rewrite', start), timings['rewrite'],
                  inputs=data.get('question'), outputs={'fr': data.get('fr_query'), 'en': data.get('en_query')},
                  status='OK' if data.get('rewrite_ok') is not False else 'ERROR',
                  attributes={SpanAttributeKey.MODEL: data.get('rewrite_endpoint'),
                              SpanAttributeKey.CHAT_USAGE: _usage(data.get('rewrite_input_tokens'),
                                                                  data.get('rewrite_output_tokens')),
                              'cost_eur': data.get('rewrite_cost_eur')})
        if 'search' in timings:
            child(retrieval, 'search', SpanType.CHAIN, starts.get('search', start), timings['search'],
                  attributes={'vs_calls_expected': data.get('vs_calls_expected'), 'vs_calls_ok': data.get('vs_calls_ok'),
                              'rerank_ok': data.get('rerank_ok'), 'index': data.get('index_name')})
        if 'ref_lookup' in timings:
            child(retrieval, 'ref_lookup', SpanType.CHAIN, starts.get('ref_lookup', start), timings['ref_lookup'],
                  inputs=data.get('named_refs'))
        retrieval.end(outputs=[_document(p) for p in sent], end_time_ns=at(start + timings['retrieval']),
                      attributes={'passages_retrieved': data.get('passages_retrieved'),
                                  'passages_sent': data.get('passages_sent'),
                                  'documents_sent': data.get('documents_sent'), 'prompt_chars': data.get('prompt_chars'),
                                  'dropped': json.dumps(dropped[:200])})

    if 'generation' in timings or data.get('answer_endpoint') or 'answer' in starts:
        start = starts.get('answer', timings.get('retrieval', 0))
        wait, generation = timings.get('queue_wait') or 0, timings.get('generation') or 0
        answer = mlflow.start_span_no_context(
            'answer', span_type=SpanType.LLM, parent_span=root,
            inputs={'question': data.get('question'), 'documents': data.get('documents_sent')},
            start_time_ns=at(start))
        if wait:
            child(answer, 'queue_wait', SpanType.CHAIN, start, wait)
        cursor = start + wait
        for n, attempt in enumerate(data.get('llm_attempts') or [], 1):
            seconds = int(float(attempt.get('s') or 0) * 1000)
            child(answer, 'call', SpanType.LLM, cursor, seconds, label=f'call_{n}',
                  status='OK' if attempt.get('outcome') in ('ok', 'continued') else 'ERROR',
                  attributes={SpanAttributeKey.MODEL: attempt.get('endpoint'), 'outcome': attempt.get('outcome'),
                              'http_status': attempt.get('status')})
            cursor += seconds
        answer.end(outputs=log.answer or None, status='ERROR' if failed else 'OK',
                   end_time_ns=at(start + wait + generation),
                   attributes={SpanAttributeKey.MODEL: data.get('answer_endpoint'),
                               SpanAttributeKey.CHAT_USAGE: _usage(data.get('input_tokens'), data.get('output_tokens')),
                               'thinking_tokens': data.get('thinking_tokens'), 'cost_eur': data.get('cost_eur'),
                               'first_token_ms': timings.get('first_token'), 'truncated': data.get('truncated'),
                               'continuations': data.get('continuations'), 'fallback': data.get('llm_fallback'),
                               'answer_chars': data.get('answer_chars'), 'instructions_sha': data.get('instructions_sha')})

    for step in ('translate_back', 'persist'):
        if step in timings:
            child(root, step, SpanType.CHAIN, starts.get(step, 0), timings[step])

    root.end(outputs=log.answer or None, status='ERROR' if failed else 'OK', end_time_ns=at(total_ms),
             attributes={'timings_ms': json.dumps(timings), 'issues': json.dumps(issues),
                         'cited_refs': json.dumps(data.get('cited_refs') or []), 'sources_count': data.get('sources_count'),
                         'config': json.dumps(data.get('config') or {}, default=str),
                         'cost_eur_total': round((data.get('cost_eur') or 0) + (data.get('rewrite_cost_eur') or 0), 6)})


def _usage(input_tokens: Optional[int], output_tokens: Optional[int]) -> Optional[Dict[str, int]]:
    if input_tokens is None and output_tokens is None:
        return None
    i, o = int(input_tokens or 0), int(output_tokens or 0)
    return {'input_tokens': i, 'output_tokens': o, 'total_tokens': i + o}
