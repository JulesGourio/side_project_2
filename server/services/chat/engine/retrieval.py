"""Passages handed to the answer model for a turn: named documents, one language per document."""

import logging
import re
from typing import Any, Dict, List, Optional, Tuple


from ...infra.doc_catalog import _catalog, canon_ref
from ..turn_log import TurnLog
from .queries import opening_language, rewrite_queries, translate_query
from .search import _merge_by_rank, fetch_documents, search, start_question_search
from .config import (
    division_filter, index_name, max_search_passages, pair_raw_top_k, pair_rerank_top_k, raw_on,
)

logger = logging.getLogger(__name__)

_REF_LOOKUP_MAX = 6              # REFs (language variants included) filtered on
_REF_LOOKUP_K = 8                # passages fetched from the named documents
_DATE_PREFIX_RE = re.compile(r'^\[Date: \d{4}-\d{2}-\d{2}\]\n\n')
# Inline citation markers the chat route bakes into stored answers (⟦n⟧) — present in history.
_STORED_MARKER_RE = re.compile(r'⟦\d+⟧')


# --- Conversation helpers ---

def _clean_history(messages: List[Dict[str, str]]) -> List[Dict[str, str]]:
    """Copy of the conversation without the ``⟦n⟧`` markers stored in earlier answers."""
    return [{'role': m['role'], 'content': _STORED_MARKER_RE.sub('', m.get('content') or '')} for m in messages]


def _without_date(text: str) -> str:
    return _DATE_PREFIX_RE.sub('', text, count=1)


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


def one_language_per_document(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Only the passages of the best-ranked language variant of each document."""
    chosen: Dict[str, str] = {}
    return [row for row in rows if chosen.setdefault(canon_ref(row['REF']), row['REF']) == row['REF']]


def group_documents(rows: List[Dict[str, Any]]) -> List[Tuple[str, Dict[str, Any]]]:
    """Passages grouped by document (REF), in order of first appearance."""
    docs: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        docs.setdefault(row['REF'], {'url': row['url'], 'division': row.get('division'), 'passages': []})['passages'].append(row['chunk_text'])
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
        early = None
        today = _DATE_PREFIX_RE.match(conversation[-1]['content'])
        log.today = today.group(0).strip()[7:-1] if today else ''
        language = opening_language(conversation, question)
        log.data['rewrite_mode'] = 'translate' if language else 'rewrite'
        if language:
            with log.timed('rewrite'):
                translated = await translate_query(host, token, question, 'en' if language == 'fr' else 'fr', log=log)
            fr_query, en_query = (question, translated) if language == 'fr' else (translated, question)
            log.prompts.insert(0, 'translate_query')
        else:
            early = start_question_search(host, token, index, filters, question, log)
            with log.timed('rewrite'):
                fr_query, en_query = await rewrite_queries(
                    host, token, conversation[:-1] + [{'role': 'user', 'content': question}], log=log)
            log.prompts.insert(0, 'rewrite')
        queries = [question]
        for q in (fr_query, en_query):
            if q and q.casefold() not in {x.casefold() for x in queries}:
                queries.append(q)

        # REFs named in the question first, then in the earlier turns (a follow-up often names no REF itself).
        named: List[str] = []
        for ref in refs_named_in(question) + refs_named_in('\n'.join(m['content'] for m in conversation[:-1])):
            if ref not in named:
                named.append(ref)
        named = named[:_REF_LOOKUP_MAX]
        log.data.update({'index_name': index, 'fr_query': fr_query, 'en_query': en_query, 'named_refs': named})

        by_name = {'question': question, 'fr': fr_query, 'en': en_query}
        raw_texts: List[str] = []
        for name in raw_on():
            text = by_name[name]
            if text and text.casefold() not in {x.casefold() for x in raw_texts}:
                raw_texts.append(text)
        if raw_on() and not raw_texts:   # rewrite failed: the question keeps its raw search
            raw_texts = [question]
        sizes = {'rerank_k': pair_rerank_top_k(), 'raw_k': pair_raw_top_k()} if language and len(queries) == 2 else {}
        with log.timed('search'):
            rows = await search(host, token, index, queries, filters, raw_texts, log=log, first=early, **sizes)
        over_cap: List[Dict[str, Any]] = []
        if max_search_passages():
            rows, over_cap = rows[:max_search_passages()], rows[max_search_passages():]
        if named:
            with log.timed('ref_lookup'):
                found = await fetch_documents(host, token, index, question, named, _REF_LOOKUP_K, filters,
                                              via='ref_lookup', log=log)
            rows = _merge_by_rank([found, rows])
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
            'index': index, 'log': log}
