"""Chat VSI — the chat engine: answers from the Vector Search index, no Knowledge Assistant.

What happens for each question (measures behind the configuration: ``docs/chat_vsi_tests.md``):

1. **Rewrite** — GPT-6 Luna turns the last question, with the conversation as context, into
   one standalone search query in French and one in English (acronyms expanded when certain).
2. **Search** — three queries (the question as asked + the two rewrites) in HYBRID mode on the
   single passage index: for each, the top ``rerank_top_k()`` passages (12) reranked by the
   Databricks reranker (reading REF, section headings and text); for the French rewrite only
   (``raw_on()``), also the raw top ``raw_top_k()`` (10). Everything is merged by best rank (``union``), then cut to
   ``max_search_passages()`` (0 = no cap, the default). The chat's division (AS / IS) is a filter on
   the ``division`` column (ALL: no filter).
3. **Named documents** — REFs named in the question or the earlier turns (MI-14242…) get their
   own filtered search, ranked first; documents whose catalog title matches the question
   (``chat_vsi_titles.py``) get one too, added at the end.
4. **One language per document** — only the best-ranked language variant of a document is kept.
5. **Prompt** — the division's instructions (``server/config/chat_vsi/instructions_<div>.md``)
   + the common answering rules (``answer_rules.md``: documents only, off-topic refusal,
   document types, citations) + the numbered documents + the question + one line naming the
   language the answer must be written in.
6. **Answer** — GPT-6 Luna, GPT-5.6 Luna as fallback (``chat_vsi_llm.py``: retries, fallback,
   continuation of a cut answer, concurrency limit); ``[n]`` markers are turned into citations
   (``CitationStreamParser``).

``stream_chat_vsi`` yields the event stream ``chat.py`` expects (text deltas, ``sources`` +
``citations``, ``metadata``, ``error``, ``[DONE]``) and fills the turn's ``TurnLog``
(``turn_log.py``: every step, its duration, the passages retrieved, what went wrong — saved
in Lakebase ``chat_turns`` / ``chat_retrieved_chunks`` / ``errors`` by ``chat.py``). Settings (environment, read at call time):
``CHAT_VSI_INDEX``, ``CHAT_VSI_LLM_ENDPOINT`` / ``CHAT_VSI_LLM_FALLBACK_ENDPOINTS``,
``CHAT_VSI_REWRITE_ENDPOINT`` / ``CHAT_VSI_REWRITE_FALLBACK_ENDPOINTS``,
``CHAT_VSI_ANSWER_MAX_TOKENS``, ``CHAT_VSI_REWRITE_MAX_TOKENS``, ``VS_MAX_CONCURRENT_QUERIES`` /
``VS_QUERY_RETRIES`` (``vs_gate.py``),
``CHAT_VSI_RERANK_TOP_K`` / ``CHAT_VSI_RAW_TOP_K`` / ``CHAT_VSI_RAW_ON`` / ``CHAT_VSI_MAX_SEARCH_PASSAGES`` (search sizes,
defaults = the measured configuration; other values are for ``retrieval_eval`` comparisons),
plus the resilience settings documented in ``chat_vsi_llm.py``. The earlier engine versions
(baseline, measured options) are kept in ``archive/``.
"""

import asyncio
import hashlib
import json
import logging
import os
import re
import time
from functools import lru_cache
from pathlib import Path
from typing import Any, AsyncGenerator, Dict, List, Optional, Tuple

import httpx

from . import chat_vsi_llm, vs_gate
from .chat_vsi_titles import documents_titled
from .doc_catalog import _catalog, canon_ref, document_info
from .turn_log import TurnLog
from .vector_search import _ARCHIVE_NOTICE_MARKER, _COLUMNS, _QUERY_TIMEOUT_S

logger = logging.getLogger(__name__)

_PROMPTS_DIR = Path(__file__).resolve().parent.parent / 'config' / 'chat_vsi'
_DIVISIONS = ('ALL', 'AS', 'IS')
_DEFAULT_INDEX = 'dev_landingzone.qualibot.chunks_index'
_DEFAULT_LLM_ENDPOINT = 'databricks-gpt-6-luna'      # no Claude model in the chatbot (2026-10-08)
_DEFAULT_ANSWER_MAX_TOKENS = 8000                     # reasoning counts inside it
_DEFAULT_REWRITE_MAX_TOKENS = 2000
_RERANK_TOP_K = 12               # reranked passages kept per query (20 measured worse, 2026-10-08)
_RAW_TOP_K = 10                  # raw HYBRID passages per query (0 = reranked passages only)
_RERANK_COLUMNS = ['REF', 'semantic_headers', 'chunk_text']
_RERANKER_MODEL = 'databricks_reranker'
_REF_LOOKUP_MAX = 6              # REFs (language variants included) filtered on
_REF_LOOKUP_K = 8                # passages fetched from the named documents
_TITLE_LOOKUP_DOCS = 3           # best title matches searched
_TITLE_LOOKUP_K = 6              # passages fetched from them
_MAX_QUERY_CHARS = 20000         # Vector Search rejects query_text past ~29k chars
_OPERATION = 'Chat'              # label used in user-facing error messages

