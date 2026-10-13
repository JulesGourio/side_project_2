"""Segmentation-artifact filters and relocation detection for the leftovers of the paragraph pairing."""

import difflib
import os
import re
from collections import defaultdict
from typing import Any, Dict, List, Set, Tuple

from .text import _DIFF_ENTRY_RE, canonicalize_keep_modals

_RELOCATION_MIN_WORDS = 6
# Below _RELOCATION_MIN_WORDS the relocation check never runs, so short blocks need their own net: a block still
# present VERBATIM in the other revision is a segmentation artifact (table cells regrouped by the PDF text extraction,
# a header-dedup pass dropping a repeat on one side only), never a real change. The occurrence COUNT means nothing for
# a repeated header or table cell.
_ARTIFACT_MAX_WORDS = int(os.getenv('COMPARE_ARTIFACT_MAX_WORDS', '12'))
# "Still present verbatim" alone is not enough: on a wiring diagram the change can be a short annotation moving
# sheets. The surviving occurrence must also sit at roughly the SAME relative position (a repagination moves a block
# by a fraction of a percent, a real relocation by far more). Fractional position, not page number, so a document-wide
# page offset does not look like a move.
_ARTIFACT_MAX_POS_DRIFT = float(os.getenv('COMPARE_ARTIFACT_MAX_POS_DRIFT', '0.035'))
# A label recurring this many times in BOTH documents is a diagram annotation stamped once per connection. There the
# count IS the signal and no positional rule separates a genuine extra occurrence from a re-segmentation, so dense
# labels are exempt from the net and stay visible.
_ARTIFACT_DENSE_LABEL_MIN = int(os.getenv('COMPARE_ARTIFACT_DENSE_LABEL_MIN', '20'))
# Merged-cell net (_drop_merged_cells): only fragments of at least this many
# words, absorbed into a container at least this many times longer. Both bounds
# exist to keep short diagram labels out of it.
_ARTIFACT_MERGED_MIN_WORDS = 1
_ARTIFACT_MERGED_MIN_RATIO = 3.0
# Above this many (old x new) occurrences of the SAME text, the exact-match DP is skipped for index-wise pairing: both
# lists are sorted by position, so it is right whenever the extra occurrences sit at the end. The DP only pays off on
# small, ambiguous groups.
_DUP_ALIGN_MAX_CELLS = 250_000


def _align_duplicates(
    olds: List[Dict[str, Any]],
    news: List[Dict[str, Any]],
    n_old: int,
    n_new: int,
) -> Tuple[List[Dict[str, Any]], Set[int]]:
    """Pair equal-text blocks order-preservingly, minimising position drift.

    Returns (unmatched_olds, unmatched_new_idxs).
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
            best = cost[i][j + 1]                      # skip news[j]
            if k - j >= m - i:                          # enough news left to skip olds[i]
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
    """Short block text -> fractional positions where it occurs in that document."""
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
    """Drop short leftovers that still exist verbatim at the same spot elsewhere.

    Safety net behind _align_duplicates, for the
    residual segmentation surplus those two cannot cancel.

    Gated on a surplus test so the net cannot swallow a genuine addition of text
    that already existed elsewhere: six new 'TO PATCH' annotations on a wiring
    diagram were all suppressed because another one sat nearby. The test counts occurrences as SUBSTRINGS of the whole
    document, not as standalone blocks — the artifacts this net exists for are
    precisely cases where the text survived merged into a neighbouring block, so
    a block-level count reads them as a surplus and protects them (a heading with 2 standalone blocks in one revision and 1 in the
    other, but the same number of substring hits in both because of the page headers).
    """
    kept: List[Dict[str, Any]] = []
    dropped = 0
    # A text in surplus justifies at most `surplus` entries, not one per leftover: the surplus is budgeted per text
    # and given to the leftovers furthest from any surviving occurrence (the likeliest genuine losses).
    # A loss already visible inside a MODIFIED entry's ~~strikethrough~~ must not also be charged to a leftover
    # elsewhere.
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
    """Drop leftovers whose whole text was absorbed into a longer nearby block.

    The other half of the segmentation problem: instead of a repeated cell
    surviving on one side only, a cell that stood alone in one revision is
    merged into its neighbour in the other. There is no standalone counterpart
    to match, so the block becomes a REMOVED/ADDED for text that did not change
    at all, e.g. a line of a change-history table merged into the surrounding sentence.

    Deliberately narrow: same relative position, and the container must be much
    longer than the fragment. Without the length ratio this also swallows a
    label sitting next to a longer sibling that merely starts with it
    ('TO PATCH' vs 'TO PATCH DC12_14+').
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

