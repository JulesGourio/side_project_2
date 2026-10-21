"""Impact search — Vector Search retrieval, then one LLM judgment per candidate document.

Flow (run_impact_search, an async generator of events streamed to the UI):
1. one HYBRID Vector Search query per reported change (impact_queries.py);
2. hits grouped per document (language variants of one document merged);
3. the best-ranked candidates are judged in parallel, one LLM call each, from
   all of their retrieved passages — the judge returns a verdict plus every
   conflicting passage, the change(s) it conflicts with and a verbatim quote;
4. lower-ranked candidates are still returned, flagged as not judged, so the
   UI never silently hides a retrieved document.
"""

import asyncio
import json
import logging
import re
import time
from typing import Any, AsyncIterator, Dict, List, Tuple

from server.services.compare.llm import CompareLlmUnavailable, LlmFailure, TIRED_MESSAGE, compare_chain, complete_chain
from server.services.compare.llm import message_text as _message_text
from server.services.infra import vs_gate
from server.services.infra.config import (
    IMPACT_JUDGE_CONCURRENCY, IMPACT_JUDGE_MAX_TOKENS_CAP, IMPACT_JUDGE_RETRIES, IMPACT_JUDGE_TIMEOUT_S,
    IMPACT_JUDGE_TRUNCATION_BUDGET_FACTOR, IMPACT_PASSAGE_CHARS, IMPACT_PASSAGES_PER_CANDIDATE, IMPACT_QUERY_CONCURRENCY,
    IMPACT_QUERY_RETRIES, VECTOR_SEARCH_QUERY_TIMEOUT_S,
)
from server.services.infra.doc_catalog import canon_ref, other_language_refs, site_code, site_flag, title_for_ref
from server.services.infra.streaming import _cost_eur

logger = logging.getLogger(__name__)

_COLUMNS = ['chunk_id', 'IDDOC', 'REF', 'division', 'url', 'semantic_headers', 'chunk_text']
# Metadata-only chunk the parsing pipeline writes for an archived document (config.ARCHIVE_NOTICE_MARKER):
# it holds no content, so it can never be an impacted passage.
_ARCHIVE_NOTICE_MARKER = 'ARCHIVED DOCUMENT — CONTENT NOT INDEXED'

_JUDGE_SYSTEM_PROMPT = """\
You are Qualibot, a technical documentation controller. A reference document has just been revised. \
You are given the numbered list of CHANGES made to it (C1, C2, ...) and ONE candidate document from the \
knowledge base, retrieved by an approximate similarity search, with several numbered PASSAGES of it.

Decide whether this candidate document is genuinely impacted, i.e. must be updated to stay consistent \
with the revised reference. "Genuinely impacted" means at least one passage states, prescribes, or \
relies on something — a value, step, requirement, tool, term, role, or reference — that a change \
directly contradicts, replaces, or makes outdated. Sharing vocabulary or the general topic is NOT \
enough (mentioning "torque" does not make a document impacted by a change to a specific torque \
value, unless a passage states that value or a step depending on it). Judge only from the passages \
shown; never assume content that isn't there. Read ALL passages: the conflict can sit in any of them.

Passages may start with a heading hint like [heading: "6. DESCRIPTION DETAILLEE"]; the passage text \
itself often embeds more precise section markers like "[6. DESCRIPTION > 6.1 GENERER DES IDEES]". \
Prefer the inline markers, fall back to the hint. [no heading] just means none was captured — never \
echo it as a section name.

confidence:
- "high": a passage explicitly states the exact fact the change modifies (impacted), or every \
passage is clearly about something unrelated (not impacted).
- "medium": same topic and plausibly affected, but the link needs a reasonable inference.
- "low": thin or ambiguous evidence either way — a human should double-check.

"reason": open with a one-sentence verdict on the whole document, then (if impacted) in 1-2 more \
sentences what the document currently says versus what the change sets (old vs new values when \
concrete). If not impacted, say concretely what the document is about and why that is unrelated. \
No vague filler ("may be affected", "seems related").

"passages": ONLY when impacted, one entry per passage that conflicts with a change — list EVERY such \
passage, not just the most obvious one. For each:
- "passage": its number;
- "changes": the ids of the change(s) it conflicts with;
- "section": section number and/or title where it sits (from the inline markers or heading hint), \
"" if none;
- "quote": the exact conflicting words copied VERBATIM from the passage (a few words to one \
sentence, no ellipsis, no rewording) — it is highlighted in the UI by exact match;
- "explanation": one short sentence, what the passage says versus what the change now requires.

Respond with ONLY a JSON object, no markdown fences:
{"impacted": true|false, "confidence": "high"|"medium"|"low", "reason": "...", \
"passages": [{"passage": 1, "changes": ["C1"], "section": "...", "quote": "...", "explanation": "..."}]}

Be conservative: no clear conflict in the passages → impacted false and "passages": [].

LANGUAGE: write "reason" and "explanation" in the SAME language as the CHANGES text and the \
passages. Do not switch to English when they are not in English.\
"""

