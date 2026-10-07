"""Cross-lingual bridge for chat: translate non-French/English questions to
English before hitting the Knowledge Assistant, then translate the answer back.

Why this exists
----------------
The KA's retrieval embeds the raw question text. A question asked in a third
language (Czech, German, Spanish, ...) embeds far from the (mostly French,
partly English) corpus and only matches the sparse same-language document
slice, missing most of the relevant content (confirmed empirically
2026-07-08 — a Czech question about warehouse rules retrieved a different,
narrower set of documents than the same question in French/English). Adding
a "search in French" instruction to the KA system prompt had no measurable
effect, suggesting retrieval embeds the raw conversation text outside the
model's control — so the fix has to happen before the question reaches the
KA at all.

English, not French, is used as the pivot language: a side-by-side test on
several questions (2026-07-08) showed English retrieval consistently pulling
a broader, more diverse set of relevant documents (across FR/EN/ES/BG
variants) than French, likely because the embedding model's multilingual
alignment is itself English-anchored.

Kept off by default (CHAT_TRANSLATE_BRIDGE_ENABLED) — enable only on
environments where this is being evaluated.
"""

import json
import logging
import os
from functools import lru_cache
from typing import Any, Dict, Optional

import httpx

from .streaming import supports_temperature
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

logger = logging.getLogger(__name__)

ENABLED = os.getenv('CHAT_TRANSLATE_BRIDGE_ENABLED', 'false').lower() == 'true'
TRANSLATE_ENDPOINT = os.getenv('CHAT_TRANSLATE_ENDPOINT', 'databricks-gpt-5-6-luna')
_TIMEOUT_S = 20.0

# fastText's own lid.176 language-ID model — chosen over langdetect (noisy on
# short/technical text) for the local pre-check. Sub-millisecond per call
# once loaded; ~0.26s one-time load cost at first use.
_FASTTEXT_MODEL_PATH = os.path.join(os.path.dirname(__file__), '..', 'data', 'lid.176.ftz')

# Below this, a fastText guess is treated as noise (e.g. a short REF-code-only
# message can get labelled Polish at 12% confidence) and detection falls back
# to the LLM instead of trusting the low-confidence label.
_FASTTEXT_CONFIDENCE_THRESHOLD = 0.65

# Skip the LLM round trip entirely for the two languages the corpus and the
# KA's own language-matching instructions already handle natively.
_NO_TRANSLATION_NEEDED = {'fr', 'en'}


@lru_cache(maxsize=1)
def _fasttext_model():
    import fasttext
    fasttext.FastText.eprint = lambda *_a, **_kw: None  # silence the noisy stderr warning
    return fasttext.load_model(_FASTTEXT_MODEL_PATH)


class TranslationContext:
    """Carries the detected source language across the question/answer hop."""

    __slots__ = ('lang_code', 'lang_name', 'needs_translation')

    def __init__(self, lang_code: str, lang_name: str, needs_translation: bool):
        self.lang_code = lang_code
        self.lang_name = lang_name
        self.needs_translation = needs_translation


def _fast_lang_guess(text: str) -> Optional[str]:
    """Cheap local language guess (no network call). Returns an ISO 639-1
    code, or None if detection isn't confident enough to trust (in which
    case the caller falls back to the LLM's own detection).

    Calls the fastText C++ binding directly rather than the `predict()`
    Python wrapper — that wrapper's final `np.array(probs, copy=False)`
    raises under NumPy>=2.0 (confirmed 2026-07-08), while the underlying
    binding call it wraps works fine.
    """
    single_line = ' '.join(text.split()) + '\n'
    try:
        model = _fasttext_model()
        predictions = model.f.predict(single_line, 1, 0.0, 'strict')
    except Exception as exc:
        logger.warning('translation_bridge: fasttext lang guess failed: %s', exc)
        return None
    if not predictions:
        return None
    prob, label = predictions[0]
    if prob < _FASTTEXT_CONFIDENCE_THRESHOLD:
        # A short/technical message (a bare REF code, an acronym) can get a
        # confident-looking top-1 label at a low actual probability — e.g.
        # "X12-B" scoring 12% Polish. Below threshold, let the LLM decide.
        logger.debug('translation_bridge: fasttext confidence too low (%.2f) for %r', prob, label)
        return None
    return label.replace('__label__', '')


def _answer_language_mismatch(answer: str, expected_lang: str) -> bool:
    """True if the answer confidently looks like a different language than
    the question asked for. Guards the fr/en case, which normally skips the
    LLM bridge entirely and trusts the KA's own language-matching — but the
    KA has been observed answering in the wrong one of the two anyway (chat
    session 5bd9382a, 2026-07-08: an all-English conversation got one French
    answer mid-thread with nothing in the history to explain the switch)."""
    detected = _fast_lang_guess(answer)
    return detected is not None and detected != expected_lang


_http_client: Optional[httpx.AsyncClient] = None


