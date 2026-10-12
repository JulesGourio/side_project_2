"""Tests for the DocCompare analysis pipeline: extraction filters, diff engine,
and chunked (map-reduce) LLM analysis.

Coverage:
  - _is_page_artifact          : pagination noise filter (PDF)
  - paragraph_semantic_diff    : MODIFIED/ADDED/REMOVED detection, modal filtering
  - split_messages_for_chunking: threshold, section boundaries, image placement
  - extract_json_objects       : brace scanning incl. braces inside strings
"""

from server.services.chunked_analysis import (
    _split_body,
    extract_json_objects,
    split_messages_for_chunking,
)
from server.services.processors._diff_engines import paragraph_semantic_diff
from server.services.processors.pdf import _is_page_artifact


# --- PDF pagination artifacts ---

def test_page_artifacts_detected():
    for t in ('12', ' 12 ', '- 12 -', '12/70', 'Page 12', 'Page 12 of 70',
              'page 3 sur 45', 'PAGE 7 DE 20'):
        assert _is_page_artifact(t), t


def test_real_content_not_flagged_as_artifact():
    for t in ('12 mm torque', 'Page layout requirements', '4.3 Torque values',
              'Chapter 12 describes the process', 'NAS 410'):
        assert not _is_page_artifact(t), t


# --- paragraph_semantic_diff — behaviour guard for the O(n²) pre-filter ---

OLD_DOC = """\
1. GENERAL REQUIREMENTS [Page 1]
The operator shall verify the torque value of 35 N·m at each installation step. [Page 1]
Inspection must be performed according to NAS 410 Rev 2. [Page 1]
This paragraph is completely unchanged between the two revisions of the document. [Page 2]
This requirement will be deleted in the new revision of this document entirely. [Page 2]
"""

NEW_DOC = """\
1. GENERAL REQUIREMENTS [Page 1]
The operator shall verify the torque value of 40 N·m at each installation step. [Page 1]
Inspection must be performed according to NAS 410 Rev 3. [Page 1]
This paragraph is completely unchanged between the two revisions of the document. [Page 2]
A brand new requirement about protective gloves is introduced in this revision. [Page 2]
"""


def test_diff_detects_value_change_as_modified():
    diff, _ = paragraph_semantic_diff(OLD_DOC, NEW_DOC, page_label='Page')
    assert 'MODIFIED' in diff
    assert '~~35~~' in diff and '**40**' in diff


def test_diff_detects_reference_revision_change():
    diff, _ = paragraph_semantic_diff(OLD_DOC, NEW_DOC, page_label='Page')
    assert '~~2.~~' in diff or ('Rev' in diff and '**3' in diff.replace('.', ''))


def test_diff_detects_added_and_removed():
    diff, _ = paragraph_semantic_diff(OLD_DOC, NEW_DOC, page_label='Page')
    assert 'REMOVED' in diff and 'deleted in the new revision' in diff
    assert 'ADDED' in diff and 'protective gloves' in diff


def test_diff_ignores_unchanged_paragraphs():
    diff, _ = paragraph_semantic_diff(OLD_DOC, NEW_DOC, page_label='Page')
    assert 'completely unchanged' not in diff


def test_diff_filters_within_group_modal_swap():
    old = 'The supplier shall provide the certificate of conformity for every delivered batch. [Page 1]'
    new = 'The supplier must provide the certificate of conformity for every delivered batch. [Page 1]'
    diff, filtered = paragraph_semantic_diff(old, new, page_label='Page')
    assert 'MODIFIED' not in diff
    assert filtered == 1


def test_diff_reports_cross_group_modal_swap():
    old = 'The supplier may provide the certificate of conformity for every delivered batch. [Page 1]'
    new = 'The supplier shall provide the certificate of conformity for every delivered batch. [Page 1]'
    diff, _ = paragraph_semantic_diff(old, new, page_label='Page')
    assert 'MODIFIED' in diff


def test_diff_pairs_heavily_rewritten_table_row_by_row_key():
    # Same table-row label, content rewritten well below the 0.55 similarity
    # gate — must still pair as MODIFIED (second-chance row-key pass).
    old = 'Torque table: apply 35 Nm on connector J3 [Page 6, Para 17]'
    new = ('Torque table: apply 40 Nm on connector J3 then verify with a calibrated gauge '
           'and record the value on the routing card after each assembly step [Page 6, Para 22]')
    diff, _ = paragraph_semantic_diff(old, new, page_label='Page')
    assert 'MODIFIED' in diff
    assert 'REMOVED' not in diff and 'ADDED' not in diff
    assert '~~35~~' in diff and '**40**' in diff