REWRITE_PROMPT = """Using the conversation for context, rewrite the user's LAST question as one standalone search
query, for a search engine over Latécoère quality documents written in French or in English.
Write it twice, as exactly two lines:
FR: <the query in French>
EN: <the same query in English>
Keep document codes, acronyms and technical terms as they are. When the question uses an
acronym and you are certain of its meaning, keep it and add its expanded form in each language.
Return only the two lines."""

LANGUAGE_REMINDER = ('Reminder: write your whole answer in the language of the question above '
                     '(the question, not the documents).')
NAMED_LANGUAGE_REMINDER = ('Reminder: write your whole answer in {language}, the language of the question above '
                           '(not the language of the documents).')

# Prefix chat.py's _with_today_date puts on the last user turn.
_DATE_PREFIX_RE = re.compile(r'^\[Date: \d{4}-\d{2}-\d{2}\]\n\n')
# Inline citation markers chat.py bakes into stored answers (⟦n⟧) — present in history.
_STORED_MARKER_RE = re.compile(r'⟦\d+⟧')
# A complete citation marker in the model's output, and a possible start of one at the
# very end of a chunk (held back until the next chunk decides).
_MARKER_RE = re.compile(r'\[(\d+)\]')
_PARTIAL_MARKER_RE = re.compile(r'\[\d*$')


class ChatVsiError(Exception):
    """A failure surfaced to the user as an ``error`` event."""

    def __init__(self, message: str, error_type: str, http_status: int = 0):
        super().__init__(message)
        self.message = message
        self.error_type = error_type
        self.http_status = http_status


# --- Settings ---

def normalize_division(division: Optional[str]) -> str:
    d = (division or 'ALL').upper()
    return d if d in _DIVISIONS else 'ALL'


def index_name() -> str:
    return os.getenv('CHAT_VSI_INDEX', _DEFAULT_INDEX).strip()


def llm_endpoint() -> str:
    return os.getenv('CHAT_VSI_LLM_ENDPOINT', _DEFAULT_LLM_ENDPOINT).strip()


def rewrite_endpoint() -> str:
    return os.getenv('CHAT_VSI_REWRITE_ENDPOINT', '').strip() or llm_endpoint()


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)) or default)
    except ValueError:
        return default


def answer_max_tokens() -> int:
    return _int_env('CHAT_VSI_ANSWER_MAX_TOKENS', _DEFAULT_ANSWER_MAX_TOKENS)


def rewrite_max_tokens() -> int:
    return _int_env('CHAT_VSI_REWRITE_MAX_TOKENS', _DEFAULT_REWRITE_MAX_TOKENS)


def rerank_top_k() -> int:
    return max(1, _int_env('CHAT_VSI_RERANK_TOP_K', _RERANK_TOP_K))


def raw_top_k() -> int:
    return max(0, _int_env('CHAT_VSI_RAW_TOP_K', _RAW_TOP_K))


_RAW_ON_ALL = ('question', 'fr', 'en')
_RAW_ON = 'fr'                   # raw search on the French rewrite only (rawfr: as good, 4 queries instead of 6)


def raw_on() -> List[str]:
    """Which queries also get a raw search: ``question`` (as asked), ``fr``, ``en`` (the
    rewrites). Fewer = fewer Vector Search queries per question."""
    names = [n.strip().lower() for n in os.getenv('CHAT_VSI_RAW_ON', _RAW_ON).replace('+', ',').split(',')]
    return [n for n in _RAW_ON_ALL if n in names]


def max_search_passages() -> int:
    """Cap on the merged search passages (REF and title lookups come on top); 0 = no cap."""
    return max(0, _int_env('CHAT_VSI_MAX_SEARCH_PASSAGES', 0))


def rewrite_timeout_s() -> float:
    return float(_int_env('CHAT_VSI_REWRITE_TIMEOUT_S', 45))


def division_filter(division: str) -> Dict[str, Any]:
    """Vector Search filter of the chat's division (ALL: none)."""
    d = normalize_division(division)
    return {} if d == 'ALL' else {'division': [d]}


