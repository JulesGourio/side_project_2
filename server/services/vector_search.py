"""Direct Databricks Vector Search queries — used by the /compare/impact route.

Hits the Vector Search REST API directly (index retrieval), then a single
plain LLM call (synthesize_impact_with_llm) judges which of the retrieved
candidates are genuinely impacted — a lighter-weight alternative to routing
through a full RAG agent.
"""

import asyncio
import json
import logging
import math
import re
from typing import Any, Dict, List

import httpx

from .doc_catalog import canon_ref, other_language_refs, site_code, site_flag, title_for_ref
from .streaming import _cost_eur, supports_temperature

logger = logging.getLogger(__name__)

_QUERY_TIMEOUT_S = 30.0
_LLM_TIMEOUT_S = 60.0
_COLUMNS = ['chunk_id', 'IDDOC', 'REF', 'division', 'url', 'semantic_headers', 'chunk_text']
_CHUNK_TEXT_EXCERPT_CHARS = 800
# Raised from 3 (2026-07-22): the judge otherwise only sees a candidate's top-3
# scoring chunks, so a lower-ranked chunk holding the actual conflicting detail
# could be excluded from what the LLM sees. Bounded, not unlimited — cost scales
# with max_candidates * this value * _CHUNK_TEXT_EXCERPT_CHARS.
_EXCERPTS_PER_CANDIDATE = 5
# The embedding endpoint behind query_text rejects concurrent requests with
# "Request id already running" under load — keep the fan-out narrow and retry.
_QUERY_CONCURRENCY = 3
_QUERY_RETRIES = 2

_SYNTHESIS_SYSTEM_PROMPT = """\
You are Qualibot, a technical documentation controller. You are given a summary of changes made to a \
reference document, and a list of CANDIDATE documents retrieved from the knowledge base by a \
similarity search (they may or may not be genuinely impacted — the search is approximate).

Each candidate comes with several retrieved passages, because the conflicting content can sit \
anywhere in the document, not just in the single highest-scoring hit. Judge each candidate from ALL \
of its passages together, not just the first one — a lower passage in the list can hold the actual \
conflicting detail even when an earlier one looks more topical.

A passage may come with a heading hint in brackets, e.g. [heading: "6. DESCRIPTION DETAILLEE"] — \
treat it only as a hint: it can be missing, approximate, or point to a higher-level section than the \
specific part that actually conflicts. Some passages have no hint at all ([no heading]) — that is \
expected for a real fraction of the knowledge base and does not mean anything is wrong with the \
passage. The passage TEXT itself is the more reliable source: parsed documents embed structural \
markers directly in the text, like \
"[6. DESCRIPTION DETAILLEE > 6.1 GENERER DES IDEES A VALEUR AJOUTEE]" — these mark section/subsection \
boundaries inline and usually pinpoint exactly where in the passage each piece of content sits.

For each candidate, decide whether it is genuinely impacted by the reported changes and must be \
updated to remain consistent. "Genuinely impacted" means at least one passage states, prescribes, or \
relies on something — a value, step, requirement, tool, term, role, or reference — that the reported \
change directly contradicts, replaces, or makes outdated. It is NOT enough for a passage to share \
vocabulary or be on the same general topic as the change (e.g. mentioning "autoclave" or "torque" \
does not make a document impacted by a change to a specific torque value, unless a passage actually \
states that value or a step depending on it). Judge only from the passages shown; do not assume \
content that isn't there. "matched by N of the reported changes" tells you how many independent \
changes retrieved the candidate — treat it as a retrieval signal, not as proof of impact.

Set "confidence" using these criteria, for both impacted and non-impacted verdicts:
- "high": a passage explicitly contains the specific fact/value/step/requirement that the change \
modifies or contradicts (impacted=true), OR every passage is clearly about an unrelated process/topic \
despite the retrieval match, with nothing plausibly touched by the change (impacted=false).
- "medium": the passages are clearly on the same topic/process as the change and plausibly affected, \
but none of them spells out the exact conflicting detail verbatim — the link requires a reasonable \
inference rather than a direct textual match.
- "low": the evidence is thin either way — only generic/peripheral overlap, or the passages are too \
short/ambiguous to tell; use this to flag "worth a human double-check" rather than committing to a \
confident call.

The "reason" must open with a general, one-sentence verdict on what makes this document impacted (or \
not) — the overall nature of the conflict or lack of one, not a description of a single passage in \
isolation. Then, in 1 to 3 more sentences: contrast what the document currently says/prescribes with \
what the reported change actually modifies (old vs new when the CHANGES text makes that concrete — a \
value changing from X to Y, a step added/removed/reordered, a requirement tightened or dropped), and \
why that matters for this document. If the conflicting content recurs in more than one passage or \
location, say so briefly ("this also recurs in ...") instead of writing as if only one spot in the \
document was affected. When impacted is false, skip the conflict description and instead say \
concretely what the document is actually about and why that's unrelated to the reported changes (e.g. \
a different process, a different site's version of the same step, only vocabulary overlap). Avoid \
vague filler like "seems related", "may be affected", or "could potentially conflict" — every claim \
in "reason" must trace back to something actually stated in a passage or the CHANGES text.

When impacted is true, list in "sections" every distinct section (number and/or title, e.g. \
"6.2.3 Sélectionner le(s) sujet(s)") where the conflicting content actually appears — read it from \
the inline markers in the passage text where possible; fall back to the heading hint only where the \
text has no inline marker at that point, and prefer the text over the hint whenever they disagree. \
List more than one entry when the conflict genuinely spans several sections — don't collapse them \
into one just because one passage happened to score highest. Leave "sections" empty only when the \
candidate is not impacted, or truly no section information (neither inline marker nor heading hint) \
exists anywhere relevant.

Respond with ONLY a JSON array (no markdown fences, no prose), one object per candidate, in this \
exact shape:
[{"ref": "<candidate ref>", "impacted": true|false, "sections": ["<section number/title>", ...] (empty \
array if none found or not impacted), "confidence": "high"|"medium"|"low", "reason": "<general verdict \
first, then the conflict detail and where else it recurs — or, if not impacted, what the document is \
actually about instead>"}]

Include every candidate exactly once, even if impacted is false. Be conservative: if the passages \
don't clearly show a conflict with the reported changes, mark impacted false.

LANGUAGE: write "reason" in the SAME language as the CHANGES text and the candidate passages. Do \
not switch to English when they are not in English.\
"""


