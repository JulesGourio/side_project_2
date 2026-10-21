"""Passes that merge or cancel repeated entries: running headers, resegmented blocks, per-page repeats and global restyles."""

import os
import re
from collections import defaultdict
from typing import Any, Dict, List, Set, Tuple

from server.services.compare.processors._diff_engines.text import _DIFF_ENTRY_RE

# A change whose identical text recurs on at least this many distinct pages is title-block/boilerplate (revision
# stamp, classification field, footer): one real change rendered once per page by the PDF extraction.
_BOILERPLATE_MIN_PAGES = int(os.getenv('COMPARE_BOILERPLATE_MIN_PAGES', '3'))


def _dedup_repeated_headers(items: List[Dict[str, Any]], window: int = 25) -> List[Dict[str, Any]]:
    """Drop a short block that reappears within `window` source lines (repeated column headers, running footers,
    wiring labels: layout, not content), when the block occurs on at least ``_BOILERPLATE_MIN_PAGES`` distinct pages.
    A block of a document without page numbers is dropped on its position alone.

    Not symmetric under repagination: the leftover surplus copy is caught downstream by _drop_present_verbatim /
    _drop_merged_cells, since no window value is symmetric.

    :param items: blocks in source order, each with its ``clean`` text and its ``idx`` (source line)
    :param window: source lines within which a repeat of a short block is dropped
    :return: the blocks without their repeats
    """
    MAX_WORDS = 15
    pages: Dict[str, Set[int]] = defaultdict(set)
    for item in items:
        if item.get('page') is not None:
            pages[item['clean']].add(item['page'])
    seen: Dict[str, int] = {}
    result: List[Dict[str, Any]] = []
    for item in items:
        clean = item['clean']
        if 0 < len(clean.split()) <= MAX_WORDS:
            prev = seen.get(clean)
            recurring = not pages[clean] or len(pages[clean]) >= _BOILERPLATE_MIN_PAGES
            if prev is not None and recurring and (item['idx'] - prev) <= window:
                seen[clean] = item['idx']
                continue
        seen[clean] = item['idx']
        result.append(item)
    return result


# A paragraph cut in two (or two merged into one) between revisions: up to this many consecutive blocks, no further
# apart than this many source lines.
_RESEGMENT_MAX_RUN = 3
_RESEGMENT_MAX_GAP = 3
_RESEGMENT_MIN_WORDS = 8


def _cancel_resegmented(
    olds: List[Dict[str, Any]], news: List[Dict[str, Any]],
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], int]:
    """Remove blocks whose text is unchanged but cut differently (one block equal to 2-3 consecutive leftovers on the
    other side: a paragraph break added or removed).

    Returns (remaining_olds, remaining_news, n_blocks_cancelled).

    :param olds: old-side blocks
    :param news: new-side blocks
    :return: the remaining old blocks, the remaining new blocks and the number of blocks cancelled
    """
    def _runs(items: List[Dict[str, Any]]) -> Dict[str, Tuple[int, ...]]:
        """Map from the joined text of 1 to 3 consecutive blocks to the positions they cover.

        :param items: blocks of one side
        :return: text -> tuple of the positions in ``items``
        """
        out: Dict[str, Tuple[int, ...]] = {}
        for a in range(len(items)):
            text = items[a]['clean']
            for b in range(a + 1, min(a + _RESEGMENT_MAX_RUN, len(items))):
                if items[b]['idx'] - items[b - 1]['idx'] > _RESEGMENT_MAX_GAP:
                    break
                text = f"{text} {items[b]['clean']}"
                out.setdefault(text, tuple(range(a, b + 1)))
        return out

    drop_old: Set[int] = set()
    drop_new: Set[int] = set()
    for wholes, parts, drop_w, drop_p in ((olds, news, drop_old, drop_new), (news, olds, drop_new, drop_old)):
        runs = _runs(parts)
        for i, whole in enumerate(wholes):
            if i in drop_w or len(whole['clean'].split()) < _RESEGMENT_MIN_WORDS:
                continue
            run = runs.get(whole['clean'])
            if run and not any(k in drop_p for k in run):
                drop_w.add(i)
                drop_p.update(run)
    if not drop_old and not drop_new:
        return olds, news, 0
    return (
        [o for i, o in enumerate(olds) if i not in drop_old],
        [n for j, n in enumerate(news) if j not in drop_new],
        len(drop_old) + len(drop_new),
    )