def settings() -> Dict[str, Any]:
    """What the engine runs with — saved with evaluation results."""
    return {'index': index_name(), 'llm': llm_endpoint(),
            'llm_fallbacks': chat_vsi_llm.answer_chain(llm_endpoint())[1:],
            'rewrite_llm': rewrite_endpoint(), 'answer_max_tokens': answer_max_tokens(),
            'rewrite_max_tokens': rewrite_max_tokens(), 'rerank_top_k': rerank_top_k(), 'raw_top_k': raw_top_k(), 'raw_on': raw_on(),
            'max_search_passages': max_search_passages()}


@lru_cache(maxsize=None)
def load_instructions(division: str) -> str:
    """The division's instructions, then the common answering rules."""
    div = normalize_division(division).lower()
    instructions = (_PROMPTS_DIR / f'instructions_{div}.md').read_text(encoding='utf-8').strip()
    rules = (_PROMPTS_DIR / 'answer_rules.md').read_text(encoding='utf-8').strip()
    return f'{instructions}\n\n{rules}'


# --- Citation markers ---

class CitationStreamParser:
    """Strips ``[n]`` markers from streamed text and records where each citation goes.

    ``feed()`` returns the text that is safe to show now; a trailing ``[`` / ``[12`` that
    could still become a marker is held back until the next chunk. ``pos`` is the length of
    the clean text emitted before the marker — the offset ``chat.py`` uses to place ``⟦n⟧``.
    Markers numbering a document that doesn't exist are dropped from the text without a
    citation. Sources are numbered by first appearance.
    """

    def __init__(self, documents: List[Tuple[str, Dict[str, Any]]]):
        self._documents = documents
        self._pending = ''
        self._emitted = 0
        self._n_of: Dict[str, int] = {}
        self.sources: List[Dict[str, Any]] = []
        self.citations: List[Dict[str, int]] = []

    def feed(self, chunk: str) -> str:
        text = self._pending + chunk
        partial = _PARTIAL_MARKER_RE.search(text)
        cut = partial.start() if partial else len(text)
        self._pending = text[cut:]
        return self._consume(text[:cut])

    @property
    def emitted_chars(self) -> int:
        """Length of the clean answer text emitted so far."""
        return self._emitted

    def flush(self) -> str:
        """End of stream: a held-back incomplete marker is plain text."""
        text, self._pending = self._pending, ''
        return self._consume(text)

    def _consume(self, text: str) -> str:
        out, pos = [], 0
        for m in _MARKER_RE.finditer(text):
            out.append(text[pos:m.start()])
            pos = m.end()
            i = int(m.group(1))
            if not 1 <= i <= len(self._documents):
                continue
            ref, doc = self._documents[i - 1]
            if ref not in self._n_of:
                self.sources.append({'title': ref, 'url': doc['url'], 'doc_uri': doc['url']})
                self._n_of[ref] = len(self.sources)
            self.citations.append({'n': self._n_of[ref], 'pos': self._emitted + sum(len(s) for s in out)})
        out.append(text[pos:])
        clean = ''.join(out)
        self._emitted += len(clean)
        return clean


def parse_citations(raw: str, documents: List[Tuple[str, Dict[str, Any]]]) -> Tuple[str, List[dict], List[dict]]:
    """Whole-text parse (reference for the streaming parser, used by the tests)."""
    sources, citations, n_of, clean, pos = [], [], {}, '', 0
    for m in _MARKER_RE.finditer(raw):
        clean += raw[pos:m.start()]
        pos = m.end()
        i = int(m.group(1))
        if not 1 <= i <= len(documents):
            continue
        ref, doc = documents[i - 1]
        if ref not in n_of:
            sources.append({'title': ref, 'url': doc['url'], 'doc_uri': doc['url']})
            n_of[ref] = len(sources)
        citations.append({'n': n_of[ref], 'pos': len(clean)})
    clean += raw[pos:]
    return clean, sources, citations


# --- Conversation helpers ---

def _clean_history(messages: List[Dict[str, str]]) -> List[Dict[str, str]]:
    """Copy of the conversation without the ``⟦n⟧`` markers stored in earlier answers."""
    return [{'role': m['role'], 'content': _STORED_MARKER_RE.sub('', m.get('content') or '')} for m in messages]


def _without_date(text: str) -> str:
    return _DATE_PREFIX_RE.sub('', text, count=1)


def split_bilingual(text: str) -> Tuple[str, str]:
    """(French query, English query) from the rewrite; a reply without the ``FR:``/``EN:``
    labels is taken as the French query."""
    fr, en = '', ''
    for line in (text or '').splitlines():
        head, _, rest = line.strip().partition(':')
        if head.strip().upper() == 'FR' and rest.strip():
            fr = rest.strip()
        elif head.strip().upper() == 'EN' and rest.strip():
            en = rest.strip()
    if not fr and not en:
        fr = (text or '').strip()
    return fr, en


