"""Segmentation-artifact filters and relocation detection for the leftovers of the paragraph pairing."""

import difflib
import os
import re
from collections import defaultdict
from typing import Any, Dict, List, Set, Tuple

from server.services.compare.processors._diff_engines.text import _DIFF_ENTRY_RE, canonicalize_keep_modals

_RELOCATION_MIN_WORDS = 6
# Shorter blocks skip the relocation check, so a short block still present VERBATIM in the other revision is treated as
# a segmentation artifact (regrouped table cells, header dedup on one side only), never as a change.
_ARTIFACT_MAX_WORDS = int(os.getenv('COMPARE_ARTIFACT_MAX_WORDS', '12'))
# The surviving occurrence must also sit at about the SAME fractional position (a repagination moves a block by a
# fraction of a percent, a real relocation by far more); page numbers would see a document-wide offset as a move.
_ARTIFACT_MAX_POS_DRIFT = float(os.getenv('COMPARE_ARTIFACT_MAX_POS_DRIFT', '0.035'))
# A label recurring this often in BOTH documents is a diagram annotation stamped once per connection: its count is the
# signal, so such labels are exempt from the nets.
_ARTIFACT_DENSE_LABEL_MIN = int(os.getenv('COMPARE_ARTIFACT_DENSE_LABEL_MIN', '20'))
# Merged-cell net bounds (keep short diagram labels out of it): fragment of at least this many words, absorbed into a
# container this many times longer.
_ARTIFACT_MERGED_MIN_WORDS = 1
_ARTIFACT_MERGED_MIN_RATIO = 3.0
# Above this many (old x new) occurrences of the same text the exact-match DP is skipped for index-wise pairing.
_DUP_ALIGN_MAX_CELLS = 250_000


def _align_duplicates(
    olds: List[Dict[str, Any]],
    news: List[Dict[str, Any]],
    n_old: int,
    n_new: int,
) -> Tuple[List[Dict[str, Any]], Set[int]]:
    """Pair equal-text blocks order-preservingly, minimising position drift.

    Returns (unmatched_olds, unmatched_new_idxs).

    :param olds: old blocks with the same text, in order
    :param news: new blocks with the same text, in order
    :param n_old: number of blocks in the old document, to scale the positions
    :param n_new: number of blocks in the new document, to scale the positions
    :return: ``(unmatched old blocks, indexes of the unmatched new blocks)``
    """
    m, k = len(olds), len(news)
    if m == 1 and k == 1:
        return [], set()
    if m * k > _DUP_ALIGN_MAX_CELLS:
        pairs = min(m, k)
        return olds[pairs:], {n['idx'] for n in news[pairs:]}

    po = [o['idx'] / n_old for o in olds]
    pn = [n['idx'] / n_new for n in news]

    # cost[i][j] = min total drift aligning olds[i:] with news[j:], skipping the
    # surplus on whichever side is longer.
    INF = float('inf')
    cost = [[INF] * (k + 1) for _ in range(m + 1)]
    for i in range(m + 1):
        cost[i][k] = 0.0 if i == m else INF  # every old must be paired or skipped below
    for j in range(k + 1):
        cost[m][j] = 0.0
    for i in range(m - 1, -1, -1):
        for j in range(k - 1, -1, -1):
            best = cost[i][j + 1]
            if k - j >= m - i:
                best = min(best, cost[i + 1][j])
            best = min(best, abs(po[i] - pn[j]) + cost[i + 1][j + 1])
            cost[i][j] = best
        cost[i][k] = INF

    unmatched_olds: List[Dict[str, Any]] = []
    unmatched_new: Set[int] = set()
    i = j = 0
    while i < m and j < k:
        if cost[i][j] == abs(po[i] - pn[j]) + cost[i + 1][j + 1]:
            i += 1
            j += 1
        elif cost[i][j] == cost[i][j + 1]:
            unmatched_new.add(news[j]['idx'])
            j += 1
        else:
            unmatched_olds.append(olds[i])
            i += 1
    unmatched_olds.extend(olds[i:])
    unmatched_new.update(n['idx'] for n in news[j:])
    return unmatched_olds, unmatched_new


def _position_index(items: List[Dict[str, Any]], n_total: int) -> Dict[str, List[float]]:
    """Short block text -> fractional positions where it occurs in that document.

    :param items: blocks of one document
    :param n_total: number of blocks of the document, to scale the positions
    :return: short block text -> the positions where it occurs, from 0 to 1
    """
    index: Dict[str, List[float]] = defaultdict(list)
    for item in items:
        clean = item['clean']
        if clean and len(clean.split()) <= _ARTIFACT_MAX_WORDS:
            index[clean].append(item['idx'] / n_total)
    return index


