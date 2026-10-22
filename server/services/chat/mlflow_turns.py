"""Every finished chat turn as one MLflow trace — the live view of what the chatbot does.

The chat route calls ``export_turn`` once the answer is sent, just before the turn is saved to Lakebase
(the MLflow trace id is stored in ``chat_turns.mlflow_trace_id``). The trace is rebuilt from the turn's
``TurnLog`` (``turn_log.py``): the same steps, durations, passages and issues as in ``chat_turns``, at
their real start offsets, so nothing is measured twice; the user has already got the answer. Off unless
``CHAT_MLFLOW_EXPERIMENT_ID`` or ``CHAT_MLFLOW_EXPERIMENT`` (its workspace path) names an existing experiment; a failure is logged and never reaches the chat.
Traces upload in MLflow's own background queue, so the call is local and short.

    chat_turn                       the question → the answer shown (tags: division, status, warnings…; total tokens)
    ├─ translate_in                 LLM: non-French/English question → English (messages, tokens)
    ├─ retrieval                    output = the documents handed to the model (n, REF, title, passages, cited)
    │   ├─ rewrite                  LLM: messages, FR + EN search queries (model, tokens, cost)
    │   ├─ search                   the Vector Search calls, one span each (query, rerank / raw, results, duration)
    │   ├─ ref_lookup               REFs named in the conversation
    │   └─ passages_sent            RETRIEVER: the passages' full text, prompt order (what the judges read)
    ├─ answer                       LLM: messages, tokens, cost, time to first token, streaming time, tokens/s, truncation, fallback
    │   ├─ queue_wait               wait for a free answer slot
    │   └─ call_1, call_2…          one span per model call (outcome, status, seconds)
    │       ├─ time_to_first_token  call start → first text of the model
    │       └─ generation           first text → end of the call
    ├─ translate_back               LLM: answer translated back (messages, tokens)
    └─ persist                      messages saved to Lakebase

The tags carry what the free scorers read (``utils/evaluation/chat_scorers.py``); the registered
versions of the prompts the turn used are linked from the experiment's ``chat_prompt.<key>`` tags (written by
``register_chat_prompts``; the app never reads the registry); the user's thumbs up / down becomes a ``user_vote`` assessment on the trace (``log_vote``, called by the feedback route).
"""

import asyncio
import hashlib
import json
import logging
import os
import re
import threading
import time
from typing import Any, Dict, List, Tuple

from server.services.chat import prompts
from server.services.chat.config import (
    MLFLOW_EXPORT_TIMEOUT_S, MLFLOW_MAX_DOC_CHARS, MLFLOW_MAX_ISSUE_CHARS, MLFLOW_PROMPT_EMPTY_TTL_S,
    MLFLOW_PROMPT_TAG, MLFLOW_PROMPT_TTL_S, MLFLOW_RETRY_MISSING_S,
)
from server.services.chat import translation_bridge
from server.services.infra.doc_catalog import document_info
from server.services.chat.turn_log import passage_title

logger = logging.getLogger(__name__)

_NS_PER_MS = 1_000_000
_lock = threading.Lock()
_activated: set = set()                        # experiment ids made active in this process
_experiment: Dict[str, str | None] = {}       # experiment name -> id, once found
_missing_until: Dict[str, float] = {}           # experiment name -> when to look again
_prompt_cache: Dict[str, Any] = {}              # experiment id -> (fetched at, chat_prompt.* tags)


def experiment_name() -> str:
    return os.getenv('CHAT_MLFLOW_EXPERIMENT', '').strip()


def experiment_id_setting() -> str:
    """``CHAT_MLFLOW_EXPERIMENT_ID``: when set, the experiment is addressed by id, so moving or renaming it, or a change of the
    identity that created it, never cuts the traces."""
    return os.getenv('CHAT_MLFLOW_EXPERIMENT_ID', '').strip()


def enabled() -> bool:
    return bool(experiment_id_setting() or experiment_name())


