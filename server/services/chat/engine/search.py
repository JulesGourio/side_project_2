"""Vector Search calls of the chat: hybrid queries with the reranker, merged by best rank."""

import asyncio
import json
import logging
import time
from typing import Dict, List, Tuple

import httpx

from server.services.infra import vs_gate
from server.services.infra.config import VECTOR_SEARCH_QUERY_TIMEOUT_S
from server.services.infra.vector_search import _ARCHIVE_NOTICE_MARKER, _COLUMNS
from server.services.chat.turn_log import TurnLog, passage_title
from server.services.chat.engine.config import raw_top_k, rerank_top_k
from server.services.chat.engine.types import Hit, QueryPayload, SearchRow, VsFilters

logger = logging.getLogger(__name__)

_RERANK_COLUMNS = ['REF', 'semantic_headers', 'chunk_text']
_RERANKER_MODEL = 'databricks_reranker'
_MAX_QUERY_CHARS = 20000         # Vector Search rejects query_text past ~29k chars


class ChatVsiError(Exception):
    """A failure surfaced to the user as an ``error`` event."""

    def __init__(self, message: str, error_type: str, http_status: int = 0):
        """Stores the message, the error name and the HTTP status of a failure.

        :param message: text shown to the user
        :param error_type: name of the error, sent in the error event
        :param http_status: HTTP status of the failed call, 0 when there is none
        """
        super().__init__(message)
        self.message = message
        self.error_type = error_type
        self.http_status = http_status


# --- Vector Search ---

async def _post_query(host: str, token: str, index: str, payload: QueryPayload) -> httpx.Response:
    """One Vector Search query through the app-wide gate (``vs_gate``: at most
    ``VS_MAX_CONCURRENT_QUERIES`` in flight, retried on 429 / 5xx / timeout).

    :param host: Databricks workspace URL
    :param token: Databricks access token
    :param index: full name of the Vector Search index
    :param payload: query body sent to the index
    :return: the HTTP response
    """
    return await vs_gate.post(host, token, index, payload, VECTOR_SEARCH_QUERY_TIMEOUT_S)


def _rows(resp: httpx.Response) -> List[SearchRow]:
    """The rows of a query response, keyed by column name.

    :param resp: HTTP response of a query
    :return: the rows as dicts, archive notices left out
    """
    data = resp.json()
    columns = [c['name'] for c in data.get('manifest', {}).get('columns', [])]
    chunks = [dict(zip(columns, row)) for row in data.get('result', {}).get('data_array', [])]
    return [c for c in chunks if _ARCHIVE_NOTICE_MARKER not in (c.get('chunk_text') or '')]


class RerankUnavailable(Exception):
    """The index refused the reranker parameter."""


async def _query(host: str, token: str, index: str, query_text: str, k: int, filters: VsFilters,
                 rerank: bool) -> List[SearchRow]:
    """One HYBRID Vector Search query, with the reranker when asked.

    :param host: Databricks workspace URL
    :param token: Databricks access token
    :param index: full name of the Vector Search index
    :param query_text: text searched, cut to ``_MAX_QUERY_CHARS`` characters (a warning is logged when it is)
    :param k: number of results asked
    :param filters: Vector Search filters, empty for none
    :param rerank: whether the reranker orders the results
    :return: the rows of the response
    """
    if len(query_text) > _MAX_QUERY_CHARS:
        logger.warning('chat: query of %d characters cut to %d for Vector Search', len(query_text), _MAX_QUERY_CHARS)
    payload: QueryPayload = {'query_text': query_text[:_MAX_QUERY_CHARS], 'columns': _COLUMNS,
                               'num_results': k, 'query_type': 'HYBRID'}
    if filters:
        payload['filters_json'] = json.dumps(filters)
    if rerank:
        payload['reranker'] = {'model': _RERANKER_MODEL, 'parameters': {'columns_to_rerank': _RERANK_COLUMNS}}
    resp = await _post_query(host, token, index, payload)
    if rerank and resp.status_code == 400 and 'rerank' in resp.text.lower():
        raise RerankUnavailable(resp.text[:300])
    resp.raise_for_status()
    return _rows(resp)