def test_diff_does_not_pair_different_row_keys():
    old = 'Torque table: apply 35 Nm on connector J3 [Page 6, Para 17]'
    new = 'Cleaning step: wipe the surface with isopropyl alcohol before bonding operations [Page 6, Para 22]'
    diff, _ = paragraph_semantic_diff(old, new, page_label='Page')
    assert 'REMOVED' in diff and 'ADDED' in diff
    assert 'MODIFIED' not in diff


def test_diff_collapses_per_page_boilerplate():
    # A title-block stamp added on every page must become ONE annotated entry,
    # not one ADDED per page — measured as 85-95% of all entries on real
    # wiring-diagram revisions (utils/compare_eval, 2026-07-17).
    def page(i, stamp):
        lines = [f'Wire {i}-{k} routed from connector J{i} to terminal T{k} [Page {i}]'
                 for k in range(30)]
        if stamp:
            lines.append(f'ENV UPDATED [Page {i}]')
        return '\n'.join(lines)

    old = '\n'.join(page(i, stamp=False) for i in range(1, 6))
    new = '\n'.join(page(i, stamp=True) for i in range(1, 6))
    diff, _ = paragraph_semantic_diff(old, new, page_label='Page')
    assert diff.count('ENV UPDATED [Page') == 0  # no per-page entries left
    assert 'repeated on 5 pages' in diff
    assert 'Page 1-5' in diff


def test_diff_keeps_below_threshold_repeats_expanded():
    # Two occurrences only (< _BOILERPLATE_MIN_PAGES): nothing proves it is a
    # page artefact - keep both entries untouched. Pages carry enough filler for
    # the two occurrences to sit further apart than _dedup_repeated_headers'
    # window, which would otherwise absorb the second one and make this test
    # pass for the wrong reason (it did until 2026-08-17: the old assertion
    # counted the '## NEW NOTE' section heading plus a single surviving entry).
    def page(i, stamp):
        rows = [f'Wiring for connector J{i} pin {k} routed to terminal T{k} [Page {i}]'
                for k in range(30)]
        if stamp:
            rows.append(f'NEW NOTE [Page {i}]')
        return '\n'.join(rows)

    old = '\n'.join(page(i, stamp=False) for i in (1, 2))
    new = '\n'.join(page(i, stamp=True) for i in (1, 2))
    diff, _ = paragraph_semantic_diff(old, new, page_label='Page')
    added = [ln for ln in diff.splitlines() if ln.startswith('ADDED') and 'NEW NOTE' in ln]
    assert len(added) == 2, diff
    assert 'repeated on' not in diff


# ---------------------------------------------------------------------------
# paragraph_semantic_diff — section order alignment across recased/renumbered
# headings (a revision restyling ALL CAPS headings to Title Case must not
# scatter that section's entries away from their physical page order)
# ---------------------------------------------------------------------------

def test_section_order_aligns_recased_headings_across_revisions():
    old = (
        '1. ALPHA [Page 1]\n'
        'Alpha content describing the initial configuration steps for this section entirely unchanged. [Page 1]\n'
        '2. BETA [Page 2]\n'
        'Torque value must not exceed 35 Nm for this fastener as previously specified in detail. [Page 2]\n'
    )
    new = (
        '1. Alpha [Page 1]\n'
        'Alpha content describing the initial configuration steps for this section entirely unchanged. [Page 1]\n'
        'A brand new clarifying note about calibration tolerances is introduced here now for reference. [Page 1]\n'
        '2. BETA [Page 2]\n'
        'Torque value must not exceed 40 Nm for this fastener as previously specified in detail. [Page 2]\n'
    )
    diff, _ = paragraph_semantic_diff(old, new, page_label='Page')
    headers = [l for l in diff.splitlines() if l.startswith('## ')]
    # The ADDED note sits under the recased "1. Alpha" heading (section 1,
    # page 1) and must sort there — not after "2. BETA" (page 2) just because
    # "1. Alpha" != "1. ALPHA" as a literal string.
    assert headers == ['## 1. Alpha', '## 2. BETA']


