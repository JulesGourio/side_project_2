"""Chat VSI, variant ``rerank`` — the baseline (``chat_vsi.py``, left unchanged) with
Vector Search's built-in reranker on each query.

Only retrieval differs from the baseline:

- each query (question as-is + French rewrite, as in the baseline) asks Vector Search to
  rerank its top 50 HYBRID candidates with the Databricks reranker (cross-encoder) and
  return the best ``CHAT_VSI_RERANK_TOP_K`` (default 12) instead of the raw top 10;
- the reranked lists are merged by best rank, as in the baseline.

Two optional settings, off by default (the variant then behaves as first measured):

- ``CHAT_VSI_RERANK_MERGE=union`` — also run the baseline search (raw HYBRID top
  ``CHAT_VSI_NUM_RESULTS``) and merge both lists by best rank: the reranker can add passages
  the raw search ranked low, without pushing out what the raw search found;
- ``CHAT_VSI_MAX_PASSAGES_PER_DOC=N`` — keep at most N passages per document (REF), so a
  few documents can't fill the whole context ("which documents…" questions need breadth);
- ``CHAT_VSI_CONTEXT_BUDGET_CHARS=N`` — chunks range from ~250 to 1000+ tokens (tables,
  image descriptions), so a fixed passage count gives a context of very uneven size: with a
  budget, passages are taken in rank order until N characters (~N/4 tokens), whatever their
  count. Pair it with a larger ``CHAT_VSI_RERANK_TOP_K`` so there is enough to choose from;
- ``CHAT_VSI_REF_LOOKUP=on`` — documents the question (or an earlier turn) names by REF (QP-1518, MI-14242,
  NF-10065…, found with the app's document catalog, language variants included) get their
  own filtered search (``filters_json`` on REF), ranked first: "summarize / compare MI-14242"
  reads that document instead of whatever looks similar;
- ``CHAT_VSI_TITLE_LOOKUP=on`` — documents whose catalog title matches the question
  (``chat_vsi_titles.py``: "processus Stocker" → IQ22-223 "Stocker") get a filtered search
  too; their passages are added after the others;
- ``CHAT_VSI_ONE_LANGUAGE=on`` — keep one language variant per document (the best-ranked):
  Q0102QP_GB and Q0102QP_BG passages say the same thing and take two slots;
- ``CHAT_VSI_REWRITE=bilingual`` — the rewrite returns a French AND an English query (the
  corpus mixes both; a French question can target an English-only document such as
  QP-2270 or MI-14183), acronyms expanded when certain; three queries instead of two;
- ``CHAT_VSI_REWRITE_ENDPOINT`` — model of the rewrite only (default: the answer model,
  ``CHAT_VSI_LLM_ENDPOINT``): a small fast model shortens the time to first token;
- ``CHAT_VSI_SEARCH_RETRIES`` (default 2) — Vector Search queries retried on failure (the
  embedding endpoint refuses bursts with "Request id already running", see vector_search.py);
- ``CHAT_VSI_RERANK_ENABLED=false`` — raw HYBRID search as in the baseline (to test the
  other settings, e.g. the v2 prompt, without the reranker);
- ``CHAT_VSI_INSTRUCTIONS=v2|v3`` — the VSI-specific prompts (``chat_vsi_prompts.py``);
- ``CHAT_VSI_ANSWER_MAX_TOKENS`` / ``CHAT_VSI_REWRITE_MAX_TOKENS`` (defaults 2000 / 120, the
  baseline's) — output ceilings of the answer and of the French rewrite. Models that think
  by default (Claude Sonnet 5.5) spend part of the ceiling on reasoning: 2000 truncated
  answers and 120 can leave the rewrite empty (2026-10-07) — raise both for them.

Everything else — rewrite, grouping by document, prompt, instructions, generation, citation
parsing, event stream — is the baseline's own code, imported from ``chat_vsi``. If the
index rejects the reranker (preview not enabled, 400), the turn falls back to the baseline
retrieval and says so in the logs and in ``metadata.tool_name``.

Selected with ``CHAT_VSI_VARIANT=rerank`` (see ``chat_vsi_variants.py``).
"""