# High bar on purpose: a REMOVED block is often several sentences merged into one paragraph, and a genuinely mixed
# paragraph (part relocated, part deleted) can still rack up a long verbatim run from the relocated half alone. 0.75
# sits between the highest measured false positive (0.70) and the lowest confirmed true positive (0.79).
_RELOCATION_COVERAGE_THRESHOLD = 0.75
# A single long verbatim run is a much stronger relocation signal than scattered short matches: recurring boilerplate
# phrasing alone can reach 50%+ scattered coverage for a paragraph that exists nowhere else. Gating on both keeps a
# real removal from being masked as a relocation.
_RELOCATION_MIN_RUN_WORDS = 8
_STRIKETHROUGH_RE = re.compile(r'~~(.+?)~~', re.S)


class _RelocationIndex:
    """The new document, indexed once, to ask "does this removed text still
    exist somewhere?" for every REMOVED entry and struck-through span.

    Built once per comparison: constructing a SequenceMatcher per entry
    re-indexed the whole new document each time, which was 80% of the diff
    time on a large pair (10 s out of 12 for 600 removed paragraphs).
    """

    def __init__(self, new_text: str) -> None:
        words = canonicalize_keep_modals(new_text).split()
        self._matcher = difflib.SequenceMatcher(None, autojunk=False)
        self._matcher.set_seq2(words)
        run = _RELOCATION_MIN_RUN_WORDS
        self._runs = {tuple(words[k:k + run]) for k in range(len(words) - run + 1)}

    def signal(self, text: str) -> Tuple[float, int]:
        """(coverage, longest_run_words): coverage is the fraction of `text`'s
        words matched, in order, anywhere in the new document; longest_run_words
        is the single longest contiguous match.

        Uses difflib's word-level LCS (get_matching_blocks()) — unlike a rigid
        n-gram scan, one inserted word ("training program" -> "training and
        examination program") doesn't fracture an otherwise-long match into two
        just-under-threshold fragments.
        """
        words = canonicalize_keep_modals(text).split()
        n = len(words)
        if n < _RELOCATION_MIN_WORDS:
            return 0.0, 0
        self._matcher.set_seq1(words)
        blocks = self._matcher.get_matching_blocks()
        return sum(b.size for b in blocks) / n, max((b.size for b in blocks), default=0)

    def is_relocated(self, text: str) -> bool:
        # A contiguous run of _RELOCATION_MIN_RUN_WORDS is required anyway: without one in the new document the costly
        # alignment is skipped.
        words = canonicalize_keep_modals(text).split()
        run = _RELOCATION_MIN_RUN_WORDS
        if not any(tuple(words[k:k + run]) in self._runs for k in range(len(words) - run + 1)):
            return False
        coverage, longest_run = self.signal(text)
        return coverage >= _RELOCATION_COVERAGE_THRESHOLD and longest_run >= run


def _tag_relocated(results: List[Dict[str, Any]], new_text: str) -> int:
    """Mark content that moved rather than disappeared, so the analysis
    prompt sees a plain fact ("this text is still present, just moved")
    instead of having to infer it from reading the whole diff itself:

    - A standalone REMOVED entry whose substance reappears elsewhere becomes
      a RELOCATED entry.
    - A ~~struck-through~~ span inside a MODIFIED entry gets an inline note
      when its substance reappears elsewhere — the surrounding MODIFIED
      block is left intact (never split into separate rows).

    Returns the number of entries/spans annotated.
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