def _drop_present_verbatim(
    leftovers: List[Dict[str, Any]],
    n_own: int,
    other_index: Dict[str, List[float]],
    own_joined: str,
    other_joined: str,
    already_reported: str = '',
) -> Tuple[List[Dict[str, Any]], int]:
    """Drop short leftovers that still exist verbatim at the same spot elsewhere (segmentation surplus).

    Gated on a surplus test so a genuine addition of text that already existed elsewhere is kept. Occurrences are
    counted as SUBSTRINGS of the whole document: in the artifacts this net targets, the text survived merged into a
    neighbouring block, which a block-level count would read as a surplus.

    :param leftovers: unmatched blocks of one side
    :param n_own: number of blocks on that side, to scale the positions
    :param other_index: positions of the short texts on the other side, from ``_position_index``
    :param own_joined: all the blocks of that side, newline-joined
    :param other_joined: all the blocks of the other side, newline-joined
    :param already_reported: strikethrough texts already shown in MODIFIED entries
    :return: ``(kept leftovers, number of leftovers dropped)``
    """
    kept: List[Dict[str, Any]] = []
    dropped = 0
    # A text in surplus protects at most `surplus` leftovers, the ones furthest from a surviving occurrence (the likeliest
    # genuine losses); a loss already shown inside a MODIFIED entry's strikethrough is not charged again.
    budget: Dict[str, int] = {}
    for item in leftovers:
        clean = item['clean']
        if clean and clean not in budget:
            surplus = own_joined.count(clean) - other_joined.count(clean)
            budget[clean] = max(0, surplus - already_reported.count(clean))
    order = sorted(
        range(len(leftovers)),
        key=lambda k: -min(
            (abs(p - leftovers[k]['idx'] / n_own)
             for p in other_index.get(leftovers[k]['clean'], ())),
            default=1.0,
        ),
    )
    verdict: Dict[int, bool] = {}
    for k in order:
        item = leftovers[k]
        clean = item['clean']
        words = clean.split() if clean else []
        if not words or len(words) > _ARTIFACT_MAX_WORDS:
            verdict[k] = True
            continue
        pos = item['idx'] / n_own
        if not any(abs(p - pos) <= _ARTIFACT_MAX_POS_DRIFT for p in other_index.get(clean, ())):
            verdict[k] = True
            continue
        own_n, other_n = own_joined.count(clean), other_joined.count(clean)
        if min(own_n, other_n) >= _ARTIFACT_DENSE_LABEL_MIN:
            verdict[k] = True
            continue
        if budget.get(clean, 0) > 0:
            budget[clean] -= 1
            verdict[k] = True
            continue
        verdict[k] = False
    for k, item in enumerate(leftovers):
        if verdict.get(k, True):
            kept.append(item)
        else:
            dropped += 1
    return kept, dropped


def _drop_merged_cells(
    leftovers: List[Dict[str, Any]],
    n_own: int,
    other_items: List[Dict[str, Any]],
    n_other: int,
    own_joined: str,
    other_joined: str,
) -> Tuple[List[Dict[str, Any]], int]:
    """Drop leftovers whose whole text was absorbed into a longer nearby block of the other revision.

    Deliberately narrow: same relative position and a much longer container; without the length ratio it would
    swallow a label next to a longer sibling that merely starts with it ('TO PATCH' vs 'TO PATCH DC12_14+').

    :param leftovers: unmatched blocks of one side
    :param n_own: number of blocks on that side, to scale the positions
    :param other_items: blocks of the other side
    :param n_other: number of blocks on the other side, to scale the positions
    :param own_joined: all the blocks of that side, newline-joined
    :param other_joined: all the blocks of the other side, newline-joined
    :return: ``(kept leftovers, number of leftovers dropped)``
    """
    kept: List[Dict[str, Any]] = []
    dropped = 0
    for item in leftovers:
        clean = item['clean']
        words = clean.split() if clean else []
        if not (_ARTIFACT_MERGED_MIN_WORDS <= len(words) <= _ARTIFACT_MAX_WORDS):
            kept.append(item)
            continue
        # Same surplus gate as _drop_present_verbatim: an occurrence COUNT that grew or shrank is content, not layout.
        if own_joined.count(clean) != other_joined.count(clean):
            kept.append(item)
            continue
        pos = item['idx'] / n_own
        needle_len = len(clean)
        if any(
            len(o['clean']) >= needle_len * _ARTIFACT_MERGED_MIN_RATIO
            and abs(o['idx'] / n_other - pos) <= _ARTIFACT_MAX_POS_DRIFT
            and clean in o['clean']
            for o in other_items
        ):
            dropped += 1
            continue
        kept.append(item)
    return kept, dropped

