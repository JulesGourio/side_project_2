"""Regression guard for the single-document summary prompts' LANGUAGE contract.

Bug found 2026-08-20 while evaluating a model swap: gpt-5-4-mini/gpt-5-mini
summarized a French document in English because _SUMMARY_SYSTEM_PROMPT and
_SUMMARY_IMAGE_SYSTEM_PROMPT had no language instruction (same class of bug
as SYSTEM_PROMPT_STANDARD, fixed 2026-08-19). Locks in the fix.
"""

import re

from server.services.summarize import _SUMMARY_IMAGE_SYSTEM_PROMPT, _SUMMARY_SYSTEM_PROMPT

_FLAT_TEXT = re.sub(r'\s+', ' ', _SUMMARY_SYSTEM_PROMPT).lower()
_FLAT_IMAGE = re.sub(r'\s+', ' ', _SUMMARY_IMAGE_SYSTEM_PROMPT).lower()


def test_text_summary_prompt_has_a_language_instruction():
    assert 'language' in _FLAT_TEXT
    assert 'same language as the document' in _FLAT_TEXT


def test_image_summary_prompt_has_a_language_instruction():
    assert 'language' in _FLAT_IMAGE
    assert 'same language' in _FLAT_IMAGE
