"""Search queries of a turn: bilingual rewrite, or translation of an opening message."""

import logging
import re
from typing import Any, Dict, List, Tuple

from server.services.chat import llm as chat_llm, prompts
from server.services.chat import translation_bridge
from server.services.chat.turn_log import TurnLog
from server.services.chat.engine.config import (
    NOISE_PHRASES, llm_endpoint, rewrite_endpoint, rewrite_max_tokens, rewrite_timeout_s, translate_max_tokens,
    translate_only_enabled,
)

logger = logging.getLogger(__name__)

_TRANSLATE_NAMES = {'fr': 'French', 'en': 'English'}

REWRITE_PROMPT = prompts.read('rewrite')


def opening_language(conversation: List[Dict[str, str]], question: str) -> str | None:
    """'fr' or 'en' when ``question`` opens the conversation and is confidently in that language (the translation
    path applies), else None: follow-ups, other or unsure languages keep the bilingual rewrite."""
    if not translate_only_enabled() or len(conversation) > 1:
        return None
    code = translation_bridge.fast_lang_guess(question)
    return code if code in _TRANSLATE_NAMES else None


def split_bilingual(text: str) -> Tuple[str, str]:
    """(French query, English query) from the rewrite; a reply without the ``FR:``/``EN:``
    labels is taken as the French query."""
    fr, en = '', ''
    for line in (text or '').splitlines():
        head, _, rest = line.strip().partition(':')
        if head.strip().upper() == 'FR' and rest.strip():
            fr = rest.strip()
        elif head.strip().upper() == 'EN' and rest.strip():
            en = rest.strip()
    if not fr and not en:
        fr = (text or '').strip()
    return fr, en


def strip_added_noise(query: str, conversation_text: str) -> str:
    """``query`` without company / document-type filler the conversation never used."""
    out = query
    for pattern in NOISE_PHRASES:
        if not pattern.search(conversation_text):
            out = pattern.sub(' ', out)
    out = re.sub(r'\s+', ' ', out).strip(' ,;:-')
    return out or query


async def translate_query(host: str, token: str, question: str, target: str,
                          log: TurnLog | None = None) -> str:
    """``question`` translated into ``target`` ('fr' or 'en'); raises when every model failed after its retries."""
    log = log if log is not None else TurnLog()
    system = prompts.read('translate_query').replace('{language}', _TRANSLATE_NAMES[target])
    chain = chat_llm.rewrite_chain(rewrite_endpoint(), llm_endpoint())
    info: Dict[str, Any] = {}
    try:
        text, used = await chat_llm.complete_patiently(host, token, chain, [{'role': 'system', 'content': system},
                                                                   {'role': 'user', 'content': question}],
                                             translate_max_tokens(), rewrite_timeout_s(), info=info)
    except Exception as exc:
        logger.warning('chat: query translation failed after its retries (%s)', exc)
        log.data.update({'rewrite_ok': False, 'rewrite_attempts': info.get('attempts')})
        log.issue('rewrite_failed', 'rewrite', f'every model failed after the retries: {exc}',
                  error_type=type(exc).__name__, http_status=getattr(exc, 'status', 0), upstream=','.join(chain),
                  context={'attempts': info.get('attempts')}, exc=exc)
        raise
    usage = info.get('usage') or {}
    log.data.update({'rewrite_ok': True, 'rewrite_endpoint': used,
                     'rewrite_input_tokens': usage.get('input_tokens'),
                     'rewrite_output_tokens': usage.get('output_tokens'),
                     'rewrite_cost_eur': usage.get('cost_eur')})
    if used != rewrite_endpoint():
        log.issue('rewrite_fallback', 'rewrite', f'translated by {used} instead of {rewrite_endpoint()}',
                  upstream=used, context={'expected': rewrite_endpoint(), 'attempts': info.get('attempts')})
    return ' '.join(text.split()).strip(' "«»')


async def rewrite_queries(host: str, token: str, conversation: List[Dict[str, str]],
                          log: TurnLog | None = None) -> Tuple[str, str]:
    """(French query, English query) for the last question; raises when every rewrite model failed after its retries."""
    log = log if log is not None else TurnLog()
    transcript = '\n'.join(f"{m['role']}: {m['content']}" for m in conversation)
    messages = [{'role': 'system', 'content': REWRITE_PROMPT}, {'role': 'user', 'content': transcript}]
    chain = chat_llm.rewrite_chain(rewrite_endpoint(), llm_endpoint())
    info: Dict[str, Any] = {}
    try:
        text, used = await chat_llm.complete_patiently(host, token, chain, messages, rewrite_max_tokens(),
                                                 rewrite_timeout_s(), info=info)
    except Exception as exc:
        logger.warning('chat: search query rewrite failed after its retries (%s)', exc)
        log.data.update({'rewrite_ok': False, 'rewrite_attempts': info.get('attempts')})
        log.issue('rewrite_failed', 'rewrite', f'every model failed after the retries: {exc}',
                  error_type=type(exc).__name__, http_status=getattr(exc, 'status', 0), upstream=','.join(chain),
                  context={'attempts': info.get('attempts')}, exc=exc)
        raise
    usage = info.get('usage') or {}
    log.data.update({'rewrite_ok': True, 'rewrite_endpoint': used,
                     'rewrite_input_tokens': usage.get('input_tokens'),
                     'rewrite_output_tokens': usage.get('output_tokens'),
                     'rewrite_cost_eur': usage.get('cost_eur')})
    if used != rewrite_endpoint():
        logger.warning('chat: search query rewritten by fallback %s', used)
        log.issue('rewrite_fallback', 'rewrite', f'rewritten by {used} instead of {rewrite_endpoint()}',
                  upstream=used, context={'expected': rewrite_endpoint(), 'attempts': info.get('attempts')})
    fr, en = split_bilingual(text)
    said = ' '.join(m['content'] for m in conversation)
    return strip_added_noise(fr, said), strip_added_noise(en, said)