# "[Source: REF | Title: ... | Division: ... | Category: ... | Date de diffusion: YYYY-MM-DD | Image: page 4, ...]\n\n"
# — the provenance prefix the parsing pipeline embeds in every chunk (utils.source_prefixed_text).
_PREFIX_RE = re.compile(r'^\[Source:([^\n]*)\]\n\n')
_TITLE_RE = re.compile(r'\| Title: (.*?) \| Division:')
_DATE_RE = re.compile(r'Date de diffusion: (\d{4}-\d{2}-\d{2})')
_PAGE_RE = re.compile(r'Image: page (\d+)')
_STATUS_ORDER = {'impacted': 0, 'check': 1, 'not_impacted': 2, 'error': 3}


def _split_prefix(chunk_text: str) -> Tuple[str, Dict[str, str]]:
    """Strip the pipeline's provenance prefix; return (body, {title, doc_date, page}).

    :param chunk_text: chunk text, with or without its provenance prefix
    :return: ``(body, {title, doc_date, page})``, the metadata keys present only when found
    """
    m = _PREFIX_RE.match(chunk_text or '')
    if not m:
        return chunk_text or '', {}
    head = m.group(1)
    meta = {}
    for key, rx in (('title', _TITLE_RE), ('doc_date', _DATE_RE), ('page', _PAGE_RE)):
        hit = rx.search(head)
        if hit:
            meta[key] = hit.group(1).strip()
    return chunk_text[m.end():], meta


def _extract_header(semantic_headers: Any) -> str:
    """Best-effort: the first header value of the semantic_headers JSON blob.

    :param semantic_headers: JSON text of the semantic headers of the chunk, None or empty allowed
    :return: the first header value, empty for an image chunk
    """
    if not semantic_headers:
        return ''
    try:
        parsed = json.loads(semantic_headers)
        if isinstance(parsed, dict):
            if 'image_label' in parsed:
                return ''
            return str(next(iter(parsed.values()), '')) if parsed else ''
    except (json.JSONDecodeError, TypeError):
        pass
    return str(semantic_headers)


def _chunk_order(chunk_id: str) -> Tuple[int, int]:
    """Document order: text chunks by index, image chunks after them.

    :param chunk_id: chunk id, ending with its index
    :return: ``(group, index)`` sort key: text chunks first, then image chunks, then the unknown ones
    """
    parts = (chunk_id or '').split('-')
    try:
        return (1, int(parts[-1])) if 'IMG' in parts else (0, int(parts[-1]))
    except ValueError:
        return (2, 0)


def _parse_json_array(text: str) -> List[Dict[str, Any]] | None:
    """Robust JSON array parser — mirrors the compare exports' parse_json_response fallback chain.

    :param text: model reply that holds a JSON array
    :return: the list, or None when no array can be read
    """
    stripped = text.strip()
    for candidate in (
        stripped,
        (m.group(1).strip() if (m := re.search(r'```(?:json)?\s*([\s\S]*?)```', stripped)) else None),
        (m.group(1) if (m := re.search(r'(\[[\s\S]*\])', stripped)) else None),
    ):
        if not candidate:
            continue
        try:
            data = json.loads(candidate)
            if isinstance(data, list):
                return data
        except json.JSONDecodeError:
            continue
    return None