def _extract_header(semantic_headers: Any) -> str:
    """Best-effort: pull the first header value out of the semantic_headers JSON blob."""
    if not semantic_headers:
        return ''
    try:
        parsed = json.loads(semantic_headers)
        if isinstance(parsed, dict):
            return next(iter(parsed.values()), '') if parsed else ''
    except (json.JSONDecodeError, TypeError):
        pass
    return str(semantic_headers)


async def _fetch_chunks(
    host: str,
    token: str,
    index_name: str,
    query_text: str,
    num_results: int,
    max_query_chars: int,
) -> Dict[str, Any]:
    """Query a Vector Search index; returns raw chunk rows plus retrieval metadata."""
    truncated = len(query_text) > max_query_chars
    query_text = query_text[:max_query_chars]

    url = f'{host}/api/2.0/vector-search/indexes/{index_name}/query'
    payload = {
        'query_text': query_text,
        'columns': _COLUMNS,
        'num_results': num_results,
        'query_type': 'HYBRID',
    }
    headers = {'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'}

    async with httpx.AsyncClient(timeout=_QUERY_TIMEOUT_S) as client:
        resp = await client.post(url, json=payload, headers=headers)
        resp.raise_for_status()
        data = resp.json()

    columns = [c['name'] for c in data.get('manifest', {}).get('columns', [])]
    rows = data.get('result', {}).get('data_array', [])
    chunks = [dict(zip(columns, row)) for row in rows]
    return {'chunks': chunks, 'truncated': truncated, 'chunks_returned': len(chunks)}


