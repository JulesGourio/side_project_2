"""Guard for the impact-search judge prompt's LANGUAGE contract.

A French document must get its "reason" in French: _SYNTHESIS_SYSTEM_PROMPT carries the language
instruction that keeps a small model from answering in English, as SYSTEM_PROMPT_STANDARD does.
"""

import re

from server.services.infra.vector_search import _JUDGE_SYSTEM_PROMPT

_FLAT = re.sub(r'\s+', ' ', _JUDGE_SYSTEM_PROMPT).lower()


def test_judge_prompt_has_a_language_instruction():
    assert 'language' in _FLAT
    assert 'same language as the changes text' in _FLAT
