"""Chat VSI — answers Chat questions from the Vector Search indexes, without the Knowledge Assistant.

Port of the "VSI v0" pipeline measured against the KA on the golden dataset
(`scripts/qualibot_golden_ka_vs_vsi.py`, see docs/dev-conception.md):

1. the last question is rewritten as a French search query (the corpus is mostly French);
2. the division's index is searched in HYBRID mode with the question as-is and with
   that French query; both result lists are merged by best rank;
3. passages are grouped and numbered by document — the unit the UI numbers;
4. the answer model (``CHAT_VSI_LLM_ENDPOINT``, default GPT-6 Luna since 2026-10-08; Sonnet 4.6 in
   the v0 benchmark) answers with the division's instructions (copied from
   the live KAs, ``server/config/chat_vsi/``) and cites documents with ``[n]`` markers;
5. the markers are parsed out of the stream (``CitationStreamParser``).

``stream_chat_vsi`` yields exactly the events ``streaming.stream_chat`` yields for the KA
(text deltas, ``sources`` + ``citations``, ``metadata``, ``error``, ``[DONE]``), so the
whole downstream — ``chat.py`` post-processing, persistence, display — is unchanged.
See docs/ka-io-contract.md.
"""

import asyncio
import json
import logging
import os
import re
import uuid
from functools import lru_cache
from pathlib import Path
from typing import Any, AsyncGenerator, Dict, List, Optional, Tuple

import httpx

from .chat_vsi_llm import _message_text as message_text, answer_chain, effective_max_tokens, stream_answer
from .streaming import supports_temperature
from .vector_search import _fetch_chunks

logger = logging.getLogger(__name__)

_INSTRUCTIONS_DIR = Path(__file__).resolve().parent.parent / 'config' / 'chat_vsi'

# Defaults are the values measured in the v0 benchmark (dev workspace indexes, the
# KAs' own knowledge sources). Read at call time, so per-environment overrides
# (app.yaml / target_config.env / .env.local) and tests apply without re-import.
_DEFAULT_INDEXES = {
    'ALL': 'dev_landingzone.qualibot.chunks_index_v1',
    'AS': 'dev_landingzone.qualibot.chunks_as_index_v1',
    'IS': 'dev_landingzone.qualibot.chunks_is_index_v1',
}
# No Claude model in the chatbot (decision 2026-10-08): GPT-6 Luna, GPT-5.6 Luna as fallback.
_DEFAULT_LLM_ENDPOINT = 'databricks-gpt-6-luna'
_DEFAULT_NUM_RESULTS = 10        # passages per query — what the KA passed to generation
_ANSWER_MAX_TOKENS = 2000
_QUERY_MAX_TOKENS = 120
_MAX_QUERY_CHARS = 20000         # Vector Search rejects query_text past ~29k chars
_LLM_TIMEOUT_S = 60.0
_OPERATION = 'Chat'
_REASONING_REWRITE_FLOOR = 1000              # label used in user-facing error messages

REWRITE_PROMPT = """Using the conversation for context, rewrite the user's LAST question as one standalone search query in French, for a search
engine over a mostly French document base (Latécoère quality documents). Keep document codes,
acronyms and technical terms as they are. Return only the query."""

CITATION_RULE = """
# How to cite (mandatory)
You are given numbered documents, each with one or more passages. Cite inline: put the document
number in square brackets at the end of the sentence or list item it supports, before the line
break, e.g. "... EN4179. [3]". Never put markers on a line of their own, after a heading, or under
a table. Use only the document numbers given. Do not write URLs."""

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
    return d if d in _DEFAULT_INDEXES else 'ALL'


def index_for_division(division: Optional[str]) -> str:
    d = normalize_division(division)
    return os.getenv(f'CHAT_VSI_INDEX_{d}', _DEFAULT_INDEXES[d]).strip()


def llm_endpoint() -> str:
    return os.getenv('CHAT_VSI_LLM_ENDPOINT', _DEFAULT_LLM_ENDPOINT).strip()


def num_results() -> int:
    return int(os.getenv('CHAT_VSI_NUM_RESULTS', str(_DEFAULT_NUM_RESULTS)))


@lru_cache(maxsize=None)
def load_instructions(division: str) -> str:
    """The division's answer instructions, verbatim from the live KA (minus the stray AS note)."""
    path = _INSTRUCTIONS_DIR / f'instructions_{normalize_division(division).lower()}.md'
    return path.read_text(encoding='utf-8').rstrip('\n')