def _parse_json_object(text: str) -> Dict[str, Any] | None:
    """Robust JSON object parser: the text itself, a fenced block, or its first brace group.

    :param text: model reply that holds a JSON object, None allowed
    :return: the dict, or None when no object can be read
    """
    stripped = (text or '').strip()
    for candidate in (
        stripped,
        (m.group(1).strip() if (m := re.search(r'```(?:json)?\s*([\s\S]*?)```', stripped)) else None),
        (m.group(1) if (m := re.search(r'(\{[\s\S]*\})', stripped)) else None),
    ):
        if not candidate:
            continue
        try:
            data = json.loads(candidate)
            if isinstance(data, dict):
                return data
        except json.JSONDecodeError:
            continue
    return None


def _find_quote(text: str, quote: str) -> List[int] | None:
    """[start, end] of the quote in text, tolerant to whitespace/case drift; None if absent.

    :param text: passage text to search
    :param quote: quote copied from the passage by the judge
    :return: ``[start, end]`` of the quote in the text, or None when it is absent
    """
    words = (quote or '').strip().strip('"«»“”').split()
    if not words:
        return None
    m = re.search(r'\s+'.join(re.escape(w) for w in words), text, re.IGNORECASE)
    return [m.start(), m.end()] if m else None


# --- Retrieval ---

async def _fetch_chunks(host: str, token: str, index_name: str, query_text: str, num_results: int) -> List[Dict[str, Any]]:
    """One HYBRID query of an index for the impact search; returns its rows as dicts.

    :param host: Databricks workspace URL
    :param token: Databricks access token
    :param index_name: full name of the Vector Search index
    :param query_text: text of the change searched for
    :param num_results: number of rows asked
    :return: the rows, archive notices left out
    """
    payload = {'query_text': query_text, 'columns': _COLUMNS, 'num_results': num_results, 'query_type': 'HYBRID'}
    # Through the app-wide gate shared with the chat (vs_gate: bounded concurrency, 429 retries).
    resp = await vs_gate.post(host, token, index_name, payload, VECTOR_SEARCH_QUERY_TIMEOUT_S)
    resp.raise_for_status()
    data = resp.json()
    columns = [c['name'] for c in data.get('manifest', {}).get('columns', [])]
    chunks = [dict(zip(columns, row)) for row in data.get('result', {}).get('data_array', [])]
    return [c for c in chunks if _ARCHIVE_NOTICE_MARKER not in (c.get('chunk_text') or '')]


async def _fetch_chunks_multi(
    host: str, token: str, index_name: str, queries: List[Dict[str, Any]], num_results: int,
) -> Dict[str, Any]:
    """One Vector Search query per change, bounded concurrency + retry.

    Each chunk is tagged with '_change_ids' (the changes whose query hit it).
    Raises only if EVERY query fails; partial failures are counted.

    :param host: Databricks workspace URL
    :param token: Databricks access token
    :param index_name: full name of the Vector Search index
    :param queries: one dict per change, with its ``text`` and its ``change_ids``
    :param num_results: rows asked per query
    :return: ``{'chunks': [...], 'queries_failed': n}``
    """
    sem = asyncio.Semaphore(IMPACT_QUERY_CONCURRENCY)
    failures: List[str] = []

    async def _one(q: Dict[str, Any]) -> List[Dict[str, Any]] | None:
        """Runs the query of one change, retried, and tags its rows with the change id.

        :param q: query dict: ``text`` and ``change_ids``
        :return: the rows, None when every try failed
        """
        async with sem:
            last_err: Exception | None = None
            for attempt in range(IMPACT_QUERY_RETRIES + 1):
                try:
                    chunks = await _fetch_chunks(host, token, index_name, q['text'], num_results)
                    for c in chunks:
                        c['_change_ids'] = list(q['change_ids'])
                    return chunks
                except Exception as e:  # noqa: BLE001 — retried, then surfaced via failures
                    last_err = e
                    if attempt < IMPACT_QUERY_RETRIES:
                        await asyncio.sleep(1.0 * (attempt + 1))
            failures.append(str(last_err))
            logger.warning(f'Vector Search query for {q["change_ids"]} failed after retries: {last_err}')
            return None

    results = await asyncio.gather(*(_one(q) for q in queries))
    ok = [r for r in results if r is not None]
    if not ok:
        raise RuntimeError(f'All {len(queries)} Vector Search queries failed — first error: {failures[0] if failures else "unknown"}')
    return {'chunks': [c for r in ok for c in r], 'queries_failed': len(failures)}


