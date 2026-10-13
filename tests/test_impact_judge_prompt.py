"""Regression guard for the impact-search judge prompt's LANGUAGE contract.

Bug found 2026-08-20 while evaluating a model swap: gpt-5-4-mini answered
"reason" in English for a French document because _SYNTHESIS_SYSTEM_PROMPT
had no language instruction (same class of bug as SYSTEM_PROMPT_STANDARD,
fixed 2026-08-19). Locks in the fix.
"""

import re

from server.services.vector_search import _SYNTHESIS_SYSTEM_PROMPT

_FLAT = re.sub(r'\s+', ' ', _SYNTHESIS_SYSTEM_PROMPT).lower()


def test_judge_prompt_has_a_language_instruction():
    assert 'language' in _FLAT
    assert 'same language as the changes text' in _FLAT
