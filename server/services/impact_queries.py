"""Turn a Compare analysis output into a numbered change list + vector-search queries.

The frontend sends whichever analysis produced the changes summary:
- structured method → a raw JSON array (section/type/criticality/before/after/rationale)
- standard method   → a Markdown report (## sections, bullet per change)
- manual mode       → free text

Every change gets a stable id (C1, C2… in document order) so the judge can say
which change a conflicting passage depends on, and the UI can show results
per change. Each searched change gets its own focused query: embedding the
whole blob at once dilutes the signal and matches nothing precisely.
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


def _clean(value: Any) -> str:
    return '' if _is_absent(value) else str(value).strip()


def _row_to_line(row: Dict[str, Any]) -> str:
    """One structured-JSON row → one compact natural-language change statement."""
    section = _clean(row.get('section'))
    before = _clean(row.get('before'))
    after = _clean(row.get('after'))
    rationale = _clean(row.get('rationale'))

    if not before and not after:
        core = ''
    elif not before:
        core = f'added: {after}'
    elif not after:
        core = f'removed: {before}'
    else:
        core = f'{before} → {after}'

    line = ' — '.join(p for p in (section, core) if p)
    if rationale:
        line = f'{line} ({rationale})' if line else rationale
    return line


_MD_MARKUP_RE = re.compile(r'(\*\*|~~|__|`)')
_MD_BULLET_RE = re.compile(r'^\s*[-*+]\s+', re.M)


def _strip_markdown(text: str) -> str:
    text = _MD_MARKUP_RE.sub('', text)
    text = _MD_BULLET_RE.sub('', text)
    return re.sub(r'[ \t]+', ' ', text).strip()


def _change(idx: int, *, section='', type_='', criticality='', before='', after='',
            summary='', text='', searched=True) -> Dict[str, Any]:
    return {
        'id': f'C{idx}', 'section': section, 'type': type_, 'criticality': criticality,
        'before': before, 'after': after, 'summary': summary, 'text': text, 'searched': searched,
    }


def extract_changes(changes_text: str, max_text_chars: int = 2000) -> Dict[str, Any]:
    """Numbered change list from an analysis output.

    Returns {'changes': [...], 'source': 'structured'|'markdown'|'raw'}. An
    empty list means the analysis reported no substantive change. Editorial/
    structural rows are kept (so ids match the Change Table) but flagged
    searched=False — renumbering or TOC edits never impact other documents.
    """
    text = (changes_text or '').strip()
    if not text:
        return {'changes': [], 'source': 'raw'}

    rows = _parse_json_array(text)
    if rows is not None:
        dict_rows = [r for r in rows if isinstance(r, dict)]
        if rows and not dict_rows:
            return {'changes': [_change(1, summary=text[:max_text_chars], text=text[:max_text_chars])],
                    'source': 'raw'}
        changes = []
        for i, r in enumerate(dict_rows, start=1):
            type_ = _clean(r.get('type'))
            changes.append(_change(
                i,
                section=_clean(r.get('section')),
                type_=type_,
                criticality=_clean(r.get('criticality')).lower(),
                before=_clean(r.get('before')),
                after=_clean(r.get('after')),
                summary=_clean(r.get('rationale')),
                text=_row_to_line(r)[:max_text_chars],
                searched=type_.lower() != 'editorial/structural',
            ))
        changes = [c for c in changes if c['text']]
        # Nothing but editorial rows left — search them rather than report nothing.
        if changes and not any(c['searched'] for c in changes):
            for c in changes:
                c['searched'] = True
        return {'changes': changes, 'source': 'structured'}

    if _NO_CHANGES_RE.search(text) and len(_strip_markdown(text)) < 120:
        return {'changes': [], 'source': 'markdown'}

    sections = [s.strip() for s in re.split(r'^##\s+', text, flags=re.M) if s.strip()]
    if len(sections) > 1 or text.lstrip().startswith('##'):
        changes = []
        for i, block in enumerate(sections, start=1):
            heading, _, body = block.partition('\n')
            content = _strip_markdown(body) if body.strip() else ''
            line = f'{heading.strip()}: {content}' if content else heading.strip()
            changes.append(_change(i, section=heading.strip(), summary=content[:max_text_chars],
                                   text=line[:max_text_chars]))
        return {'changes': changes, 'source': 'markdown'}

    return {'changes': [_change(1, summary=text[:max_text_chars], text=text[:max_text_chars])],
            'source': 'raw'}


def changes_to_queries(changes_text: str, max_queries: int = 30, max_query_chars: int = 2000) -> Dict[str, Any]:
    """extract_changes() plus one vector-search query per searched change.

    Each query is {'text', 'change_ids'}. Past max_queries, the lowest-
    criticality changes are packed together (in order) so none is dropped.
    """
    extracted = extract_changes(changes_text, max_query_chars)
    searched = sorted(
        (c for c in extracted['changes'] if c['searched']),
        key=lambda c: _CRIT_ORDER.get(c['criticality'], 1),
    )
    if len(searched) <= max_queries:
        groups = [[c] for c in searched]
    else:
        head = [[c] for c in searched[:max_queries - 1]]
        rest = searched[max_queries - 1:]
        groups = head + [rest]
    queries = [
        {'text': '\n'.join(c['text'] for c in g)[:max_query_chars], 'change_ids': [c['id'] for c in g]}
        for g in groups
    ]
    return {**extracted, 'queries': queries}