def _aggregate_docs(chunks: List[Dict[str, Any]], excluded_canon: frozenset = frozenset()) -> List[Dict[str, Any]]:
    """Group chunk hits per document, ranked by (changes matched, best score).

    The same chunk often comes back under several changes' queries: it is kept
    once, with the union of change ids that retrieved it. Language variants of
    one document (REF differing only by a site/language suffix, see
    doc_catalog.canon_ref) are merged: the best-evidence variant is judged and
    the others are attached as 'variants'. Documents whose canonical REF is in
    excluded_canon (the document being compared) are dropped.

    :param chunks: rows of all the queries
    :param excluded_canon: canonical REFs to leave out, the document being compared
    :return: the documents, best ranked first, each with its passages and its language variants
    """
    docs: Dict[str, Dict[str, Any]] = {}
    for rec in sorted(chunks, key=lambda c: c.get('score', 0.0) or 0.0, reverse=True):
        ref = rec.get('REF', '') or ''
        if canon_ref(ref) in excluded_canon:
            continue
        key = f"{rec.get('IDDOC', '')}|{ref}"
        score = rec.get('score', 0.0) or 0.0
        doc = docs.get(key)
        if doc is None:
            doc = docs[key] = {
                'iddoc': rec.get('IDDOC', ''), 'ref': ref, 'division': rec.get('division', ''),
                'url': rec.get('url', ''), 'max_score': score, 'change_ids': set(), 'passages': {},
                'title': '', 'doc_date': '',
            }
        doc['change_ids'].update(rec.get('_change_ids', []))
        chunk_id = rec.get('chunk_id', '')
        passage = doc['passages'].get(chunk_id)
        if passage is None:
            body, meta = _split_prefix(rec.get('chunk_text', '') or '')
            doc['title'] = doc['title'] or meta.get('title', '')
            doc['doc_date'] = doc['doc_date'] or meta.get('doc_date', '')
            doc['passages'][chunk_id] = {
                'chunk_id': chunk_id, 'score': score, 'header': _extract_header(rec.get('semantic_headers')),
                'text': body[:IMPACT_PASSAGE_CHARS], 'page': meta.get('page', ''), 'change_ids': set(rec.get('_change_ids', [])),
            }
        else:
            passage['change_ids'].update(rec.get('_change_ids', []))

    clusters: Dict[str, List[Dict[str, Any]]] = {}
    for doc in docs.values():
        clusters.setdefault(canon_ref(doc['ref']) or doc['ref'], []).append(doc)

    rank = lambda d: (len(d['change_ids']), d['max_score'])  # noqa: E731
    merged = []
    for group in clusters.values():
        group.sort(key=rank, reverse=True)
        primary, *others = group
        primary['variants'] = [{'ref': o['ref'], 'division': o['division'], 'url': o['url']} for o in others]
        for o in others:
            primary['change_ids'] |= o['change_ids']
        merged.append(primary)

    for doc in merged:
        top = sorted(doc['passages'].values(), key=lambda p: p['score'], reverse=True)[:IMPACT_PASSAGES_PER_CANDIDATE]
        doc['passages'] = sorted(top, key=lambda p: _chunk_order(p['chunk_id']))
        for p in doc['passages']:
            p['change_ids'] = sorted(p['change_ids'], key=_change_num)
        doc['change_ids'] = sorted(doc['change_ids'], key=_change_num)
    return sorted(merged, key=rank, reverse=True)


