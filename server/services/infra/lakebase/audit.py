"""Request logging: errors, chat turns, compare/impact LLM requests (all best-effort, never raising)."""

import asyncio
import json
import logging
import traceback
from typing import Any, Dict, List, Optional

from .. import error_origin
from . import connection
from .columns import CHAT_CHUNK_COLUMNS, CHAT_TURN_COLUMNS, CHAT_TURN_TIMINGS, _db_value, _placeholders

logger = logging.getLogger(__name__)

_ERROR_INSERT = '''
    INSERT INTO errors
        (endpoint, error_type, error_msg, user_id, workspace_id,
         old_filename, new_filename, file_type, stack_trace, llm_request_id,
         severity, stage, http_status, upstream, trace_id, session_id, context, app_version,
         origin, fingerprint, user_message)
    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16, $17::jsonb, $18,
            $19, $20, $21)
'''


def _error_row(
    *,
    endpoint: str = '',
    error_type: str = '',
    error_msg: str = '',
    user_id: str = '',
    workspace_id: str = '',
    old_filename: str = '',
    new_filename: str = '',
    file_type: str = '',
    stack_trace: str = '',
    llm_request_id: Optional[int] = None,
    severity: str = 'error',
    stage: str = '',
    http_status: Optional[int] = None,
    upstream: str = '',
    trace_id: str = '',
    session_id: str = '',
    context: Optional[Dict[str, Any]] = None,
    origin: str = '',
    user_message: str = '',
) -> tuple:
    """Parameters of _ERROR_INSERT, in order. ``origin``: where in the code (``path:line in
    function``), taken from the stack trace when absent."""
    origin = origin or error_origin.from_stack_trace(stack_trace)
    return (
        endpoint or None,
        error_type or None,
        (error_msg or '')[:2000],
        user_id or None,
        workspace_id or None,
        old_filename or None,
        new_filename or None,
        file_type or None,
        (stack_trace or '')[:4000] or None,
        llm_request_id,
        severity or 'error',
        stage or None,
        http_status or None,
        upstream or None,
        trace_id or None,
        session_id or None,
        json.dumps(context, ensure_ascii=False, default=str) if context else None,
        connection.app_version(),
        origin or None,
        error_origin.fingerprint(endpoint, stage, error_type, origin),
        user_message or None,
    )


def store_error(*, exc: Optional[BaseException] = None, **kwargs: Any) -> Optional[asyncio.Task]:
    """Persist an application error to the errors table, in the background (never raises).
    Arguments: those of ``_error_row`` (all optional, keyword only); ``exc`` adds its stack trace.
    Called synchronously so the origin can be read from the caller's frame when there is no
    stack trace."""
    if exc is not None and not kwargs.get('stack_trace'):
        kwargs['stack_trace'] = ''.join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    kwargs.setdefault('origin', error_origin.from_exception(exc) or error_origin.from_stack_trace(
        kwargs.get('stack_trace', '')) or error_origin.caller(1))
    try:
        return asyncio.get_running_loop().create_task(_write_error(_error_row(**kwargs)))
    except RuntimeError:                   # no event loop (scripts, tests)
        return None


async def _write_error(row: tuple) -> None:
    # Bounded acquire: an exhausted pool must not pile up tasks on top of the original failure.
    pool = connection.get_pool()
    if not pool:
        return
    try:
        async with pool.acquire(timeout=5.0) as conn:
            await conn.execute(_ERROR_INSERT, *row)
    except Exception as e:
        logger.debug('store_error failed (non-blocking): %s', e)