def _activate(mlflow: Any, experiment_id: str) -> bool:
    """Makes the experiment the active one of the process, once.

    MLflow chooses where the spans go when the first span of the process is created, from the *active* experiment: the Unity
    Catalog tables it is linked to. The ``experiment_id`` given to a span does not do it, and the traces would then land in the
    experiment's own storage, outside Unity Catalog.

    :param mlflow: the mlflow module
    :param experiment_id: id of the chat experiment
    :return: False when the experiment could not be activated (no trace is written then)
    """
    with _lock:
        if experiment_id in _activated:
            return True
    try:
        mlflow.set_experiment(experiment_id=experiment_id)
    except Exception as exc:  # noqa: BLE001 — retried on the next turn
        logger.warning('mlflow_turns: experiment %s could not be activated, no trace written: %s', experiment_id, exc)
        return False
    with _lock:
        _activated.add(experiment_id)
    return True


def _experiment_id(name: str) -> str | None:
    """Id of the experiment ``name``, looked up once it exists.

    Never created here: the ``create_chat_experiment`` job creates it, as the job service principal.
    A missing one is looked up again after a minute.

    :param name: experiment name or workspace path
    :return: the experiment id, None while the experiment is missing or cannot be reached
    """
    with _lock:
        if _experiment.get(name):
            return _experiment[name]
        if time.monotonic() < _missing_until.get(name, 0.0):
            return None
    import mlflow
    warehouse = os.getenv('CHAT_MLFLOW_WAREHOUSE_ID', '').strip()      # read by the Unity Catalog trace tables
    if warehouse:
        os.environ['MLFLOW_TRACING_SQL_WAREHOUSE_ID'] = warehouse
    elif os.getenv('DATABRICKS_WAREHOUSE_ID'):
        os.environ.setdefault('MLFLOW_TRACING_SQL_WAREHOUSE_ID', os.environ['DATABRICKS_WAREHOUSE_ID'])
    fixed = experiment_id_setting()
    if fixed:
        return fixed if _activate(mlflow, fixed) else None
    try:
        found = mlflow.get_experiment_by_name(name)
    except Exception as exc:  # noqa: BLE001 — retried on the next turn
        logger.warning('mlflow_turns: experiment lookup %s failed: %s', name, exc)
        return None
    if found and not _activate(mlflow, found.experiment_id):
        return None
    with _lock:
        if found:
            _experiment[name] = found.experiment_id
            return found.experiment_id
        _missing_until[name] = time.monotonic() + MLFLOW_RETRY_MISSING_S
    logger.warning('mlflow_turns: experiment %s does not exist, chat traces are off until it is created', name)
    return None


async def export_turn(log: Any) -> None:
    """Writes the finished turn ``log`` as a trace and records its id in ``log.data`` (never raises).

    :param log: finished TurnLog of the turn
    """
    name = experiment_name()
    if not enabled() or not log.status:
        return
    try:
        trace_id = await asyncio.wait_for(asyncio.to_thread(_write_trace, log, name), timeout=MLFLOW_EXPORT_TIMEOUT_S)
    except Exception as exc:  # noqa: BLE001 — telemetry must not disturb the chat
        logger.warning('mlflow_turns: trace of %s not written: %s', log.trace_id, exc)
        return
    if trace_id:
        log.data['mlflow_trace_id'] = trace_id


def _language_guess(text: str | None) -> str:
    """Two-letter code of the language of ``text`` (local fastText model), '' when unsure or unavailable.

    :param text: text to guess the language of, None allowed
    :return: the two-letter code, or an empty string when unsure
    """
    if not text:
        return ''
    try:
        return translation_bridge.fast_lang_guess(text[:2000]) or ''
    except Exception:  # noqa: BLE001 — a tag, not worth a failed trace
        return ''


def _hash(value: str) -> str:
    """Short SHA-256 of ``value``, so that identifiers stay out of the traces.

    :param value: text to hash
    :return: 12 hexadecimal characters
    """
    return hashlib.sha256(value.encode('utf-8')).hexdigest()[:12]


_DATE_RE = re.compile(r'Date de diffusion:\s*([0-9]{4}-[0-9]{2}-[0-9]{2}|inconnue)')


