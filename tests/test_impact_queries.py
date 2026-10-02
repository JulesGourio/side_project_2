"""Tests for impact-search query derivation and multi-query aggregation.

Coverage:
  - changes_to_queries : structured JSON / markdown / raw blob / no-changes / capping
  - changes_to_readable: structured JSON → grouped markdown, passthrough otherwise
  - _aggregate_docs    : query_hits tracking, ranking, top excerpt collection
"""

import json

from server.services.impact_queries import changes_to_queries, changes_to_readable
from server.services.vector_search import _aggregate_docs


STRUCTURED = json.dumps([
    {'section': '4.3 Torque', 'type': 'Value changed', 'criticality': 'High',
     'before': '35 N·m', 'after': '40 N·m', 'rationale': 'J3 connector torque limit raised as part of the fastening procedure revision.'},
    {'section': '2.1 References', 'type': 'Reference updated', 'criticality': 'Medium',
     'before': 'NAS 410 Rev 2', 'after': 'NAS 410 Rev 3', 'rationale': ''},
    {'section': 'TOC', 'type': 'Editorial/Structural', 'criticality': 'Low',
     'before': 'section 5', 'after': 'section 6', 'rationale': ''},
    {'section': '6.2 Inspection', 'type': 'Requirement added', 'criticality': 'Low',
     'before': '--', 'after': 'visual inspection before assembly', 'rationale': ''},
])

MARKDOWN = """\
## 4.3 Torque

- Torque limit changed from **35 N·m** to **40 N·m**

## 2.1 References

- NAS 410 revision updated to Rev 3
"""


# ---------------------------------------------------------------------------
# changes_to_queries
# ---------------------------------------------------------------------------

def test_structured_json_one_query_per_change():
    out = changes_to_queries(STRUCTURED)
    assert out['source'] == 'structured'
    # Editorial/Structural row dropped → 3 substantive changes
    assert out['total_changes'] == 3
    assert len(out['queries']) == 3
    assert not any('{' in q for q in out['queries'])  # no JSON syntax leaks


def test_structured_high_criticality_first():
    out = changes_to_queries(STRUCTURED)
    assert '35 N·m' in out['queries'][0]  # High row before Medium/Low


def test_structured_added_removed_rendering():
    out = changes_to_queries(STRUCTURED)
    added = [q for q in out['queries'] if 'added:' in q]
    assert added and 'visual inspection' in added[0]


def test_structured_editorial_only_falls_back_to_all_rows():
    text = json.dumps([{'section': 'TOC', 'type': 'Editorial/Structural',
                        'criticality': 'Low', 'before': 'a', 'after': 'b', 'rationale': ''}])
    out = changes_to_queries(text)
    assert out['total_changes'] == 1


def test_structured_empty_array_means_no_changes():
    out = changes_to_queries('[]')
    assert out['queries'] == []
    assert out['total_changes'] == 0


def test_markdown_one_query_per_section():
    out = changes_to_queries(MARKDOWN)
    assert out['source'] == 'markdown'
    assert len(out['queries']) == 2
    assert out['queries'][0].startswith('4.3 Torque:')
    assert '**' not in out['queries'][0]  # markup stripped


def test_markdown_no_changes_detected():
    out = changes_to_queries('**No significant changes detected.**')
    assert out['queries'] == []


def test_raw_blob_single_query():
    out = changes_to_queries('The torque changed from 35 to 40 N·m.')
    assert out['source'] == 'raw'
    assert len(out['queries']) == 1


def test_capping_groups_preserve_all_changes():
    rows = [{'section': f'S{i}', 'type': 'Value changed', 'criticality': 'Medium',
             'before': f'{i}', 'after': f'{i + 1}', 'rationale': ''} for i in range(20)]
    out = changes_to_queries(json.dumps(rows), max_queries=8)
    assert len(out['queries']) <= 8
    assert out['total_changes'] == 20
    combined = '\n'.join(out['queries'])
    assert all(f'S{i}' in combined for i in range(20))  # nothing silently dropped


def test_empty_input():
    assert changes_to_queries('')['queries'] == []


# ---------------------------------------------------------------------------
# changes_to_readable
# ---------------------------------------------------------------------------

def test_readable_groups_by_section():
    text = changes_to_readable(STRUCTURED)
    assert '## 4.3 Torque' in text
    assert '35 N·m → 40 N·m' in text
    assert '{' not in text


def test_readable_passthrough_for_markdown():
    assert changes_to_readable(MARKDOWN) == MARKDOWN.strip()


# ---------------------------------------------------------------------------
# _aggregate_docs — multi-query behaviour
# ---------------------------------------------------------------------------

def _chunk(iddoc, ref, score, qidx=None, text='chunk text', chunk_id='c1'):
    rec = {'IDDOC': iddoc, 'REF': ref, 'division': 'AS', 'url': '',
           'semantic_headers': '', 'chunk_text': text, 'chunk_id': chunk_id, 'score': score}
    if qidx is not None:
        rec['_qidx'] = qidx
    return rec


def test_aggregate_counts_distinct_query_hits():
    chunks = [
        _chunk('D1', 'REF-1', 0.9, qidx=0),
        _chunk('D1', 'REF-1', 0.8, qidx=1),
        _chunk('D1', 'REF-1', 0.7, qidx=1),  # same query twice → still 2 hits
        _chunk('D2', 'REF-2', 0.95, qidx=0),
    ]
    docs = _aggregate_docs(chunks, max_docs=10)
    by_ref = {d['ref']: d for d in docs}
    assert by_ref['REF-1']['query_hits'] == 2
    assert by_ref['REF-2']['query_hits'] == 1


def test_aggregate_ranks_multi_hit_doc_above_single_high_score():
    chunks = [
        _chunk('D1', 'REF-1', 0.6, qidx=0),
        _chunk('D1', 'REF-1', 0.6, qidx=1),
        _chunk('D2', 'REF-2', 0.99, qidx=0),
    ]
    docs = _aggregate_docs(chunks, max_docs=10)
    assert docs[0]['ref'] == 'REF-1'  # 2 query hits beat one lucky 0.99


def test_aggregate_keeps_top_excerpts_for_judge():
    chunks = [
        _chunk('D1', 'REF-1', 0.9, qidx=0, text='best'),
        _chunk('D1', 'REF-1', 0.8, qidx=0, text='second'),
        _chunk('D1', 'REF-1', 0.7, qidx=0, text='third'),
        _chunk('D1', 'REF-1', 0.6, qidx=0, text='fourth'),
    ]
    docs = _aggregate_docs(chunks, max_docs=10)
    assert docs[0]['_top_excerpts'] == [
        ('', 'best'), ('', 'second'), ('', 'third'), ('', 'fourth'),
    ]


def test_aggregate_untagged_chunks_default_to_one_hit():
    docs = _aggregate_docs([_chunk('D1', 'REF-1', 0.9)], max_docs=10)
    assert docs[0]['query_hits'] == 1
