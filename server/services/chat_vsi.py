"""Chat VSI — the chat engine: answers from the Vector Search index, no Knowledge Assistant.

What happens for each question (configuration chosen 2026-10-08, measures in
``docs/chat_vsi_tests.md``):

1. **Rewrite** — GPT-6 Luna turns the last question, with the conversation as context, into
   one standalone search query in French and one in English (acronyms expanded when certain).
2. **Search** — three queries (the question as asked + the two rewrites), each run twice in
   HYBRID mode on the single passage index: the top ``_RERANK_TOP_K`` passages reranked by the
   Databricks reranker (reading REF, section headings and text) and the raw top ``_RAW_TOP_K``.
   Everything is merged by best rank (``union``). The chat's division (AS / IS) is a filter on
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
``citations``, ``metadata``, ``error``, ``[DONE]``). Settings (environment, read at call time):
``CHAT_VSI_INDEX``, ``CHAT_VSI_LLM_ENDPOINT`` / ``CHAT_VSI_LLM_FALLBACK_ENDPOINTS``,
``CHAT_VSI_REWRITE_ENDPOINT`` / ``CHAT_VSI_REWRITE_FALLBACK_ENDPOINTS``,
``CHAT_VSI_ANSWER_MAX_TOKENS``, ``CHAT_VSI_REWRITE_MAX_TOKENS``, ``CHAT_VSI_SEARCH_RETRIES``,
plus the resilience settings documented in ``chat_vsi_llm.py``. The earlier engine versions
(baseline, measured options) are kept in ``archive/``.
"""

import asyncio
import json
import logging
import os
import random
import re
import uuid
from functools import lru_cache
from pathlib import Path
from typing import Any, AsyncGenerator, Dict, List, Optional, Tuple

import httpx

from . import chat_vsi_llm
from .chat_vsi_titles import documents_titled
from .doc_catalog import _catalog, canon_ref
from .vector_search import _ARCHIVE_NOTICE_MARKER, _COLUMNS, _QUERY_TIMEOUT_S

logger = logging.getLogger(__name__)

_PROMPTS_DIR = Path(__file__).resolve().parent.parent / 'config' / 'chat_vsi'
_DIVISIONS = ('ALL', 'AS', 'IS')
_DEFAULT_INDEX = 'dev_landingzone.qualibot.chunks_index'
_DEFAULT_LLM_ENDPOINT = 'databricks-gpt-6-luna'      # no Claude model in the chatbot (2026-10-08)
_DEFAULT_ANSWER_MAX_TOKENS = 8000                     # reasoning counts inside it
_DEFAULT_REWRITE_MAX_TOKENS = 2000
_RERANK_TOP_K = 12               # reranked passages kept per query (20 measured worse, 2026-10-08)
_RAW_TOP_K = 10                  # raw HYBRID passages per query
_RERANK_COLUMNS = ['REF', 'semantic_headers', 'chunk_text']
_RERANKER_MODEL = 'databricks_reranker'
_REF_LOOKUP_MAX = 6              # REFs (language variants included) filtered on
_REF_LOOKUP_K = 8                # passages fetched from the named documents
_TITLE_LOOKUP_DOCS = 3           # best title matches searched
_TITLE_LOOKUP_K = 6              # passages fetched from them
_MAX_QUERY_CHARS = 20000         # Vector Search rejects query_text past ~29k chars
_OPERATION = 'Chat'              # label used in user-facing error messages
_RETRYABLE_STATUS = {429, 500, 502, 503, 504}

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


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

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


def search_retries() -> int:
    return _int_env('CHAT_VSI_SEARCH_RETRIES', 2)


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
            'rewrite_max_tokens': rewrite_max_tokens(), 'rerank_top_k': _RERANK_TOP_K, 'raw_top_k': _RAW_TOP_K}


@lru_cache(maxsize=None)
def load_instructions(division: str) -> str:
    """The division's instructions, then the common answering rules."""
    div = normalize_division(division).lower()
    instructions = (_PROMPTS_DIR / f'instructions_{div}.md').read_text(encoding='utf-8').strip()
    rules = (_PROMPTS_DIR / 'answer_rules.md').read_text(encoding='utf-8').strip()
    return f'{instructions}\n\n{rules}'


# ---------------------------------------------------------------------------
# Citation markers
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Conversation helpers
# ---------------------------------------------------------------------------

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


async def rewrite_queries(host: str, token: str, conversation: List[Dict[str, str]]) -> Tuple[str, str]:
    """(French query, English query) for the last question; ('', '') when every rewrite model
    failed — the search then runs with the question only."""
    transcript = '\n'.join(f"{m['role']}: {m['content']}" for m in conversation)
    messages = [{'role': 'system', 'content': REWRITE_PROMPT}, {'role': 'user', 'content': transcript}]
    try:
        text, used = await chat_vsi_llm.complete(
            host, token, chat_vsi_llm.rewrite_chain(rewrite_endpoint(), llm_endpoint()), messages,
            rewrite_max_tokens(), rewrite_timeout_s())
    except Exception as exc:  # noqa: BLE001 — the turn goes on with the question only
        logger.warning('chat_vsi: search query rewrite failed (%s) — searching with the question only', exc)
        return '', ''
    if used != rewrite_endpoint():
        logger.warning('chat_vsi: search query rewritten by fallback %s', used)
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