async def _fetch_chunks_multi(
    host: str,
    token: str,
    index_name: str,
    queries: List[str],
    num_results: int,
    max_query_chars: int,
) -> Dict[str, Any]:
    """Run one Vector Search query per derived change, bounded concurrency + retry.

    Each returned chunk is tagged with '_qidx' (index of the query that hit it)
    so aggregation can report per-document change coverage. Raises only if
    EVERY query fails; partial failures are counted in 'queries_failed'.
    """
    sem = asyncio.Semaphore(_QUERY_CONCURRENCY)
    failures: List[str] = []

    async def _one(qidx: int, q: str) -> Dict[str, Any] | None:
        async with sem:
            last_err: Exception | None = None
            for attempt in range(_QUERY_RETRIES + 1):
                try:
                    fetched = await _fetch_chunks(host, token, index_name, q, num_results, max_query_chars)
                    for c in fetched['chunks']:
                        c['_qidx'] = qidx
                    return fetched
                except Exception as e:  # noqa: BLE001 — retried, then surfaced via failures
                    last_err = e
                    if attempt < _QUERY_RETRIES:
                        await asyncio.sleep(1.0 * (attempt + 1))
            failures.append(f'query {qidx}: {last_err}')
            logger.warning(f'Vector Search query {qidx} failed after retries: {last_err}')
            return None

    results = await asyncio.gather(*(_one(i, q) for i, q in enumerate(queries)))
    ok = [r for r in results if r is not None]
    if not ok:
        raise RuntimeError(f'All {len(queries)} Vector Search queries failed — first error: {failures[0] if failures else "unknown"}')

    chunks = [c for r in ok for c in r['chunks']]
    return {
        'chunks': chunks,
        'truncated': any(r['truncated'] for r in ok),
        'chunks_returned': len(chunks),
        'queries_used': len(queries),
        'queries_failed': len(failures),
    }


def _aggregate_docs(chunks: List[Dict[str, Any]], max_docs: int) -> List[Dict[str, Any]]:
    """Dedupe chunk hits into a doc-level list, sorted by best score, capped to max_docs.

    Multi-query aware: chunks tagged with '_qidx' contribute to the document's
    'query_hits' count (how many distinct derived changes matched it) and
    documents are ranked by (query_hits, max_score) — a document matched by
    several independent changes outranks a single lucky high-score hit.

    Multi-query retrieval runs one Vector Search query per derived change, so
    the SAME chunk_id routinely comes back as a hit under several different
    queries — 'chunk_count' dedupes on chunk_id so it reports genuinely
    distinct chunks (it can never exceed the document's real chunk count),
    not raw hit-count across queries.

    Language variants of the SAME document (REF differing only by a trailing
    site/language suffix, e.g. "GO-1316_FR" / "GO-1316_GB" — see
    doc_catalog.canon_ref) are then merged into a single candidate: the
    best-evidence variant (highest query_hits, then max_score) is the one
    whose excerpts get judged by the LLM, and the other retrieved variants are
    kept as 'variants' on it instead of being sent to the LLM as separate,
    near-duplicate candidates.
    """
    docs: Dict[str, Dict[str, Any]] = {}
    # Chunks from parallel queries are only sorted within each query — order
    # globally so 'top_chunks' and the best excerpt are the true best hits.
    chunks = sorted(chunks, key=lambda c: c.get('score', 0.0) or 0.0, reverse=True)
    for rec in chunks:
        key = f"{rec.get('IDDOC', '')}|{rec.get('REF', '')}"
        score = rec.get('score', 0.0) or 0.0
        header = _extract_header(rec.get('semantic_headers'))
        chunk_id = rec.get('chunk_id', '')

        doc = docs.get(key)
        if doc is None:
            doc = {
                'iddoc': rec.get('IDDOC', ''),
                'ref': rec.get('REF', ''),
                'division': rec.get('division', ''),
                'url': rec.get('url', ''),
                'max_score': score,
                'chunk_count': 0,
                'query_hits': 0,
                'top_chunks': [],
                '_matched_queries': set(),
                '_seen_chunk_ids': set(),
                '_top_excerpts': [],
            }
            docs[key] = doc

        if chunk_id not in doc['_seen_chunk_ids']:
            doc['_seen_chunk_ids'].add(chunk_id)
            doc['chunk_count'] += 1
            if len(doc['top_chunks']) < 3:
                doc['top_chunks'].append({'chunk_id': chunk_id, 'score': score, 'header': header})
        if score > doc['max_score']:
            doc['max_score'] = score
        if '_qidx' in rec:
            doc['_matched_queries'].add(rec['_qidx'])
        text = rec.get('chunk_text', '') or ''
        seen_texts = {t for _, t in doc['_top_excerpts']}
        if text and len(doc['_top_excerpts']) < _EXCERPTS_PER_CANDIDATE and text not in seen_texts:
            doc['_top_excerpts'].append((header, text))

    for doc in docs.values():
        doc['query_hits'] = len(doc.pop('_matched_queries')) or 1
        doc.pop('_seen_chunk_ids', None)

    # Merge same-document language variants (see docstring) before ranking/capping,
    # so max_docs candidate slots — and LLM judgment calls — aren't spent on what is
    # really the same document retrieved once per translated variant.
    clusters: Dict[str, List[Dict[str, Any]]] = {}
    for doc in docs.values():
        clusters.setdefault(canon_ref(doc['ref']), []).append(doc)

    merged: List[Dict[str, Any]] = []
    for group in clusters.values():
        group.sort(key=lambda d: (d['query_hits'], d['max_score']), reverse=True)
        primary, *others = group
        primary['variants'] = [
            {
                'iddoc': o['iddoc'], 'ref': o['ref'], 'division': o['division'], 'url': o['url'],
                'max_score': o['max_score'], 'chunk_count': o['chunk_count'], 'query_hits': o['query_hits'],
            }
            for o in others
        ]
        merged.append(primary)

    return sorted(merged, key=lambda d: (d['query_hits'], d['max_score']), reverse=True)[:max_docs]