def _passage_date(chunk_text: str | None) -> str:
    """Diffusion date from the passage's ``[Source: …]`` prefix (what the answer model reads).

    :param chunk_text: passage text, None allowed
    :return: the date as ``YYYY-MM-DD``, ``inconnue`` when the source says so, or an empty string
    """
    found = _DATE_RE.search((chunk_text or '')[:600])
    return found.group(1) if found else ''


def _vs_call_label(call: Dict[str, Any]) -> str:
    """Label of a Vector Search call span: ``q1 rerank``, ``q2 raw``… (``q-`` when it belongs to no query).

    :param call: one entry of ``TurnLog.vs_calls`` (``query_index`` may be None, ``mode`` is always set).
    :return: the label.
    """
    index = call.get('query_index')
    return f"q{index + 1 if index is not None else '-'} {call['mode']}"


def _documents_summary(sent: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """The documents handed to the model, in prompt order: n, REF, title, diffusion date, revision, passages, best score, cited.

    :param sent: kept passages, in prompt order
    :return: one dict per document, in prompt order
    """
    docs: Dict[str, Dict[str, Any]] = {}
    for p in sent:
        d = docs.setdefault(p.get('ref') or '', {'n': p.get('doc_number'), 'ref': p.get('ref'), 'title': passage_title(p.get('chunk_text')),
                                                'date': _passage_date(p.get('chunk_text')),
                                                'revision': document_info(p.get('ref') or '').get('revision'),
                                                'passages': 0, 'best_score': None, 'found_by': set(),
                                                'cited': bool(p.get('cited'))})
        d['passages'] += 1
        score = p.get('best_score')
        if score is not None and (d['best_score'] is None or score > d['best_score']):
            d['best_score'] = round(float(score), 4)
        d['found_by'].add(p.get('source') or 'search')
        d.setdefault('hits', []).extend(_hit_label(h) for h in p.get('hits') or [])
    return [{**d, 'found_by': sorted(d['found_by']), 'hits': d['hits'][:8]}
            for d in sorted(docs.values(), key=lambda d: d['n'] or 0)]


def _hit_label(hit: Dict[str, Any]) -> str:
    """'q1 rerank #3 (0.83)': which query found the passage, how, at which rank, with which score.

    :param hit: one hit of a passage: query index, way found, rank and score
    :return: the label
    """
    q = 'ref' if hit.get('q') is None else f"q{hit['q'] + 1}"
    score = hit.get('score')
    return f"{q} {hit.get('via')} #{(hit.get('rank') or 0) + 1}" + (f' ({score:.3f})' if isinstance(score, (int, float)) else '')


def _document(passage: Dict[str, Any]) -> Any:
    """The passage as an MLflow document, its text cut to MLFLOW_MAX_DOC_CHARS.

    :param passage: passage record of the turn log
    :return: the MLflow document
    """
    from mlflow.entities import Document
    return Document(page_content=(passage.get('chunk_text') or '')[:MLFLOW_MAX_DOC_CHARS],
                    metadata={'doc_uri': passage.get('url') or passage.get('ref') or '', 'ref': passage.get('ref'),
                              'doc_number': passage.get('doc_number'), 'cited': bool(passage.get('cited')),
                              'source': passage.get('source'), 'score': passage.get('best_score'),
                              'semantic_headers': passage.get('semantic_headers') or ''})


def _prompt_tags(experiment_id: str) -> Dict[str, str]:
    """The experiment's ``chat_prompt.<key>`` tags, refreshed every ``MLFLOW_PROMPT_TTL_S``.

    :param experiment_id: id of the experiment
    :return: the tags by name, refreshed after MLFLOW_PROMPT_TTL_S
    """
    cached = _prompt_cache.get(experiment_id)
    if cached and time.monotonic() - cached[0] < (MLFLOW_PROMPT_TTL_S if cached[1] else MLFLOW_PROMPT_EMPTY_TTL_S):
        return cached[1]
    tags: Dict[str, str] = {}
    try:
        import mlflow
        tags = {k: v for k, v in (mlflow.get_experiment(experiment_id).tags or {}).items() if k.startswith(MLFLOW_PROMPT_TAG)}
    except Exception as exc:  # noqa: BLE001 — retried after the TTL
        logger.warning('mlflow_turns: experiment tags unreadable: %s', exc)
    _prompt_cache[experiment_id] = (time.monotonic(), tags)
    return tags


def _linked_prompts(experiment_id: str, keys: List[str]) -> str:
    """JSON for the ``mlflow.linkedPrompts`` trace tag: the registered version of each prompt ``keys`` the turn used,
    when its recorded hash is the file's (a prompt edited since the last registration is not linked).

    :param experiment_id: id of the experiment
    :param keys: prompt keys the turn used
    :return: the JSON of the linked prompts, empty when none is linked
    """
    tags, linked = _prompt_tags(experiment_id), []
    for key in keys:
        try:
            entry = json.loads(tags.get(MLFLOW_PROMPT_TAG + key) or '')
        except ValueError:
            continue
        if entry.get('sha') == prompts.digest(key):
            linked.append({'name': entry['name'], 'version': str(entry['version'])})
        else:
            logger.warning('mlflow_turns: prompt %s differs from the registered version (run register_chat_prompts)', key)
    return json.dumps(linked) if linked else ''


def log_vote(mlflow_trace_id: str, vote: str, comment: str = '', user_id: str = '') -> None:
    """Attaches the user's thumbs up / down to the turn's trace (blocking; run it in a thread, never raises).

    :param mlflow_trace_id: trace of the turn
    :param vote: ``up`` or ``down``
    :param comment: comment of the user, cut to 2000 characters, optional
    :param user_id: id of the user, hashed before it is stored
    """
    if not (enabled() and mlflow_trace_id):
        return
    try:
        import mlflow
        from mlflow.entities import AssessmentSource, AssessmentSourceType
        mlflow.log_feedback(
            trace_id=mlflow_trace_id, name='user_vote', value=vote == 'up',
            source=AssessmentSource(source_type=AssessmentSourceType.HUMAN, source_id=_hash(user_id or 'unknown')),
            rationale=(comment or '')[:2000] or None)
    except Exception as exc:  # noqa: BLE001
        logger.warning('mlflow_turns: vote on %s not logged: %s', mlflow_trace_id, exc)


def _write_trace(log: Any, name: str) -> str | None:
    """Writes the finished turn as one MLflow trace.

    :param log: finished TurnLog of the turn
    :param name: experiment name, from CHAT_MLFLOW_EXPERIMENT
    :return: the trace id, None when the experiment is not found
    """
    import mlflow
    from mlflow.entities import SpanType
    from mlflow.tracing.constant import SpanAttributeKey

    experiment_id = _experiment_id(name)
    if not experiment_id:
        return None
    data, timings, starts = log.data, log.timings_ms, log.starts_ms
    t0 = log.started_at_ns
    total_ms = timings.get('total') or log.elapsed_ms()
    failed = log.status == 'error'

    def at(ms: int) -> int:
        """Absolute time in nanoseconds of an offset from the start of the turn.

        :param ms: offset from the start of the turn, in milliseconds
        :return: the time in nanoseconds
        """
        return t0 + int(ms) * _NS_PER_MS

    def child(parent: Any, step: str, span_type: str, start_ms: int, duration_ms: int, *,
              inputs: Any = None, outputs: Any = None, attributes: Dict[str, Any] | None = None,
              status: str | None = None, label: str | None = None) -> Any:
        """Adds a span that has already finished under ``parent``.

        :param parent: parent span
        :param step: span name, used when no label is given
        :param span_type: MLflow span type, e.g. ``SpanType.RETRIEVER``
        :param start_ms: offset of the span start from the start of the turn, in milliseconds
        :param duration_ms: length of the span, in milliseconds
        :param inputs: span inputs, None for none
        :param outputs: span outputs, None for none
        :param attributes: span attributes; the None values are left out
        :param status: span status such as ``OK`` or ``ERROR``, None for the default
        :param label: span name replacing ``step``, None to use ``step``
        :return: the span
        """
        span = mlflow.start_span_no_context(label or step, span_type=span_type, parent_span=parent, inputs=inputs,
                                            start_time_ns=at(start_ms))
        span.end(outputs=outputs, attributes={k: v for k, v in (attributes or {}).items() if v is not None},
                 status=status, end_time_ns=at(start_ms + max(0, duration_ms)))
        return span

    def tokens(step: str) -> Tuple[int | None, int | None]:
        """Input and output tokens of a model step recorded in ``TurnLog.model_io``.

        :param step: step name, the key of ``model_io``
        :return: the input and output token counts, None for each one that is unknown
        """
        io = log.model_io.get(step) or {}
        return io.get('input_tokens'), io.get('output_tokens')

    issues = [{'code': i['code'], 'stage': i['stage'], 'severity': i['severity'],
               'message': (i['message'] or '')[:MLFLOW_MAX_ISSUE_CHARS], 'at_ms': i['at_ms']} for i in log.issues]
    tags = {
        'chat_trace_id': log.trace_id, 'division': str(data.get('division') or ''), 'status': log.status,
        'question_lang': str(data.get('question_lang') or ''), 'answer_endpoint': str(data.get('answer_endpoint') or ''),
        'llm_fallback': str(bool(data.get('llm_fallback'))).lower(), 'warnings': ','.join(log.codes),
        'app_version': str(data.get('app_version') or ''), 'index_name': str(data.get('index_name') or ''),
        'cited_count': str(data.get('citations_count') or 0), 'passages_sent': str(data.get('passages_sent') or 0),
        'first_token_ms': str(timings.get('first_token') or ''), 'total_ms': str(total_ms),
        'cost_eur': str(round((data.get('cost_eur') or 0) + (data.get('rewrite_cost_eur') or 0), 6)),
        'question_lang_guess': _language_guess(data.get('question')), 'answer_lang_guess': _language_guess(log.answer),
        'rewrite_mode': str(data.get('rewrite_mode') or ''),
    }
    linked = _linked_prompts(experiment_id, getattr(log, 'prompts', []))
    if linked:
        tags['mlflow.linkedPrompts'] = linked
    metadata = {k: v for k, v in {'mlflow.trace.session': data.get('session_id'),
                                  'mlflow.trace.user': _hash(str(data['user_id'])) if data.get('user_id') else None
                                  }.items() if v}
    root = mlflow.start_span_no_context(
        'chat_turn', span_type=SpanType.CHAIN, inputs={'question': data.get('question') or '',
                                                       'division': data.get('division')},
        tags=tags, metadata=metadata, experiment_id=experiment_id, start_time_ns=t0)

    if 'translate_in' in timings:
        io = log.model_io.get('translate_in') or {}
        child(root, 'translate_in', SpanType.LLM if io.get('messages') else SpanType.CHAIN, starts.get('translate_in', 0),
              timings['translate_in'], inputs={'messages': io['messages']} if io.get('messages') else data.get('question'),
              outputs=data.get('question_en'),
              attributes={'question_lang': data.get('question_lang'), SpanAttributeKey.MODEL: io.get('endpoint'),
                          SpanAttributeKey.CHAT_USAGE: _usage(*tokens('translate_in'))})

    if 'retrieval' in timings:
        start = starts.get('retrieval', 0)
        sent = sorted((p for p in log.passages if p.get('kept')), key=lambda p: p.get('prompt_rank') or 0)
        dropped = [{'ref': p.get('ref'), 'reason': p.get('drop_reason')} for p in log.passages if not p.get('kept')]
        retrieval = mlflow.start_span_no_context(
            'retrieval', span_type=SpanType.CHAIN, parent_span=root,
            inputs={'question': data.get('question'), 'named_refs': data.get('named_refs') or [],
                    'search_queries': data.get('rewrite_mode') or 'rewrite'},
            start_time_ns=at(start))
        if 'rewrite' in timings:
            rewrite_name = 'translate' if data.get('rewrite_mode') == 'translate' else 'rewrite'
            rewrite_status = 'ERROR' if data.get('rewrite_ok') is False else 'OK'
            rewrite_usage = _usage(data.get('rewrite_input_tokens'), data.get('rewrite_output_tokens'))
            rewrite_io = log.model_io.get('rewrite') or {}
            child(retrieval, rewrite_name, SpanType.LLM, starts.get('rewrite', start), timings['rewrite'],
                  inputs={'messages': rewrite_io['messages']} if rewrite_io.get('messages') else {'question': data.get('question')},
                  outputs={'fr': data.get('fr_query'), 'en': data.get('en_query')},
                  status=rewrite_status,
                  attributes={SpanAttributeKey.MODEL: data.get('rewrite_endpoint'),
                              SpanAttributeKey.CHAT_USAGE: rewrite_usage,
                              'cost_eur': data.get('rewrite_cost_eur')})
        if 'search' in timings:
            search = mlflow.start_span_no_context(
                'search', span_type=SpanType.CHAIN, parent_span=retrieval, start_time_ns=at(starts.get('search', start)),
                inputs={'queries': {'question': data.get('question'), 'fr': data.get('fr_query'), 'en': data.get('en_query')},
                        'index': data.get('index_name')})
            for call in log.vs_calls:
                label = _vs_call_label(call)
                child(search, 'vector_search', SpanType.RETRIEVER, call['start_ms'], call.get('ms') or 0, label=label,
                      inputs={'query': call['query'], 'mode': call['mode'], 'num_results': call['k']},
                      outputs={'results': call.get('results'), 'error': call.get('error'),
                               'documents': call.get('hits') or []},
                      status='OK' if call.get('ok', True) else 'ERROR')
            search.end(outputs={'calls': len(log.vs_calls), 'calls_ok': data.get('vs_calls_ok'),
                                'rerank_ok': data.get('rerank_ok'), 'passages_retrieved': data.get('passages_retrieved')},
                       end_time_ns=at(starts.get('search', start) + timings['search']))
        if 'ref_lookup' in timings:
            child(retrieval, 'ref_lookup', SpanType.CHAIN, starts.get('ref_lookup', start), timings['ref_lookup'],
                  inputs=data.get('named_refs'))
        child(retrieval, 'passages_sent', SpanType.RETRIEVER, start + timings['retrieval'], 0,
              inputs={'question': data.get('question'), 'fr_query': data.get('fr_query')},
              outputs=[_document(p) for p in sent],
              attributes={'passages_sent': data.get('passages_sent'), 'documents_sent': data.get('documents_sent'),
                          'prompt_chars': data.get('prompt_chars')})
        retrieval.end(outputs={'documents': _documents_summary(sent), 'passages_retrieved': data.get('passages_retrieved'),
                               'passages_sent': data.get('passages_sent'), 'dropped': dropped[:50]},
                      end_time_ns=at(start + timings['retrieval']),
                      attributes={'prompt_chars': data.get('prompt_chars')})

    if 'generation' in timings or data.get('answer_endpoint') or 'answer' in starts:
        start = starts.get('answer', timings.get('retrieval', 0))
        wait, generation = timings.get('queue_wait') or 0, timings.get('generation') or 0
        attempts = data.get('llm_attempts') or []
        answering = next((a for a in attempts if a.get('first_text_s') is not None), None)
        first_ms = int(answering['first_text_s'] * 1000) if answering else None
        streaming_ms = max(0, generation - first_ms) if first_ms is not None else None
        output_tokens = data.get('output_tokens')
        answer_io = log.model_io.get('answer') or {}
        answer_inputs = {'question': data.get('question'), 'documents': [d['ref'] for d in _documents_summary(
            sorted((p for p in log.passages if p.get('kept')), key=lambda p: p.get('prompt_rank') or 0))],
                         'today': getattr(log, 'today', ''), 'instructions_sha': data.get('instructions_sha'),
                         'prompts': getattr(log, 'prompts', [])}
        answer = mlflow.start_span_no_context(
            'answer', span_type=SpanType.LLM, parent_span=root,
            inputs={'messages': answer_io['messages'], **answer_inputs} if answer_io.get('messages') else answer_inputs,
            start_time_ns=at(start))
        if wait:
            child(answer, 'queue_wait', SpanType.CHAIN, start, wait)
        cursor = start + wait
        for n, attempt in enumerate(attempts, 1):
            seconds = int(float(attempt.get('s') or 0) * 1000)
            first = attempt.get('first_text_s')
            call = mlflow.start_span_no_context(f'call_{n}', span_type=SpanType.LLM, parent_span=answer,
                                                start_time_ns=at(cursor))
            if first is not None:
                call_first_ms = min(seconds, int(first * 1000))
                child(call, 'time_to_first_token', SpanType.CHAIN, cursor, call_first_ms)
                child(call, 'generation', SpanType.CHAIN, cursor + call_first_ms, seconds - call_first_ms)
            call.end(status='OK' if attempt.get('outcome') in ('ok', 'continued') else 'ERROR',
                     attributes={SpanAttributeKey.MODEL: attempt.get('endpoint'), 'outcome': attempt.get('outcome'),
                                 'http_status': attempt.get('status'),
                                 'time_to_first_token_ms': int(first * 1000) if first is not None else None},
                     end_time_ns=at(cursor + seconds))
            cursor += seconds
        answer.end(outputs=log.answer or None, status='ERROR' if failed else 'OK',
                   end_time_ns=at(start + wait + generation),
                   attributes={SpanAttributeKey.MODEL: data.get('answer_endpoint'),
                               SpanAttributeKey.CHAT_USAGE: _usage(data.get('input_tokens'), output_tokens),
                               'thinking_tokens': data.get('thinking_tokens'), 'cost_eur': data.get('cost_eur'),
                               'queue_wait_ms': wait, 'time_to_first_token_ms': first_ms, 'streaming_ms': streaming_ms,
                               'tokens_per_second': round(output_tokens * 1000 / streaming_ms, 1) if streaming_ms and output_tokens else None,
                               'first_token_ms': timings.get('first_token'), 'truncated': data.get('truncated'),
                               'continuations': data.get('continuations'), 'fallback': data.get('llm_fallback'),
                               'answer_chars': data.get('answer_chars'), 'instructions_sha': data.get('instructions_sha')})

    if 'translate_back' in timings:
        io = log.model_io.get('translate_back') or {}
        child(root, 'translate_back', SpanType.LLM if io.get('messages') else SpanType.CHAIN, starts.get('translate_back', 0),
              timings['translate_back'], inputs={'messages': io['messages']} if io.get('messages') else None,
              outputs=log.answer or None,
              attributes={SpanAttributeKey.MODEL: io.get('endpoint'), SpanAttributeKey.CHAT_USAGE: _usage(*tokens('translate_back'))})
    if 'persist' in timings:
        child(root, 'persist', SpanType.CHAIN, starts.get('persist', 0), timings['persist'])

    parts = [(data.get('rewrite_input_tokens'), data.get('rewrite_output_tokens')), tokens('translate_in'),
             tokens('translate_back'), (data.get('input_tokens'), data.get('output_tokens'))]
    root.end(outputs=log.answer or None, status='ERROR' if failed else 'OK', end_time_ns=at(total_ms),
             attributes={'timings_ms': json.dumps(timings), 'issues': json.dumps(issues),
                         'cited_refs': json.dumps(data.get('cited_refs') or []), 'sources_count': data.get('sources_count'),
                         'config': json.dumps(data.get('config') or {}, default=str),
                         'cost_eur_total': round((data.get('cost_eur') or 0) + (data.get('rewrite_cost_eur') or 0), 6),
                         'input_tokens_total': sum(int(i or 0) for i, _ in parts),
                         'output_tokens_total': sum(int(o or 0) for _, o in parts)})
    return root.trace_id


def _usage(input_tokens: int | None, output_tokens: int | None) -> Dict[str, int] | None:
    """Token usage of a span, None when no count is known.

    :param input_tokens: input tokens, None when unknown
    :param output_tokens: output tokens, None when unknown
    :return: the input, output and total token counts, or None
    """
    if input_tokens is None and output_tokens is None:
        return None
    i, o = int(input_tokens or 0), int(output_tokens or 0)
    return {'input_tokens': i, 'output_tokens': o, 'total_tokens': i + o}
