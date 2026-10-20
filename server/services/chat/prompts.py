"""The chatbot's prompts: files under ``server/prompts/chat/``, never strings in Python.

Each prompt has a key (``system_base``, ``system_as``, ``rewrite``…). ``utils/evaluation/register_chat_prompts.py``
registers the same files in the MLflow prompt registry (``<schema>.chat_<key>``, alias ``deployed``) and
``mlflow_turns`` links the registered version of every prompt used by a turn to its trace, when the file's
hash matches the registered one.
"""

import hashlib
from functools import lru_cache
from pathlib import Path
from typing import Dict, List

PROMPTS_DIR = Path(__file__).resolve().parents[2] / 'prompts' / 'chat'

FILES: Dict[str, str] = {
    'system_base': 'system/base.md',
    'system_all': 'system/all.md',
    'system_as': 'system/as.md',
    'system_is': 'system/is.md',
    'rewrite': 'rewrite.md',
    'translate_query': 'translate_query.md',
    'language_reminder': 'language_reminder.md',
}


@lru_cache(maxsize=None)
def read(key: str) -> str:
    return (PROMPTS_DIR / FILES[key]).read_text(encoding='utf-8').strip()


def digest(key: str) -> str:
    return hashlib.sha256(read(key).encode('utf-8')).hexdigest()[:16]


def registry_name(schema: str, key: str) -> str:
    return f'{schema}.chat_{key}'


def keys_for_turn(division: str) -> List[str]:
    """Prompts the answer step sends to the model: the system instructions, the language reminder. The prompt of the
    search-query step (``rewrite`` or ``translate_query``) is added by the engine once it knows which one ran."""
    return [f'system_{division.lower()}', 'system_base', 'language_reminder']