# Pagination fragments in repeated header/footer text ('Page 2', 'PAGE: 1 of 34', 'Sheet 5/48') are masked only around
# these keywords: bare numbers such as pin numbers must stay distinct.
_PAGINATION_RE = re.compile(
    r'\b(page|sheet|folio|feuille|list|strana|str)\s*[:.]?\s*'
    r'\d+(?:\s*(?:of|/|sur|z)\s*\d+)?'
    r'(?:\s+\d+(?:\s*(?:of|/|sur|z)\s*\d+)?)*',
    re.I,
)
# Inline word-diff markup, stripped before computing a grouping key only.
_MARKUP_RE = re.compile(r'~~|\*\*')
# Table-of-contents rows (dot/dash leaders to a page number): their section changes appear as body entries. Matches
# mid-text too (inline markup wraps the leaders); the digit requirement keeps plain '------' separators out.
_TOC_LINE_RE = re.compile(r'(?:\.{4,}\s*\d*\s*$|[.\-]{6,}.{0,40}\d)')


# "PAGE : ~~1/37~~ **1/38**" is a repagination substitution: a MODIFIED entry left without markup once every such
# pair is stripped changed only its page number.
_PAGE_FRACTION_SUB_RE = re.compile(
    r'~~\s*(?:page\s*[:.]?\s*)?\d{1,4}\s*/\s*\d{1,4}\s*~~\s*\*\*\s*(?:page\s*[:.]?\s*)?\d{1,4}\s*/\s*\d{1,4}\s*\*\*',
    re.I,
)
_ANY_MARKUP_RE = re.compile(r'~~[^~]*~~|\*\*[^*]*\*\*')


def _is_pagination_only(content: str) -> bool:
    """Whether a MODIFIED entry changed only its page number in a repagination.

    :param content: entry text, starting with its type
    :return: True when the only change is a page number
    """
    m = _DIFF_ENTRY_RE.match(content)
    if not m or m.group(1) != 'MODIFIED':
        return False
    stripped = _PAGE_FRACTION_SUB_RE.sub('', m.group(3))
    return bool(_PAGE_FRACTION_SUB_RE.search(m.group(3))) and not _ANY_MARKUP_RE.search(stripped)


def _collapse_page_repeats(results: List[Dict[str, Any]], page_label: str | None) -> Tuple[List[Dict[str, Any]], int]:
    """Collapse per-page repeats of the SAME change into one entry that keeps the first occurrence's position and
    says how many pages repeat it. Untagged entries are never collapsed. Pagination fragments are masked so
    'Issue A6 Page N' repeats group across pages, and dot-leader TOC rows group per kind.

    Returns (collapsed_results, n_entries_absorbed).

    :param results: entries of the diff, in order
    :param page_label: word that names the page in the summary, e.g. ``Page``; falsy disables the collapse
    :return: the collapsed entries and the number of entries absorbed
    """
    if not page_label:
        return results, 0

    def _group_key(kind: str, text: str) -> Tuple[str, str]:
        """Grouping key of an entry: its kind and its text without pagination and markup.

        :param kind: entry type, e.g. ``MODIFIED``
        :param text: entry text
        :return: ``(kind, key)``; ``<toc>`` as the key for table-of-contents rows
        """
        if _TOC_LINE_RE.search(text):
            return kind, '<toc>'
        return kind, _PAGINATION_RE.sub(r'\1 #', _MARKUP_RE.sub('', text))

    groups: Dict[Tuple[str, str], List[Tuple[int, Dict[str, Any], str]]] = defaultdict(list)
    parsed: List[Tuple[str, int, str] | None] = []
    for r in results:
        m = _DIFF_ENTRY_RE.match(r['content'])
        pm = re.search(r'(\d+)', m.group(2) or '') if m else None
        if not m or not pm:
            parsed.append(None)
            continue
        kind, text = m.group(1), m.group(3)
        page = int(pm.group(1))
        parsed.append((kind, page, text))
        groups[_group_key(kind, text)].append((page, r, text))

    collapsed_keys = set()
    for key, members in groups.items():
        if key[1] == '<toc>':
            # A rebuilt TOC lands on ONE page as dozens of rows: count rows, not pages.
            if len(members) >= _BOILERPLATE_MIN_PAGES:
                collapsed_keys.add(key)
        elif len({p for p, _, _ in members}) >= _BOILERPLATE_MIN_PAGES:
            collapsed_keys.add(key)

    out: List[Dict[str, Any]] = []
    emitted: set = set()
    absorbed = 0
    for r, p in zip(results, parsed):
        if p is None:
            out.append(r)
            continue
        kind, _page, text = p
        key = _group_key(kind, text)
        if key not in collapsed_keys:
            out.append(r)
            continue
        if key in emitted:
            absorbed += 1
            continue
        emitted.add(key)
        members = groups[key]
        pages = sorted({pg for pg, _, _ in members})
        first = min(members, key=lambda m_: m_[1]['sort_idx'])
        span = f'{page_label} {pages[0]}-{pages[-1]}'
        if key[1] == '<toc>':
            content = (f'{kind} [{span}]: {len(members)} table-of-contents lines '
                       f'(dot-leader rows — the section changes themselves appear as '
                       f'their own entries), e.g.: {first[2][:100]}')
        else:
            content = (f'{kind} [{span}, repeated on {len(pages)} pages — recurring '
                       f'header/footer block]: {first[2]}')
        out.append({**first[1], 'content': content})
    return out, absorbed