# ---------------------------------------------------------------------------
# Vector Search
# ---------------------------------------------------------------------------

async def _post_query(host: str, token: str, index: str, payload: Dict[str, Any]) -> httpx.Response:
    """One Vector Search query, retried on 429 / 5xx / timeout / network error (backoff 0.5 s,
    1 s, 2 s… plus jitter, at most CHAT_VSI_SEARCH_RETRIES times)."""
    attempts = search_retries()
    for attempt in range(attempts + 1):
        try:
            async with httpx.AsyncClient(timeout=_QUERY_TIMEOUT_S) as client:
                resp = await client.post(f'{host}/api/2.0/vector-search/indexes/{index}/query', json=payload,
                                         headers={'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'})
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            if attempt == attempts:
                raise
            logger.warning('chat_vsi: Vector Search %s: %s, retry %d/%d', index, type(exc).__name__, attempt + 1, attempts)
        else:
            if resp.status_code not in _RETRYABLE_STATUS or attempt == attempts:
                return resp
            logger.warning('chat_vsi: Vector Search %s returned %d, retry %d/%d', index, resp.status_code,
                           attempt + 1, attempts)
        await asyncio.sleep(min(4.0, 0.5 * 2 ** attempt) + random.random() * 0.5)
    raise RuntimeError('unreachable')


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


