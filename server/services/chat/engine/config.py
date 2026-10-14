"""Engine settings read from the environment at call time, and the system instructions."""

import logging
import os
from functools import lru_cache
from typing import Any, Dict, List, Optional


from .. import llm as chat_llm, prompts

logger = logging.getLogger(__name__)

_DIVISIONS = ('ALL', 'AS', 'IS')

_DEFAULT_INDEX = 'dev_proj.qualibot.chunks_index'
_DEFAULT_LLM_ENDPOINT = 'databricks-gpt-6-luna'
_DEFAULT_ANSWER_MAX_TOKENS = 8000                     # reasoning counts inside it
_DEFAULT_REWRITE_MAX_TOKENS = 2000
_RERANK_TOP_K = 12               # reranked passages kept per query
_RAW_TOP_K = 12                  # raw HYBRID passages per query (0 = reranked passages only)
_PAIR_RERANK_TOP_K = 16          # two queries (the question and its translation) + one raw: the same 48 candidates as the rewrite
_PAIR_RAW_TOP_K = 16


# --- Settings ---

def normalize_division(division: Optional[str]) -> str:
    d = (division or 'ALL').upper()
    return d if d in _DIVISIONS else 'ALL'


def index_name() -> str:
    return os.getenv('CHAT_INDEX', _DEFAULT_INDEX).strip()


def llm_endpoint() -> str:
    return os.getenv('CHAT_LLM_ENDPOINT', _DEFAULT_LLM_ENDPOINT).strip()


def rewrite_endpoint() -> str:
    return os.getenv('CHAT_REWRITE_ENDPOINT', '').strip() or llm_endpoint()


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)) or default)
    except ValueError:
        return default


def answer_max_tokens() -> int:
    return _int_env('CHAT_ANSWER_MAX_TOKENS', _DEFAULT_ANSWER_MAX_TOKENS)


def rewrite_max_tokens() -> int:
    return _int_env('CHAT_REWRITE_MAX_TOKENS', _DEFAULT_REWRITE_MAX_TOKENS)


def rerank_top_k() -> int:
    return max(1, _int_env('CHAT_RERANK_TOP_K', _RERANK_TOP_K))


def raw_top_k() -> int:
    return max(0, _int_env('CHAT_RAW_TOP_K', _RAW_TOP_K))


_RAW_ON_ALL = ('question', 'fr', 'en')
_RAW_ON = 'fr'                   # raw search on the French rewrite only (rawfr: as good, 4 queries instead of 6)


def raw_on() -> List[str]:
    """Which queries also get a raw search: ``question`` (as asked), ``fr``, ``en`` (the
    rewrites). Fewer = fewer Vector Search queries per question."""
    names = [n.strip().lower() for n in os.getenv('CHAT_RAW_ON', _RAW_ON).replace('+', ',').split(',')]
    return [n for n in _RAW_ON_ALL if n in names]


def translate_only_enabled() -> bool:
    """Off by default: translating the question alone loses the acronym and synonym expansions the rewrite
    adds. On: the first message of a conversation, in French or English, only gets its
    translation into the other language (one short call, no rewrite, nothing expanded)."""
    return os.getenv('CHAT_TRANSLATE_ONLY', 'off').strip().lower() in ('on', 'true', '1', 'yes')


def pair_rerank_top_k() -> int:
    return max(1, _int_env('CHAT_PAIR_RERANK_TOP_K', _PAIR_RERANK_TOP_K))


def pair_raw_top_k() -> int:
    return max(0, _int_env('CHAT_PAIR_RAW_TOP_K', _PAIR_RAW_TOP_K))


def max_search_passages() -> int:
    """Cap on the merged search passages (REF and title lookups come on top); 0 = no cap."""
    return max(0, _int_env('CHAT_MAX_SEARCH_PASSAGES', 0))


def translate_max_tokens() -> int:
    return max(1, _int_env('CHAT_TRANSLATE_MAX_TOKENS', 600))


def rewrite_timeout_s() -> float:
    return float(_int_env('CHAT_REWRITE_TIMEOUT_S', 15))


def division_filter(division: str) -> Dict[str, Any]:
    """Vector Search filter of the chat's division (ALL: none)."""
    d = normalize_division(division)
    return {} if d == 'ALL' else {'division': [d]}


def settings() -> Dict[str, Any]:
    """What the engine runs with — saved with evaluation results."""
    return {'index': index_name(), 'llm': llm_endpoint(),
            'llm_fallbacks': chat_llm.answer_chain(llm_endpoint())[1:],
            'rewrite_llm': rewrite_endpoint(), 'answer_max_tokens': answer_max_tokens(),
            'rewrite_max_tokens': rewrite_max_tokens(), 'rerank_top_k': rerank_top_k(), 'raw_top_k': raw_top_k(), 'raw_on': raw_on(),
            'max_search_passages': max_search_passages(), 'translate_only': translate_only_enabled(),
            'pair_rerank_top_k': pair_rerank_top_k(), 'pair_raw_top_k': pair_raw_top_k()}


def prompt_parts(division: str) -> Dict[str, str]:
    """The files the system instructions are made of: ``division_<div>`` then ``base``."""
    div = normalize_division(division).lower()
    return {f'division_{div}': prompts.read(f'system_{div}'), 'base': prompts.read('system_base')}


@lru_cache(maxsize=None)
def load_instructions(division: str) -> str:
    """System instructions: the division's part, then the base shared by all."""
    return '\n\n'.join(prompt_parts(division).values())
