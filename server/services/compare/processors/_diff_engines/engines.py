"""The two text diff engines: paragraph_semantic_diff (primary) and section_canonical_diff."""

import difflib
import os
import re
from collections import Counter, defaultdict
from typing import Any, Dict, List, Tuple

from server.services.compare.processors._diff_engines.collapse import (
    _BOILERPLATE_MIN_PAGES,
    _cancel_resegmented,
    _collapse_page_repeats,
    _collapse_systemic_substitutions,
    _dedup_repeated_headers,
    _is_pagination_only,
)
from server.services.compare.processors._diff_engines.relocation import (
    _STRIKETHROUGH_RE,
    _align_duplicates,
    _drop_merged_cells,
    _drop_present_verbatim,
    _position_index,
    _tag_relocated,
)
from server.services.compare.processors._diff_engines.text import SECTION_RE, inline_word_diff, is_substantive_keep_modals, strip_tag

# Word similarity above which two unmatched paragraphs pair as one MODIFIED entry (below: REMOVED + ADDED).
_PAIR_RATIO_THRESHOLD = float(os.getenv('COMPARE_PAIR_RATIO_THRESHOLD', '0.55'))
# The bare ALL-CAPS half of SECTION_RE, used to spot running headers/footers (company name, standard title, address)
# that recur on every page and would each register as a new section.
_ALLCAPS_SECTION_RE = re.compile(r"^[A-Z][A-Z0-9 \-–—:,./()&']{3,}$")
# Trailing "N/M" page fraction of a running footer stamp ("ABC-0001 - V09 4/39"): masked only when counting heading
# repeats, so each page's stamp counts as the same label.
_TRAILING_PAGE_FRACTION_RE = re.compile(r'\s+\d+\s*/\s*\d+$')

_CONT_RE = re.compile(
    r'^((?:\d+(?:\.\d+)*\.?\s+)?[A-Z][A-Z\s\d]*?)\s*\((Continued|Suite|Cont\.)\)(?:\s+(.*))?',
    re.I | re.DOTALL,
)


def _clean(s: str) -> str:
    """Lowercase text with dot leaders and repeated spaces collapsed, used as the comparison key.

    :param s: text to normalise
    :return: the normalised text
    """
    s = re.sub(r'[\.\-_]{2,}', ' ', s)
    return re.sub(r'\s+', ' ', s).strip().lower()


def _parse_blocks(text: str, page_label: str | None) -> List[Dict[str, Any]]:
    """Splits a text into blocks, each tagged with its section, its page and its comparison key.

    :param text: text of one document, one block per line
    :param page_label: label that makes ``[Page N]`` tags give the page number; None reads no page
    :return: the blocks as dicts with ``idx``, ``sec``, ``txt``, ``clean`` and ``page``
    """
    lines = text.splitlines()
    bare_lines = [strip_tag(b) for b in lines]
    # Mask a trailing page fraction ("... V09 4/39") before counting repeats: a footer stamp embedding its page
    # number is a different string on every page.
    _heading_key = lambda b: _TRAILING_PAGE_FRACTION_RE.sub('', b)
    # A bare ALL-CAPS line repeating on at least _BOILERPLATE_MIN_PAGES pages is a running header or footer, not a section.
    # Numbered headings are exempt: a clause number essentially never repeats that often.
    running_header_counts = Counter(_heading_key(b) for b in bare_lines if _ALLCAPS_SECTION_RE.match(b))

    result: List[Dict[str, Any]] = []
    cur_sec = 'Preamble'
    for idx, (block, bare) in enumerate(zip(lines, bare_lines)):
        pm = re.search(r'\[(?:Page|Slide|Item)\s+(\d+)', block, re.I) if page_label else None
        page_num = int(pm.group(1)) if pm else None
        is_toc = bool(re.search(r'\.{3,}\s*\d+$', bare))
        is_running_header = running_header_counts.get(_heading_key(bare), 0) >= _BOILERPLATE_MIN_PAGES
        if SECTION_RE.match(bare) and 2 < len(bare) < 100 and not is_toc and not is_running_header:
            cur_sec = re.sub(
                r'\s*\((?:Continued|Suite|Cont\.)\)\s*$', '', bare, flags=re.I
            ).strip() or bare
        if bare.strip():
            result.append({'idx': idx, 'sec': cur_sec, 'txt': bare, 'clean': _clean(bare), 'page': page_num})
    return result


