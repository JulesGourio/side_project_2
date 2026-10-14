"""Regression guard for the standard-comparison prompt's LANGUAGE contract.

SYSTEM_PROMPT_STANDARD must tell the model to answer in the document's language, as
SYSTEM_PROMPT_STRUCTURED does; without it the Change Summary comes back in English while quoting French content.
"""

import re

from server.services.compare.processors._diff_engines import SYSTEM_PROMPT_STANDARD

_FLAT = re.sub(r'\s+', ' ', SYSTEM_PROMPT_STANDARD).lower()


def test_standard_prompt_has_a_language_instruction():
    assert 'language' in _FLAT


def test_standard_prompt_requires_matching_document_language():
    assert 'same language as the document being compared' in _FLAT
    assert 'do not switch to english' in _FLAT