def _tag(rows: List[SearchRow], q: int | None, via: str) -> List[SearchRow]:
    """Records on each row which query found it (``q``: index in the turn's queries, None for a
    lookup), how (``via``: rerank / raw / ref_lookup), at which rank, with which
    score — kept in chat_retrieved_chunks.hits.

    :param rows: rows returned by one query
    :param q: index of the query in the turn's queries, None for a lookup
    :param via: how the rows were found: ``rerank``, ``raw`` or ``ref_lookup``
    :return: the same rows, each with its ``_hits`` set
    """
    for rank, row in enumerate(rows):
        row['_hits'] = [{'q': q, 'via': via, 'rank': rank, 'score': row.get('score')}]
    return rows


def _merge_by_rank(lists: List[List[SearchRow]]) -> List[SearchRow]:
    """One row per chunk, ordered by its best rank in any list; the ``_hits`` of every copy kept.

    :param lists: result lists, each in rank order
    :return: one row per chunk, best rank first
    """
    ranked: Dict[str, Tuple[int, SearchRow]] = {}
    hits: Dict[str, List[Hit]] = {}
    for result in lists:
        for rank, row in enumerate(result):
            cid = row['chunk_id']
            seen = hits.setdefault(cid, [])
            seen.extend(h for h in row.get('_hits') or [] if h not in seen)
            if cid not in ranked or rank < ranked[cid][0]:
                ranked[cid] = (rank, row)
    merged = []
    for _, row in sorted(ranked.values(), key=lambda x: x[0]):
        if hits[row['chunk_id']]:
            row['_hits'] = hits[row['chunk_id']]
        merged.append(row)
    return merged


def _search_error(index: str, exc: BaseException) -> ChatVsiError:
    """The ``ChatVsiError`` to raise for a failed Vector Search call.

    :param index: index name, for the log
    :param exc: exception of the failed call
    :return: the error, with the message shown to the user
    """
    if isinstance(exc, ChatVsiError):
        return exc
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        logger.error('chat: Vector Search %s returned %d: %s', index, status, exc.response.text[:500])
        return ChatVsiError(f'Document search failed (Vector Search returned {status}).', 'VectorSearchError', status)
    if isinstance(exc, httpx.TimeoutException):
        logger.error('chat: Vector Search %s timed out: %s', index, exc)
        return ChatVsiError('Document search timed out — please try again.', 'TimeoutError')
    logger.error('chat: Vector Search %s failed: %s', index, exc)
    return ChatVsiError(f'Document search failed: {exc}', type(exc).__name__)


def _status_of(exc: BaseException) -> int:
    """The HTTP status of a failed call, 0 for any other error.

    :param exc: exception raised by a call
    :return: the status code, or 0
    """
    return exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else 0


async def _recorded_query(host: str, token: str, index: str, filters: VsFilters, log: TurnLog,
                          i: int | None, q: str, k: int, rerank: bool) -> List[SearchRow]:
    """One Vector Search call, recorded on the turn (query, mode, size, start, duration, outcome, hits).

    :param host: Databricks workspace URL
    :param token: Databricks access token
    :param index: full name of the Vector Search index
    :param filters: Vector Search filters, empty for none
    :param log: turn log that records the call
    :param i: index of the query in the turn's queries, None for a lookup
    :param q: query text
    :param k: number of results asked
    :param rerank: whether the reranker is used
    :return: the rows of the response
    """
    entry = {'query_index': i, 'query': q[:300], 'mode': 'rerank' if rerank else 'raw', 'k': k,
             'start_ms': log.elapsed_ms()}
    if len(q) > _MAX_QUERY_CHARS:
        entry['truncated_from'] = len(q)
        if 'query_truncated' not in log.codes:
            log.issue('query_truncated', 'search',
                      f'a query of {len(q)} characters was cut to {_MAX_QUERY_CHARS} before the Vector Search call')
    t0 = time.monotonic()
    log.vs_calls.append(entry)
    try:
        rows = await _query(host, token, index, q, k, filters, rerank=rerank)
    except BaseException as exc:
        entry.update(ms=int((time.monotonic() - t0) * 1000), ok=False, error=f'{type(exc).__name__}: {str(exc)[:200]}')
        raise
    entry.update(ms=int((time.monotonic() - t0) * 1000), ok=True, results=len(rows),
                 hits=[{'rank': n, 'ref': r.get('REF'), 'title': passage_title(r.get('chunk_text')),
                        'score': r.get('score'), 'chunk_id': r.get('chunk_id')} for n, r in enumerate(rows, 1)])
    return rows