def test_section_order_does_not_merge_distant_identical_labels():
    # A numbered workflow-step label ("2 Archive the report") recurs verbatim
    # in two unrelated tables (common in flowchart/table-heavy procedures,
    # where SECTION_RE picks up "N <Capitalized text>" step labels as
    # headings). A change living in the SECOND table must not be pulled up to
    # sort next to the FIRST table just because both reuse the same label
    # string — regression found on a real document full of recurring
    # numbered flowchart steps (2026-07-29): a plain sections_order[label]=idx
    # dict can only remember one order per label text, so the second,
    # unrelated occurrence silently inherited the first one's position.
    old = (
        '1. TABLE ONE [Page 1]\n'
        'Process one overview text describing the initial workflow steps in general. [Page 1]\n'
        '1 Do preparation work for process one entirely as specified. [Page 1]\n'
        '2 Archive the report [Page 1]\n'
        'Old detail line for table one step two left completely unchanged here. [Page 1]\n'
        '3 Do closing work for process one entirely as specified. [Page 1]\n'
        '9. TABLE TWO [Page 5]\n'
        'Process two overview text describing the initial workflow steps in general. [Page 5]\n'
        '1 Do preparation work for process two entirely as specified. [Page 5]\n'
        '2 Archive the report [Page 5]\n'
        'Old detail line for table two step two about to be revised here. [Page 5]\n'
        '3 Do closing work for process two entirely as specified. [Page 5]\n'
    )
    new = old.replace(
        'Process two overview text describing the initial workflow steps in general. [Page 5]',
        'Process two overview text now updated for this revision in general. [Page 5]',
    ).replace(
        'Old detail line for table two step two about to be revised here. [Page 5]',
        'New detail line for table two step two now fully revised here today. [Page 5]',
    )
    diff, _ = paragraph_semantic_diff(old, new, page_label='Page')
    headers = [l for l in diff.splitlines() if l.startswith('## ')]
    assert headers.index('## 9. TABLE TWO') < headers.index('## 2 Archive the report')


def test_running_header_does_not_swallow_real_sections():
    # A standard's title stamped on every page (a running header/footer) must
    # never register as a section — every real clause under it would
    # otherwise silently inherit that single recurring, meaningless label
    # instead of its own heading. Also covers the companion fix: a heading
    # with real-world punctuation (en dash, parentheses) must still be
    # recognized as its own section — regression found on NAS410, where
    # 'ANNEX C – NATIONAL AEROSPACE ... (NANDTB)' never matched the old
    # ALL-CAPS-only pattern and fell back to whatever running header preceded
    # it (2026-07-29).
    old = (
        'STANDARD TITLE [Page 1]\n'
        '1. FIRST CLAUSE [Page 1]\n'
        'Original wording describing the first clause requirement in complete detail here. [Page 1]\n'
        'STANDARD TITLE [Page 2]\n'
        '2. SECOND CLAUSE [Page 2]\n'
        'Original wording describing the second clause requirement in complete detail here. [Page 2]\n'
        'STANDARD TITLE [Page 3]\n'
        'ANNEX A – SPECIAL PROVISIONS (SUPPLEMENTARY) [Page 3]\n'
        'Original wording describing the annex requirement in complete detail for this case. [Page 3]\n'
    )
    new = old.replace(
        'Original wording describing the second clause requirement in complete detail here. [Page 2]',
        'Revised wording describing the second clause requirement in complete detail today. [Page 2]',
    ).replace(
        'Original wording describing the annex requirement in complete detail for this case. [Page 3]',
        'Revised wording describing the annex requirement in complete detail for this new case. [Page 3]',
    )
    diff, _ = paragraph_semantic_diff(old, new, page_label='Page')
    headers = [l for l in diff.splitlines() if l.startswith('## ')]
    assert '## STANDARD TITLE' not in headers
    assert headers == ['## 2. SECOND CLAUSE', '## ANNEX A – SPECIAL PROVISIONS (SUPPLEMENTARY)']


# ---------------------------------------------------------------------------
# paragraph_semantic_diff — systemic substitution collapse (a document-wide
# restyle like a bullet-marker swap or a renamed term must not multiply into
# one MODIFIED entry per occurrence)
# ---------------------------------------------------------------------------

