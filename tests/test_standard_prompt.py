"""Regression guard for the standard-comparison prompt's LANGUAGE contract.

Bug found 2026-08-19 on MOP_AX (003)/(004).pdf (French document): the
Change Summary (standard method) came back in English while quoting French
content, because SYSTEM_PROMPT_STANDARD — unlike SYSTEM_PROMPT_STRUCTURED —
had no instruction to match the document's language. Locks in the fix.
"""

import re

from server.services.processors._diff_engines import SYSTEM_PROMPT_STANDARD

_FLAT = re.sub(r'\s+', ' ', SYSTEM_PROMPT_STANDARD).lower()


def test_standard_prompt_has_a_language_instruction():
    assert 'language' in _FLAT


def test_standard_prompt_requires_matching_document_language():
    assert 'same language as the document being compared' in _FLAT
    assert 'do not switch to english' in _FLAT