def _evidence_rank(chunk_count: int, max_score: float) -> float:
    """Combine chunk_count and max_score into one display-order ranking value.

    Log-dampened so a document with many matching chunks can't outrank a
    single strong hit purely on volume, but breadth of evidence still counts.
    """
    return max_score * (1.0 + math.log1p(chunk_count))


def _parse_json_array(text: str) -> List[Dict[str, Any]] | None:
    """Robust JSON array parser — mirrors compare.py's _parse_json_response fallback chain."""
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


async def synthesize_impact_with_llm(
    host: str,
    token: str,
    index_name: str,
    llm_endpoint: str,
    queries: List[str],
    changes_summary: str,
    num_results: int,
    max_candidates: int,
    max_query_chars: int,
    max_tokens: int,
) -> Dict[str, Any]:
    """Index retrieval + a single LLM call to judge which candidates are genuinely impacted.

    'queries' drive retrieval (one focused vector search per derived change);
    'changes_summary' is the human-readable changes text shown to the judge.
    Raises on network/HTTP failure (vector search or LLM endpoint) — the caller
    is expected to catch and report it as an error response.
    """
    fetched = await _fetch_chunks_multi(host, token, index_name, queries, num_results, max_query_chars)
    candidates = _aggregate_docs(fetched['chunks'], max_candidates)

    if not candidates:
        return {
            'documents': [], 'truncated': fetched['truncated'], 'chunks_returned': fetched['chunks_returned'],
            'candidates_considered': 0,
            'queries_used': fetched['queries_used'], 'queries_failed': fetched['queries_failed'],
            'usage': {'input_tokens': 0, 'output_tokens': 0, 'total_tokens': 0, 'cost_eur': 0.0},
        }

    candidate_blocks = []
    for c in candidates:
        passages = '\n'.join(
            (f'  passage {i + 1} [heading: "{header}"]' if header else f'  passage {i + 1} [no heading]')
            + f': {text[:_CHUNK_TEXT_EXCERPT_CHARS]}'
            for i, (header, text) in enumerate(c['_top_excerpts'])
        ) or '  passage 1 [no heading]:'
        candidate_blocks.append(
            f"- ref: {c['ref']}\n  division: {c['division']}\n"
            f"  matched by {c['query_hits']} of the reported changes\n{passages}"
        )
    user_content = (
        f"CHANGES:\n{changes_summary[:max_query_chars]}\n\n"
        f"CANDIDATES:\n" + "\n".join(candidate_blocks)
    )

    url = f'{host}/serving-endpoints/{llm_endpoint}/invocations'
    payload: Dict[str, Any] = {
        'messages': [
            {'role': 'system', 'content': _SYNTHESIS_SYSTEM_PROMPT},
            {'role': 'user', 'content': user_content},
        ],
        'max_tokens': max_tokens,
    }
    if supports_temperature(llm_endpoint):
        payload['temperature'] = 0.0
    headers = {'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'}

    async with httpx.AsyncClient(timeout=_LLM_TIMEOUT_S) as client:
        resp = await client.post(url, json=payload, headers=headers)
        resp.raise_for_status()
        completion = resp.json()

    llm_text = completion.get('choices', [{}])[0].get('message', {}).get('content', '')
    judgments = _parse_json_array(llm_text) or []
    judgments_by_ref = {j.get('ref'): j for j in judgments if isinstance(j, dict)}

    usage = completion.get('usage') or {}
    input_tokens = usage.get('prompt_tokens', 0) or 0
    output_tokens = usage.get('completion_tokens', 0) or 0
    total_tokens = usage.get('total_tokens', input_tokens + output_tokens) or (input_tokens + output_tokens)
    cost_eur = _cost_eur(llm_endpoint, input_tokens, output_tokens)

    documents = []
    for c in candidates:
        judgment = judgments_by_ref.get(c['ref'], {})
        sections_raw = judgment.get('sections', [])
        # Some non-Claude models (observed with gemini-3-1-flash-lite, gpt-oss-20b)
        # occasionally return a single string instead of the requested array —
        # coerce rather than crash the whole request over one malformed field.
        if isinstance(sections_raw, str):
            sections_raw = [sections_raw] if sections_raw.strip() else []
        sections = []
        for s in sections_raw:
            s = str(s).strip()
            # Defense in depth: even with an explicit instruction not to, the LLM
            # occasionally still echoes a prompt placeholder as if it were a real
            # section name — strip it back out.
            if s and s.lower().strip('.[] ') not in ('no heading', 'no section label captured'):
                sections.append(s)

        # Other-language REFs for this same document: variants already retrieved by
        # vector search (merged in _aggregate_docs, so not independently judged by
        # the LLM above) plus any further siblings known to the doc catalog but not
        # retrieved at all for this query — e.g. a lower-scoring translation. The
        # judgment above (impacted/section/confidence/reason) was made from the
        # primary REF's excerpts and applies to the whole document, not just that
        # one language variant.
        seen_refs = {c['ref'].strip().upper()}
        other_languages: List[Dict[str, Any]] = []
        for v in c.get('variants', []):
            ref_key = v['ref'].strip().upper()
            if ref_key in seen_refs:
                continue
            seen_refs.add(ref_key)
            other_languages.append({
                'ref': v['ref'], 'division': v['division'], 'url': v['url'],
                'site_code': site_code(v['ref']), 'flag': site_flag(v['ref']),
                'source': 'retrieved',
            })
        for sib in other_language_refs(c['ref']):
            ref_key = (sib.get('ref') or '').strip().upper()
            if not ref_key or ref_key in seen_refs:
                continue
            seen_refs.add(ref_key)
            other_languages.append({
                'ref': sib['ref'], 'division': '', 'url': sib.get('url') or '',
                'site_code': site_code(sib['ref']), 'flag': site_flag(sib['ref']),
                'source': 'catalog',
            })

        documents.append({
            'iddoc': c['iddoc'],
            'ref': c['ref'],
            'title': title_for_ref(c['ref']),
            'division': c['division'],
            'url': c['url'],
            'max_score': c['max_score'],
            'chunk_count': c['chunk_count'],
            'query_hits': c['query_hits'],
            'impacted': bool(judgment.get('impacted', False)),
            'sections': sections,
            'confidence': judgment.get('confidence', ''),
            'reason': judgment.get('reason', ''),
            'site_code': site_code(c['ref']),
            'flag': site_flag(c['ref']),
            'other_languages': other_languages,
        })
    documents.sort(key=lambda d: (not d['impacted'], -_evidence_rank(d['chunk_count'], d['max_score'])))

    return {
        'documents': documents,
        'truncated': fetched['truncated'],
        'chunks_returned': fetched['chunks_returned'],
        'candidates_considered': len(candidates),
        'queries_used': fetched['queries_used'],
        'queries_failed': fetched['queries_failed'],
        'usage': {
            'input_tokens': input_tokens,
            'output_tokens': output_tokens,
            'total_tokens': total_tokens,
            'cost_eur': cost_eur,
        },
    }