def _change_num(change_id: str) -> int:
    """Numeric part of a change id: ``C12`` gives 12.

    :param change_id: change id, such as ``C12``
    :return: the number, 0 when there is none
    """
    try:
        return int(change_id.lstrip('C'))
    except ValueError:
        return 0


# --- Judgment ---

def _changes_block(changes: List[Dict[str, Any]], max_chars: int, priority_ids: Tuple[str, ...] = ()) -> str:
    """The change list shown to the judge, in table order, within max_chars.

    Over budget, whole changes are left out (never cut mid-sentence) and the
    ones in priority_ids — those whose query retrieved this candidate — go in
    first. A flat [:max_chars] dropped the END of the table for every
    candidate, including the very changes that had retrieved it.

    :param changes: change records, in table order
    :param max_chars: length budget of the block
    :param priority_ids: ids of the changes that retrieved the candidate, kept first when the budget is short
    :return: the lines of the changes shown to the judge
    """
    lines: Dict[str, str] = {}
    for c in changes:
        if not c.get('searched', True):
            continue
        crit = f" [{c['criticality']}]" if c.get('criticality') else ''
        lines[c['id']] = f"{c['id']}{crit}: {c['text']}"
    if sum(len(line) + 1 for line in lines.values()) <= max_chars:
        return '\n'.join(lines.values())

    kept: set = set()
    used = 0
    for cid in [i for i in priority_ids if i in lines] + [i for i in lines if i not in priority_ids]:
        if used + len(lines[cid]) + 1 > max_chars:
            continue
        kept.add(cid)
        used += len(lines[cid]) + 1
    if not kept:  # one change alone exceeds the budget
        first = next((i for i in priority_ids if i in lines), next(iter(lines)))
        return lines[first][:max_chars]
    out = [line for cid, line in lines.items() if cid in kept]
    out.append(f'({len(lines) - len(kept)} other change(s) omitted for length — judge only against the ones listed.)')
    return '\n'.join(out)


def _candidate_block(cand: Dict[str, Any]) -> str:
    """The candidate document as shown to the judge: its header and its numbered passages.

    :param cand: candidate document of ``_aggregate_docs``
    :return: the text of the block
    """
    head = f"ref: {cand['ref']}\ntitle: {cand['title'] or title_for_ref(cand['ref'])}\ndivision: {cand['division']}"
    if cand.get('doc_date'):
        head += f"\npublished: {cand['doc_date']}"
    passages = []
    for i, p in enumerate(cand['passages'], start=1):
        hint = f'[heading: "{p["header"]}"]' if p['header'] else '[no heading]'
        passages.append(f'PASSAGE {i} {hint} (retrieved by {", ".join(p["change_ids"]) or "?"}):\n{p["text"]}')
    return head + '\n\nPASSAGES:\n\n' + '\n\n'.join(passages)


class JudgeTruncated(ValueError):
    """The judge hit max_tokens before writing a complete JSON answer."""


