"""Chat VSI — choice of answer instructions, used by the variants (not by the baseline).

``CHAT_VSI_INSTRUCTIONS`` (read at call time):

- ``ka`` (default) — the baseline prompt, unchanged: the live KA instructions
  (``server/config/chat_vsi/instructions_<div>.md``) + the baseline citation rule
  (``chat_vsi.build_prompt``);
- ``v2`` — instructions written for VSI (``server/config/chat_vsi_v2/``): the division scope
  + one compact common block. Compared with the KA text it drops what VSI can't do (metadata
  search, tool calls, archive records — filtered out before the LLM), drops the final
  sources table (the UI shows the chips), and adds the grounding rule (no general
  knowledge, no reconstructed sections) and the off-topic refusal;
- ``v3`` — the KA instructions unchanged (as ``ka``) + ``chat_vsi_v2/addendum_v3.md``: the
  grounding rules, off-topic refusal and document-type glossary of v2, nothing removed
  (v2 lost 3 golden questions vs ``ka`` on the same search, 2026-10-07).

``CHAT_VSI_LANGUAGE_REMINDER=on`` (default off, any instruction set) adds one line after the
question, at the very end of the prompt: the language rule sits at the top of the instructions,
20-25k tokens of (mostly English) passages earlier, and GPT-6 Luna lost it (answered in Bulgarian
or English to questions in other languages, pairwise eval ``luna6-versions``, 2026-10-08).
When chat.py knows the question's language (translation bridge or local detection), the line
names it ("…in French…"); otherwise it says "the language of the question above".
"""

import os
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import chat_vsi as base

_V2_DIR = Path(__file__).resolve().parent.parent / 'config' / 'chat_vsi_v2'


LANGUAGE_REMINDER = ('Reminder: write your whole answer in the language of the question above '
                     '(the question, not the documents).')
NAMED_LANGUAGE_REMINDER = ('Reminder: write your whole answer in {language}, the language of the question above '
                           '(not the language of the documents).')


def language_reminder() -> bool:
    return os.getenv('CHAT_VSI_LANGUAGE_REMINDER', 'off').strip().lower() in ('on', 'true', '1')


def instructions_set() -> str:
    v = os.getenv('CHAT_VSI_INSTRUCTIONS', 'ka').strip().lower()
    return v if v in ('ka', 'v2', 'v3') else 'ka'


@lru_cache(maxsize=None)
def load_v2_instructions(division: str) -> str:
    div = base.normalize_division(division).lower()
    scope = (_V2_DIR / f'scope_{div}.md').read_text(encoding='utf-8').strip()
    common = (_V2_DIR / 'common.md').read_text(encoding='utf-8').strip()
    return f'{scope}\n\n{common}'


@lru_cache(maxsize=None)
def load_v3_instructions(division: str) -> str:
    addendum = (_V2_DIR / 'addendum_v3.md').read_text(encoding='utf-8').strip()
    return f'{base.load_instructions(division)}\n\n{addendum}'


def system_text(division: str, which: str) -> str:
    """The system message of instruction set ``which`` (``ka``, ``v2`` or ``v3``)."""
    if which == 'v2':
        return load_v2_instructions(division)
    if which == 'v3':
        # Same layout as the baseline: instructions, then the citation rule.
        return load_v3_instructions(division) + '\n' + base.CITATION_RULE
    return base.load_instructions(division) + '\n' + base.CITATION_RULE


def with_language_reminder(messages: List[Dict[str, str]], language: Optional[str] = None) -> List[Dict[str, str]]:
    """``messages`` with the language reminder after the last user turn (a copy)."""
    line = NAMED_LANGUAGE_REMINDER.format(language=language) if language else LANGUAGE_REMINDER
    out = [dict(m) for m in messages]
    out[-1]['content'] = f'{out[-1]["content"]}\n\n{line}'
    return out


def build_prompt(division: str, conversation: List[Dict[str, str]],
                 documents: List[Tuple[str, Dict[str, Any]]],
                 answer_language: Optional[str] = None) -> List[Dict[str, str]]:
    """Same message layout as ``chat_vsi.build_prompt``; the system text follows the instruction set."""
    messages = base.build_prompt(division, conversation, documents)
    which = instructions_set()
    if which != 'ka':
        messages[0] = {'role': 'system', 'content': system_text(division, which)}
    return with_language_reminder(messages, answer_language) if language_reminder() else messages
