"""The chatbot's prompts: files under ``server/prompts/chat/``, never strings in Python.

Each prompt has a key (``system_base``, ``system_division``, ``rewrite``…). ``utils/evaluation/register_chat_prompts.py``
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
    'system_division': 'system/division.md',
    'system_as_references': 'system/as_reference_ordering.md',
    'rewrite': 'rewrite.md',
    'translate_query': 'translate_query.md',
    'language_reminder': 'language_reminder.md',
    'language_reminder_named': 'language_reminder_named.md',
}


@lru_cache(maxsize=None)
def read(key: str) -> str:
    """The text of a prompt file, read once and stripped.

    :param key: prompt key, a key of FILES
    :return: the prompt text
    """
    return (PROMPTS_DIR / FILES[key]).read_text(encoding='utf-8').strip()


def digest(key: str) -> str:
    """Short hash of a prompt's text, to tell whether a registered version is the file's.

    :param key: prompt key
    :return: 16 hexadecimal characters of the SHA-256 of the text
    """
    return hashlib.sha256(read(key).encode('utf-8')).hexdigest()[:16]


def registry_name(schema: str, key: str) -> str:
    """Name of a prompt in the MLflow registry.

    :param schema: Unity Catalog schema of the registry, e.g. ``dev_proj.qualibot``
    :param key: prompt key
    :return: the name, ``<schema>.chat_<key>``
    """
    return f'{schema}.chat_{key}'


def keys_for_turn(division: str, language_named: bool) -> List[str]:
    """Prompts the answer step sends to the model: the system instructions, the language reminder. The prompt of the
    search-query step (``rewrite`` or ``translate_query``) is added by the engine once it knows which one ran.

    :param division: division code, any case
    :param language_named: True when the reminder names the answer language
    :return: the prompt keys of the answer step, in the order they are sent
    """
    division = division.upper()
    division_keys = ['system_all'] if division == 'ALL' else ['system_division'] + (
        ['system_as_references'] if division == 'AS' else [])
    return division_keys + ['system_base', 'language_reminder_named' if language_named else 'language_reminder']