def start_question_search(host: str, token: str, index: str, filters: VsFilters, question: str,
                          log: TurnLog) -> 'asyncio.Task[List[SearchRow]]':
    """Starts the reranked search of the question as asked, now: the rewrite runs while it does, and ``search``
    (``first=``) picks the result up instead of querying again.

    :param host: Databricks workspace URL
    :param token: Databricks access token
    :param index: full name of the Vector Search index
    :param filters: Vector Search filters, empty for none
    :param question: the question as asked, without its date line
    :param log: turn log that records the call
    :return: task that resolves to the reranked rows of the question
    """
    task = asyncio.ensure_future(_recorded_query(host, token, index, filters, log, 0, question, rerank_top_k(), True))
    task.add_done_callback(lambda t: t.cancelled() or t.exception())      # Read the exception so an unused failure is not logged.
    return task


async def search(host: str, token: str, index: str, queries: List[str], filters: VsFilters,
                 raw_queries: List[str] | None = None, log: TurnLog | None = None,
                 rerank_k: int | None = None, raw_k: int | None = None,
                 first: 'asyncio.Task[List[SearchRow]] | None' = None) -> List[SearchRow]:
    """Reranked top k for every query + raw top k for ``raw_queries`` (default: every query),
    merged by best rank. A query (or the
    reranked side) that fails is dropped when the rest answered; raises ``ChatVsiError`` only
    if everything failed. Without raw passages (``raw_top_k() == 0``), a refused reranker falls
    back to the raw search with the reranked size. ``first``: the reranked search of ``queries[0]``, already
    started by ``start_question_search`` (same size).

    :param host: Databricks workspace URL
    :param token: Databricks access token
    :param index: full name of the Vector Search index
    :param queries: queries searched with the reranker; the first one is the question
    :param filters: Vector Search filters, empty for none
    :param raw_queries: queries also searched without the reranker, None for every query
    :param log: turn log to fill, created when None
    :param rerank_k: passages kept per reranked query, None for CHAT_RERANK_TOP_K
    :param raw_k: passages kept per raw query, None for CHAT_RAW_TOP_K; 0 skips the raw searches
    :param first: task of the reranked question search started earlier, used instead of a new call
    :return: the merged rows, best rank first
    """
    log = log if log is not None else TurnLog()
    rerank_k = rerank_top_k() if rerank_k is None else rerank_k
    raw_k = raw_top_k() if raw_k is None else raw_k
    rerank_ok = True

    async def call(i: int | None, q: str, k: int, rerank: bool) -> List[SearchRow]:
        """One recorded Vector Search call.

        :param i: index of the query in the turn's queries, None for a lookup
        :param q: query text
        :param k: number of results asked
        :param rerank: whether the reranker is used
        :return: the rows of the response
        """
        return await _recorded_query(host, token, index, filters, log, i, q, k, rerank)

    async def reranked(i: int, q: str) -> List[SearchRow]:
        """The reranked search of a query.

        :param i: index of the query in ``queries``
        :param q: query text
        :return: the rows of that query; a raw search when the index refused the reranker
        """
        nonlocal rerank_ok
        if not rerank_ok:
            return [] if raw_k else _tag(await call(i, q, rerank_k, False), i, 'raw')
        try:
            return _tag(await (first if first is not None and i == 0 else call(i, q, rerank_k, True)), i, 'rerank')
        except RerankUnavailable as exc:
            if rerank_ok:
                log.issue('rerank_refused', 'search', f'raw search only: {exc}', upstream=index, exc=exc)
            rerank_ok = False
            logger.warning('chat: reranker refused by %s (%s) — raw search only', index, exc)
            return [] if raw_k else _tag(await call(i, q, rerank_k, False), i, 'raw')

    async def raw(i: int, q: str) -> List[SearchRow]:
        """The raw search of a query.

        :param i: index of the query in ``queries``, None when ``q`` is not one of them
        :param q: raw query text
        :return: the rows of the raw search
        """
        return _tag(await call(i, q, raw_k, False), i, 'raw')

    calls = [reranked(i, q) for i, q in enumerate(queries)]
    if raw_k:
        position = {q.casefold(): i for i, q in enumerate(queries)}
        calls += [raw(position.get(q.casefold()), q) for q in (queries if raw_queries is None else raw_queries)]
    results = await asyncio.gather(*calls, return_exceptions=True)
    for r in results:
        if isinstance(r, asyncio.CancelledError):
            raise r
    ok = [r for r in results if not isinstance(r, BaseException)]
    failed = [r for r in results if isinstance(r, BaseException)]
    log.data.update({'vs_calls_expected': len(results), 'vs_calls_ok': len(ok), 'rerank_ok': rerank_ok})
    if not ok:
        raise _search_error(index, failed[0]) from failed[0]
    if failed:
        logger.warning('chat: %d/%d Vector Search queries failed, kept the others: %s', len(failed),
                       len(results), failed[0])
        log.issue('vs_partial', 'search', f'{len(failed)}/{len(results)} Vector Search queries failed: {failed[0]}',
                  error_type=type(failed[0]).__name__, http_status=_status_of(failed[0]), upstream=index,
                  context={'failed': len(failed), 'sent': len(results),
                           'errors': sorted({f'{type(f).__name__}: {str(f)[:200]}' for f in failed})},
                  exc=failed[0])
    n = len(queries)
    return _merge_by_rank([_merge_by_rank(ok_part) for ok_part in (
        [r for r in results[:n] if not isinstance(r, BaseException)],
        [r for r in results[n:] if not isinstance(r, BaseException)]) if ok_part])