# ---------------------------------------------------------------------------
# Citation markers
# ---------------------------------------------------------------------------

class CitationStreamParser:
    """Strips ``[n]`` markers from streamed text and records where each citation goes.

    ``feed()`` returns the text that is safe to show now; a trailing ``[`` / ``[12`` that
    could still become a marker is held back until the next chunk. ``pos`` is the length of
    the clean text emitted before the marker — the offset ``chat.py`` uses to place ``⟦n⟧``.
    Markers numbering a document that doesn't exist are dropped from the text without a
    citation. Sources are numbered by first appearance, like the KA annotations.

    Fed chunk by chunk, the result is identical to ``parse_citations`` on the whole text.
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
    """Whole-text parse, exactly as the measured v0 did it (reference for the streaming parser)."""
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
# Pipeline pieces
# ---------------------------------------------------------------------------

def _clean_history(messages: List[Dict[str, str]]) -> List[Dict[str, str]]:
    """Copy of the conversation without the ``⟦n⟧`` markers stored in earlier answers."""
    return [{'role': m['role'], 'content': _STORED_MARKER_RE.sub('', m.get('content') or '')} for m in messages]


def _without_date(text: str) -> str:
    return _DATE_PREFIX_RE.sub('', text, count=1)


async def _complete(host: str, token: str, endpoint: str, messages: List[Dict[str, str]], max_tokens: int) -> str:
    """Non-streamed chat completion on a Model Serving endpoint."""
    # A reasoning model (GPT-6 Luna) thinks inside max_tokens: the 120-token rewrite ceiling would
    # leave it empty, so it gets the reasoning floor; its content may be a list of blocks.
    payload: Dict[str, Any] = {'messages': messages,
                               'max_tokens': effective_max_tokens(endpoint, max_tokens, _REASONING_REWRITE_FLOOR)}
    if supports_temperature(endpoint):
        payload['temperature'] = 0.0
    async with httpx.AsyncClient(timeout=_LLM_TIMEOUT_S) as client:
        resp = await client.post(f'{host}/serving-endpoints/{endpoint}/invocations', json=payload,
                                 headers={'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'})
        resp.raise_for_status()
        return message_text(resp.json()['choices'][0]['message']['content'])


async def search_query_fr(host: str, token: str, endpoint: str, conversation: List[Dict[str, str]]) -> Optional[str]:
    """The last question as a standalone French search query, or None if the rewrite fails.

    On failure the turn still runs, searching with the question as-is (logged).
    """
    transcript = '\n'.join(f"{m['role']}: {m['content']}" for m in conversation)
    try:
        query = (await _complete(host, token, endpoint, [{'role': 'system', 'content': REWRITE_PROMPT},
                                                         {'role': 'user', 'content': transcript}],
                                 _QUERY_MAX_TOKENS)).strip()
    except Exception as exc:
        logger.warning('chat_vsi: search query rewrite failed (%s) — searching with the question only', exc)
        return None
    return query or None


async def retrieve(host: str, token: str, index_name: str, queries: List[str], k: int) -> List[Dict[str, Any]]:
    """HYBRID search for each query; results merged by best rank, deduplicated by chunk."""
    try:
        results = await asyncio.gather(*(_fetch_chunks(host, token, index_name, q[:_MAX_QUERY_CHARS], k) for q in queries))
    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code
        logger.error('chat_vsi: Vector Search %s returned %d: %s', index_name, status, exc.response.text[:500])
        raise ChatVsiError(f'Document search failed (Vector Search returned {status}).', 'VectorSearchError', status) from exc
    except httpx.TimeoutException as exc:
        logger.error('chat_vsi: Vector Search %s timed out: %s', index_name, exc)
        raise ChatVsiError('Document search timed out — please try again.', 'TimeoutError') from exc
    except httpx.HTTPError as exc:
        logger.error('chat_vsi: Vector Search %s failed: %s', index_name, exc)
        raise ChatVsiError(f'Document search failed: {exc}', type(exc).__name__) from exc
    ranked: Dict[str, Tuple[int, Dict[str, Any]]] = {}
    for result in results:
        for rank, row in enumerate(result):
            cid = row['chunk_id']
            if cid not in ranked or rank < ranked[cid][0]:
                ranked[cid] = (rank, row)
    return [row for _, row in sorted(ranked.values(), key=lambda x: x[0])]


def group_documents(rows: List[Dict[str, Any]]) -> List[Tuple[str, Dict[str, Any]]]:
    """Passages grouped by document (REF), in order of first appearance."""
    docs: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        docs.setdefault(row['REF'], {'url': row['url'], 'passages': []})['passages'].append(row['chunk_text'])
    return list(docs.items())


def build_prompt(division: str, conversation: List[Dict[str, str]],
                 documents: List[Tuple[str, Dict[str, Any]]]) -> List[Dict[str, str]]:
    context = '\n\n'.join(f'[{i}] Document {ref}\n' + '\n\n'.join(d['passages'])
                          for i, (ref, d) in enumerate(documents, 1))
    turns = [dict(m) for m in conversation]
    turns[-1]['content'] = f'Documents:\n\n{context}\n\n---\n\n{turns[-1]["content"]}'
    return [{'role': 'system', 'content': load_instructions(division) + '\n' + CITATION_RULE}] + turns


def _event(payload: Dict[str, Any]) -> str:
    return f'data: {json.dumps(payload)}\n\n'


def _error_events(message: str, error_type: str, http_status: int = 0) -> List[str]:
    return [_event({'type': 'error', 'error': message, 'error_type': error_type, 'http_status': http_status}),
            'data: [DONE]\n\n']


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def stream_chat_vsi(host: str, token: str, division: str,
                          messages: List[Dict[str, str]],
                          answer_language: Optional[str] = None) -> AsyncGenerator[str, None]:
    """Answer the last user turn of ``messages`` — same event stream as ``stream_chat``.

    ``messages`` is what chat.py sends the KA: trimmed history, the last user turn
    prefixed with ``[Date: …]`` (and translated to English by the bridge if needed).
    ``answer_language`` is accepted for the variants' common signature and not used here:
    the baseline prompt is kept as delivered. The answer goes through ``chat_vsi_llm``
    (retries, CHAT_VSI_LLM_FALLBACK_ENDPOINTS), which changes no prompt.
    """
    yield ': keepalive\n\n'                      # first byte out, before the slow steps
    trace_id = f'vsi-{uuid.uuid4().hex}'
    div = normalize_division(division)
    index_name = index_for_division(div)
    endpoint = llm_endpoint()
    if not index_name:
        for e in _error_events(f'Chat VSI: no Vector Search index configured for division {div} '
                               f'(set CHAT_VSI_INDEX_{div}).', 'ConfigError'):
            yield e
        return
    conversation = _clean_history(messages)
    if not conversation or conversation[-1]['role'] != 'user':
        for e in _error_events('Chat VSI: the last message must be a user question.', 'InputError'):
            yield e
        return

    question = _without_date(conversation[-1]['content'])
    rewrite_view = conversation[:-1] + [{'role': 'user', 'content': question}]
    fr_query = await search_query_fr(host, token, endpoint, rewrite_view)
    queries = [question] + ([fr_query] if fr_query else [])

    try:
        rows = await retrieve(host, token, index_name, queries, num_results())
    except ChatVsiError as exc:
        for e in _error_events(exc.message, exc.error_type, exc.http_status):
            yield e
        return
    documents = group_documents(rows)
    logger.info('chat_vsi: division=%s index=%s fr_query=%r passages=%d documents=%d trace_id=%s',
                div, index_name, fr_query, len(rows), len(documents), trace_id)

    parser = CitationStreamParser(documents)
    answered_by = endpoint
    async for chunk in stream_answer(host, token, answer_chain(endpoint), build_prompt(div, conversation, documents),
                                     _ANSWER_MAX_TOKENS, _OPERATION):
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
            logger.error('chat_vsi: generation failed on %s: %s', endpoint, event.get('error'))
            for e in _error_events(event.get('error') or 'Answer generation failed.',
                                   event.get('error_type') or 'LLMError', event.get('http_status') or 0):
                yield e
            return
        elif kind == 'warning':
            logger.warning('chat_vsi: %s', event.get('detail') or event)
        elif kind == 'llm':
            answered_by = event.get('endpoint') or endpoint
        # 'usage' and other events are not part of the chat contract.

    tail = parser.flush()
    if tail:
        yield _event({'type': 'response.output_text.delta', 'delta': tail})
    if parser.sources or parser.citations:
        yield _event({'type': 'sources', 'sources': parser.sources, 'citations': parser.citations})
    yield _event({'type': 'metadata', 'trace_id': trace_id, 'tool_name': 'vector_search',
                  'tool_query': fr_query or question,
                  'tool_result': ', '.join(ref for ref, _ in documents),
                  'reasoning_steps': [], 'llm': answered_by})
    yield 'data: [DONE]\n\n'
