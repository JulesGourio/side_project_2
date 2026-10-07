"""Tests for inline citation marker insertion (server-side).

The streaming layer emits, per ``url_citation`` annotation, a
``{'n': source_number, 'pos': char_offset}`` entry. ``_apply_citation_markers``
bakes ``⟦n⟧`` markers into the answer text at those offsets; the client renders
them as superscript links to source #n.
"""

from server.routers.chat import _apply_citation_markers


def test_no_citations_returns_text_unchanged():
    assert _apply_citation_markers('Hello world', []) == 'Hello world'
    assert _apply_citation_markers('', [{'n': 1, 'pos': 0}]) == ''


def test_single_marker_inserted_at_offset():
    # offset 5 = right after "Hello"
    assert _apply_citation_markers('Hello world', [{'n': 1, 'pos': 5}]) == 'Hello⟦1⟧ world'


def test_multiple_markers_keep_offsets_valid():
    text = 'Alpha beta gamma'
    # insert after "Alpha" (5) and after "beta" (10)
    out = _apply_citation_markers(text, [{'n': 1, 'pos': 5}, {'n': 2, 'pos': 10}])
    assert out == 'Alpha⟦1⟧ beta⟦2⟧ gamma'


def test_two_citations_same_offset_render_in_ascending_order():
    out = _apply_citation_markers('Fact.', [{'n': 3, 'pos': 5}, {'n': 2, 'pos': 5}])
    assert out == 'Fact.⟦2⟧⟦3⟧'


def test_duplicate_pairs_deduped():
    out = _apply_citation_markers('Fact.', [{'n': 1, 'pos': 5}, {'n': 1, 'pos': 5}])
    assert out == 'Fact.⟦1⟧'


def test_out_of_range_offset_is_clamped_not_crashing():
    assert _apply_citation_markers('abc', [{'n': 1, 'pos': 999}]) == 'abc⟦1⟧'
    assert _apply_citation_markers('abc', [{'n': 1, 'pos': -5}]) == '⟦1⟧abc'


def test_malformed_citation_entries_ignored():
    out = _apply_citation_markers('abc', [{'n': 'x', 'pos': 1}, {'pos': 2}, {'n': 1}])
    assert out == 'abc'