def _merge_by_rank(lists: List[List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    ranked: Dict[str, Tuple[int, Dict[str, Any]]] = {}
    for result in lists:
        for rank, row in enumerate(result):
            cid = row['chunk_id']
            if cid not in ranked or rank < ranked[cid][0]:
                ranked[cid] = (rank, row)
    return [row for _, row in sorted(ranked.values(), key=lambda x: x[0])]


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


async def search(host: str, token: str, index: str, queries: List[str], filters: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Reranked top 12 + raw top 10 for every query, merged by best rank. A query (or the
    reranked side) that fails is dropped when the rest answered; raises ``ChatVsiError`` only
    if everything failed."""
    rerank_ok = True

    async def reranked(q: str) -> List[Dict[str, Any]]:
        nonlocal rerank_ok
        if not rerank_ok:
            return []
        try:
            return await _query(host, token, index, q, _RERANK_TOP_K, filters, rerank=True)
        except RerankUnavailable as exc:
            rerank_ok = False
            logger.warning('chat_vsi: reranker refused by %s (%s) — raw search only', index, exc)
            return []

    calls = [reranked(q) for q in queries] + [_query(host, token, index, q, _RAW_TOP_K, filters, rerank=False)
                                              for q in queries]
    results = await asyncio.gather(*calls, return_exceptions=True)
    for r in results:
        if isinstance(r, asyncio.CancelledError):
            raise r
    ok = [r for r in results if not isinstance(r, BaseException)]
    failed = [r for r in results if isinstance(r, BaseException)]
    if not ok:
        raise _search_error(index, failed[0])
    if failed:
        logger.warning('chat_vsi: %d/%d Vector Search queries failed, kept the others: %s', len(failed),
                       len(results), failed[0])
    n = len(queries)
    return _merge_by_rank([_merge_by_rank(ok_part) for ok_part in (
        [r for r in results[:n] if not isinstance(r, BaseException)],
        [r for r in results[n:] if not isinstance(r, BaseException)]) if ok_part])


async def fetch_documents(host: str, token: str, index: str, query: str, refs: List[str], k: int,
                          filters: Dict[str, Any]) -> List[Dict[str, Any]]:
    """HYBRID search restricted to the given REFs. Failures are logged, never raised."""
    try:
        return await _query(host, token, index, query, k, {**filters, 'REF': refs}, rerank=False)
    except Exception as exc:  # noqa: BLE001 — the turn goes on without the lookup
        logger.warning('chat_vsi: document lookup %s on %s failed: %s', refs, index, exc)
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


async def retrieve_for_turn(host: str, token: str, division: str,
                            conversation: List[Dict[str, str]]) -> Dict[str, Any]:
    """The passages handed to the LLM for the last user turn of ``conversation`` (already
    cleaned by ``_clean_history``). Raises ``ChatVsiError`` when the search fails. Also used
    alone by the retrieval evaluation (no answer generated)."""
    index = index_name()
    filters = division_filter(division)
    question = _without_date(conversation[-1]['content'])
    fr_query, en_query = await rewrite_queries(host, token, conversation[:-1] + [{'role': 'user', 'content': question}])
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
    titled = documents_titled(queries, _TITLE_LOOKUP_DOCS)

    rows = await search(host, token, index, queries, filters)
    if named:
        rows = _merge_by_rank([await fetch_documents(host, token, index, question, named, _REF_LOOKUP_K, filters), rows])
    if titled:
        title_refs = [ref for _, refs, _ in titled for ref in refs]
        rows = append_new(rows, await fetch_documents(host, token, index, fr_query or question, title_refs,
                                                      _TITLE_LOOKUP_K, filters))
    rows = one_language_per_document(rows)
    return {'question': question, 'fr_query': fr_query, 'en_query': en_query, 'rows': rows, 'named': named,
            'titled': [canon for canon, _, _ in titled], 'index': index}


# ---------------------------------------------------------------------------
# Prompt and answer
# ---------------------------------------------------------------------------

def with_language_reminder(messages: List[Dict[str, str]], language: Optional[str] = None) -> List[Dict[str, str]]:
    """``messages`` with the language reminder after the last user turn (a copy)."""
    line = NAMED_LANGUAGE_REMINDER.format(language=language) if language else LANGUAGE_REMINDER
    out = [dict(m) for m in messages]
    out[-1]['content'] = f'{out[-1]["content"]}\n\n{line}'
    return out


def build_prompt(division: str, conversation: List[Dict[str, str]], documents: List[Tuple[str, Dict[str, Any]]],
                 answer_language: Optional[str] = None) -> List[Dict[str, str]]:
    """System instructions, then the conversation; the last turn carries the numbered documents
    before the question and the language reminder after it."""
    context = '\n\n'.join(f'[{i}] Document {ref}\n' + '\n\n'.join(d['passages'])
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


async def stream_chat_vsi(host: str, token: str, division: str, messages: List[Dict[str, str]],
                          answer_language: Optional[str] = None) -> AsyncGenerator[str, None]:
    """Answer the last user turn of ``messages`` (trimmed history, last turn prefixed with
    ``[Date: …]``, translated to English by the bridge when needed). ``answer_language`` (e.g.
    "French", from chat.py) is named in the language reminder."""
    yield ': keepalive\n\n'                      # first byte out, before the slow steps
    trace_id = f'vsi-{uuid.uuid4().hex}'
    div = normalize_division(division)
    conversation = _clean_history(messages)
    if not conversation or conversation[-1]['role'] != 'user':
        for e in _error_events('Chat: the last message must be a user question.', 'InputError'):
            yield e
        return
    try:
        found = await retrieve_for_turn(host, token, div, conversation)
    except ChatVsiError as exc:
        for e in _error_events(exc.message, exc.error_type, exc.http_status):
            yield e
        return
    rows, named, titled = found['rows'], found['named'], found['titled']
    documents = group_documents(rows)
    logger.info('chat_vsi: division=%s index=%s fr_query=%r en_query=%r passages=%d documents=%d trace_id=%s',
                div, found['index'], found['fr_query'], found['en_query'], len(rows), len(documents), trace_id)

    parser = CitationStreamParser(documents)
    usage: Dict[str, Any] = {}
    llm: Dict[str, Any] = {}
    prompt = build_prompt(div, conversation, documents, answer_language)
    async for chunk in chat_vsi_llm.stream_answer(host, token, chat_vsi_llm.answer_chain(llm_endpoint()), prompt,
                                                  answer_max_tokens(), _OPERATION):
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
                yield _event({'type': 'response.output_text.delta', 'delta': clean})
        elif kind == 'error':
            logger.error('chat_vsi: generation failed: %s', event.get('error'))
            for e in _error_events(event.get('error') or 'Answer generation failed.',
                                   event.get('error_type') or 'LLMError', event.get('http_status') or 0):
                yield e
            return
        elif kind == 'warning':
            logger.warning('chat_vsi: %s', event.get('detail') or event)
        elif kind == 'usage':
            usage = {k: event.get(k) for k in ('input_tokens', 'output_tokens', 'thinking_tokens', 'cost_eur')}
        elif kind == 'llm':
            llm = event

    tail = parser.flush()
    if tail:
        yield _event({'type': 'response.output_text.delta', 'delta': tail})
    if parser.sources or parser.citations:
        yield _event({'type': 'sources', 'sources': parser.sources, 'citations': parser.citations})
    yield _event({'type': 'metadata', 'trace_id': trace_id,
                  'tool_name': 'vector_search' + (f'+ref_lookup({",".join(named)})' if named else '')
                               + (f'+title_lookup({",".join(titled)})' if titled else ''),
                  'tool_query': found['fr_query'] or found['question'],
                  'tool_result': ', '.join(ref for ref, _ in documents),
                  'reasoning_steps': [],
                  # Answer generation only (the short rewrite call is not counted).
                  'usage': usage, 'llm': llm.get('endpoint') or llm_endpoint(),
                  'llm_fallback': bool(llm.get('fallback')), 'llm_attempts': llm.get('attempts') or []})
    yield 'data: [DONE]\n\n'
