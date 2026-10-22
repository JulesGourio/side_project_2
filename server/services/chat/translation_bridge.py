"""Cross-lingual bridge for chat: translate non-French/English questions to
English before the document search, then translate the answer back.

Why this exists
----------------
The search embeds the question text. A question asked in a third
language (Czech, German, Spanish, ...) embeds far from the (mostly French,
partly English) corpus and only matches the sparse same-language document
slice, missing most of the relevant content. The translation therefore happens before the question
reaches the search, not inside the answer prompt.

English is the pivot language: its retrievals cover a broader set of documents than French ones, since
the embedding model's multilingual alignment is English-anchored.

Off unless CHAT_TRANSLATE_BRIDGE_ENABLED is true.
"""

import json
import logging
import re
from functools import lru_cache
from typing import Any, Dict

import httpx

from server.services.infra.streaming import supports_temperature
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from server.services.chat.config import (
    CODE_BLOCK_RE, FASTTEXT_CONFIDENCE_THRESHOLD, FASTTEXT_MODEL_PATH, LANGUAGE_NAMES, LETTERS_RE,
    MISMATCH_CONFIDENCE_THRESHOLD, NO_TRANSLATION_NEEDED, NOISE_RE, QUOTED_RE, TRANSLATE_ENDPOINT,
    TRANSLATE_FALLBACK_ENDPOINTS, TRANSLATE_TIMEOUT_S,
)

logger = logging.getLogger(__name__)


def clean_for_language(text: str, answer: bool = False) -> str:
    """``text`` without what misleads language detection (see NOISE_RE / QUOTED_RE).

    :param text: text to clean, None allowed
    :param answer: True for a model answer, which also loses its quoted and table lines
    :return: the cleaned text, words separated by single spaces
    """
    text = CODE_BLOCK_RE.sub(' ', text or '')
    if answer:
        text = '\n'.join(line for line in text.splitlines() if not line.lstrip().startswith(('>', '|')))
        text = QUOTED_RE.sub(' ', text)
    return ' '.join(NOISE_RE.sub(' ', text).split())


@lru_cache(maxsize=1)
def _fasttext_model() -> Any:
    import fasttext
    fasttext.FastText.eprint = lambda *_a, **_kw: None  # silence the noisy stderr warning
    return fasttext.load_model(FASTTEXT_MODEL_PATH)


class TranslationContext:
    """Carries the detected source language across the question/answer hop."""

    __slots__ = ('lang_code', 'lang_name', 'needs_translation', 'error', 'answer_translated', 'answer_error')

    def __init__(self, lang_code: str, lang_name: str, needs_translation: bool, error: str = ''):
        """Starts the context of one question.

        :param lang_code: ISO 639-1 code of the question's language, or ``unknown``
        :param lang_name: name of that language in English, used in the translation prompt
        :param needs_translation: True when the question was translated to English
        :param error: why the question step fell back to the original text, empty when it did not
        """
        self.lang_code = lang_code
        self.lang_name = lang_name
        self.needs_translation = needs_translation
        # Why the question step fell back to the original text ('' when it did not):
        # saved as a warning of the chat turn (chat_turns, app_errors).
        self.error = error
        # Set by translate_answer_back: the answer was translated / why it was not.
        self.answer_translated = False
        self.answer_error = ''


def fast_lang_guess(text: str) -> str | None:
    """Cheap local language guess (no network call). Returns an ISO 639-1
    code, or None if detection isn't confident enough to trust (in which
    case the caller falls back to the LLM's own detection).

    Calls the fastText C++ binding directly rather than the `predict()`
    Python wrapper — that wrapper's final `np.array(probs, copy=False)`
    raises under NumPy>=2.0, while the underlying
    binding call it wraps works fine.

    :param text: question text
    :return: the ISO 639-1 code, or None when detection is not confident enough
    """
    cleaned = clean_for_language(text)
    if len(LETTERS_RE.findall(cleaned)) < 2:      # a bare REF code, "OK", an acronym
        return None
    single_line = cleaned + '\n'
    try:
        model = _fasttext_model()
        predictions = model.f.predict(single_line, 1, 0.0, 'strict')
    except Exception as exc:
        logger.warning('translation_bridge: fasttext lang guess failed: %s', exc)
        return None
    if not predictions:
        return None
    prob, label = predictions[0]
    if prob < FASTTEXT_CONFIDENCE_THRESHOLD:
        # A short or technical message can get a confident-looking top label at a low real probability: below the threshold the LLM decides.
        logger.debug('translation_bridge: fasttext confidence too low (%.2f) for %r', prob, label)
        return None
    return label.replace('__label__', '')