# High bar on purpose, and a long verbatim run is also required: scattered boilerplate phrasing alone can cover
# much of a block.
_RELOCATION_COVERAGE_THRESHOLD = 0.75
_RELOCATION_MIN_RUN_WORDS = 8
_STRIKETHROUGH_RE = re.compile(r'~~(.+?)~~', re.S)


class _RelocationIndex:
    """The new document indexed once, to ask "does this removed text still exist somewhere?" per entry.

    Built once per comparison, not once per entry: re-indexing the whole document for each entry is the costly part.
    """

    def __init__(self, new_text: str) -> None:
        """Indexes the new document once: its canonical words and the runs of 8 words.

        :param new_text: text of the new document, indexed once
        """
        words = canonicalize_keep_modals(new_text).split()
        self._matcher = difflib.SequenceMatcher(None, autojunk=False)
        self._matcher.set_seq2(words)
        run = _RELOCATION_MIN_RUN_WORDS
        self._runs = {tuple(words[k:k + run]) for k in range(len(words) - run + 1)}

    def signal(self, text: str) -> Tuple[float, int]:
        """(coverage, longest_run_words): the fraction of `text`'s words matched in order anywhere in the new document,
        and its longest contiguous match. Word-level LCS, so one inserted word does not split a long match.

        :param text: text to look up in the new document
        :return: ``(coverage, longest_run_words)``
        """
        words = canonicalize_keep_modals(text).split()
        n = len(words)
        if n < _RELOCATION_MIN_WORDS:
            return 0.0, 0
        self._matcher.set_seq1(words)
        blocks = self._matcher.get_matching_blocks()
        return sum(b.size for b in blocks) / n, max((b.size for b in blocks), default=0)

    def is_relocated(self, text: str) -> bool:
        # Cheap pre-check: without a long enough contiguous run in the new document, skip the costly alignment.
        """Whether a removed text still exists in the new document, moved elsewhere.

        :param text: removed text
        :return: True when the text is relocated rather than eliminated
        """
        words = canonicalize_keep_modals(text).split()
        run = _RELOCATION_MIN_RUN_WORDS
        if not any(tuple(words[k:k + run]) in self._runs for k in range(len(words) - run + 1)):
            return False
        coverage, longest_run = self.signal(text)
        return coverage >= _RELOCATION_COVERAGE_THRESHOLD and longest_run >= run


def _tag_relocated(results: List[Dict[str, Any]], new_text: str) -> int:
    """Mark content that moved rather than disappeared: a REMOVED entry whose text reappears elsewhere becomes
    RELOCATED, and a struck-through span of a MODIFIED entry gets an inline note (the entry itself stays whole).

    Returns the number of entries/spans annotated.

    :param results: diff entries, rewritten in place
    :param new_text: text of the new document
    :return: the number of entries and spans annotated
    """
    index = _RelocationIndex(new_text)
    tagged = 0

    for r in results:
        m = _DIFF_ENTRY_RE.match(r['content'])
        if not m:
            continue

        if m.group(1) == 'REMOVED':
            removed_text = m.group(3)
            if index.is_relocated(removed_text):
                tag = f' [{m.group(2)}]' if m.group(2) else ''
                r['content'] = (
                    f'RELOCATED{tag}: {removed_text} '
                    '(same content found elsewhere in the new document — moved, not eliminated)'
                )
                tagged += 1

        elif m.group(1) == 'MODIFIED':
            def _annotate(span_match: 're.Match') -> str:
                """Annotates a struck-through span whose text still exists in the new document.

                :param span_match: match of a ``~~struck~~`` span
                :return: the span with its note, or the match unchanged
                """
                nonlocal tagged
                span = span_match.group(1)
                if index.is_relocated(span):
                    tagged += 1
                    return f'~~{span}~~ [RELOCATED elsewhere in the new document, not eliminated]'
                return span_match.group(0)

            new_body = _STRIKETHROUGH_RE.sub(_annotate, m.group(3))
            if new_body != m.group(3):
                tag = f' [{m.group(2)}]' if m.group(2) else ''
                r['content'] = f'MODIFIED{tag}: {new_body}'

    return tagged