import asyncio
import json
import logging
import os
import uuid
from typing import Any, AsyncGenerator, Dict, List, Tuple

import httpx

from . import chat_vsi as base
from . import chat_vsi_prompts
from .chat_vsi_titles import documents_titled
from .doc_catalog import _catalog, canon_ref
from .streaming import stream_analysis
from .vector_search import _ARCHIVE_NOTICE_MARKER, _COLUMNS, _QUERY_TIMEOUT_S

logger = logging.getLogger(__name__)

_DEFAULT_TOP_K = 12                  # passages kept per query after reranking (baseline: 10, no rerank)
_DEFAULT_RERANK_COLUMNS = 'chunk_text'
_RERANKER_MODEL = 'databricks_reranker'


def rerank_top_k() -> int:
    return int(os.getenv('CHAT_VSI_RERANK_TOP_K', str(_DEFAULT_TOP_K)))


def rerank_columns() -> List[str]:
    return [c.strip() for c in os.getenv('CHAT_VSI_RERANK_COLUMNS', _DEFAULT_RERANK_COLUMNS).split(',') if c.strip()]


def merge_mode() -> str:
    return 'union' if os.getenv('CHAT_VSI_RERANK_MERGE', 'rerank').strip().lower() == 'union' else 'rerank'


def max_passages_per_doc() -> int:
    return int(os.getenv('CHAT_VSI_MAX_PASSAGES_PER_DOC', '0') or 0)


def context_budget_chars() -> int:
    return int(os.getenv('CHAT_VSI_CONTEXT_BUDGET_CHARS', '0') or 0)


def _on(name: str, default: str) -> bool:
    return os.getenv(name, default).strip().lower() in ('1', 'true', 'on', 'yes')


def rerank_enabled() -> bool:
    return _on('CHAT_VSI_RERANK_ENABLED', 'true')


def ref_lookup_enabled() -> bool:
    return _on('CHAT_VSI_REF_LOOKUP', 'off')


def title_lookup_enabled() -> bool:
    return _on('CHAT_VSI_TITLE_LOOKUP', 'off')


def one_language() -> bool:
    return _on('CHAT_VSI_ONE_LANGUAGE', 'off')


def rewrite_mode() -> str:
    return 'bilingual' if os.getenv('CHAT_VSI_REWRITE', 'fr').strip().lower() == 'bilingual' else 'fr'


def rewrite_endpoint(answer_endpoint: str) -> str:
    return os.getenv('CHAT_VSI_REWRITE_ENDPOINT', '').strip() or answer_endpoint


def search_retries() -> int:
    return int(os.getenv('CHAT_VSI_SEARCH_RETRIES', '2') or 0)


_REF_LOOKUP_MAX = 6          # REFs (variants included) filtered on
_REF_LOOKUP_K = 8            # passages fetched from the named documents
_TITLE_LOOKUP_DOCS = 3       # best title matches searched
_TITLE_LOOKUP_K = 6          # passages fetched from them


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


async def fetch_named_documents(host: str, token: str, index_name: str, query: str,
                                refs: List[str], k: int = _REF_LOOKUP_K) -> List[Dict[str, Any]]:
    """HYBRID search restricted to the given REFs. Failures are logged, never raised."""
    payload = {'query_text': query[:base._MAX_QUERY_CHARS], 'columns': _COLUMNS, 'num_results': k,
               'query_type': 'HYBRID', 'filters_json': json.dumps({'REF': refs})}
    try:
        async with httpx.AsyncClient(timeout=_QUERY_TIMEOUT_S) as client:
            resp = await client.post(f'{host}/api/2.0/vector-search/indexes/{index_name}/query', json=payload,
                                     headers={'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'})
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:  # noqa: BLE001 — the turn goes on without the lookup
        logger.warning('chat_vsi_rerank: REF lookup %s on %s failed: %s', refs, index_name, exc)
        return []
    columns = [c['name'] for c in data.get('manifest', {}).get('columns', [])]
    chunks = [dict(zip(columns, row)) for row in data.get('result', {}).get('data_array', [])]
    return [c for c in chunks if _ARCHIVE_NOTICE_MARKER not in (c.get('chunk_text') or '')]