def _answer_language_mismatch(answer: str, expected_lang: str) -> bool:
    """True if the answer confidently looks like a different language than
    the question asked for. Guards the fr/en case, which normally skips the LLM bridge and trusts the
    answer prompt's language rule, because the model still answers in the wrong one of the two sometimes.

    :param answer: model answer text
    :param expected_lang: ISO 639-1 code the answer should be in
    :return: True when the answer confidently looks like another language
    """
    detected = _fast_lang_guess_with_prob(clean_for_language(answer, answer=True))
    return detected is not None and detected[1] >= MISMATCH_CONFIDENCE_THRESHOLD and detected[0] != expected_lang


def _fast_lang_guess_with_prob(text: str) -> tuple | None:
    """(code, probability) of fastText's top label on already-cleaned text, or None.

    :param text: cleaned text
    :return: ``(code, probability)`` of the top label, or None
    """
    if len(LETTERS_RE.findall(text)) < 5:
        return None
    try:
        predictions = _fasttext_model().f.predict(' '.join(text.split()) + '\n', 1, 0.0, 'strict')
    except Exception as exc:
        logger.warning('translation_bridge: fasttext lang guess failed: %s', exc)
        return None
    if not predictions:
        return None
    prob, label = predictions[0]
    return label.replace('__label__', ''), prob


def answer_language(ctx: 'TranslationContext | None', question: str) -> str | None:
    """English name of the language the answer must be written in, or None if unsure.

    With the bridge: English when the question was translated (the answer is translated
    back after), else the detected French/English. Without it: the local guess.

    :param ctx: context of the question, None when the bridge did not run
    :param question: question text, used when there is no context
    :return: the English name of the answer language, or None when unsure
    """
    if ctx is not None:
        if ctx.needs_translation:
            return 'English'
        return LANGUAGE_NAMES.get(ctx.lang_code)
    code = fast_lang_guess(question)
    return LANGUAGE_NAMES.get(code) if code else None


_http_client: httpx.AsyncClient | None = None


def _get_http_client() -> httpx.AsyncClient:
    """Module-level, reused across calls — a fresh httpx.AsyncClient per
    request would pay TCP+TLS setup on every single chat turn."""
    global _http_client
    if _http_client is None or _http_client.is_closed:
        _http_client = httpx.AsyncClient(timeout=TRANSLATE_TIMEOUT_S)
    return _http_client


async def shutdown_http_client() -> None:
    """Called from the app's lifespan shutdown so the pooled connection
    doesn't leak past process exit."""
    global _http_client
    if _http_client is not None and not _http_client.is_closed:
        await _http_client.aclose()
    _http_client = None


def _is_retryable(exc: BaseException) -> bool:
    # 429 / 5xx / network hiccups are worth a retry; other 4xx (bad auth, bad
    # payload) will just fail again — retrying those only adds latency.
    """Whether a failed call is worth another try: a 429, a 5xx or a network error.

    :param exc: exception of the failed call
    :return: True for a retryable failure
    """
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code >= 500 or exc.response.status_code == 429
    return isinstance(exc, httpx.TransportError)


async def _call_llm(system: str, user: str, host: str, token: str, max_tokens: int,
                    record: Dict[str, Any] | None = None) -> str:
    """TRANSLATE_ENDPOINT, then each of TRANSLATE_FALLBACK_ENDPOINTS; raises the last error.

    :param system: system prompt
    :param user: user message
    :param host: Databricks workspace URL
    :param token: Databricks access token
    :param max_tokens: output budget
    :param record: dict filled with the messages, the endpoint and the token counts of the call, None to skip
    :return: the text of the first endpoint that answers
    """
    last: Exception | None = None
    for endpoint in dict.fromkeys([TRANSLATE_ENDPOINT] + TRANSLATE_FALLBACK_ENDPOINTS):
        try:
            text = await _call_endpoint(endpoint, system, user, host, token, max_tokens, record)
        except Exception as exc:  # noqa: BLE001 — next endpoint
            logger.warning('translation_bridge: %s failed (%s)', endpoint, exc)
            last = exc
            continue
        if text and text.strip():
            return text
        last = ValueError(f'{endpoint} returned an empty text')
    raise last or ValueError('no translation endpoint')


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=0.5, min=1, max=4),
    retry=retry_if_exception(_is_retryable),
    reraise=True,
)
async def _call_endpoint(endpoint: str, system: str, user: str, host: str, token: str, max_tokens: int,
                         record: Dict[str, Any] | None = None) -> str:
    """One call to a serving endpoint, retried on 429, 5xx and network errors.

    :param endpoint: serving endpoint name
    :param system: system prompt
    :param user: user message
    :param host: Databricks workspace URL
    :param token: Databricks access token
    :param max_tokens: output budget
    :param record: dict filled with the messages sent and, once the call answers, the endpoint and the token counts; None to skip
    :return: the text of the reply
    """
    url = f'{host}/serving-endpoints/{endpoint}/invocations'
    headers = {'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'}
    payload: Dict[str, Any] = {
        'messages': [
            {'role': 'system', 'content': system},
            {'role': 'user', 'content': user},
        ],
        'max_tokens': max_tokens,
    }
    if supports_temperature(endpoint):
        payload['temperature'] = 0.0
    if record is not None:
        record['messages'] = payload['messages']
    client = _get_http_client()
    resp = await client.post(url, headers=headers, json=payload)
    resp.raise_for_status()
    data = resp.json()
    if record is not None:
        usage = data.get('usage') or {}
        record.update(endpoint=endpoint, input_tokens=usage.get('prompt_tokens'), output_tokens=usage.get('completion_tokens'))
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


