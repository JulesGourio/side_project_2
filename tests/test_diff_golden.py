"""Golden-output regression tests for the text and image diff engines.

Inputs come from tests/diff_corpus.py; the expected outputs must stay byte-identical.
"""

import json
from pathlib import Path

from server.services.compare.processors._diff_engines import (
    dhash,
    hash16,
    image_diff_blocks_dual,
    image_diff_pairs,
    paragraph_semantic_diff,
    section_canonical_diff,
)
from tests.diff_corpus import snapshot

GOLDEN = Path(__file__).parent / 'golden' / 'diff_cases.json'


def test_diff_engines_match_golden_outputs():
    expected = json.loads(GOLDEN.read_text(encoding='utf-8'))
    actual = snapshot(paragraph_semantic_diff, section_canonical_diff, image_diff_blocks_dual,
                      image_diff_pairs, dhash, hash16)
    assert actual.keys() == expected.keys()
    mismatched = [k for k in expected if actual[k] != expected[k]]
    assert not mismatched, f'diff output changed for: {mismatched}'
