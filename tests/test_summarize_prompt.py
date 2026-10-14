"""Regression guard for the single-document summary prompts' LANGUAGE contract.

_SUMMARY_SYSTEM_PROMPT and _SUMMARY_IMAGE_SYSTEM_PROMPT must carry the language instruction, so a French
document is summarized in French, as SYSTEM_PROMPT_STANDARD does.
"""

import re

from server.services.compare.summarize import _SUMMARY_IMAGE_SYSTEM_PROMPT, _SUMMARY_SYSTEM_PROMPT

_FLAT_TEXT = re.sub(r'\s+', ' ', _SUMMARY_SYSTEM_PROMPT).lower()
_FLAT_IMAGE = re.sub(r'\s+', ' ', _SUMMARY_IMAGE_SYSTEM_PROMPT).lower()


def test_text_summary_prompt_has_a_language_instruction():
    assert 'language' in _FLAT_TEXT
    assert 'same language as the document' in _FLAT_TEXT


def test_image_summary_prompt_has_a_language_instruction():
    assert 'language' in _FLAT_IMAGE
    assert 'same language' in _FLAT_IMAGE