def _merge_continued(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Joins each '(Continued)' page-break header to the paragraph it continues.

    :param items: blocks in document order
    :return: the blocks, the continuations merged and the bare headers dropped
    """
    result: List[Dict[str, Any]] = []
    last_content_idx: Dict[str, int] = {}
    for item in items:
        m = _CONT_RE.match(item['txt'])
        if m:
            continuation = (m.group(3) or '').strip()
            sec_from_header = m.group(1).strip()
            target = next(
                (c for c in [sec_from_header.lower(), item['sec'].lower()] if c in last_content_idx),
                None,
            )
            if target is not None and continuation:
                ti = last_content_idx[target]
                result[ti]['txt'] += ' ' + continuation
                result[ti]['clean'] += ' ' + _clean(continuation)
                continue
            elif not continuation:
                continue  # Pure page-break header — drop to avoid false MODIFIED
        sec_key = item['sec'].lower()
        if len(item['txt'].split()) >= 10:
            last_content_idx[sec_key] = len(result)
        result.append(item)
    return result


def _sort_for_display(results: List[Dict[str, Any]], old_items: List[Dict[str, Any]],
                      new_items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Order entries by section, aligning the two documents' headings with SequenceMatcher on normalized
    keys (a revision that re-cases headings or bumps clause numbers would otherwise match almost none).

    :param results: entries to order, sorted in place
    :param old_items: blocks of the old document
    :param new_items: blocks of the new document
    :return: the entries in display order
    """
    def _sec_align_key(s: str) -> str:
        """Section name as a key for aligning two documents: no clause number, spaces collapsed, case folded.

        :param s: section name
        :return: the key
        """
        s = re.sub(r'^\d+(?:\.\d+)*\.?\s*', '', s)
        return re.sub(r'\s+', ' ', s).strip().casefold()

    def _runs_with_item_map(items: List[Dict[str, Any]]) -> Tuple[List[str], Dict[int, int]]:
        """One entry per contiguous run of the same heading, plus a map from each item's 'idx' to its run.

        A heading recurring at distant points (a generic 'General', a repeated workflow step) stays several separate
        runs instead of collapsing onto the first one's slot.

        :param items: blocks of one document
        :return: ``(sequence of section runs, map from each block idx to its run)``
        """
        seq: List[str] = []
        idx_to_run: Dict[int, int] = {}
        for i in items:
            if not seq or seq[-1] != i['sec']:
                seq.append(i['sec'])
            idx_to_run[i['idx']] = len(seq) - 1
        return seq, idx_to_run

    old_sec_seq, old_idx_to_run = _runs_with_item_map(old_items)
    new_sec_seq, new_idx_to_run = _runs_with_item_map(new_items)

    # A normalized key may match ACROSS documents only if it identifies a single run on EACH side; a recurring key is
    # ambiguous and gets a per-occurrence sentinel, so SequenceMatcher never pairs it with the wrong occurrence.
    old_raw_keys = [_sec_align_key(s) for s in old_sec_seq]
    new_raw_keys = [_sec_align_key(s) for s in new_sec_seq]
    old_key_counts = Counter(old_raw_keys)
    new_key_counts = Counter(new_raw_keys)

    def _disambiguate(raw_keys: List[str], counts_here: Counter, counts_other: Counter) -> List[str]:
        """Section keys, with a per-occurrence suffix on keys that recur on either side.

        :param raw_keys: normalised section keys of one side, in order
        :param counts_here: occurrences of each key on this side
        :param counts_other: occurrences of each key on the other side
        :return: the keys to align
        """
        return [
            k if counts_here[k] <= 1 and counts_other.get(k, 0) <= 1 else f'{k}\x00{i}'
            for i, k in enumerate(raw_keys)
        ]

    old_sec_keys = _disambiguate(old_raw_keys, old_key_counts, new_key_counts)
    new_sec_keys = _disambiguate(new_raw_keys, new_key_counts, old_key_counts)

    old_run_order: List[int] = [0] * len(old_sec_seq)
    new_run_order: List[int] = [0] * len(new_sec_seq)
    idx_sec = 0
    sec_sm = difflib.SequenceMatcher(None, old_sec_keys, new_sec_keys, autojunk=False)
    for tag, i1, i2, j1, j2 in sec_sm.get_opcodes():
        if tag == 'equal':
            for k in range(i2 - i1):
                old_run_order[i1 + k] = idx_sec
                new_run_order[j1 + k] = idx_sec
                idx_sec += 1
        else:
            for k in range(i1, i2):
                old_run_order[k] = idx_sec
                idx_sec += 1
            for k in range(j1, j2):
                new_run_order[k] = idx_sec
                idx_sec += 1

    def _order_key(r: Dict[str, Any]) -> Tuple[int, int, float]:
        """(section run, page, fractional position): one axis for both documents, so a REMOVED and the ADDED that
        replaces it end up next to each other.

        :param r: entry to order
        :return: ``(section run order, page, fractional position)``
        """
        if r.get('from_new'):
            run = new_idx_to_run.get(r['sort_idx'])
            order = new_run_order[run] if run is not None else 999_999
        else:
            run = old_idx_to_run.get(r['sort_idx'])
            order = old_run_order[run] if run is not None else 999_999
        return order, r.get('page') or 0, r.get('pos') or 0.0

    results.sort(key=_order_key)

    # Old and new runs of one label get distinct slots, which would put every old-side entry before every new-side one:
    # re-sort each contiguous same-label group by page (local, nothing crosses a section).
    i = 0
    while i < len(results):
        j = i + 1
        while j < len(results) and results[j]['sec'] == results[i]['sec']:
            j += 1
        results[i:j] = sorted(results[i:j], key=lambda r: (r.get('page') or 0, r.get('pos') or 0.0))
        i = j
    return results


def paragraph_semantic_diff(old_text: str, new_text: str, page_label: str | None = None) -> Tuple[str, int]:
    """Primary paragraph-level semantic alignment engine.

    '(Continued)' page splits are merged, exact matches dropped, near matches (word ratio > 0.55, 3+ words) become
    MODIFIED with inline ~~removed~~ / **added** markup, the rest REMOVED / ADDED, grouped by section in document
    order. Modal swaps inside one obligation group (shall/must) are ignored, cross-group ones (shall/may) reported.
    Per-page boilerplate repeats and document-wide restyles collapse into one annotated entry.

    Returns (diff_string, filtered_count).

    :param old_text: text of the old document
    :param new_text: text of the new document
    :param page_label: label of the pages in the entries, e.g. ``Page``; None when the document has no pages
    :return: ``(diff_string, filtered_count)``
    """
    # Raw (pre-dedup) blocks are kept: the artifact nets below count occurrences on them, because the dedup pass is
    # what makes counts look uneven between the two revisions.
    old_raw = _merge_continued(_parse_blocks(old_text, page_label))
    new_raw = _merge_continued(_parse_blocks(new_text, page_label))
    old_items = _dedup_repeated_headers(old_raw)
    new_items = _dedup_repeated_headers(new_raw)
    # Newline-joined so a single-line block can only match inside one block.
    old_joined = '\n'.join(i['clean'] for i in old_raw)
    new_joined = '\n'.join(i['clean'] for i in new_raw)

    # Old and new block indexes live in different spaces: the fractional position gives both documents one comparable axis,
    # so REMOVED and ADDED entries interleave.
    n_old = max(1, (old_items[-1]['idx'] if old_items else 0) + 1)
    n_new = max(1, (new_items[-1]['idx'] if new_items else 0) + 1)

    # Exact matching, grouped by clean text so repeated blocks pair by POSITION instead of document order: a FIFO
    # queue would always consume the FIRST old occurrence and leave a phantom removal.
    old_by_clean: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for o in old_items:
        old_by_clean[o['clean']].append(o)
    new_by_clean: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for n in new_items:
        new_by_clean[n['clean']].append(n)

    matched_new: set = set()
    unmatched_old: List[Dict[str, Any]] = []
    for clean, olds in old_by_clean.items():
        news = new_by_clean.get(clean)
        if not news:
            unmatched_old.extend(olds)
            continue
        keep_old, keep_new = _align_duplicates(olds, news, n_old, n_new)
        unmatched_old.extend(keep_old)
        matched_new.update(n['idx'] for n in news if n['idx'] not in keep_new)

    new_leftovers = [n for n in new_items if n['idx'] not in matched_new]
    unmatched_old.sort(key=lambda o: o['idx'])
    unmatched_old, new_leftovers, resegmented = _cancel_resegmented(unmatched_old, new_leftovers)

    # Every leftover old block against every leftover new block. Cheap upper bounds (lengths, shared words) skip most pairs
    # before any SequenceMatcher work, without changing a pairing decision.
    sims: List[Tuple[float, int, int]] = []
    old_words_cache = [o['clean'].split() for o in unmatched_old]
    old_counts = [Counter(words) for words in old_words_cache]
    for j, n in enumerate(new_leftovers):
        n_words = n['clean'].split()
        ln = len(n_words)
        if ln < 3:
            continue
        n_count = Counter(n_words)
        sm: difflib.SequenceMatcher | None = None
        for i, o_words in enumerate(old_words_cache):
            lo = len(o_words)
            if lo < 3:
                continue
            if 2.0 * min(lo, ln) / (lo + ln) <= _PAIR_RATIO_THRESHOLD:
                continue
            o_count = old_counts[i]
            small, large = (o_count, n_count) if len(o_count) <= len(n_count) else (n_count, o_count)
            shared = sum(min(c, large[w]) for w, c in small.items() if w in large)
            if 2.0 * shared / (lo + ln) <= _PAIR_RATIO_THRESHOLD:
                continue
            if sm is None:
                sm = difflib.SequenceMatcher(None, autojunk=False)
                sm.set_seq2(n_words)
            sm.set_seq1(o_words)
            ratio = sm.ratio()
            if ratio > _PAIR_RATIO_THRESHOLD:
                sims.append((ratio, i, j))

    # Best ratio first; ties in (old, new) order.
    sims.sort(key=lambda x: (-x[0], x[1], x[2]))
    matched_o: set = set()
    matched_n: set = set()
    results: List[Dict[str, Any]] = []
    filtered = resegmented

    def _emit_modified(o: Dict[str, Any], n: Dict[str, Any]) -> None:
        """Adds the MODIFIED entry of a pair when the change is substantive; counts the pair as filtered otherwise.

        :param o: old-side block
        :param n: new-side block
        """
        nonlocal filtered
        if is_substantive_keep_modals(o['txt'], n['txt']):
            diff_str = inline_word_diff(o['txt'], n['txt'])
            op, np_ = o.get('page'), n.get('page')
            lbl = page_label or 'Page'
            if op and np_ and op != np_:
                page_tag = f' [{lbl} {op}→{np_}]'
            elif op or np_:
                page_tag = f' [{lbl} {op or np_}]'
            else:
                page_tag = ''
            results.append({
                'sec': o['sec'],
                'content': f'MODIFIED{page_tag}: {diff_str}',
                'sort_idx': o['idx'],
                'from_new': False,
                'page': op or np_,
                'pos': o['idx'] / n_old,
            })
        else:
            filtered += 1

    for ratio, i, j in sims:
        if i in matched_o or j in matched_n:
            continue
        matched_o.add(i)
        matched_n.add(j)
        _emit_modified(unmatched_old[i], new_leftovers[j])

    # ── Second-chance pairing: table rows sharing the same row key ──────────
    # DOCX table rows are emitted as 'RowLabel: col: val | …'. A heavily rewritten row is still the same row evolving:
    # pair leftovers with the same row label and section instead of reporting REMOVED + ADDED.
    def _row_key(txt: str) -> str | None:
        """Row label of a table row written 'Label: ...', used to pair rows; None when the text is no row.

        :param txt: block text
        :return: the cleaned label, or None
        """
        head, sep, _ = txt.partition(':')
        if not sep:
            return None
        key = _clean(head)
        return key if len(key) >= 6 and 2 <= len(key.split()) <= 8 else None

    keyed_new: Dict[Tuple[str, str], List[int]] = {}
    for j, n in enumerate(new_leftovers):
        if j in matched_n:
            continue
        k = _row_key(n['txt'])
        if k:
            keyed_new.setdefault((n['sec'], k), []).append(j)
    for i, o in enumerate(unmatched_old):
        if i in matched_o:
            continue
        k = _row_key(o['txt'])
        if not k:
            continue
        bucket = keyed_new.get((o['sec'], k))
        if not bucket:
            continue
        j = bucket.pop(0)
        matched_o.add(i)
        matched_n.add(j)
        _emit_modified(o, new_leftovers[j])

    # ── Third-chance pairing: same text behind a different leading number ───
    # '3. INSPECTION' -> '4. INSPECTION' is too short for the similarity pass. Paired, it shows only the number change;
    # a real value change ('2.5 mm max' -> '3.5 mm max') must never be hidden.
    def _numbered_key(txt: str) -> str | None:
        """Text behind a leading number ('3. INSPECTION' gives 'inspection'), used to pair renumbered headings.

        :param txt: block text
        :return: the text, or None when there is no leading number or no words after it
        """
        head, _, rest = txt.strip().partition(' ')
        rest = _clean(rest)
        if not rest or not any(ch.isdigit() for ch in head) or not any(ch.isalpha() for ch in rest):
            return None
        return rest

    numbered_new: Dict[str, List[int]] = {}
    for j, n in enumerate(new_leftovers):
        if j not in matched_n:
            k = _numbered_key(n['txt'])
            if k:
                numbered_new.setdefault(k, []).append(j)
    for i, o in enumerate(unmatched_old):
        if i in matched_o:
            continue
        bucket = numbered_new.get(_numbered_key(o['txt']) or '')
        if not bucket:
            continue
        j = bucket.pop(0)
        matched_o.add(i)
        matched_n.add(j)
        _emit_modified(o, new_leftovers[j])

    # The artifact filter runs on the FINAL leftovers only, never on the pairing pool: filtering earlier removes
    # candidates that would legitimately pair into a MODIFIED entry, downgrading real changes.
    old_index = _position_index(old_items, n_old)
    new_index = _position_index(new_items, n_new)
    # Canonicalized text of every ~~removed~~ span already emitted in a MODIFIED
    # entry, so the artifact nets can tell a second report from a first one.
    struck_old = '\n'.join(_clean(m) for r in results
                           for m in _STRIKETHROUGH_RE.findall(r['content']))
    added_new = '\n'.join(_clean(m) for r in results
                          for m in re.findall(r'\*\*([^*]*)\*\*', r['content']))
    leftover_old, dropped_old = _drop_present_verbatim(
        [o for i, o in enumerate(unmatched_old) if i not in matched_o],
        n_old, new_index, old_joined, new_joined, struck_old,
    )
    leftover_new, dropped_new = _drop_present_verbatim(
        [n for j, n in enumerate(new_leftovers) if j not in matched_n],
        n_new, old_index, new_joined, old_joined, added_new,
    )
    leftover_old, merged_old = _drop_merged_cells(
        leftover_old, n_old, new_items, n_new, old_joined, new_joined)
    leftover_new, merged_new = _drop_merged_cells(
        leftover_new, n_new, old_items, n_old, new_joined, old_joined)
    filtered += merged_old + merged_new
    filtered += dropped_old + dropped_new

    lbl = page_label or 'Page'

    for o in leftover_old:
        page_tag = f' [{lbl} {o["page"]}]' if o.get('page') and page_label else ''
        results.append({
            'sec': o['sec'],
            'content': f"REMOVED{page_tag}: {o['txt']}",
            'sort_idx': o['idx'],
            'from_new': False,
            'page': o.get('page'),
            'pos': o['idx'] / n_old,
        })

    for n in leftover_new:
        page_tag = f' [{lbl} {n["page"]}]' if n.get('page') and page_label else ''
        results.append({
            'sec': n['sec'],
            'content': f"ADDED{page_tag}: {n['txt']}",
            'sort_idx': n['idx'],
            'from_new': True,
            'page': n.get('page'),
            'pos': n['idx'] / n_new,
        })

    _tag_relocated(results, new_text)
    before_pagination = len(results)
    results = [r for r in results if not _is_pagination_only(r['content'])]
    filtered += before_pagination - len(results)
    results, _absorbed = _collapse_page_repeats(results, page_label)
    results, _absorbed_sub = _collapse_systemic_substitutions(results)

    results = _sort_for_display(results, old_items, new_items)

    out: List[str] = []
    cur_sec = ''
    for r in results:
        if r['sec'] != cur_sec:
            out.append(f"\n## {r['sec']}")
            cur_sec = r['sec']
        out.append(r['content'])

    return '\n'.join(out), filtered


# --- Alternative text diff engine — section canonical diff ---

def section_canonical_diff(old_text: str, new_text: str) -> Tuple[str, int]:
    """Alternative diff engine using classic unified diff grouped by section heading.

    Uses unified_diff n=3, filters trivial pairs with is_substantive_keep_modals
    (canonicalization + number/ref check, obligation-level switches count). Context lines are removed from output —
    only ADDED / REMOVED blocks are emitted.

    :param old_text: text of the old document
    :param new_text: text of the new document
    :return: ``(diff_string, filtered_count)``
    """
    raw = list(difflib.unified_diff(old_text.splitlines(), new_text.splitlines(), n=3, lineterm=''))
    cur_sec = ''
    sec_emitted = False
    out: List[str] = []
    filtered = 0
    i = 2  # skip the --- / +++ header lines
    while i < len(raw):
        line = raw[i]
        if line.startswith('@@'):
            sec_emitted = False
            i += 1
        elif line.startswith(' '):
            bare = strip_tag(line[1:])
            if SECTION_RE.match(bare) and len(bare) < 100:
                cur_sec = bare
                sec_emitted = False
            i += 1
        elif line.startswith('-'):
            removed: List[str] = []
            added: List[str] = []
            while i < len(raw) and raw[i].startswith('-'):
                removed.append(raw[i][1:])
                i += 1
            while i < len(raw) and raw[i].startswith('+'):
                added.append(raw[i][1:])
                i += 1
            old_chunk = ' '.join(strip_tag(l) for l in removed)
            new_chunk = ' '.join(strip_tag(l) for l in added)
            if not removed or not added or is_substantive_keep_modals(old_chunk, new_chunk):
                if cur_sec and not sec_emitted:
                    out.append(f'\n## {cur_sec}')
                    sec_emitted = True
                for l in removed:
                    out.append(f'REMOVED: {l}')
                for l in added:
                    out.append(f'ADDED: {l}')
            else:
                filtered += 1
        elif line.startswith('+'):
            if cur_sec and not sec_emitted:
                out.append(f'\n## {cur_sec}')
                sec_emitted = True
            out.append(f'ADDED: {line[1:]}')
            i += 1
        else:
            i += 1
    return '\n'.join(out), filtered
