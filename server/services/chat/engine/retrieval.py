"""Passages handed to the answer model for a turn: named documents, one language per document."""

import asyncio
import logging
import re
from typing import Dict, Iterable, List, Tuple

from server.services.infra.doc_catalog import _catalog, canon_ref, document_info, site_code
from server.services.chat.turn_log import TurnLog, passage_date
from server.services.chat.engine.queries import opening_language, rewrite_queries, translate_query
from server.services.chat.engine.references import cited_codes_asked, exact_citations
from server.services.chat.engine.types import DocumentPassages, PassageRecord, RetrievalResult, SearchRow
from server.services.chat.engine.search import _merge_by_rank, fetch_documents, search, start_question_search
from server.services.chat.engine.config import (
    division_filter, index_name, max_search_passages, pair_raw_top_k, pair_rerank_top_k, raw_on, raw_top_k, rerank_top_k,
)
from utils.parsing_pipeline.code_normalization import canonical_codes

logger = logging.getLogger(__name__)

_REF_LOOKUP_MAX = 6              # REFs (language variants included) filtered on
_REF_LOOKUP_K = 8                # passages fetched from the named documents
_CITING_LOOKUP_DOCS = 10         # most recent documents citing an asked code whose passages are fetched
_CITING_LOOKUP_K = 20            # passages fetched from them
_DATE_PREFIX_RE = re.compile(r'^\[Date: \d{4}-\d{2}-\d{2}\]\n\n')
# Inline citation markers the chat route bakes into stored answers (⟦n⟧) — present in history.
_STORED_MARKER_RE = re.compile(r'⟦\d+⟧')
# REF suffixes of a variant written in the question's language (opening_language: 'fr' or 'en').
_LANGUAGE_SUFFIXES = {'fr': ('FR',), 'en': ('EN', 'GB')}


# --- Conversation helpers ---

def _clean_history(messages: List[Dict[str, str]]) -> List[Dict[str, str]]:
    """Copy of the conversation without the ``⟦n⟧`` markers stored in earlier answers.

    :param messages: chat messages, each with ``role`` and ``content``
    :return: copies with ``role`` and ``content``, markers removed
    """
    return [{'role': m['role'], 'content': _STORED_MARKER_RE.sub('', m.get('content') or '')} for m in messages]


def _without_date(text: str) -> str:
    """The text without its leading ``[Date: YYYY-MM-DD]`` line.

    :param text: last user turn, possibly prefixed by its date line
    :return: the text without that prefix
    """
    return _DATE_PREFIX_RE.sub('', text, count=1)


def refs_named_in(text: str) -> List[str]:
    """Catalog REFs the text names, with their language variants (at most _REF_LOOKUP_MAX).

    :param text: a question or an earlier turn
    :return: the REFs in order of appearance, language variants included
    """
    cat = _catalog()
    if not cat or not text:
        return []
    refs = []
    for entry in cat.find_in_text(text):
        siblings = [sibling['ref'] for sibling in cat.group_siblings(entry['ref'])]
        refs.extend([entry['ref']] + siblings)
    return list(dict.fromkeys(refs))[:_REF_LOOKUP_MAX]


def _refs_named_in_turns(question: str, earlier_turns: str) -> List[str]:
    """REFs named in the question, then in the earlier turns, without duplicates (at most _REF_LOOKUP_MAX).

    :param question: last user turn
    :param earlier_turns: the previous turns joined by newlines
    :return: the REFs in order of appearance, the question's first
    """
    return list(dict.fromkeys(refs_named_in(question) + refs_named_in(earlier_turns)))[:_REF_LOOKUP_MAX]


def _variant_date(row: SearchRow) -> str:
    """Diffusion date of a passage's document: the ``[Source: …]`` prefix, else the catalog.

    :param row: search row with ``REF`` and ``chunk_text``
    :return: ``YYYY-MM-DD``, or an empty string when unknown
    """
    date = passage_date(row.get('chunk_text'))
    if date[:1].isdigit():
        return date
    return str(document_info(row['REF']).get('doc_date') or '')[:10]


def one_language_per_document(rows: List[SearchRow], question_codes: Iterable[str] = (),
                              language: str | None = None) -> List[SearchRow]:
    """Only the passages of one language variant of each document (Q0102QP_GB or Q0102QP_BG, not both).

    The variant kept is, in this order: the one whose passages cite a reference code of the question (the other
    language may be an older revision that does not cite it yet), then the most recently published, then the one in
    the question's language, then the best ranked.

    :param rows: passages in rank order
    :param question_codes: canonical reference codes of the question (``code_normalization``)
    :param language: language of the question, ``fr`` or ``en``; None when unknown
    :return: the passages of the chosen variant of each document, in rank order
    """
    codes = set(question_codes)
    preferred = _LANGUAGE_SUFFIXES.get(language or '', ())
    variants: Dict[str, Dict[str, Tuple]] = {}
    for rank, row in enumerate(rows):
        ref = row['REF']
        cites = bool(codes & set(canonical_codes(row.get('chunk_text') or '')))
        best = variants.setdefault(canon_ref(ref), {}).get(ref)
        if best is None:
            best = (False, _variant_date(row), site_code(ref) in preferred, -rank)
        variants[canon_ref(ref)][ref] = (best[0] or cites,) + best[1:]
    chosen = {canon: max(by_ref, key=by_ref.get) for canon, by_ref in variants.items()}
    return [row for row in rows if chosen[canon_ref(row['REF'])] == row['REF']]


