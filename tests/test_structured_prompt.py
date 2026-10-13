"""Regression guard for the structured-comparison prompt's RATIONALE contract.

Locks in the 2026-08-19 rewrite: rationale is a short summary of what
changed, never an instruction telling the reader what to do. See the
RATIONALE section of SYSTEM_PROMPT_STRUCTURED in _diff_engines.py.
"""

import re

from server.services.processors._diff_engines import SYSTEM_PROMPT_STRUCTURED

_FLAT = re.sub(r'\s+', ' ', SYSTEM_PROMPT_STRUCTURED).lower()


def test_rationale_no_longer_demands_an_imperative():
    assert 'starting with a verb in the imperative' not in _FLAT
    assert 'say what a reader must do' not in _FLAT
    assert 'one imperative sentence on what to do' not in _FLAT


def test_rationale_contract_is_a_summary():
    assert 'never start with an imperative verb' in _FLAT
    assert 'this is a summary, not an instruction' in _FLAT


def test_rationale_still_empties_for_trivial_cosmetic_changes():
    assert 'trivial or cosmetic' in _FLAT