_ANNEX_SENTENCES = [
    'ANNEX B defines the credit system for Level 3 personnel. [Page {p}]',
    'See ANNEX B for the recertification credit system details. [Page {p}]',
    'The ANNEX covers scope of recertification for Level 3 staff. [Page {p}]',
    'Refer to ANNEX C for outside agency board procedures. [Page {p}]',
    'This ANNEX lists provisions for outside agency qualification. [Page {p}]',
]


def _annex_doc(n: int, use_appendix: bool) -> str:
    word = 'APPENDIX' if use_appendix else 'ANNEX'
    lines = [
        s.format(p=i + 1).replace('ANNEX', word) for i, s in enumerate(_ANNEX_SENTENCES[:n])
    ]
    return '\n'.join(lines)


def test_diff_collapses_systemic_substitution():
    old = _annex_doc(5, use_appendix=False)
    new = _annex_doc(5, use_appendix=True)
    diff, _ = paragraph_semantic_diff(old, new, page_label='Page')
    assert diff.count('MODIFIED') == 1
    assert 'same substitution repeated in 4 more entries' in diff
    assert "'annex' -> 'appendix'" in diff


def test_diff_keeps_below_threshold_substitution_expanded():
    # Only 4 occurrences (< _SYSTEMIC_SUB_MIN default of 5): keep all entries.
    old = _annex_doc(4, use_appendix=False)
    new = _annex_doc(4, use_appendix=True)
    diff, _ = paragraph_semantic_diff(old, new, page_label='Page')
    assert diff.count('MODIFIED') == 4
    assert 'same substitution repeated' not in diff


def test_diff_never_collapses_numeric_substitution():
    # Same value swap repeated across many unrelated rows must stay expanded —
    # a value that differs per row (e.g. a pin number) is never boilerplate.
    def row(i):
        return (f'Pin count set to 10 for connector assembly configuration number {i} used broadly. '
                 f'[Page {i}]')

    old = '\n'.join(row(i) for i in range(1, 7))
    new = '\n'.join(row(i).replace('set to 10', 'set to 20') for i in range(1, 7))
    diff, _ = paragraph_semantic_diff(old, new, page_label='Page')
    assert diff.count('MODIFIED') == 6
    assert 'same substitution repeated' not in diff


def test_diff_does_not_collapse_entry_mixing_systemic_and_unique_change():
    # A sixth entry reuses the recurring ANNEX->APPENDIX swap but also carries
    # its own one-off wording change — it must be kept in full, not absorbed
    # into the systemic-change summary, so the unique change stays visible.
    old = _annex_doc(5, use_appendix=False)
    new = _annex_doc(5, use_appendix=True)
    old += '\nANNEX D also mentions extra unique wording found nowhere else in this document. [Page 6]\n'
    new += '\nAPPENDIX D also mentions completely different phrasing found nowhere else in this document. [Page 6]\n'
    diff, _ = paragraph_semantic_diff(old, new, page_label='Page')
    assert '~~extra unique wording~~' in diff and '**completely different phrasing**' in diff
    assert 'same substitution repeated in 4 more entries' in diff  # unaffected by the 6th entry


# --- Chunked analysis — splitting ---

def _make_messages(body: str, n_images: int = 0):
    text = (
        'Global paragraph alignment: 3 trivial lines filtered.\n\n'
        f'--- TEXT CHANGES ---{body}\n\n--- VISUAL CHANGES ---'
    )
    blocks = [{'type': 'text', 'text': text}]
    for i in range(n_images):
        blocks.append({'type': 'image_url', 'image_url': {'url': f'data:image/jpeg;base64,img{i}'}})
    return [
        {'role': 'system', 'content': 'SYSTEM PROMPT'},
        {'role': 'user', 'content': blocks},
    ]


