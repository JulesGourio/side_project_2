"""Streaming service — LLM endpoint calls via httpx (Compare analysis, impact judge, chat), costs and pricing."""

import asyncio
import json
import logging
import os
import re
from typing import Any, AsyncGenerator, Dict, List

import httpx

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_S = float(os.getenv('COMPARE_ANALYSIS_TIMEOUT_S', '300'))
DEFAULT_CONNECT_TIMEOUT_S = float(os.getenv('COMPARE_ANALYSIS_CONNECT_TIMEOUT_S', '30'))

_PRICING_USD: Dict[str, Dict[str, float]] = {
    'databricks-claude-haiku-4-5': {'input': 1.0, 'output': 5.0},
    # USD per 1M tokens = workspace console DBU rate x 0.078 EUR/DBU, via _EUR_PER_USD. DBU in/out: Sonnet 4.6 42.857/214.286,
    # Sonnet 5.5 28.571/142.857.
    'databricks-claude-sonnet-4-6': {'input': 3.634, 'output': 18.168},
    'databricks-claude-sonnet-5-5': {'input': 2.422, 'output': 12.112},
    'databricks-claude-opus-4-6':   {'input': 15.0, 'output': 75.0},
    # Judge / summary models, DBU in/out: gpt-5-4-mini 21.428/128.572, gpt-5-mini 6.427/28.571, gemini-3-1-flash-lite 6.428/38.572.
    # An unlisted endpoint falls back to _DEFAULT_PRICING.
    'databricks-gpt-5-4-mini':      {'input': 1.817, 'output': 10.901},
    'databricks-gpt-5-mini':        {'input': 0.545, 'output': 2.422},
    'databricks-gemini-3-1-flash-lite': {'input': 0.545, 'output': 3.270},
    # GPT-5.6 Luna: 2.857 / 17.143 DBU.
    'databricks-gpt-5-6-luna':      {'input': 0.242, 'output': 1.453},
    # GPT-6 Luna: 1.428571 / 7.142857 DBU.
    'databricks-gpt-6-luna':        {'input': 0.121, 'output': 0.606},
    # Gemini 3.8 Flash: 10.714285 / 53.571425 DBU.
    'databricks-gemini-3-8-flash':  {'input': 0.908, 'output': 4.542},
}
_DEFAULT_PRICING = {'input': 3.0, 'output': 15.0}
_EUR_PER_USD = float(os.getenv('EUR_PER_USD', '0.92'))
_MAX_RETRIES_429 = int(os.getenv('COMPARE_ANALYSIS_RETRIES', '1'))
_RETRY_DELAY_429_S = 60.0
# 502/503/504/503/504 are transient gateway / serving-endpoint hiccups (cold start, brief overload): a couple of short
# retries, unlike 429 which needs a long backoff.
_RETRYABLE_5XX = {500, 502, 503, 504}
_MAX_RETRIES_5XX = int(os.getenv('COMPARE_ANALYSIS_RETRIES_5XX', '2'))
_RETRY_DELAY_5XX_S = 2.0


# These families reject any non-default temperature (a 0 sends a 400): GPT-5.6 (Luna/Terra/Sol, names containing
# "gpt-5-6"), bare "gpt-5-mini"
# (the `\b` keeps it from matching "gpt-5-4-mini", which accepts 0), the Claude 5 generation (Sonnet 5 / 5.5, Opus 5 /
# 5.5, Fable), and GPT-6 (assumed like GPT-5.6).
_NO_TEMPERATURE_RE = re.compile(r'gpt-5-6|gpt-6|gpt-5-mini\b|claude-(?:sonnet|opus)-5|claude-fable', re.I)


def supports_temperature(endpoint_name: str) -> bool:
    return not _NO_TEMPERATURE_RE.search(endpoint_name)


def _fix_mojibake(text: str) -> str:
    if not text:
        return text
    try:
        return text.encode('latin-1').decode('utf-8')
    except (UnicodeDecodeError, UnicodeEncodeError):
        pass

    def _fix_segment(match):
        seg = match.group(0)
        try:
            return seg.encode('latin-1').decode('utf-8')
        except (UnicodeDecodeError, UnicodeEncodeError):
            return seg

    return re.sub(r'[\u0080-\u00ff]+', _fix_segment, text)


def _friendly_error(operation: str, reason: str) -> str:
    return f'{operation} failed. Details: {reason}'


