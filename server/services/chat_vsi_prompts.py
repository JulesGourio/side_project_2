"""Chat VSI — choice of answer instructions, used by the variants (not by the baseline).

``CHAT_VSI_INSTRUCTIONS`` (read at call time):

- ``ka`` (default) — the baseline prompt, unchanged: the live KA instructions
  (``server/config/chat_vsi/instructions_<div>.md``) + the baseline citation rule
  (``chat_vsi.build_prompt``);
- ``v2`` — instructions written for VSI (``server/config/chat_vsi_v2/``): the division scope
  + one compact common block. Compared with the KA text it drops what VSI can't do (metadata
  search, tool calls, archive records — filtered out before the LLM), drops the final
  sources table (the UI shows the chips), and adds the grounding rule (no general
  knowledge, no reconstructed sections) and the off-topic refusal.
"""

import os
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Tuple

from . import chat_vsi as base

_V2_DIR = Path(__file__).resolve().parent.parent / 'config' / 'chat_vsi_v2'


def instructions_set() -> str:
    return 'v2' if os.getenv('CHAT_VSI_INSTRUCTIONS', 'ka').strip().lower() == 'v2' else 'ka'


@lru_cache(maxsize=None)
def load_v2_instructions(division: str) -> str:
    div = base.normalize_division(division).lower()
    scope = (_V2_DIR / f'scope_{div}.md').read_text(encoding='utf-8').strip()
    common = (_V2_DIR / 'common.md').read_text(encoding='utf-8').strip()
    return f'{scope}\n\n{common}'


def build_prompt(division: str, conversation: List[Dict[str, str]],
                 documents: List[Tuple[str, Dict[str, Any]]]) -> List[Dict[str, str]]:
    """Same message layout as ``chat_vsi.build_prompt``; only the system text differs with ``v2``."""
    messages = base.build_prompt(division, conversation, documents)
    if instructions_set() == 'v2':
        messages[0] = {'role': 'system', 'content': load_v2_instructions(division)}
    return messages