async def fetch_chunks(host: str, token: str, index: str, query: str, chunk_ids: List[str], filters: VsFilters,
                       via: str = 'code_lookup', log: TurnLog | None = None) -> List[SearchRow]:
    """HYBRID search restricted to the given passages (``via``: code_lookup), ranked against the question.
    Failures are logged, never raised.

    :param host: Databricks workspace URL
    :param token: Databricks access token
    :param index: full name of the Vector Search index
    :param query: question text, used to rank the passages
    :param chunk_ids: passages the results are restricted to
    :param filters: other Vector Search filters
    :param via: label recorded on the hits
    :param log: turn log that records a failure, optional
    :return: the rows, or an empty list when the lookup failed
    """
    try:
        return _tag(await _query(host, token, index, query, len(chunk_ids), {**filters, 'chunk_id': chunk_ids},
                                 rerank=False), None, via)
    except Exception as exc:  # noqa: BLE001 — the turn goes on without the lookup
        logger.warning('chat: passage lookup on %s failed: %s', index, exc)
        if log is not None:
            log.issue(f'{via}_failed', via, f'{len(chunk_ids)} passages: {exc}', error_type=type(exc).__name__,
                      http_status=_status_of(exc), upstream=index, context={'chunk_ids': chunk_ids}, exc=exc)
        return []


async def fetch_documents(host: str, token: str, index: str, query: str, refs: List[str], k: int,
                          filters: VsFilters, via: str = 'ref_lookup',
                          log: TurnLog | None = None) -> List[SearchRow]:
    """HYBRID search restricted to the given REFs (``via``: ref_lookup).
    Failures are logged, never raised.

    :param host: Databricks workspace URL
    :param token: Databricks access token
    :param index: full name of the Vector Search index
    :param query: question text, searched within the named documents
    :param refs: REFs the results are restricted to
    :param k: passages fetched
    :param filters: other Vector Search filters
    :param via: label recorded on the hits
    :param log: turn log that records a failure, optional
    :return: the rows, or an empty list when the lookup failed
    """
    try:
        return _tag(await _query(host, token, index, query, k, {**filters, 'REF': refs}, rerank=False), None, via)
    except Exception as exc:  # noqa: BLE001 — the turn goes on without the lookup
        logger.warning('chat: document lookup %s on %s failed: %s', refs, index, exc)
        if log is not None:
            log.issue(f'{via}_failed', via, f'{refs}: {exc}', error_type=type(exc).__name__,
                      http_status=_status_of(exc), upstream=index, context={'refs': refs}, exc=exc)
        return []