def answer_max_tokens() -> int:
    return int(os.getenv('CHAT_VSI_ANSWER_MAX_TOKENS', str(base._ANSWER_MAX_TOKENS)))


def rewrite_max_tokens() -> int:
    return int(os.getenv('CHAT_VSI_REWRITE_MAX_TOKENS', str(base._QUERY_MAX_TOKENS)))


def _message_text(content: Any) -> str:
    """Chat-completion ``message.content``: a string, or a list of blocks (reasoning models)
    from which only the text blocks are kept."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return ''.join(b.get('text', '') for b in content
                       if isinstance(b, dict) and b.get('type') in (None, 'text', 'output_text'))
    return ''


BILINGUAL_REWRITE_PROMPT = """Using the conversation for context, rewrite the user's LAST question as one standalone search
query, for a search engine over Latécoère quality documents written in French or in English.
Write it twice, as exactly two lines:
FR: <the query in French>
EN: <the same query in English>
Keep document codes, acronyms and technical terms as they are. When the question uses an
acronym and you are certain of its meaning, keep it and add its expanded form in each language.
Return only the two lines."""


def split_bilingual(text: str) -> Tuple[str, str]:
    """(French query, English query) from the bilingual rewrite; a reply without the
    ``FR:``/``EN:`` labels is taken as the French query."""
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


async def search_query_fr(host: str, token: str, endpoint: str, conversation: List[Dict[str, str]]) -> Any:
    """``chat_vsi.search_query_fr`` with a configurable ceiling and reasoning-model content.
    With ``CHAT_VSI_REWRITE=bilingual`` the reply holds two lines (``split_bilingual``)."""
    transcript = '\n'.join(f"{m['role']}: {m['content']}" for m in conversation)
    bilingual = rewrite_mode() == 'bilingual'
    payload: Dict[str, Any] = {'messages': [{'role': 'system',
                                             'content': BILINGUAL_REWRITE_PROMPT if bilingual else base.REWRITE_PROMPT},
                                            {'role': 'user', 'content': transcript}],
                               'max_tokens': max(rewrite_max_tokens(), 250) if bilingual else rewrite_max_tokens()}
    if base.supports_temperature(endpoint):
        payload['temperature'] = 0.0
    try:
        async with httpx.AsyncClient(timeout=base._LLM_TIMEOUT_S) as client:
            resp = await client.post(f'{host}/serving-endpoints/{endpoint}/invocations', json=payload,
                                     headers={'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'})
            resp.raise_for_status()
            query = _message_text(resp.json()['choices'][0]['message'].get('content')).strip()
    except Exception as exc:  # noqa: BLE001 — the turn goes on with the question only
        logger.warning('chat_vsi_rerank: search query rewrite failed (%s) — searching with the question only', exc)
        return None
    if not query:
        logger.warning('chat_vsi_rerank: empty search query rewrite on %s (max_tokens=%d) — question only',
                       endpoint, rewrite_max_tokens())
    return query or None


def settings() -> Dict[str, Any]:
    """What this variant runs with — saved with evaluation results."""
    return {'variant': 'rerank', 'reranker': _RERANKER_MODEL, 'top_k': rerank_top_k(),
            'columns_to_rerank': rerank_columns(), 'merge': merge_mode(),
            'max_passages_per_doc': max_passages_per_doc(), 'context_budget_chars': context_budget_chars(),
            'rerank_enabled': rerank_enabled(), 'ref_lookup': ref_lookup_enabled(),
            'title_lookup': title_lookup_enabled(), 'one_language': one_language(),
            'rewrite': rewrite_mode(), 'rewrite_llm': rewrite_endpoint(base.llm_endpoint()),
            'search_retries': search_retries(),
            'instructions': chat_vsi_prompts.instructions_set(), 'answer_max_tokens': answer_max_tokens(),
            'rewrite_max_tokens': rewrite_max_tokens(), 'llm': base.llm_endpoint()}


def _merge_by_rank(lists: List[List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    ranked: Dict[str, Tuple[int, Dict[str, Any]]] = {}
    for result in lists:
        for rank, row in enumerate(result):
            cid = row['chunk_id']
            if cid not in ranked or rank < ranked[cid][0]:
                ranked[cid] = (rank, row)
    return [row for _, row in sorted(ranked.values(), key=lambda x: x[0])]


def fit_budget(rows: List[Dict[str, Any]], budget: int) -> List[Dict[str, Any]]:
    """Passages in rank order until ``budget`` characters of chunk_text (budget <= 0: unchanged).

    The best-ranked passage is always kept; a passage that would overflow is skipped and
    smaller ones further down can still fit.
    """
    if budget <= 0:
        return rows
    kept, used = [], 0
    for row in rows:
        size = len(row.get('chunk_text') or '')
        if kept and used + size > budget:
            continue
        kept.append(row)
        used += size
    return kept


def cap_per_document(rows: List[Dict[str, Any]], n: int) -> List[Dict[str, Any]]:
    """At most n passages per REF, keeping the best-ranked ones (n <= 0: unchanged)."""
    if n <= 0:
        return rows
    kept, per_doc = [], {}
    for row in rows:
        per_doc[row['REF']] = per_doc.get(row['REF'], 0) + 1
        if per_doc[row['REF']] <= n:
            kept.append(row)
    return kept


def one_language_per_document(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Only the passages of the best-ranked language variant of each document."""
    chosen: Dict[str, str] = {}
    kept = []
    for row in rows:
        ref = row['REF']
        if chosen.setdefault(canon_ref(ref), ref) == ref:
            kept.append(row)
    return kept