async def rewrite_queries(host: str, token: str, conversation: List[Dict[str, str]],
                          log: Optional[TurnLog] = None) -> Tuple[str, str]:
    """(French query, English query) for the last question; ('', '') when every rewrite model
    failed — the search then runs with the question only."""
    log = log if log is not None else TurnLog()
    transcript = '\n'.join(f"{m['role']}: {m['content']}" for m in conversation)
    messages = [{'role': 'system', 'content': REWRITE_PROMPT}, {'role': 'user', 'content': transcript}]
    chain = chat_vsi_llm.rewrite_chain(rewrite_endpoint(), llm_endpoint())
    info: Dict[str, Any] = {}
    try:
        text, used = await chat_vsi_llm.complete(host, token, chain, messages, rewrite_max_tokens(),
                                                 rewrite_timeout_s(), info=info)
    except Exception as exc:  # noqa: BLE001 — the turn goes on with the question only
        logger.warning('chat_vsi: search query rewrite failed (%s) — searching with the question only', exc)
        log.data['rewrite_ok'] = False
        log.issue('rewrite_failed', 'rewrite', f'search ran with the question only: {exc}',
                  error_type=type(exc).__name__, http_status=getattr(exc, 'status', 0), upstream=','.join(chain),
                  context={'attempts': info.get('attempts')}, exc=exc)
        return '', ''
    usage = info.get('usage') or {}
    log.data.update({'rewrite_ok': True, 'rewrite_endpoint': used,
                     'rewrite_input_tokens': usage.get('input_tokens'),
                     'rewrite_output_tokens': usage.get('output_tokens'),
                     'rewrite_cost_eur': usage.get('cost_eur')})
    if used != rewrite_endpoint():
        logger.warning('chat_vsi: search query rewritten by fallback %s', used)
        log.issue('rewrite_fallback', 'rewrite', f'rewritten by {used} instead of {rewrite_endpoint()}',
                  upstream=used, context={'expected': rewrite_endpoint(), 'attempts': info.get('attempts')})
    return split_bilingual(text)


def refs_named_in(text: str) -> List[str]:
    """Catalog REFs the text names, with their language variants (at most _REF_LOOKUP_MAX)."""
    cat = _catalog()
    if not cat or not text:
        return []
    refs: List[str] = []
    for entry in cat.find_in_text(text):
        for ref in [entry['ref']] + [sib['ref'] for sib in cat.group_siblings(entry['ref'])]:
            if ref not in refs:
                refs.append(ref)
    return refs[:_REF_LOOKUP_MAX]


# --- Vector Search ---

async def _post_query(host: str, token: str, index: str, payload: Dict[str, Any]) -> httpx.Response:
    """One Vector Search query through the app-wide gate (``vs_gate``: at most
    ``VS_MAX_CONCURRENT_QUERIES`` in flight, retried on 429 / 5xx / timeout)."""
    return await vs_gate.post(host, token, index, payload, _QUERY_TIMEOUT_S)


def _rows(resp: httpx.Response) -> List[Dict[str, Any]]:
    data = resp.json()
    columns = [c['name'] for c in data.get('manifest', {}).get('columns', [])]
    chunks = [dict(zip(columns, row)) for row in data.get('result', {}).get('data_array', [])]
    return [c for c in chunks if _ARCHIVE_NOTICE_MARKER not in (c.get('chunk_text') or '')]


class RerankUnavailable(Exception):
    """The index refused the reranker parameter."""