async def store_chat_turn(
    log: Any,
    *,
    endpoint: str = '',
    user_message_id: Optional[int] = None,
    assistant_message_id: Optional[int] = None,
) -> Optional[int]:
    """Write one chat turn (a finished ``turn_log.TurnLog``): its chat_turns row, its
    chat_retrieved_chunks rows and one errors row per warning/error issue — in one
    transaction. Best-effort, never raises; returns the chat_turns id."""
    pool = connection.get_pool()
    if not pool:
        return None
    data = {**log.data, 'app_version': log.data.get('app_version') or connection.app_version()}
    columns = [c for c, _ in CHAT_TURN_COLUMNS] + [f'{t}_ms' for t in CHAT_TURN_TIMINGS]
    typedefs = [t for _, t in CHAT_TURN_COLUMNS] + ['INTEGER'] * len(CHAT_TURN_TIMINGS)
    values = ([_db_value(data.get(c), t) for c, t in CHAT_TURN_COLUMNS]
              + [log.timings_ms.get(t) for t in CHAT_TURN_TIMINGS])
    fixed = ['trace_id', 'user_message_id', 'assistant_message_id', 'status', 'error_stage', 'error_type',
             'error_msg', 'warnings']
    fixed_values = [log.trace_id, user_message_id, assistant_message_id, log.status or 'error',
                    log.error.get('stage'), log.error.get('type'), log.error.get('message'), log.codes]
    turn_sql = (f'INSERT INTO chat_turns ({", ".join(fixed + columns)}) '
                f'VALUES ({_placeholders(["TEXT"] * len(fixed) + typedefs)}) RETURNING id')

    chunk_cols = [c for c, _ in CHAT_CHUNK_COLUMNS]
    chunk_types = [t for _, t in CHAT_CHUNK_COLUMNS]
    chunk_sql = (f'INSERT INTO chat_retrieved_chunks (turn_id, {", ".join(chunk_cols)}) '
                 f'VALUES ($1, {_placeholders(chunk_types, offset=1)})')
    chunk_rows = [tuple(_db_value({**p, 'trace_id': log.trace_id}.get(c), t) for c, t in CHAT_CHUNK_COLUMNS)
                  for p in log.passages]

    error_rows = [
        _error_row(endpoint=endpoint, error_type=i['error_type'], error_msg=i['message'],
                   user_id=data.get('user_id') or '', workspace_id=data.get('workspace_id') or '',
                   severity=i['severity'], stage=i['stage'], http_status=i['http_status'],
                   upstream=i['upstream'] or '', trace_id=log.trace_id, session_id=data.get('session_id') or '',
                   stack_trace=(i.get('context') or {}).get('stack_trace', ''), origin=i.get('origin') or '',
                   user_message=(data.get('user_message') or '') if i['severity'] == 'error' else '',
                   context={'code': i['code'], 'at_ms': i['at_ms'],
                            **{k: v for k, v in (i.get('context') or {}).items() if k != 'stack_trace'}})
        for i in log.issues if i['severity'] in ('error', 'warning')
    ]
    try:
        async with pool.acquire(timeout=10.0) as conn, conn.transaction():
            turn_id = await conn.fetchval(turn_sql, *fixed_values, *values)
            if chunk_rows:
                await conn.executemany(chunk_sql, [(turn_id, *row) for row in chunk_rows])
            if error_rows:
                await conn.executemany(_ERROR_INSERT, error_rows)
        return turn_id
    except Exception as e:
        logger.warning('store_chat_turn failed (non-blocking): %s', e)
        return None


async def store_llm_request(
    *,
    old_file_hash: str = '',
    new_file_hash: str = '',
    file_type: str = '',
    endpoint_name: str = '',
    messages_json: str = '',
) -> Optional[int]:
    """Insert an LLM request audit record; returns the inserted id (or None on failure)."""
    pool = connection.get_pool()
    if not pool:
        return None
    try:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                '''
                INSERT INTO llm_requests
                    (old_file_hash, new_file_hash, file_type, endpoint_name, messages_json)
                VALUES ($1, $2, $3, $4, $5)
                RETURNING id
                ''',
                old_file_hash or None,
                new_file_hash or None,
                file_type or None,
                endpoint_name or None,
                messages_json or None,
            )
        return row['id'] if row else None
    except Exception as e:
        logger.debug('store_llm_request failed: %s', e)
        return None


async def update_llm_request_usage(
    llm_request_id: Optional[int],
    *,
    input_tokens: int = 0,
    output_tokens: int = 0,
    thinking_tokens: int = 0,
    total_tokens: int = 0,
    cost_eur: float = 0.0,
    http_status: int = 0,
    error_type: str = '',
    error_msg: str = '',
) -> None:
    """Update token usage and status on an llm_requests row (best-effort, never raises)."""
    pool = connection.get_pool()
    if not pool or not llm_request_id:
        return
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                '''
                UPDATE llm_requests
                SET input_tokens    = COALESCE($2, input_tokens),
                    output_tokens   = COALESCE($3, output_tokens),
                    thinking_tokens = COALESCE($4, thinking_tokens),
                    total_tokens    = COALESCE($5, total_tokens),
                    cost_eur        = COALESCE($6, cost_eur),
                    http_status     = COALESCE($7, http_status),
                    error_type      = COALESCE($8, error_type),
                    error_msg       = COALESCE($9, error_msg)
                WHERE id = $1
                ''',
                llm_request_id,
                input_tokens or None,
                output_tokens or None,
                thinking_tokens or None,
                total_tokens or None,
                cost_eur or None,
                http_status or None,
                error_type or None,
                (error_msg or '')[:2000] or None,
            )
    except Exception as e:
        logger.debug('update_llm_request_usage failed: %s', e)