def _get_http_client() -> httpx.AsyncClient:
    """Module-level, reused across calls — a fresh httpx.AsyncClient per
    request would pay TCP+TLS setup on every single chat turn."""
    global _http_client
    if _http_client is None or _http_client.is_closed:
        _http_client = httpx.AsyncClient(timeout=_TIMEOUT_S)
    return _http_client


async def shutdown_http_client() -> None:
    """Called from the app's lifespan shutdown so the pooled connection
    doesn't leak past process exit."""
    global _http_client
    if _http_client is not None and not _http_client.is_closed:
        await _http_client.aclose()
    _http_client = None


def _is_retryable(exc: BaseException) -> bool:
    # 5xx / network hiccups are worth a retry; 4xx (bad auth, bad payload)
    # will just fail again — retrying those only adds latency to a chat turn.
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code >= 500
    return isinstance(exc, httpx.TransportError)


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=0.5, min=1, max=4),
    retry=retry_if_exception(_is_retryable),
    reraise=True,
)
async def _call_llm(system: str, user: str, host: str, token: str, max_tokens: int) -> str:
    url = f'{host}/serving-endpoints/{TRANSLATE_ENDPOINT}/invocations'
    headers = {'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'}
    payload: Dict[str, Any] = {
        'messages': [
            {'role': 'system', 'content': system},
            {'role': 'user', 'content': user},
        ],
        'max_tokens': max_tokens,
    }
    if supports_temperature(TRANSLATE_ENDPOINT):
        payload['temperature'] = 0.0
    client = _get_http_client()
    resp = await client.post(url, headers=headers, json=payload)
    resp.raise_for_status()
    data = resp.json()
    content = data['choices'][0]['message']['content']
    if isinstance(content, list):
        content = ''.join(b.get('text', '') for b in content if isinstance(b, dict))
    return content


_DETECT_TRANSLATE_SYSTEM = (
    'You are a translation utility. Given a user question in any language, '
    'respond with STRICT JSON only, no markdown, no explanation, in this exact '
    'shape: {"lang_name": "<name of the source language in English, e.g. Czech>", '
    '"lang_code": "<ISO 639-1 code, e.g. cs>", "en_translation": "<faithful English '
    'translation of the question, preserving technical terms, document REF codes, '
    'and any \\u27e6n\\u27e7 markers unchanged>"}.'
)


async def translate_question_to_en(question: str, host: str, token: str) -> tuple[str, TranslationContext]:
    """Return (english_question, context). No-ops (and no network call) when
    the question is already French/English by the fast local check."""
    guess = _fast_lang_guess(question)
    if guess in _NO_TRANSLATION_NEEDED:
        return question, TranslationContext(guess, guess, needs_translation=False)

    try:
        raw = await _call_llm(_DETECT_TRANSLATE_SYSTEM, question, host, token, max_tokens=400)
    except Exception as exc:
        logger.warning('translation_bridge: detect/translate LLM call failed, using original text: %s', exc)
        return question, TranslationContext('unknown', 'the original language', needs_translation=False)

    try:
        raw = raw.strip().strip('`')
        if raw.startswith('json'):
            raw = raw[4:]
        det = json.loads(raw)
    except json.JSONDecodeError as exc:
        logger.warning('translation_bridge: LLM returned non-JSON, using original text: %s', exc)
        return question, TranslationContext('unknown', 'the original language', needs_translation=False)

    lang_code = (det.get('lang_code') or '').lower()
    if lang_code in _NO_TRANSLATION_NEEDED:
        return question, TranslationContext(lang_code, det.get('lang_name', lang_code), needs_translation=False)
    en_text = det.get('en_translation') or question
    return en_text, TranslationContext(lang_code or 'unknown', det.get('lang_name', 'the original language'), needs_translation=True)


async def translate_answer_back(answer: str, ctx: TranslationContext, host: str, token: str) -> str:
    """Translate the KA's answer back to the question's language. No-op when
    the question didn't need bridging AND the answer isn't a confident
    language mismatch — see _answer_language_mismatch for why the fr/en case
    still needs a check rather than being trusted outright."""
    if not answer.strip():
        return answer
    if not ctx.needs_translation:
        # Only the confidently-detected fr/en case gets the mismatch safety
        # net — an 'unknown' lang_code (detection itself failed earlier)
        # gives _answer_language_mismatch nothing reliable to compare against.
        if ctx.lang_code not in _NO_TRANSLATION_NEEDED or not _answer_language_mismatch(answer, ctx.lang_code):
            return answer
    system = (
        f'Translate the following text into {ctx.lang_name} ({ctx.lang_code}). '
        f'Preserve Markdown formatting, document REF codes (e.g. "MI-1331-GB", '
        f'"QP-2096"), dates, and any ⟦n⟧ citation markers EXACTLY unchanged. '
        f'Output ONLY the translated text, no preamble.'
    )
    try:
        return await _call_llm(system, answer, host, token, max_tokens=4000)
    except Exception as exc:
        logger.warning('translation_bridge: answer translation failed, returning original text: %s', exc)
        return answer