# A 'replace' span from inline_word_diff(): "~~old words~~ **new words**".
_WORD_SUB_RE = re.compile(r'~~([^~]*)~~\s*\*\*([^*]*)\*\*')
_SYSTEMIC_SUB_MIN = int(os.getenv('COMPARE_SYSTEMIC_SUB_MIN', '5'))


def _collapse_systemic_substitutions(
    results: List[Dict[str, Any]], min_occurrences: int = _SYSTEMIC_SUB_MIN
) -> Tuple[List[Dict[str, Any]], int]:
    """Collapse a document-wide restyle (bullet marker swap, ANNEX->APPENDIX rename) that recurs identically across
    many MODIFIED entries into one annotated example.

    Only entries made up ENTIRELY of recurring pairs are collapsed (an entry mixing a systemic swap with other wording
    changes is kept in full), and a pair with a digit on either side is never systemic (a pin number repeated across
    table rows must stay visible per row).

    :param results: entries of the diff, in order
    :param min_occurrences: number of entries a substitution must recur in to be systemic
    :return: the collapsed entries and the number of entries absorbed
    """

    def _norm(tok: str) -> str:
        """Normalised text of one side of a substitution: spaces collapsed, case folded.

        :param tok: text of one side of a substitution
        :return: the normalised text
        """
        return re.sub(r'\s+', ' ', tok).strip().casefold()

    entry_pairs: Dict[int, List[Tuple[str, str]]] = {}
    pair_entries: Dict[Tuple[str, str], set] = defaultdict(set)

    for idx, r in enumerate(results):
        if not r['content'].startswith('MODIFIED'):
            continue
        pairs = [
            (_norm(m.group(1)), _norm(m.group(2)))
            for m in _WORD_SUB_RE.finditer(r['content'])
        ]
        pairs = [p for p in pairs if p[0] or p[1]]
        if not pairs:
            continue
        entry_pairs[idx] = pairs
        for p in set(pairs):
            if any(ch.isdigit() for ch in p[0] + p[1]):
                continue
            pair_entries[p].add(idx)

    systemic = {p for p, idxs in pair_entries.items() if len(idxs) >= min_occurrences}
    if not systemic:
        return results, 0

    signature_entries: Dict[Tuple[Tuple[str, str], ...], List[int]] = defaultdict(list)
    for idx, pairs in entry_pairs.items():
        uniq = set(pairs)
        if uniq and uniq <= systemic:
            signature_entries[tuple(sorted(uniq))].append(idx)

    to_collapse = {sig: idxs for sig, idxs in signature_entries.items() if len(idxs) >= min_occurrences}
    if not to_collapse:
        return results, 0

    idx_to_sig = {idx: sig for sig, idxs in to_collapse.items() for idx in idxs}
    out: List[Dict[str, Any]] = []
    absorbed = 0
    emitted: set = set()
    for idx, r in enumerate(results):
        sig = idx_to_sig.get(idx)
        if sig is None:
            out.append(r)
            continue
        if sig in emitted:
            absorbed += 1
            continue
        emitted.add(sig)
        idxs = to_collapse[sig]
        first = results[min(idxs, key=lambda i: results[i]['sort_idx'])]
        desc = ', '.join(f"'{o}' -> '{n}'" for o, n in sig)
        note = f' [same substitution repeated in {len(idxs) - 1} more entries — {desc}]'
        out.append({**first, 'content': first['content'] + note})
    return out, absorbed