async def translate_question_to_en(question: str, host: str, token: str,
                                   record: Dict[str, Any] | None = None) -> tuple[str, TranslationContext]:
    """Return (english_question, context). No-ops (and no network call) when
    the question is already French/English by the fast local check.

    :param question: question in any language
    :param host: Databricks workspace URL
    :param token: Databricks access token
    :param record: dict filled with the messages, endpoint and token counts of the model call; None to skip
    :return: ``(english_question, context)``; the question itself when no translation was needed
    """
    guess = fast_lang_guess(question)
    if guess in NO_TRANSLATION_NEEDED:
        return question, TranslationContext(guess, guess, needs_translation=False)

    try:
        raw = await _call_llm(_DETECT_TRANSLATE_SYSTEM, question, host, token, max_tokens=400, record=record)
    except Exception as exc:
        logger.warning('translation_bridge: detect/translate LLM call failed, using original text: %s', exc)
        return question, TranslationContext('unknown', 'the original language', needs_translation=False,
                                            error=f'{type(exc).__name__}: {exc}')

    try:
        raw = raw.strip().strip('`')
        if raw.startswith('json'):
            raw = raw[4:]
        det = json.loads(raw)
    except json.JSONDecodeError as exc:
        logger.warning('translation_bridge: LLM returned non-JSON, using original text: %s', exc)
        return question, TranslationContext('unknown', 'the original language', needs_translation=False,
                                            error=f'non-JSON detection reply: {exc}')

    lang_code = (det.get('lang_code') or '').lower()
    if lang_code in NO_TRANSLATION_NEEDED:
        return question, TranslationContext(lang_code, det.get('lang_name', lang_code), needs_translation=False)
    en_text = det.get('en_translation') or question
    return en_text, TranslationContext(lang_code or 'unknown', det.get('lang_name', 'the original language'), needs_translation=True)


async def translate_answer_back(answer: str, ctx: TranslationContext, host: str, token: str,
                                record: Dict[str, Any] | None = None) -> str:
    """Translate the answer back to the question's language. No-op when
    the question didn't need bridging AND the answer isn't a confident
    language mismatch — see _answer_language_mismatch for why the fr/en case
    still needs a check rather than being trusted outright.

    :param answer: answer text, in English
    :param ctx: context returned by ``translate_question_to_en``
    :param host: Databricks workspace URL
    :param token: Databricks access token
    :param record: dict filled with the messages, endpoint and token counts of the model call; None to skip
    :return: the answer in the question's language, or the original text when it was not translated
    """
    if not answer.strip():
        return answer
    if not ctx.needs_translation:
        # Only the confident fr/en case gets the mismatch check: an 'unknown' lang_code leaves nothing to compare against.
        if ctx.lang_code not in NO_TRANSLATION_NEEDED or not _answer_language_mismatch(answer, ctx.lang_code):
            return answer
    system = (
        f'Translate the following text into {ctx.lang_name} ({ctx.lang_code}). '
        f'Preserve Markdown formatting, document REF codes (e.g. "MI-1331-GB", '
        f'"QP-2096"), dates, and any ⟦n⟧ citation markers EXACTLY unchanged. '
        f'Output ONLY the translated text, no preamble.'
    )
    # Reasoning models count their thinking in max_tokens: room for the text (~3 chars per
    # token) plus a margin, so a long answer is not cut at the end of its translation.
    max_tokens = min(16000, max(4000, len(answer) // 2 + 3000))
    try:
        translated = await _call_llm(system, answer, host, token, max_tokens=max_tokens, record=record)
    except Exception as exc:
        logger.warning('translation_bridge: answer translation failed, returning original text: %s', exc)
        ctx.answer_error = f'{type(exc).__name__}: {exc}'
        return answer
    if len(translated.strip()) < len(answer.strip()) * 0.3:
        logger.warning('translation_bridge: translation much shorter than the answer (%d vs %d chars) — '
                       'returning original text', len(translated), len(answer))
        ctx.answer_error = f'translation much shorter than the answer ({len(translated)} vs {len(answer)} chars)'
        return answer
    ctx.answer_translated = True
    return translated