def _section(name: str, size: int) -> str:
    line = f'MODIFIED: some change in {name} '
    return f'\n## {name}\n' + (line * (size // len(line) + 1))[:size]


def test_no_chunking_below_threshold():
    messages = _make_messages(_section('A', 1000))
    assert split_messages_for_chunking(messages, threshold_chars=60000, chunk_chars=45000) is None


def test_chunking_splits_at_section_boundaries():
    body = _section('Alpha', 3000) + _section('Bravo', 3000) + _section('Charlie', 3000)
    parts = _split_body(body, chunk_chars=6500)
    assert len(parts) == 2
    assert parts[0].startswith('## Alpha')
    assert parts[1].startswith('## Charlie')
    assert sum('MODIFIED' in p for p in parts) == 2  # no content lost
    assert ''.join(parts).count('## ') == 3


def test_chunking_hard_splits_oversized_section():
    body = _section('Huge', 10000)
    parts = _split_body(body, chunk_chars=4000)
    assert len(parts) >= 3
    assert all(len(p) <= 4000 for p in parts)


def test_chunked_messages_keep_images_on_last_part_only():
    body = _section('Alpha', 5000) + _section('Bravo', 5000)
    messages = _make_messages(body, n_images=2)
    parts = split_messages_for_chunking(messages, threshold_chars=8000, chunk_chars=6000)
    assert parts is not None and len(parts) == 2
    first_blocks = parts[0][1]['content']
    last_blocks = parts[1][1]['content']
    assert sum(b['type'] == 'image_url' for b in first_blocks) == 0
    assert sum(b['type'] == 'image_url' for b in last_blocks) == 2
    # every part keeps the system prompt and the part banner
    assert parts[0][0]['content'] == 'SYSTEM PROMPT'
    assert 'part 1/2' in first_blocks[0]['text']
    assert 'part 2/2' in last_blocks[0]['text']


def test_chunking_ignores_unexpected_message_shapes():
    assert split_messages_for_chunking([{'role': 'user', 'content': 'plain string'}], 10, 10) is None
    assert split_messages_for_chunking([{'role': 'system', 'content': 'x'}], 10, 10) is None


def test_chunked_stream_parallel_parts_merge_in_order(monkeypatch):
    """Parts run concurrently; output must stay in document order with usage
    summed into one event and a single valid JSON array."""
    import asyncio
    import json

    from server.services import chunked_analysis as ca

    async def fake_stream(host, token, ep, messages, *args, **kwargs):
        i = int(messages[-1]['content'][0]['text'])
        await asyncio.sleep(0.03 * (3 - i))  # later parts complete FIRST
        delta = json.dumps({'type': 'response.output_text.delta', 'delta': f'[{{"part": {i}}}]'})
        usage = json.dumps({'type': 'usage', 'input_tokens': 10, 'output_tokens': 5,
                            'thinking_tokens': 0, 'total_tokens': 15, 'cost_eur': 0.01})
        yield f'data: {delta}\n\n'
        yield f'data: {usage}\n\n'
        yield 'data: [DONE]\n\n'

    monkeypatch.setattr(ca, 'stream_analysis', fake_stream)
    parts = [[{'role': 'user', 'content': [{'type': 'text', 'text': str(i)}]}] for i in range(3)]

    async def collect():
        text, usage = '', None
        async for chunk in ca.stream_analysis_chunked('h', 't', 'ep', parts, 100, 0, 0.0, structured=True):
            if not chunk.startswith('data: ') or '[DONE]' in chunk:
                continue
            ev = json.loads(chunk[6:])
            if ev.get('type') == 'response.output_text.delta':
                text += ev['delta']
            elif ev.get('type') == 'usage':
                usage = ev
        return text, usage

    text, usage = asyncio.run(collect())
    rows = json.loads(text)
    assert [r['part'] for r in rows] == [0, 1, 2]  # document order preserved
    assert usage['total_tokens'] == 45 and usage['input_tokens'] == 30


# --- extract_json_objects ---

def test_extract_objects_from_clean_array():
    objs = extract_json_objects('[{"a": 1}, {"b": 2}]')
    assert objs == [{'a': 1}, {'b': 2}]


def test_extract_objects_with_braces_inside_strings():
    text = '[{"before": "value {x} and }", "after": "ok"}, {"a": "{"}]'
    objs = extract_json_objects(text)
    assert len(objs) == 2
    assert objs[0]['before'] == 'value {x} and }'


def test_extract_objects_from_fenced_output():
    text = '```json\n[{"a": 1}]\n```'
    assert extract_json_objects(text) == [{'a': 1}]


def test_extract_objects_skips_malformed():
    text = '[{"a": 1}, {broken}, {"b": 2}]'
    assert extract_json_objects(text) == [{'a': 1}, {'b': 2}]


def test_extract_objects_empty_output():
    assert extract_json_objects('[]') == []
    assert extract_json_objects('No changes detected.') == []