async def _query(host: str, token: str, index: str, query_text: str, k: int, filters: Dict[str, Any],
                 rerank: bool) -> List[Dict[str, Any]]:
    payload: Dict[str, Any] = {'query_text': query_text[:_MAX_QUERY_CHARS], 'columns': _COLUMNS,
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


def _tag(rows: List[Dict[str, Any]], q: Optional[int], via: str) -> List[Dict[str, Any]]:
    """Records on each row which query found it (``q``: index in the turn's queries, None for a
    lookup), how (``via``: rerank / raw / ref_lookup / title_lookup), at which rank, with which
    score — kept in chat_retrieved_chunks.hits."""
    for rank, row in enumerate(rows):
        row['_hits'] = [{'q': q, 'via': via, 'rank': rank, 'score': row.get('score')}]
    return rows


def _merge_by_rank(lists: List[List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    """One row per chunk, ordered by its best rank in any list; the ``_hits`` of every copy kept."""
    ranked: Dict[str, Tuple[int, Dict[str, Any]]] = {}
    hits: Dict[str, List[Dict[str, Any]]] = {}
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
    if isinstance(exc, ChatVsiError):
        return exc
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        logger.error('chat_vsi: Vector Search %s returned %d: %s', index, status, exc.response.text[:500])
        return ChatVsiError(f'Document search failed (Vector Search returned {status}).', 'VectorSearchError', status)
    if isinstance(exc, httpx.TimeoutException):
        logger.error('chat_vsi: Vector Search %s timed out: %s', index, exc)
        return ChatVsiError('Document search timed out — please try again.', 'TimeoutError')
    logger.error('chat_vsi: Vector Search %s failed: %s', index, exc)
    return ChatVsiError(f'Document search failed: {exc}', type(exc).__name__)


def _status_of(exc: BaseException) -> int:
    return exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else 0


async def search(host: str, token: str, index: str, queries: List[str], filters: Dict[str, Any],
                 raw_queries: Optional[List[str]] = None, log: Optional[TurnLog] = None) -> List[Dict[str, Any]]:
    """Reranked top k for every query + raw top k for ``raw_queries`` (default: every query),
    merged by best rank. A query (or the
    reranked side) that fails is dropped when the rest answered; raises ``ChatVsiError`` only
    if everything failed. Without raw passages (``raw_top_k() == 0``), a refused reranker falls
    back to the raw search with the reranked size."""
    log = log if log is not None else TurnLog()
    rerank_k, raw_k = rerank_top_k(), raw_top_k()
    rerank_ok = True

    async def reranked(i: int, q: str) -> List[Dict[str, Any]]:
        nonlocal rerank_ok
        if not rerank_ok:
            return [] if raw_k else _tag(await _query(host, token, index, q, rerank_k, filters, rerank=False), i, 'raw')
        try:
            return _tag(await _query(host, token, index, q, rerank_k, filters, rerank=True), i, 'rerank')
        except RerankUnavailable as exc:
            if rerank_ok:
                log.issue('rerank_refused', 'search', f'raw search only: {exc}', upstream=index, exc=exc)
            rerank_ok = False
            logger.warning('chat_vsi: reranker refused by %s (%s) — raw search only', index, exc)
            return [] if raw_k else _tag(await _query(host, token, index, q, rerank_k, filters, rerank=False), i, 'raw')

    async def raw(i: int, q: str) -> List[Dict[str, Any]]:
        return _tag(await _query(host, token, index, q, raw_k, filters, rerank=False), i, 'raw')

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
        logger.warning('chat_vsi: %d/%d Vector Search queries failed, kept the others: %s', len(failed),
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


async def fetch_documents(host: str, token: str, index: str, query: str, refs: List[str], k: int,
                          filters: Dict[str, Any], via: str = 'ref_lookup',
                          log: Optional[TurnLog] = None) -> List[Dict[str, Any]]:
    """HYBRID search restricted to the given REFs (``via``: ref_lookup / title_lookup).
    Failures are logged, never raised."""
    try:
        return _tag(await _query(host, token, index, query, k, {**filters, 'REF': refs}, rerank=False), None, via)
    except Exception as exc:  # noqa: BLE001 — the turn goes on without the lookup
        logger.warning('chat_vsi: document lookup %s on %s failed: %s', refs, index, exc)
        if log is not None:
            log.issue(f'{via}_failed', via, f'{refs}: {exc}', error_type=type(exc).__name__,
                      http_status=_status_of(exc), upstream=index, context={'refs': refs}, exc=exc)
        return []


def one_language_per_document(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Only the passages of the best-ranked language variant of each document."""
    chosen: Dict[str, str] = {}
    return [row for row in rows if chosen.setdefault(canon_ref(row['REF']), row['REF']) == row['REF']]


def append_new(rows: List[Dict[str, Any]], extra: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """rows, then the passages of extra not already in rows."""
    seen = {r['chunk_id'] for r in rows}
    return rows + [r for r in extra if r['chunk_id'] not in seen]


def group_documents(rows: List[Dict[str, Any]]) -> List[Tuple[str, Dict[str, Any]]]:
    """Passages grouped by document (REF), in order of first appearance."""
    docs: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        docs.setdefault(row['REF'], {'url': row['url'], 'passages': []})['passages'].append(row['chunk_text'])
    return list(docs.items())


def _passage(row: Dict[str, Any], position: int, drop_reason: str = '') -> Dict[str, Any]:
    """One chat_retrieved_chunks record (doc_number, prompt_rank and cited are set once the prompt
    and the answer exist)."""
    hits = row.get('_hits') or []
    scores = [h['score'] for h in hits if isinstance(h.get('score'), (int, float))]
    vias = {h['via'] for h in hits}
    return {'position': position, 'kept': not drop_reason, 'drop_reason': drop_reason or None,
            'prompt_rank': None, 'doc_number': None, 'cited': None,
            'source': 'search' if vias & {'rerank', 'raw'} else next(iter(sorted(vias)), 'search'),
            'hits': hits, 'best_score': max(scores) if scores else None,
            'chunk_id': row.get('chunk_id'), 'iddoc': None if row.get('IDDOC') is None else str(row['IDDOC']),
            'ref': row.get('REF'), 'division': row.get('division'), 'url': row.get('url'),
            'semantic_headers': row.get('semantic_headers'), 'chunk_text': row.get('chunk_text')}


async def retrieve_for_turn(host: str, token: str, division: str, conversation: List[Dict[str, str]],
                            log: Optional[TurnLog] = None) -> Dict[str, Any]:
    """The passages handed to the LLM for the last user turn of ``conversation`` (already
    cleaned by ``_clean_history``). Raises ``ChatVsiError`` when the search fails. Also used
    alone by the retrieval evaluation (no answer generated). ``log`` (created when absent, returned
    as ``found['log']``) receives the step durations, the queries, every passage retrieved — kept
    or set aside — and what did not run as configured."""
    log = log if log is not None else TurnLog()
    with log.timed('retrieval'):
        index = index_name()
        filters = division_filter(division)
        question = _without_date(conversation[-1]['content'])
        with log.timed('rewrite'):
            fr_query, en_query = await rewrite_queries(
                host, token, conversation[:-1] + [{'role': 'user', 'content': question}], log=log)
        queries = [question]
        for q in (fr_query, en_query):
            if q and q.casefold() not in {x.casefold() for x in queries}:
                queries.append(q)

        # REFs named in the question first, then in the earlier turns ("résume la slide 15" after an
        # answer about MI-14242 names no REF itself).
        named: List[str] = []
        for ref in refs_named_in(question) + refs_named_in('\n'.join(m['content'] for m in conversation[:-1])):
            if ref not in named:
                named.append(ref)
        named = named[:_REF_LOOKUP_MAX]
        with log.timed('title_lookup'):
            titled = documents_titled(queries, _TITLE_LOOKUP_DOCS)
        log.data.update({'index_name': index, 'fr_query': fr_query, 'en_query': en_query, 'named_refs': named,
                         'titled_refs': [canon for canon, _, _ in titled]})

        by_name = {'question': question, 'fr': fr_query, 'en': en_query}
        raw_texts: List[str] = []
        for name in raw_on():
            text = by_name[name]
            if text and text.casefold() not in {x.casefold() for x in raw_texts}:
                raw_texts.append(text)
        if raw_on() and not raw_texts:   # rewrite failed: the question keeps its raw search
            raw_texts = [question]
        with log.timed('search'):
            rows = await search(host, token, index, queries, filters, raw_texts, log=log)
        over_cap: List[Dict[str, Any]] = []
        if max_search_passages():
            rows, over_cap = rows[:max_search_passages()], rows[max_search_passages():]
        if named:
            with log.timed('ref_lookup'):
                found = await fetch_documents(host, token, index, question, named, _REF_LOOKUP_K, filters,
                                              via='ref_lookup', log=log)
            rows = _merge_by_rank([found, rows])
        if titled:
            title_refs = [ref for _, refs, _ in titled for ref in refs]
            with log.timed('title_lookup'):
                found = await fetch_documents(host, token, index, fr_query or question, title_refs, _TITLE_LOOKUP_K,
                                              filters, via='title_lookup', log=log)
            rows = append_new(rows, found)
        candidates = rows
        rows = one_language_per_document(rows)

        # Every passage retrieved, in retrieval order (the ones past the search cap last), kept or not.
        kept = {r['chunk_id'] for r in rows}
        merged = {r['chunk_id'] for r in candidates}
        log.passages = [_passage(r, i, '' if r['chunk_id'] in kept else 'other_language')
                        for i, r in enumerate(candidates)]
        log.passages += [_passage(r, len(log.passages) + j, 'over_cap')
                         for j, r in enumerate(r for r in over_cap if r['chunk_id'] not in merged)]
        log.data.update({'passages_retrieved': len(log.passages), 'passages_sent': len(rows)})
        if not rows:
            log.issue('no_passages', 'search', 'the search returned no passage', upstream=index)
    return {'question': question, 'fr_query': fr_query, 'en_query': en_query, 'rows': rows, 'named': named,
            'titled': [canon for canon, _, _ in titled], 'index': index, 'log': log}


# --- Prompt and answer ---

def with_language_reminder(messages: List[Dict[str, str]], language: Optional[str] = None) -> List[Dict[str, str]]:
    """``messages`` with the language reminder after the last user turn (a copy)."""
    line = NAMED_LANGUAGE_REMINDER.format(language=language) if language else LANGUAGE_REMINDER
    out = [dict(m) for m in messages]
    out[-1]['content'] = f'{out[-1]["content"]}\n\n{line}'
    return out


def revision_note(ref: str) -> str:
    """' (current revision B, published 2024-10-11)' from the catalog, '' when it has neither: the
    passages themselves never carry the revision, and every document of the index is the current one."""
    info = document_info(ref)
    parts = ([f"current revision {info['revision']}"] if info.get('revision') else []) + \
            ([f"published {info['doc_date']}"] if info.get('doc_date') else [])
    return f" ({', '.join(parts)})" if parts else ''


def build_prompt(division: str, conversation: List[Dict[str, str]], documents: List[Tuple[str, Dict[str, Any]]],
                 answer_language: Optional[str] = None) -> List[Dict[str, str]]:
    """System instructions, then the conversation; the last turn carries the numbered documents
    before the question and the language reminder after it."""
    context = '\n\n'.join(f'[{i}] Document {ref}{revision_note(ref)}\n' + '\n\n'.join(d['passages'])
                          for i, (ref, d) in enumerate(documents, 1))
    turns = [dict(m) for m in conversation]
    turns[-1]['content'] = f'Documents:\n\n{context}\n\n---\n\n{turns[-1]["content"]}'
    return with_language_reminder([{'role': 'system', 'content': load_instructions(division)}] + turns,
                                  answer_language)


def _event(payload: Dict[str, Any]) -> str:
    return f'data: {json.dumps(payload)}\n\n'


def _error_events(message: str, error_type: str, http_status: int = 0) -> List[str]:
    return [_event({'type': 'error', 'error': message, 'error_type': error_type, 'http_status': http_status}),
            'data: [DONE]\n\n']


def _record_prompt(log: TurnLog, division: str, documents: List[Tuple[str, Dict[str, Any]]],
                   prompt: List[Dict[str, str]]) -> None:
    """Prompt facts on the turn, and each kept passage's place in the prompt."""
    number = {ref: n for n, (ref, _) in enumerate(documents, 1)}
    kept = [p for p in log.passages if p['kept']]
    for rank, p in enumerate(sorted(kept, key=lambda p: (number.get(p['ref'], 0), p['position']))):
        p['prompt_rank'], p['doc_number'] = rank, number.get(p['ref'])
    log.data.update({
        'documents_sent': len(documents),
        'prompt_chars': sum(len(m['content']) for m in prompt),
        'instructions_sha': hashlib.sha256(load_instructions(division).encode('utf-8')).hexdigest()[:16],
    })


def _record_answer(log: TurnLog, chain: List[str], llm: Dict[str, Any], usage: Dict[str, Any],
                   parser: CitationStreamParser) -> None:
    """What the answer model did, and which documents the answer cites."""
    cited = [s['title'] for s in parser.sources]
    for p in log.passages:
        p['cited'] = p['kept'] and p['ref'] in cited
    attempts = llm.get('attempts') or []
    log.timings_ms['queue_wait'] = llm.get('queue_wait_ms') or 0
    log.data.update({
        'answer_endpoint': llm.get('endpoint'), 'llm_fallback': bool(llm.get('fallback')), 'llm_attempts': attempts,
        'truncated': bool(llm.get('truncated')), 'continuations': llm.get('continuations') or 0,
        'input_tokens': usage.get('input_tokens'), 'output_tokens': usage.get('output_tokens'),
        'thinking_tokens': usage.get('thinking_tokens'), 'cost_eur': usage.get('cost_eur'),
        'answer_chars': parser.emitted_chars, 'citations_count': len(parser.citations), 'cited_refs': cited,
    })
    failed = [a for a in attempts if a.get('outcome') not in ('ok', 'continued')]
    context = {'expected': chain[0] if chain else None, 'chain': chain, 'attempts': attempts}
    if llm.get('fallback'):
        log.issue('llm_fallback', 'llm', f"answered by {llm.get('endpoint')} instead of {chain[0]}",
                  upstream=llm.get('endpoint') or '', context=context)
    elif failed and llm.get('endpoint'):
        log.issue('llm_retried', 'llm', f'{len(failed)} failed call(s) before the answer',
                  upstream=llm.get('endpoint') or '', context=context)
    if llm.get('interrupted'):
        log.issue('answer_interrupted', 'llm', 'part of the answer was sent, no model could finish it',
                  context=context)
    elif llm.get('continuations'):
        log.issue('answer_continued', 'llm', f"answer resumed {llm['continuations']} time(s) after a cut",
                  context=context)


async def stream_chat_vsi(host: str, token: str, division: str, messages: List[Dict[str, str]],
                          answer_language: Optional[str] = None,
                          log: Optional[TurnLog] = None) -> AsyncGenerator[str, None]:
    """Answer the last user turn of ``messages`` (trimmed history, last turn prefixed with
    ``[Date: …]``, translated to English by the bridge when needed). ``answer_language`` (e.g.
    "French", from chat.py) is named in the language reminder. ``log`` (the turn's ``TurnLog``,
    from chat.py) receives every step; its ``trace_id`` is the one in the ``metadata`` event."""
    yield ': keepalive\n\n'                      # first byte out, before the slow steps
    log = log if log is not None else TurnLog()
    trace_id = log.trace_id
    div = normalize_division(division)
    log.data.update({'division': div, 'index_name': index_name(), 'config': settings()})
    conversation = _clean_history(messages)
    if not conversation or conversation[-1]['role'] != 'user':
        log.fail('input', 'InputError', 'the last message must be a user question')
        for e in _error_events('Chat: the last message must be a user question.', 'InputError'):
            yield e
        return
    try:
        found = await retrieve_for_turn(host, token, div, conversation, log=log)
    except ChatVsiError as exc:
        log.fail('search', exc.error_type, exc.message, http_status=exc.http_status, upstream=index_name(),
                 context={'vs_calls_expected': log.data.get('vs_calls_expected'),
                          'vs_calls_ok': log.data.get('vs_calls_ok')}, exc=exc.__cause__ or exc)
        for e in _error_events(exc.message, exc.error_type, exc.http_status):
            yield e
        return
    rows = found['rows']
    documents = group_documents(rows)
    logger.info('chat_vsi: division=%s index=%s fr_query=%r en_query=%r passages=%d documents=%d trace_id=%s',
                div, found['index'], found['fr_query'], found['en_query'], len(rows), len(documents), trace_id)

    parser = CitationStreamParser(documents)
    usage: Dict[str, Any] = {}
    llm: Dict[str, Any] = {}
    chain = chat_vsi_llm.answer_chain(llm_endpoint())
    prompt = build_prompt(div, conversation, documents, answer_language)
    _record_prompt(log, div, documents, prompt)
    llm_started = time.monotonic()

    def _generation_done() -> None:
        llm_ms = int((time.monotonic() - llm_started) * 1000)
        log.timings_ms['generation'] = max(0, llm_ms - (llm.get('queue_wait_ms') or 0))
        _record_answer(log, chain, llm, usage, parser)

    async for chunk in chat_vsi_llm.stream_answer(host, token, chain, prompt, answer_max_tokens(), _OPERATION):
        if not chunk.startswith('data: '):
            yield chunk                          # keepalive comments
            continue
        data = chunk[6:].strip()
        if data == '[DONE]':
            break
        try:
            event = json.loads(data)
        except json.JSONDecodeError:
            continue
        kind = event.get('type')
        if kind == 'response.output_text.delta':
            clean = parser.feed(event.get('delta', ''))
            if clean:
                log.mark('first_token')
                yield _event({'type': 'response.output_text.delta', 'delta': clean})
        elif kind == 'error':
            logger.error('chat_vsi: generation failed: %s', event.get('error'))
            _generation_done()
            log.fail('llm', event.get('error_type') or 'LLMError', event.get('error') or 'Answer generation failed.',
                     http_status=event.get('http_status') or 0, upstream=','.join(chain),
                     context={'attempts': llm.get('attempts')})
            for e in _error_events(event.get('error') or 'Answer generation failed.',
                                   event.get('error_type') or 'LLMError', event.get('http_status') or 0):
                yield e
            return
        elif kind == 'warning':
            logger.warning('chat_vsi: %s', event.get('detail') or event)
            log.issue(event.get('warning') or 'llm_warning', 'llm', event.get('detail') or '',
                      upstream=llm.get('endpoint') or '')
        elif kind == 'usage':
            usage = {k: event.get(k) for k in ('input_tokens', 'output_tokens', 'thinking_tokens', 'cost_eur')}
        elif kind == 'llm':
            llm = event

    tail = parser.flush()
    if tail:
        log.mark('first_token')
        yield _event({'type': 'response.output_text.delta', 'delta': tail})
    _generation_done()
    if parser.sources or parser.citations:
        yield _event({'type': 'sources', 'sources': parser.sources, 'citations': parser.citations})
    yield _event({'type': 'metadata', 'trace_id': trace_id})
    yield 'data: [DONE]\n\n'