async def _judge(host: str, token: str, endpoint: str, changes_text: str, cand: Dict[str, Any], max_tokens: int) -> Dict[str, Any]:
    """One judgment, through the endpoint chain of ``endpoint``. Raises ``JudgeTruncated`` (a bigger budget may
    help) or ``CompareLlmUnavailable`` (every endpoint failed).

    :param host: Databricks workspace URL
    :param token: Databricks access token
    :param endpoint: primary endpoint of the judge chain
    :param changes_text: change list, from ``_changes_block``
    :param cand: candidate document
    :param max_tokens: output budget of the call
    :return: the judgment, the token counts, the endpoint that answered and the cost
    """
    messages = [
        {'role': 'system', 'content': _JUDGE_SYSTEM_PROMPT},
        {'role': 'user', 'content': f'CHANGES:\n{changes_text}\n\nCANDIDATE DOCUMENT:\n{_candidate_block(cand)}'},
    ]

    def accept(completion: Dict[str, Any]) -> Dict[str, Any]:
        """Turns a judge completion into its verdict, or raises when the completion has none.

        :param completion: completion returned by an endpoint
        :return: the verdict with the token counts
        """
        choice = (completion.get('choices') or [{}])[0]
        content = _message_text(choice.get('message') or {})
        usage = completion.get('usage') or {}
        judgment = _parse_json_object(content)
        if judgment is None:
            if choice.get('finish_reason') == 'length':
                reasoning = (usage.get('completion_tokens_details') or {}).get('reasoning_tokens')
                raise JudgeTruncated(f'judge ran out of tokens (max_tokens={max_tokens}, reasoning_tokens={reasoning}) '
                                     f'before a complete answer: {content[:200]!r}')
            raise LlmFailure('empty', f'judge returned no JSON object (finish_reason={choice.get("finish_reason")!r}): '
                                      f'{content[:200]!r}')
        return {
            'judgment': judgment,
            'input_tokens': usage.get('prompt_tokens', 0) or 0,
            'output_tokens': usage.get('completion_tokens', 0) or 0,
        }

    res, used = await complete_chain(host, token, compare_chain(endpoint), messages, max_tokens, IMPACT_JUDGE_TIMEOUT_S, accept=accept)
    return {**res, 'endpoint': used, 'cost_eur': _cost_eur(used, res['input_tokens'], res['output_tokens'])}


def _status(impacted: bool, confidence: str, has_passages: bool = True) -> str:
    # "Impacted" with no passage the UI can show is a claim nobody can verify.
    """Status of a judged document: ``impacted``, ``check`` or ``not_impacted``.

    :param impacted: the verdict of the judge
    :param confidence: ``high``, ``medium`` or ``low``
    :param has_passages: whether the judge gave a passage that can be shown
    :return: the status
    """
    if confidence == 'low' or (impacted and not has_passages):
        return 'check'
    return 'impacted' if impacted else 'not_impacted'