async def update_llm_request_run(
    llm_request_id: Optional[int],
    *,
    status: str,
    queue_wait_ms: Optional[int] = None,
    build_ms: Optional[int] = None,
    first_token_ms: Optional[int] = None,
    generation_ms: Optional[int] = None,
    total_ms: Optional[int] = None,
    chunked_parts: Optional[int] = None,
) -> None:
    """Record the outcome and step durations of an /compare/analyze stream on its
    llm_requests row (best-effort, never raises)."""
    pool = connection.get_pool()
    if not pool or not llm_request_id:
        return
    try:
        async with pool.acquire(timeout=5.0) as conn:
            await conn.execute(
                '''
                UPDATE llm_requests
                SET status = $2, queue_wait_ms = $3, build_ms = $4, first_token_ms = $5,
                    generation_ms = $6, total_ms = $7, chunked_parts = $8
                WHERE id = $1
                ''',
                llm_request_id, status, queue_wait_ms, build_ms, first_token_ms,
                generation_ms, total_ms, chunked_parts,
            )
    except Exception as e:
        logger.debug('update_llm_request_run failed: %s', e)


async def store_impact_request(
    *,
    method: str,
    user_id: str = '',
    workspace_id: str = '',
    old_file_hash: str = '',
    new_file_hash: str = '',
    changes_chars: int = 0,
    truncated: bool = False,
    chunks_returned: int = 0,
    num_documents: int = 0,
    duration_s: float = 0.0,
    endpoint_name: str = '',
    input_tokens: int = 0,
    output_tokens: int = 0,
    total_tokens: int = 0,
    cost_eur: float = 0.0,
    http_status: int = 200,
    error_type: str = '',
    error_msg: str = '',
    documents: List[Dict[str, Any]] | None = None,
    status: str = '',
    index_name: str = '',
    queries_used: Optional[int] = None,
    queries_failed: Optional[int] = None,
    candidates: Optional[int] = None,
    not_judged: Optional[int] = None,
    judge_failed: Optional[int] = None,
    extract_ms: Optional[int] = None,
    search_ms: Optional[int] = None,
    judge_ms: Optional[int] = None,
) -> Optional[int]:
    """Log one /compare/impact call — success or failure (best-effort, never raises).

    Returns the impact_requests id (None if the row could not be written) so
    feedback on the result can be linked back to it.

    When `documents` is given (the per-document judgments from
    synthesize_impact_with_llm), also persists one row per document to
    impact_document_results — the linked Intraqual document(s), the LLM's
    verdict/description, and nothing else, so a business reader auditing the
    trace isn't shown retrieval internals (chunk_count, query_hits, max_score)
    that don't mean anything to them.
    """
    pool = connection.get_pool()
    if not pool:
        return None
    try:
        async with pool.acquire() as conn, conn.transaction():
            request_id = await conn.fetchval(
                '''
                INSERT INTO impact_requests
                    (user_id, workspace_id, method, old_file_hash, new_file_hash,
                     changes_chars, truncated, chunks_returned, num_documents, duration_s,
                     endpoint_name, input_tokens, output_tokens, total_tokens, cost_eur,
                     http_status, error_type, error_msg,
                     status, app_version, index_name, queries_used, queries_failed, candidates,
                     not_judged, judge_failed, extract_ms, search_ms, judge_ms)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16, $17, $18,
                        $19, $20, $21, $22, $23, $24, $25, $26, $27, $28, $29)
                RETURNING id
                ''',
                user_id or None,
                workspace_id or None,
                method,
                old_file_hash or None,
                new_file_hash or None,
                changes_chars or None,
                truncated,
                chunks_returned or None,
                num_documents or None,
                duration_s or None,
                endpoint_name or None,
                input_tokens or None,
                output_tokens or None,
                total_tokens or None,
                cost_eur or None,
                http_status or None,
                error_type or None,
                (error_msg or '')[:2000] or None,
                status or ('ok' if (http_status or 200) < 400 else 'error'),
                connection.app_version(),
                index_name or None,
                queries_used,
                queries_failed,
                candidates,
                not_judged,
                judge_failed,
                extract_ms,
                search_ms,
                judge_ms,
            )
            if documents:
                await conn.executemany(
                    '''
                    INSERT INTO impact_document_results
                        (request_id, ref, division, url, impacted, confidence, section, reason, other_documents_json,
                         status, judge_ms, judge_attempts, input_tokens, output_tokens)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14)
                    ''',
                    [
                        (
                            request_id,
                            d.get('ref') or None,
                            d.get('division') or None,
                            d.get('url') or None,
                            bool(d.get('impacted', False)),
                            d.get('confidence') or None,
                            '; '.join(d.get('sections') or []) or None,
                            (d.get('reason') or '')[:2000] or None,
                            json.dumps(d.get('other_languages') or []) if d.get('other_languages') else None,
                            d.get('status') or None,
                            d.get('judge_ms'),
                            d.get('judge_attempts'),
                            d.get('input_tokens'),
                            d.get('output_tokens'),
                        )
                        for d in documents
                    ],
                )
            return request_id
    except Exception as e:
        logger.debug('store_impact_request failed: %s', e)
        return None