def append_new(rows: List[Dict[str, Any]], extra: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """rows, then the passages of extra not already in rows."""
    seen = {r['chunk_id'] for r in rows}
    return rows + [r for r in extra if r['chunk_id'] not in seen]


async def _retrying(search, attempts: int):
    """Run ``search()`` again on a Vector Search failure (bursts, timeouts), then give up."""
    for attempt in range(attempts + 1):
        try:
            return await search()
        except base.ChatVsiError as exc:
            if attempt == attempts or exc.http_status in (401, 403, 404):
                raise
            logger.warning('chat_vsi_rerank: search failed (%s), retry %d/%d', exc.message, attempt + 1, attempts)
            await asyncio.sleep(1.5 * (attempt + 1))


class RerankUnavailable(Exception):
    """The index refused the reranker parameter."""


async def _fetch_reranked(host: str, token: str, index_name: str, query_text: str, k: int) -> List[Dict[str, Any]]:
    payload = {
        'query_text': query_text[:base._MAX_QUERY_CHARS], 'columns': _COLUMNS, 'num_results': k,
        'query_type': 'HYBRID',
        'reranker': {'model': _RERANKER_MODEL, 'parameters': {'columns_to_rerank': rerank_columns()}},
    }
    async with httpx.AsyncClient(timeout=_QUERY_TIMEOUT_S) as client:
        resp = await client.post(f'{host}/api/2.0/vector-search/indexes/{index_name}/query', json=payload,
                                 headers={'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'})
    if resp.status_code == 400 and 'rerank' in resp.text.lower():
        raise RerankUnavailable(resp.text[:300])
    resp.raise_for_status()
    data = resp.json()
    columns = [c['name'] for c in data.get('manifest', {}).get('columns', [])]
    chunks = [dict(zip(columns, row)) for row in data.get('result', {}).get('data_array', [])]
    return [c for c in chunks if _ARCHIVE_NOTICE_MARKER not in (c.get('chunk_text') or '')]


async def retrieve_reranked(host: str, token: str, index_name: str, queries: List[str], k: int) -> Tuple[List[Dict[str, Any]], bool]:
    """Reranked search per query, merged by best rank. Returns (rows, reranked)."""
    try:
        results = await asyncio.gather(*(_fetch_reranked(host, token, index_name, q, k) for q in queries))
    except RerankUnavailable as exc:
        logger.warning('chat_vsi_rerank: reranker refused by %s (%s) — baseline retrieval', index_name, exc)
        return await base.retrieve(host, token, index_name, queries, base.num_results()), False
    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code
        logger.error('chat_vsi_rerank: Vector Search %s returned %d: %s', index_name, status, exc.response.text[:500])
        raise base.ChatVsiError(f'Document search failed (Vector Search returned {status}).', 'VectorSearchError', status) from exc
    except httpx.TimeoutException as exc:
        logger.error('chat_vsi_rerank: Vector Search %s timed out: %s', index_name, exc)
        raise base.ChatVsiError('Document search timed out — please try again.', 'TimeoutError') from exc
    except httpx.HTTPError as exc:
        logger.error('chat_vsi_rerank: Vector Search %s failed: %s', index_name, exc)
        raise base.ChatVsiError(f'Document search failed: {exc}', type(exc).__name__) from exc
    return _merge_by_rank(list(results)), True


async def retrieve_for_turn(host: str, token: str, index_name: str, endpoint: str,
                            conversation: List[Dict[str, str]]) -> Dict[str, Any]:
    """The passages this variant hands to the LLM for the last user turn of ``conversation``
    (already cleaned by ``chat_vsi._clean_history``). Raises ``ChatVsiError`` on a search failure.
    Also used alone by the retrieval evaluation (no answer generated)."""
    question = base._without_date(conversation[-1]['content'])
    rewrite_view = conversation[:-1] + [{'role': 'user', 'content': question}]
    rewrite = await search_query_fr(host, token, rewrite_endpoint(endpoint), rewrite_view)
    fr_query, en_query = split_bilingual(rewrite) if rewrite_mode() == 'bilingual' else (rewrite, '')
    queries = [question]
    for q in (fr_query, en_query):
        if q and q.casefold() not in {x.casefold() for x in queries}:
            queries.append(q)

    # REFs named in the question first, then in the earlier turns ("résume la slide 15" after an
    # answer about MI-14242 names no REF itself — golden run 2026-10-07: 0 lookups triggered).
    named = []
    if ref_lookup_enabled():
        for ref in refs_named_in(question) + refs_named_in('\n'.join(m['content'] for m in conversation[:-1])):
            if ref not in named:
                named.append(ref)
        named = named[:_REF_LOOKUP_MAX]
    titled = documents_titled(queries, _TITLE_LOOKUP_DOCS) if title_lookup_enabled() else []

    async def search():
        if not rerank_enabled():
            return await base.retrieve(host, token, index_name, queries, base.num_results()), False
        if merge_mode() == 'union':
            (rows, reranked), raw = await asyncio.gather(
                retrieve_reranked(host, token, index_name, queries, rerank_top_k()),
                base.retrieve(host, token, index_name, queries, base.num_results()))
            return _merge_by_rank([rows, raw]), reranked
        return await retrieve_reranked(host, token, index_name, queries, rerank_top_k())

    rows, reranked = await _retrying(search, search_retries())
    if named:
        rows = _merge_by_rank([await fetch_named_documents(host, token, index_name, question, named), rows])
    if titled:
        title_refs = [ref for _, refs, _ in titled for ref in refs]
        rows = append_new(rows, await fetch_named_documents(host, token, index_name, fr_query or question,
                                                            title_refs, _TITLE_LOOKUP_K))
    if one_language():
        rows = one_language_per_document(rows)
    rows = fit_budget(cap_per_document(rows, max_passages_per_doc()), context_budget_chars())
    return {'question': question, 'fr_query': fr_query, 'en_query': en_query, 'rows': rows, 'reranked': reranked,
            'named': named, 'titled': [canon for canon, _, _ in titled]}


async def stream_chat_vsi_rerank(host: str, token: str, division: str,
                                 messages: List[Dict[str, str]]) -> AsyncGenerator[str, None]:
    """``chat_vsi.stream_chat_vsi`` with reranked retrieval — same event stream."""
    yield ': keepalive\n\n'
    trace_id = f'vsi-rerank-{uuid.uuid4().hex}'
    div = base.normalize_division(division)
    index_name = base.index_for_division(div)
    endpoint = base.llm_endpoint()
    if not index_name:
        for e in base._error_events(f'Chat VSI: no Vector Search index configured for division {div} '
                                    f'(set CHAT_VSI_INDEX_{div}).', 'ConfigError'):
            yield e
        return
    conversation = base._clean_history(messages)
    if not conversation or conversation[-1]['role'] != 'user':
        for e in base._error_events('Chat VSI: the last message must be a user question.', 'InputError'):
            yield e
        return

    try:
        found = await retrieve_for_turn(host, token, index_name, endpoint, conversation)
    except base.ChatVsiError as exc:
        for e in base._error_events(exc.message, exc.error_type, exc.http_status):
            yield e
        return
    question, fr_query, rows, reranked, named, titled = (
        found[k] for k in ('question', 'fr_query', 'rows', 'reranked', 'named', 'titled'))
    documents = base.group_documents(rows)
    logger.info('chat_vsi_rerank: division=%s index=%s reranked=%s merge=%s cap=%d budget=%d fr_query=%r '
                'passages=%d chars=%d documents=%d trace_id=%s',
                div, index_name, reranked, merge_mode(), max_passages_per_doc(), context_budget_chars(), fr_query,
                len(rows), sum(len(r.get('chunk_text') or '') for r in rows), len(documents), trace_id)

    parser = base.CitationStreamParser(documents)
    usage: Dict[str, Any] = {}
    async for chunk in stream_analysis(host, token, endpoint, chat_vsi_prompts.build_prompt(div, conversation, documents),
                                       max_tokens=answer_max_tokens(), thinking_budget=0, temperature=0.0,
                                       operation=base._OPERATION):
        if not chunk.startswith('data: '):
            yield chunk
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
                yield base._event({'type': 'response.output_text.delta', 'delta': clean})
        elif kind == 'error':
            logger.error('chat_vsi_rerank: generation failed on %s: %s', endpoint, event.get('error'))
            for e in base._error_events(event.get('error') or 'Answer generation failed.',
                                        event.get('error_type') or 'LLMError', event.get('http_status') or 0):
                yield e
            return
        elif kind == 'warning':
            logger.warning('chat_vsi_rerank: %s', event.get('detail') or event)
        elif kind == 'usage':
            usage = {k: event.get(k) for k in ('input_tokens', 'output_tokens', 'thinking_tokens', 'cost_eur')}

    tail = parser.flush()
    if tail:
        yield base._event({'type': 'response.output_text.delta', 'delta': tail})
    if parser.sources or parser.citations:
        yield base._event({'type': 'sources', 'sources': parser.sources, 'citations': parser.citations})
    yield base._event({'type': 'metadata', 'trace_id': trace_id,
                       'tool_name': ('vector_search+rerank' if reranked
                                     else 'vector_search' if not rerank_enabled() else 'vector_search (rerank refused)')
                                    + (f'+ref_lookup({",".join(named)})' if named else '')
                                    + (f'+title_lookup({",".join(titled)})' if titled else ''),
                       'tool_query': fr_query or question,
                       'tool_result': ', '.join(ref for ref, _ in documents),
                       'reasoning_steps': [],
                       # Answer generation only (the short French rewrite call is not counted).
                       'usage': usage, 'llm': endpoint})
    yield 'data: [DONE]\n\n'