def _other_languages(cand: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Retrieved variants plus catalog-known siblings not retrieved by this search.

    :param cand: candidate document
    :return: the other language variants, retrieved ones first, then those known from the catalog
    """
    seen = {cand['ref'].strip().upper()}
    out = []
    for v in cand.get('variants', []):
        key = v['ref'].strip().upper()
        if key not in seen:
            seen.add(key)
            out.append({'ref': v['ref'], 'url': v['url'], 'flag': site_flag(v['ref']), 'source': 'retrieved'})
    for sib in other_language_refs(cand['ref']):
        key = (sib.get('ref') or '').strip().upper()
        if key and key not in seen:
            seen.add(key)
            out.append({'ref': sib['ref'], 'url': sib.get('url') or '', 'flag': site_flag(sib['ref']), 'source': 'catalog'})
    return out


def _doc_base(cand: Dict[str, Any], archive_before: str) -> Dict[str, Any]:
    """The fields every document of the result has, judged or not.

    :param cand: candidate document
    :param archive_before: ISO date before which a document is archived; empty for none
    :return: the document fields
    """
    doc_date = cand.get('doc_date', '')
    return {
        'iddoc': cand['iddoc'],
        'ref': cand['ref'],
        'title': cand['title'] or title_for_ref(cand['ref']),
        'division': cand['division'],
        'url': cand['url'],
        'doc_date': doc_date,
        'archive': bool(doc_date and archive_before and doc_date < archive_before),
        'flag': site_flag(cand['ref']),
        'site_code': site_code(cand['ref']),
        'change_ids': cand['change_ids'],
        'max_score': cand['max_score'],
        'other_languages': _other_languages(cand),
    }


def _judged_doc(cand: Dict[str, Any], judgment: Dict[str, Any], archive_before: str) -> Dict[str, Any]:
    """The result document of a judged candidate, with the passages the judge found.

    :param cand: candidate document
    :param judgment: verdict of the judge
    :param archive_before: ISO date before which a document is archived; empty for none
    :return: the result document
    """
    impacted = bool(judgment.get('impacted', False))
    confidence = str(judgment.get('confidence', '') or '').lower()
    passages = []
    if impacted:
        for item in judgment.get('passages') or []:
            if not isinstance(item, dict):
                continue
            try:
                num = int(float(str(item.get('passage'))))  # "2", 2, 2.0
            except (ValueError, OverflowError):
                continue
            if not 1 <= num <= len(cand['passages']):
                continue
            p = cand['passages'][num - 1]
            changes = item.get('changes') or []
            if isinstance(changes, str):
                changes = [changes]
            section = str(item.get('section') or '').strip()
            if section.lower().strip('.[] ') == 'no heading':
                section = ''
            quote = str(item.get('quote') or '').strip()
            passages.append({
                'chunk_id': p['chunk_id'],
                'section': section or p['header'],
                'page': p['page'],
                'text': p['text'],
                'quote': quote,
                'highlight': _find_quote(p['text'], quote),
                'changes': sorted({str(c).strip() for c in changes if str(c).strip()}, key=_change_num),
                'explanation': str(item.get('explanation') or '').strip(),
            })
    sections = list(dict.fromkeys(p['section'] for p in passages if p['section']))
    return {
        **_doc_base(cand, archive_before),
        'judged': True,
        'status': _status(impacted, confidence, bool(passages)),
        'impacted': impacted,
        'confidence': confidence,
        'reason': str(judgment.get('reason') or '').strip(),
        'sections': sections,
        'passages': passages,
    }


def _exclusion_keys(names: List[str], known_refs: List[str]) -> frozenset:
    """Canonical REFs (of retrieved documents) that appear in the compared files' names.

    :param names: file names of the compared documents
    :param known_refs: REFs of the retrieved documents
    :return: the canonical REFs to leave out
    """
    canon_names = [canon_ref(n) for n in names if n]
    out = set()
    for ref in known_refs:
        key = canon_ref(ref)
        # Not followed by a digit: "AB12" inside "AB123" is another document.
        if len(key) >= 5 and any(re.search(re.escape(key) + r'(?!\d)', n) for n in canon_names):
            out.add(key)
    return frozenset(out)


async def run_impact_search(
    *,
    host: str,
    token: str,
    index_name: str,
    llm_endpoint: str,
    extracted: Dict[str, Any],
    num_results: int,
    max_candidates: int,
    max_changes_chars: int,
    max_tokens: int,
    archive_before: str,
    exclude_names: List[str],
    info: Dict[str, Any] | None = None,
) -> AsyncIterator[Dict[str, Any]]:
    """Yield 'plan' → one 'document' per judged candidate (as each finishes) → 'done'.

    Raises before 'plan' on retrieval failure (caller reports it); a failed
    judge call yields that document with status 'error' instead of aborting.
    ``info['endpoints']``, when given, lists the endpoints that answered a judge call, in order of first answer.

    :param host: Databricks workspace URL
    :param token: Databricks access token
    :param index_name: full name of the Vector Search index
    :param llm_endpoint: primary endpoint of the judge
    :param extracted: output of ``changes_to_queries``: ``changes``, ``source`` and ``queries``
    :param num_results: rows asked per query
    :param max_candidates: number of documents judged, best ranked first
    :param max_changes_chars: length budget of the change list given to the judge
    :param max_tokens: output budget of a judge call
    :param archive_before: ISO date before which a document is archived; empty for none
    :param exclude_names: file names of the compared documents, whose own REF is left out
    :param info: dict that receives the endpoints that answered a judge call, None to skip
    :return: the events of the stream: ``plan``, one ``document`` per judged candidate, then ``done``
    """
    queries = extracted['queries']
    fetched = await _fetch_chunks_multi(host, token, index_name, queries, num_results)
    known_refs = list({c.get('REF', '') for c in fetched['chunks']})
    excluded = _exclusion_keys(exclude_names, known_refs)
    candidates = _aggregate_docs(fetched['chunks'], excluded)
    to_judge, rest = candidates[:max_candidates], candidates[max_candidates:]

    yield {
        'type': 'plan',
        'changes': extracted['changes'],
        'source': extracted['source'],
        'queries_used': len(queries),
        'queries_failed': fetched['queries_failed'],
        'chunks_returned': len(fetched['chunks']),
        'candidates': len(to_judge),
        'excluded_refs': sorted({r for r in known_refs if canon_ref(r) in excluded}),
        'not_judged': [{**_doc_base(c, archive_before), 'judged': False} for c in rest],
    }

    sem = asyncio.Semaphore(IMPACT_JUDGE_CONCURRENCY)
    usage = {'input_tokens': 0, 'output_tokens': 0, 'cost_eur': 0.0}

    async def _one(cand: Dict[str, Any]) -> Dict[str, Any]:
        """Judges one candidate document, retried, and returns its result document.

        :param cand: candidate document
        :return: the result document; its status is ``error`` when the judge failed
        """
        async with sem:
            # Judge trace (saved in impact_document_results): time from the first call to the
            # verdict, retries included, number of calls, tokens of the successful call.
            started = time.monotonic()
            last_err: Exception | None = None
            budget = max_tokens
            attempts = 0
            for attempt in range(IMPACT_JUDGE_RETRIES + 1):
                attempts = attempt + 1
                try:
                    changes_text = _changes_block(extracted['changes'], max_changes_chars, tuple(cand['change_ids']))
                    res = await _judge(host, token, llm_endpoint, changes_text, cand, budget)
                    if info is not None and res.get('endpoint'):
                        seen = info.setdefault('endpoints', [])
                        if res['endpoint'] not in seen:
                            seen.append(res['endpoint'])
                    usage['input_tokens'] += res['input_tokens']
                    usage['output_tokens'] += res['output_tokens']
                    usage['cost_eur'] = round(usage['cost_eur'] + (res.get('cost_eur') or 0.0), 6)
                    return {**_judged_doc(cand, res['judgment'], archive_before),
                            'judge_ms': int((time.monotonic() - started) * 1000), 'judge_attempts': attempts,
                            'input_tokens': res['input_tokens'], 'output_tokens': res['output_tokens']}
                except CompareLlmUnavailable as e:
                    # The chain already went through its rounds: no retry on top of that.
                    last_err = e
                    break
                except Exception as e:  # noqa: BLE001 — one bad candidate must not sink the search
                    last_err = e
                    if isinstance(e, JudgeTruncated):
                        budget = min(budget * IMPACT_JUDGE_TRUNCATION_BUDGET_FACTOR, max(IMPACT_JUDGE_MAX_TOKENS_CAP, max_tokens))
                    if attempt < IMPACT_JUDGE_RETRIES:
                        await asyncio.sleep(1.0)
            logger.warning(f'impact judge failed for {cand["ref"]}: {last_err}')
            # error_detail is the cause for the error log only; the router drops it before the browser sees the document.
            return {**_doc_base(cand, archive_before), 'judged': True, 'status': 'error', 'impacted': False,
                    'confidence': '', 'reason': TIRED_MESSAGE, 'error_detail': f'{type(last_err).__name__}: {last_err}',
                    'sections': [], 'passages': [],
                    'error_type': type(last_err).__name__,
                    'judge_ms': int((time.monotonic() - started) * 1000), 'judge_attempts': attempts}

    # Explicit tasks, cancelled when the consumer goes away: bare coroutines would keep running and billing after a closed tab.
    tasks = [asyncio.create_task(_one(c)) for c in to_judge]
    try:
        for next_done in asyncio.as_completed(tasks):
            yield {'type': 'document', 'document': await next_done}
    finally:
        for t in tasks:
            if not t.done():
                t.cancel()

    total = usage['input_tokens'] + usage['output_tokens']
    yield {'type': 'done', 'usage': {**usage, 'total_tokens': total}}


def sort_documents(documents: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Display order: impacted, to check, not impacted, failed; then retrieval evidence.

    :param documents: result documents
    :return: the documents in display order
    """
    return sorted(documents, key=lambda d: (
        _STATUS_ORDER.get(d.get('status', ''), 9), -len(d.get('change_ids', [])), -(d.get('max_score') or 0),
    ))
