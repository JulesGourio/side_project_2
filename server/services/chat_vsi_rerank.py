"""Chat VSI, variant ``rerank`` — the baseline (``chat_vsi.py``, left unchanged) with
Vector Search's built-in reranker on each query.

Only retrieval differs from the baseline:

- each query (question as-is + French rewrite, as in the baseline) asks Vector Search to
  rerank its top 50 HYBRID candidates with the Databricks reranker (cross-encoder) and
  return the best ``CHAT_VSI_RERANK_TOP_K`` (default 12) instead of the raw top 10;
- the reranked lists are merged by best rank, as in the baseline.

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


def settings() -> Dict[str, Any]:
    """What this variant runs with — saved with evaluation results."""
    return {'variant': 'rerank', 'reranker': _RERANKER_MODEL, 'top_k': rerank_top_k(),
            'columns_to_rerank': rerank_columns(), 'llm': base.llm_endpoint()}


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
    ranked: Dict[str, Tuple[int, Dict[str, Any]]] = {}
    for result in results:
        for rank, row in enumerate(result):
            cid = row['chunk_id']
            if cid not in ranked or rank < ranked[cid][0]:
                ranked[cid] = (rank, row)
    return [row for _, row in sorted(ranked.values(), key=lambda x: x[0])], True


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

    question = base._without_date(conversation[-1]['content'])
    rewrite_view = conversation[:-1] + [{'role': 'user', 'content': question}]
    fr_query = await base.search_query_fr(host, token, endpoint, rewrite_view)
    queries = [question] + ([fr_query] if fr_query else [])

    try:
        rows, reranked = await retrieve_reranked(host, token, index_name, queries, rerank_top_k())
    except base.ChatVsiError as exc:
        for e in base._error_events(exc.message, exc.error_type, exc.http_status):
            yield e
        return
    documents = base.group_documents(rows)
    logger.info('chat_vsi_rerank: division=%s index=%s reranked=%s fr_query=%r passages=%d documents=%d trace_id=%s',
                div, index_name, reranked, fr_query, len(rows), len(documents), trace_id)

    parser = base.CitationStreamParser(documents)
    async for chunk in stream_analysis(host, token, endpoint, base.build_prompt(div, conversation, documents),
                                       max_tokens=base._ANSWER_MAX_TOKENS, thinking_budget=0, temperature=0.0,
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

    tail = parser.flush()
    if tail:
        yield base._event({'type': 'response.output_text.delta', 'delta': tail})
    if parser.sources or parser.citations:
        yield base._event({'type': 'sources', 'sources': parser.sources, 'citations': parser.citations})
    yield base._event({'type': 'metadata', 'trace_id': trace_id,
                       'tool_name': 'vector_search+rerank' if reranked else 'vector_search (rerank refused)',
                       'tool_query': fr_query or question,
                       'tool_result': ', '.join(ref for ref, _ in documents),
                       'reasoning_steps': []})
    yield 'data: [DONE]\n\n'