# Shown once 502/503/504 retries are exhausted, instead of the raw gateway status.
_TIRED_MESSAGE = "Qualibot is tired right now and couldn't get an answer — please try again in a moment."


def _cost_eur(endpoint_name: str, input_tokens: int, output_tokens: int) -> float:
    p = _PRICING_USD.get(endpoint_name, _DEFAULT_PRICING)
    return round((input_tokens * p['input'] + output_tokens * p['output']) / 1_000_000 * _EUR_PER_USD, 6)



def _parse_text_delta(raw_data: str) -> str | None:
    try:
        chunk = json.loads(raw_data)
    except json.JSONDecodeError:
        return None
    if chunk.get('object') != 'chat.completion.chunk':
        return None
    choices = chunk.get('choices', [])
    if not choices:
        return None
    content = choices[0].get('delta', {}).get('content')
    if not content:
        return None
    if isinstance(content, list):
        content = ''.join(
            item.get('text', '') if isinstance(item, dict) else item
            for item in content if isinstance(item, (dict, str))
        )
    return _fix_mojibake(content) if content else None


async def stream_analysis(
    host: str,
    token: str,
    endpoint_name: str,
    messages: List[Dict[str, Any]],
    max_tokens: int,
    thinking_budget: int,
    temperature: float,
    operation: str = 'Document comparison',
) -> AsyncGenerator[str, None]:
    """Stream a chat-completion endpoint; emits SSE chunks + a final usage event.

    ``operation`` names the feature in user-facing error messages (Compare by
    default; the Chat VSI engine passes its own label).
    """
    url = f'{host}/serving-endpoints/{endpoint_name}/invocations'

    payload: Dict[str, Any] = {
        'messages': messages,
        'max_tokens': max_tokens,
        'stream': True,
        'stream_options': {'include_usage': True},
    }
    # 'thinking' is an Anthropic-specific parameter — non-Claude endpoints
    # (Gemini, GPT…) reject it, so gate on the endpoint name to keep
    # COMPARE_ANALYSIS_ENDPOINT freely swappable for A/B tests.
    if thinking_budget > 0 and 'claude' in endpoint_name.lower():
        payload['thinking'] = {'type': 'enabled', 'budget_tokens': thinking_budget}
    # Temperature is sent alongside 'thinking', not instead of it: extended thinking does not force temperature=1 on
    # this endpoint.
    # Temperature and top_p are mutually exclusive here (400 if both are set), so only temperature is sent.
    if supports_temperature(endpoint_name):
        payload['temperature'] = temperature

    headers = {'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'}
    timeout = httpx.Timeout(DEFAULT_TIMEOUT_S, connect=DEFAULT_CONNECT_TIMEOUT_S)
    logger.info('Streaming analysis from %s', endpoint_name)

    for attempt in range(1, max(_MAX_RETRIES_429, _MAX_RETRIES_5XX) + 2):
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                async with client.stream('POST', url, json=payload, headers=headers) as response:
                    if response.status_code == 429 and attempt <= _MAX_RETRIES_429:
                        await response.aread()
                        wait_s = int(_RETRY_DELAY_429_S * attempt)
                        logger.error('%s returned 429 (attempt %d/%d) — waiting %ds before retry',
                                     endpoint_name, attempt, _MAX_RETRIES_429 + 1, wait_s)
                        deadline = asyncio.get_event_loop().time() + wait_s
                        while asyncio.get_event_loop().time() < deadline:
                            yield ': keepalive\n\n'
                            await asyncio.sleep(min(15.0, max(0.1, deadline - asyncio.get_event_loop().time())))
                        continue

                    if response.status_code in _RETRYABLE_5XX and attempt <= _MAX_RETRIES_5XX:
                        await response.aread()
                        wait_s = _RETRY_DELAY_5XX_S * attempt
                        logger.error('%s returned %d (attempt %d/%d) — waiting %.0fs before retry',
                                     endpoint_name, response.status_code, attempt, _MAX_RETRIES_5XX + 1, wait_s)
                        deadline = asyncio.get_event_loop().time() + wait_s
                        while asyncio.get_event_loop().time() < deadline:
                            yield ': keepalive\n\n'
                            await asyncio.sleep(min(15.0, max(0.1, deadline - asyncio.get_event_loop().time())))
                        continue

                    if response.status_code != 200:
                        body = await response.aread()
                        err = body.decode('utf-8', errors='replace')
                        logger.error('%s returned %d (attempt %d): %s', endpoint_name, response.status_code, attempt, err)
                        error_msg = (
                            _TIRED_MESSAGE if response.status_code in _RETRYABLE_5XX
                            else _friendly_error(operation, f"endpoint returned {response.status_code}")
                        )
                        yield f'data: {json.dumps({"type": "error", "error": error_msg, "error_type": "HTTPError", "http_status": response.status_code})}\n\n'
                        yield 'data: [DONE]\n\n'
                        return

                    buffer = ''
                    usage_event: Dict[str, Any] = {}
                    # Keepalive SSE comments every 15s prevent the Databricks Apps
                    # gateway from closing idle connections during thinking mode.
                    _KEEPALIVE_S = 15.0

                    async def _iter_with_keepalives():
                        q: asyncio.Queue = asyncio.Queue()

                        async def _produce():
                            try:
                                async for chunk in response.aiter_text():
                                    await q.put(('chunk', chunk))
                            except Exception as exc:
                                logger.error('LLM stream producer error: %s', exc, exc_info=True)
                            finally:
                                await q.put(('done', None))

                        producer = asyncio.create_task(_produce())
                        try:
                            while True:
                                try:
                                    kind, item = await asyncio.wait_for(q.get(), timeout=_KEEPALIVE_S)
                                except asyncio.TimeoutError:
                                    yield ': keepalive\n\n'
                                    continue
                                if kind == 'done':
                                    break
                                yield item
                        finally:
                            producer.cancel()

                    async for raw_chunk in _iter_with_keepalives():
                        if raw_chunk.startswith(': '):
                            yield raw_chunk
                            continue
                        buffer += raw_chunk
                        while '\n' in buffer:
                            line, buffer = buffer.split('\n', 1)
                            line = line.strip()
                            if not line or not line.startswith('data: '):
                                continue
                            data = line[6:].strip()
                            if data == '[DONE]':
                                if usage_event:
                                    yield f'data: {json.dumps(usage_event)}\n\n'
                                yield 'data: [DONE]\n\n'
                                return
                            try:
                                chunk = json.loads(data)
                                if chunk.get('usage'):
                                    u = chunk['usage']
                                    inp = u.get('prompt_tokens') or 0
                                    out = u.get('completion_tokens') or 0
                                    thinking = (u.get('completion_tokens_details') or {}).get('reasoning_tokens') or 0
                                    usage_event = {
                                        'type': 'usage',
                                        'input_tokens': inp,
                                        'output_tokens': out,
                                        'thinking_tokens': thinking,
                                        'total_tokens': inp + out,
                                        'cost_eur': _cost_eur(endpoint_name, inp, out),
                                    }
                                # Generation stopped on the max_tokens ceiling: the
                                # tail of the report is silently missing — surface it.
                                choices = chunk.get('choices') or []
                                if choices and choices[0].get('finish_reason') == 'length':
                                    logger.warning('%s: output truncated at max_tokens=%d', endpoint_name, max_tokens)
                                    yield f'data: {json.dumps({"type": "warning", "warning": "output_truncated", "detail": f"The model hit the {max_tokens}-token output limit — the end of the report may be missing."})}\n\n'
                            except json.JSONDecodeError:
                                pass
                            text = _parse_text_delta(data)
                            if text:
                                yield f'data: {json.dumps({"type": "response.output_text.delta", "delta": text})}\n\n'

            if usage_event:
                yield f'data: {json.dumps(usage_event)}\n\n'
            yield 'data: [DONE]\n\n'
            return

        except httpx.TimeoutException as e:
            logger.warning('Analysis timeout: %s', e, exc_info=True)
            yield f'data: {json.dumps({"type": "error", "error": _friendly_error(operation, "request timed out"), "error_type": "TimeoutError", "http_status": 0})}\n\n'
            yield 'data: [DONE]\n\n'
            return
        except Exception as e:
            logger.error('Error streaming %s: %s', endpoint_name, e, exc_info=True)
            yield f'data: {json.dumps({"type": "error", "error": _friendly_error(operation, str(e)), "error_type": type(e).__name__, "http_status": 0})}\n\n'
            yield 'data: [DONE]\n\n'
            return

    logger.error('%s: rate limit exceeded after %d retries', endpoint_name, _MAX_RETRIES_429)
    yield f'data: {json.dumps({"type": "error", "error": _friendly_error(operation, "Rate limit exceeded — please try again later"), "error_type": "RateLimitError", "http_status": 429})}\n\n'
    yield 'data: [DONE]\n\n'