def group_documents(rows: List[SearchRow]) -> List[Tuple[str, DocumentPassages]]:
    """Passages grouped by document (REF), in order of first appearance.

    :param rows: passages with ``REF``, ``url``, ``division`` and ``chunk_text``
    :return: list of ``(REF, {url, division, passages})`` pairs
    """
    docs: Dict[str, DocumentPassages] = {}
    for row in rows:
        docs.setdefault(row['REF'], {'url': row['url'], 'division': row.get('division'), 'passages': []})['passages'].append(row['chunk_text'])
    return list(docs.items())


def _passage(row: SearchRow, position: int, drop_reason: str = '') -> PassageRecord:
    """One chat_retrieved_chunks record (doc_number, prompt_rank and cited are set once the prompt
    and the answer exist).

    :param row: search row, with its ``_hits``
    :param position: index of the passage in retrieval order
    :param drop_reason: why the passage is not sent (``other_language`` or ``over_cap``); empty when kept
    :return: the chat_retrieved_chunks record
    """
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
                            log: TurnLog | None = None) -> RetrievalResult:
    """The passages handed to the LLM for the last user turn of ``conversation`` (already
    cleaned by ``_clean_history``). A failed rewrite or translation degrades the turn: the question is searched as asked.
    Raises ``ChatVsiError`` when the search fails. Also used
    alone by the retrieval evaluation (no answer generated). ``log`` (created when absent, returned
    as ``found['log']``) receives the step durations, the queries, every passage retrieved — kept
    or set aside — and what did not run as configured.

    :param host: Databricks workspace URL
    :param token: Databricks access token
    :param division: normalised division code
    :param conversation: cleaned conversation, its last item the question
    :param log: turn log to fill, created when None
    :return: dict with the question, the two queries, the passages sent, the REFs named, the index, the log and, for
        a "which documents cite X?" question, the documents citing each code (``exact_citations``)
    """
    log = log if log is not None else TurnLog()
    with log.timed('retrieval'):
        index = index_name()
        filters = division_filter(division)
        question = _without_date(conversation[-1]['content'])
        early = None
        today = _DATE_PREFIX_RE.match(conversation[-1]['content'])
        log.today = today.group(0).strip()[7:-1] if today else ''
        language = opening_language(conversation, question)
        degraded = False
        log.data['rewrite_mode'] = 'translate' if language else 'rewrite'
        if language:
            with log.timed('rewrite'):
                try:
                    translated = await translate_query(host, token, question, 'en' if language == 'fr' else 'fr', log=log)
                except Exception:   # already logged by translate_query: the turn goes on with the question as asked
                    translated, degraded = question, True
            fr_query, en_query = (question, translated) if language == 'fr' else (translated, question)
            log.prompts.insert(0, 'translate_query')
        else:
            early = start_question_search(host, token, index, filters, question, log)
            with log.timed('rewrite'):
                try:
                    fr_query, en_query = await rewrite_queries(
                        host, token, conversation[:-1] + [{'role': 'user', 'content': question}], log=log)
                except Exception:   # already logged by rewrite_queries: the turn goes on with the question as asked
                    fr_query = en_query = question
                    degraded = True
            log.prompts.insert(0, 'rewrite')
        queries = [question]
        for q in (fr_query, en_query):
            if q and q.casefold() not in {x.casefold() for x in queries}:
                queries.append(q)

        # REFs named in the question first, then in the earlier turns (a follow-up often names no REF itself).
        earlier_turns = '\n'.join(m['content'] for m in conversation[:-1])
        named = await asyncio.to_thread(_refs_named_in_turns, question, earlier_turns)
        log.data.update({'index_name': index, 'fr_query': fr_query, 'en_query': en_query, 'named_refs': named})

        by_name = {'question': question, 'fr': fr_query, 'en': en_query}
        raw_texts: List[str] = []
        for name in raw_on():
            text = by_name[name]
            if text and text.casefold() not in {x.casefold() for x in raw_texts}:
                raw_texts.append(text)
        sizes = {'rerank_k': pair_rerank_top_k(), 'raw_k': pair_raw_top_k()} if language and len(queries) == 2 else {}
        if degraded:
            # One query stands for the three (or two) of a normal turn: it asks for as many candidates in all.
            if language:
                sizes = {'rerank_k': 2 * pair_rerank_top_k(), 'raw_k': pair_raw_top_k() * max(1, len(raw_on()))}
            else:
                sizes = {'rerank_k': 3 * rerank_top_k(), 'raw_k': raw_top_k() * max(1, len(raw_on()))}
            if early is not None:   # started with the normal size
                early.cancel()
                await asyncio.gather(early, return_exceptions=True)
                early = None
        with log.timed('search'):
            rows = await search(host, token, index, queries, filters, raw_texts, log=log, first=early, **sizes)
        over_cap: List[SearchRow] = []
        if max_search_passages():
            rows, over_cap = rows[:max_search_passages()], rows[max_search_passages():]
        if named:
            with log.timed('ref_lookup'):
                found = await fetch_documents(host, token, index, question, named, _REF_LOOKUP_K, filters,
                                              via='ref_lookup', log=log)
            rows = _merge_by_rank([found, rows])

        # "Which documents cite X?": the complete list comes from doc_references; the search adds where they cite it.
        exact = exact_citations(cited_codes_asked(question), division)
        if exact:
            log.data['exact_citations'] = {code: len(docs) for code, (_, docs) in exact.items()}
            citing = list(dict.fromkeys(d['ref'] for _, docs in exact.values() for d in docs))[:_CITING_LOOKUP_DOCS]
            if citing:
                with log.timed('citation_lookup'):
                    found = await fetch_documents(host, token, index, question, citing, _CITING_LOOKUP_K, filters,
                                                  via='citation_lookup', log=log)
                rows = _merge_by_rank([found, rows])
        candidates = rows
        rows = one_language_per_document(rows, canonical_codes(question, accept_lowercase=True), language)

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
            'index': index, 'log': log, 'exact_citations': exact}
