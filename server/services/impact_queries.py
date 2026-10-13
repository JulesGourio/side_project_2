"""Turn a Compare analysis output into focused impact-search queries.

The frontend sends whichever analysis produced the changes summary:
- structured method → a raw JSON array (section/type/criticality/before/after/rationale)
- standard method   → a Markdown report (## sections, bullet per change)

Embedding that blob as ONE vector-search query dilutes the signal: JSON
syntax, criticality labels and up to 20k chars of unrelated changes all end
up in a single embedding, so retrieval matches nothing precisely. This module
derives short, focused natural-language queries — one per change (structured)
or per section (markdown) — plus a human-readable rendering of the structured
JSON for the agent-based V1 flow (the Knowledge Assistant otherwise receives
raw JSON as its user message).
"""

import re
from typing import Any, Dict, List

from .vector_search import _parse_json_array

_CRIT_ORDER = {'high': 0, 'medium': 1, 'low': 2}

# Phrase emitted by both analysis prompts when the documents are equivalent.
_NO_CHANGES_RE = re.compile(r'no significant changes detected', re.I)

_ABSENT = ('', '--', '—', 'n/a', 'null', 'none')


def _is_absent(value: Any) -> bool:
    return not value or str(value).strip().lower() in _ABSENT


def _row_to_line(row: Dict[str, Any]) -> str:
    """One structured-JSON row → one compact natural-language change statement."""
    section = str(row.get('section') or '').strip()
    before = str(row.get('before') or '').strip()
    after = str(row.get('after') or '').strip()
    rationale = str(row.get('rationale') or '').strip()

    if _is_absent(before) and _is_absent(after):
        core = ''
    elif _is_absent(before):
        core = f'added: {after}'
    elif _is_absent(after):
        core = f'removed: {before}'
    else:
        core = f'{before} → {after}'

    parts = [p for p in (section, core) if p]
    line = ' — '.join(parts)
    if rationale:
        line = f'{line} ({rationale})' if line else rationale
    return line


def _chunk_into(items: List[str], max_groups: int) -> List[str]:
    """Pack ordered items into at most max_groups queries, preserving order."""
    if len(items) <= max_groups:
        return items
    size = -(-len(items) // max_groups)  # ceil
    return ['\n'.join(items[i:i + size]) for i in range(0, len(items), size)]


_MD_MARKUP_RE = re.compile(r'(\*\*|~~|__|`)')
_MD_BULLET_RE = re.compile(r'^\s*[-*+]\s+', re.M)


def _strip_markdown(text: str) -> str:
    text = _MD_MARKUP_RE.sub('', text)
    text = _MD_BULLET_RE.sub('', text)
    return re.sub(r'[ \t]+', ' ', text).strip()


def changes_to_queries(
    changes_text: str,
    max_queries: int = 8,
    max_query_chars: int = 2000,
) -> Dict[str, Any]:
    """Derive focused vector-search queries from an analysis output.

    Returns {'queries': [str], 'source': 'structured'|'markdown'|'raw',
             'total_changes': int}. An empty queries list means the analysis
    reported no substantive change — callers can skip retrieval entirely.
    """
    text = (changes_text or '').strip()
    if not text:
        return {'queries': [], 'source': 'raw', 'total_changes': 0}

    # ── Structured JSON array ────────────────────────────────────────────────
    rows = _parse_json_array(text)
    if rows is not None:
        dict_rows = [r for r in rows if isinstance(r, dict)]
        # Editorial/structural rows (renumbering, TOC…) never impact other
        # documents — drop them from retrieval unless nothing else remains.
        substantive = [r for r in dict_rows if str(r.get('type', '')).strip().lower() != 'editorial/structural']
        picked = substantive or dict_rows
        picked = sorted(
            picked,
            key=lambda r: _CRIT_ORDER.get(str(r.get('criticality', '')).strip().lower(), 1),
        )
        lines = []
        seen = set()
        for r in picked:
            line = _row_to_line(r)
            if line and line not in seen:
                seen.add(line)
                lines.append(line)
        if not lines and rows:
            # Parsed as JSON but no usable rows (unexpected shape) — search the
            # blob as-is rather than wrongly reporting "no changes".
            return {'queries': [text[:max_query_chars]], 'source': 'raw', 'total_changes': 1}
        queries = [q[:max_query_chars] for q in _chunk_into(lines, max_queries)]
        return {'queries': queries, 'source': 'structured', 'total_changes': len(lines)}

    # ── Markdown report ──────────────────────────────────────────────────────
    if _NO_CHANGES_RE.search(text) and len(_strip_markdown(text)) < 120:
        return {'queries': [], 'source': 'markdown', 'total_changes': 0}

    sections = re.split(r'^##\s+', text, flags=re.M)
    if len(sections) > 1:
        lines = []
        for block in sections:
            block = block.strip()
            if not block:
                continue
            heading, _, body = block.partition('\n')
            content = _strip_markdown(body) if body.strip() else ''
            line = f'{heading.strip()}: {content}' if content else heading.strip()
            lines.append(line)
        queries = [q[:max_query_chars] for q in _chunk_into(lines, max_queries)]
        return {'queries': queries, 'source': 'markdown', 'total_changes': len(lines)}

    # ── Fallback: single opaque blob ─────────────────────────────────────────
    return {'queries': [text[:max_query_chars]], 'source': 'raw', 'total_changes': 1}


def changes_to_readable(changes_text: str) -> str:
    """Human-readable rendering for the agent-based V1 flow.

    Structured JSON becomes a grouped Markdown summary; anything else is
    passed through unchanged.
    """
    text = (changes_text or '').strip()
    rows = _parse_json_array(text)
    if rows is None:
        return text

    by_section: Dict[str, List[str]] = {}
    order: List[str] = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        section = str(r.get('section') or 'General').strip() or 'General'
        crit = str(r.get('criticality') or '').strip()
        typ = str(r.get('type') or '').strip()
        line = _row_to_line({**r, 'section': ''})
        prefix = ' / '.join(p for p in (crit, typ) if p)
        bullet = f'- [{prefix}] {line}' if prefix else f'- {line}'
        if section not in by_section:
            by_section[section] = []
            order.append(section)
        by_section[section].append(bullet)

    if not order:
        return text
    out: List[str] = []
    for section in order:
        out.append(f'## {section}')
        out.extend(by_section[section])
        out.append('')
    return '\n'.join(out).strip()
