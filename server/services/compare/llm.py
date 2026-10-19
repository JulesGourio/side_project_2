"""Compare LLM calls: an endpoint chain with retries, under the same rule as the chat (``chat/llm.py``).

Every compare call goes through here: the analysis stream (``stream_analysis``, also used per part by
``chunked_analysis``), the impact judge and the summaries (``complete_chain``). Settings, read at call time:

- ``COMPARE_LLM_FALLBACK_ENDPOINTS`` — endpoints tried, in order, after the configured primary. Default: the other
  Luna model (GPT-5.6 Luna after GPT-6 Luna, GPT-6 Luna after GPT-5.6 Luna, GPT-5.6 Luna after any other primary);
- ``COMPARE_LLM_ROUNDS`` (3) — passes over the chain, waiting between them (the chat's ``ROUND_WAITS_S``);
- ``COMPARE_LLM_DEADLINE_S`` (240) — no new attempt starts after this long.

Endpoint health (cooldown, 404 for 10 minutes) is the chat's and shared with it: the same serving endpoint and
its quota serve both features.

A failed attempt is a 429, 5xx, 400/401/403/404, a timeout, a dropped connection or an answer with no text. The
caller gets ``CompareLlmUnavailable`` only when every attempt has failed; its ``detail`` is for the error log,
the user sees ``TIRED_MESSAGE``.
"""

import asyncio
import json
import logging
import os
import time
from typing import Any, AsyncGenerator, Callable, Dict, List, Optional, Tuple

import httpx

from ..chat import llm as chat_llm
from ..infra.streaming import DEFAULT_CONNECT_TIMEOUT_S, DEFAULT_TIMEOUT_S, _cost_eur, _parse_text_delta, supports_temperature

logger = logging.getLogger(__name__)

GPT_6_LUNA = 'databricks-gpt-6-luna'
GPT_5_6_LUNA = 'databricks-gpt-5-6-luna'
TIRED_MESSAGE = chat_llm.TIRED_MESSAGE
LlmFailure = chat_llm.LlmFailure
THINKING_EFFORT = os.getenv('COMPARE_THINKING_EFFORT', 'medium')
_KEEPALIVE_S = 15.0
_MAX_CONTINUATIONS = 2
CONTINUE_PROMPT = ('Your previous answer was cut off by a technical problem. Continue it exactly where it stops: '
                   'no preamble, do not repeat what is already written, same language and same format.')


class CompareLlmUnavailable(Exception):
    """Every attempt of a compare call failed. ``detail`` is the technical cause (error log only)."""

    def __init__(self, detail: str, status: int = 0):
        super().__init__(detail)
        self.detail = detail
        self.status = status


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)) or default)
    except ValueError:
        return default


def rounds() -> int:
    return max(1, int(_env_float('COMPARE_LLM_ROUNDS', 3)))


def deadline_s() -> float:
    return _env_float('COMPARE_LLM_DEADLINE_S', 240.0)


def _dedup(endpoints: List[str]) -> List[str]:
    out: List[str] = []
    for e in endpoints:
        if e and e not in out:
            out.append(e)
    return out


def compare_chain(primary: str) -> List[str]:
    """The primary, then COMPARE_LLM_FALLBACK_ENDPOINTS (default: the other Luna model)."""
    configured = [e.strip() for e in os.getenv('COMPARE_LLM_FALLBACK_ENDPOINTS', '').split(',') if e.strip()]
    fallbacks = configured or [GPT_6_LUNA if primary == GPT_5_6_LUNA else GPT_5_6_LUNA]
    return _dedup([primary] + fallbacks)


def message_text(message: Dict[str, Any]) -> str:
    """Text of a chat completion message: a plain string, or the text parts of a list (reasoning models)."""
    content = message.get('content') or ''
    if isinstance(content, list):
        return ''.join(p.get('text', '') for p in content if isinstance(p, dict) and p.get('type', 'text') == 'text')
    return content


def _round_wait(round_no: int, last: Optional[LlmFailure]) -> float:
    waits = chat_llm.ROUND_WAITS_S
    wait = waits[min(round_no - 1, len(waits) - 1)]
    if last is not None and last.retry_after:
        wait = max(wait, min(last.retry_after, 30.0))
    return wait


def _record_failure(endpoint: str, exc: LlmFailure, given_up: set) -> None:
    logger.warning('compare_llm: %s failed (%s %s): %s', endpoint, exc.kind, exc.status or '', exc.detail[:200])
    chat_llm.cool(endpoint, chat_llm.cooldown_for(exc.kind))
    if exc.kind in chat_llm.FINAL_KINDS:
        given_up.add(endpoint)


def http_client(timeout: httpx.Timeout) -> httpx.AsyncClient:
    """Separate so tests can swap the transport."""
    return httpx.AsyncClient(timeout=timeout)


def _sse(payload: Dict[str, Any]) -> str:
    return f'data: {json.dumps(payload)}\n\n'


# --- Non-streamed calls (impact judge, summaries) ---

async def _post(host: str, token: str, endpoint: str, messages: List[Dict[str, Any]], max_tokens: int,
                timeout_s: float) -> Dict[str, Any]:
    """One non-streamed call; the completion, or ``LlmFailure`` (an answer with no text is one too)."""
    payload = chat_llm.build_payload(endpoint, messages, max_tokens, False)
    headers = {'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'}
    try:
        async with http_client(httpx.Timeout(timeout_s)) as client:
            resp = await client.post(f'{host}/serving-endpoints/{endpoint}/invocations', json=payload, headers=headers)
    except httpx.TimeoutException as exc:
        raise LlmFailure('timeout', str(exc)[:200]) from exc
    except httpx.HTTPError as exc:
        raise LlmFailure('network', f'{type(exc).__name__}: {exc}'[:300]) from exc
    if resp.status_code != 200:
        raise chat_llm.failure_for_status(resp, resp.text)
    try:
        completion = resp.json()
        text = message_text(completion['choices'][0]['message']).strip()
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise LlmFailure('stream_error', f'unexpected reply: {exc}'[:200]) from exc
    if not text:
        raise LlmFailure('empty', 'no text in the answer')
    return completion


async def complete_chain(host: str, token: str, endpoints: List[str], messages: List[Dict[str, Any]],
                         max_tokens: int, timeout_s: float,
                         accept: Optional[Callable[[Dict[str, Any]], Any]] = None) -> Tuple[Any, str]:
    """``(value, endpoint)`` of the first endpoint of the chain whose completion ``accept`` takes (it returns the
    value to use, or raises ``LlmFailure`` to move on; anything else propagates). Raises
    ``CompareLlmUnavailable`` when no attempt succeeded within the rounds and the deadline."""
    chain = _dedup(endpoints)
    started = time.monotonic()
    given_up: set = set()
    last: Optional[LlmFailure] = None
    for round_no in range(rounds()):
        if round_no:
            wait = _round_wait(round_no, last)
            if time.monotonic() - started + wait >= deadline_s():
                break
            logger.warning('compare_llm: every endpoint failed (round %d) — new round in %.0f s', round_no, wait)
            await asyncio.sleep(wait)
        candidates = [e for e in chat_llm.order_by_health(chain) if e not in given_up]
        if not candidates:
            break
        for endpoint in candidates:
            if time.monotonic() - started >= deadline_s():
                break
            left = max(1.0, deadline_s() - (time.monotonic() - started))
            try:
                completion = await _post(host, token, endpoint, messages, max_tokens, min(timeout_s, left))
                value = accept(completion) if accept else completion
                if endpoint != chain[0]:
                    logger.warning('compare_llm: answered by fallback %s (primary %s)', endpoint, chain[0])
                return value, endpoint
            except LlmFailure as exc:
                last = exc
                _record_failure(endpoint, exc, given_up)
    detail = f'every endpoint failed ({", ".join(chain)}): {last}' if last else f'no endpoint tried ({", ".join(chain)})'
    raise CompareLlmUnavailable(detail, last.status if last else 0)


# --- Streamed analysis ---

def wants_thinking(endpoint: str, thinking_budget: int) -> bool:
    """Only the Claude endpoints take the thinking parameters; the name test keeps the analysis endpoint swappable."""
    return thinking_budget > 0 and 'claude' in endpoint.lower()


async def _stream_once(host: str, token: str, endpoint: str, messages: List[Dict[str, Any]], max_tokens: int,
                       thinking_budget: int, temperature: float) -> AsyncGenerator[Tuple[str, Any], None]:
    """One streamed call. Yields ('text', str), ('usage', dict), ('truncated', None) and ('keepalive', None)
    while waiting; raises ``LlmFailure``. A Claude endpoint that refuses the thinking parameters (400) is called
    again without them, so a model change on its side never takes the primary out of the chain."""
    thinking = wants_thinking(endpoint, thinking_budget)
    try:
        async for item in _stream_request(host, token, endpoint, messages, max_tokens, thinking, temperature):
            yield item
        return
    except LlmFailure as exc:
        if not (thinking and exc.kind == 'refused' and exc.status == 400):
            raise
        logger.warning('compare_llm: %s refused the thinking parameters (%s), sent without', endpoint, exc.detail[:120])
    async for item in _stream_request(host, token, endpoint, messages, max_tokens, False, temperature):
        yield item


async def _stream_request(host: str, token: str, endpoint: str, messages: List[Dict[str, Any]], max_tokens: int,
                          thinking: bool, temperature: float) -> AsyncGenerator[Tuple[str, Any], None]:
    payload: Dict[str, Any] = {'messages': messages, 'max_tokens': max_tokens,
                               'stream': True, 'stream_options': {'include_usage': True}}
    # The current Claude models take adaptive thinking steered by an effort level; a token budget is refused.
    if thinking:
        payload['thinking'] = {'type': 'adaptive'}
        payload['output_config'] = {'effort': THINKING_EFFORT}
    # Temperature and top_p are mutually exclusive on these endpoints: only temperature is sent.
    if supports_temperature(endpoint):
        payload['temperature'] = temperature
    headers = {'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'}
    timeout = httpx.Timeout(DEFAULT_TIMEOUT_S, connect=DEFAULT_CONNECT_TIMEOUT_S)
    try:
        async with http_client(timeout) as client:
            async with client.stream('POST', f'{host}/serving-endpoints/{endpoint}/invocations',
                                     json=payload, headers=headers) as response:
                if response.status_code != 200:
                    body = (await response.aread()).decode('utf-8', errors='replace')
                    raise chat_llm.failure_for_status(response, body)
                queue: asyncio.Queue = asyncio.Queue()

                async def _produce() -> None:
                    try:
                        async for piece in response.aiter_text():
                            await queue.put(('chunk', piece))
                    except Exception as exc:  # re-raised by the consumer below
                        await queue.put(('error', exc))
                    finally:
                        await queue.put(('done', None))

                producer = asyncio.ensure_future(_produce())
                buffer = ''
                try:
                    while True:
                        try:
                            kind, item = await asyncio.wait_for(queue.get(), timeout=_KEEPALIVE_S)
                        except asyncio.TimeoutError:
                            yield 'keepalive', None
                            continue
                        if kind == 'error':
                            raise item
                        if kind == 'done':
                            return
                        buffer += item
                        while '\n' in buffer:
                            line, buffer = buffer.split('\n', 1)
                            line = line.strip()
                            if not line.startswith('data: '):
                                continue
                            data = line[6:].strip()
                            if data == '[DONE]':
                                return
                            try:
                                chunk = json.loads(data)
                            except json.JSONDecodeError:
                                continue
                            if not isinstance(chunk, dict):
                                continue
                            if chunk.get('error'):
                                err = chunk['error']
                                code = err.get('code') if isinstance(err, dict) else ''
                                msg = err.get('message') if isinstance(err, dict) else str(err)
                                raise LlmFailure('rate_limit' if str(code) == '429' else 'stream_error', str(msg)[:300])
                            if chunk.get('usage'):
                                u = chunk['usage']
                                inp, out = u.get('prompt_tokens') or 0, u.get('completion_tokens') or 0
                                yield 'usage', {'input_tokens': inp, 'output_tokens': out,
                                                'thinking_tokens': (u.get('completion_tokens_details') or {}).get('reasoning_tokens') or 0,
                                                'cost_eur': _cost_eur(endpoint, inp, out)}
                            choices = chunk.get('choices') or []
                            if choices and choices[0].get('finish_reason') == 'length':
                                yield 'truncated', None
                            text = _parse_text_delta(data)
                            if text:
                                yield 'text', text
                finally:
                    producer.cancel()
    except LlmFailure:
        raise
    except httpx.TimeoutException as exc:
        raise LlmFailure('timeout', str(exc)[:200]) from exc
    except httpx.HTTPError as exc:
        raise LlmFailure('network', f'{type(exc).__name__}: {exc}'[:300]) from exc


async def stream_analysis(host: str, token: str, endpoint: str, messages: List[Dict[str, Any]], max_tokens: int,
                          thinking_budget: int, temperature: float,
                          info: Optional[Dict[str, Any]] = None) -> AsyncGenerator[str, None]:
    """SSE of the analysis of the first endpoint of the chain that answers: text deltas, a ``warning`` when the
    output hit max_tokens, one ``usage`` event, then ``[DONE]``; keepalive comments while waiting. When every
    attempt failed: one ``error`` event with ``TIRED_MESSAGE`` (``detail`` = the cause, dropped by the router).
    ``info['endpoint']`` is set to the endpoint that finished the answer (not set when none answered)."""
    chain = compare_chain(endpoint)
    started = time.monotonic()
    given_up: set = set()
    written = ''                                 # answer text already sent
    continuations = 0
    truncated = False
    answered = False
    usage_seen = False
    usage = {'input_tokens': 0, 'output_tokens': 0, 'thinking_tokens': 0, 'cost_eur': 0.0}
    last: Optional[LlmFailure] = None

    for round_no in range(rounds()):
        if round_no:
            wait = _round_wait(round_no, last)
            if time.monotonic() - started + wait >= deadline_s():
                break
            logger.warning('compare_llm: every endpoint failed (round %d) — new round in %.0f s', round_no, wait)
            end = time.monotonic() + wait
            while time.monotonic() < end:
                yield ': keepalive\n\n'
                await asyncio.sleep(min(_KEEPALIVE_S, max(0.05, end - time.monotonic())))
        candidates = [e for e in chat_llm.order_by_health(chain) if e not in given_up]
        if not candidates:
            break
        for candidate in candidates:
            if time.monotonic() - started >= deadline_s():
                break
            call_messages = messages
            if written:
                call_messages = [*messages, {'role': 'assistant', 'content': written},
                                 {'role': 'user', 'content': CONTINUE_PROMPT}]
            got = ''
            try:
                async for kind, value in _stream_once(host, token, candidate, call_messages, max_tokens,
                                                      thinking_budget, temperature):
                    if kind == 'keepalive':
                        yield ': keepalive\n\n'
                    elif kind == 'text':
                        if written and not got and written.endswith((' ', '\n')):
                            value = value.lstrip()
                        got += value
                        yield _sse({'type': 'response.output_text.delta', 'delta': value})
                    elif kind == 'usage':
                        usage_seen = True
                        for k in ('input_tokens', 'output_tokens', 'thinking_tokens'):
                            usage[k] += value.get(k) or 0
                        usage['cost_eur'] = round(usage['cost_eur'] + (value.get('cost_eur') or 0.0), 6)
                    elif kind == 'truncated':
                        truncated = True
                if not got:
                    raise LlmFailure('empty', 'no answer text')
                written += got
                answered = True
                if info is not None:
                    info['endpoint'] = candidate
                if candidate != chain[0]:
                    logger.warning('compare_llm: answered by fallback %s (primary %s)', candidate, chain[0])
                break
            except LlmFailure as exc:
                last = exc
                written += got
                _record_failure(candidate, exc, given_up)
                if written:
                    continuations += 1
                    if continuations > _MAX_CONTINUATIONS:
                        break
        if answered or continuations > _MAX_CONTINUATIONS:
            break

    if truncated:
        yield _sse({'type': 'warning', 'warning': 'output_truncated',
                    'detail': f'The model hit the {max_tokens}-token output limit — the end of the report may be missing.'})
    if usage_seen:
        yield _sse({'type': 'usage', **usage, 'total_tokens': usage['input_tokens'] + usage['output_tokens']})
    if not answered:
        detail = (f'every endpoint failed ({", ".join(chain)}): {last}' if written == '' else
                  f'answer cut after {len(written)} chars, no endpoint could finish it ({", ".join(chain)}): {last}')
        logger.error('compare_llm: no answer — %s', detail)
        yield _sse({'type': 'error', 'error': TIRED_MESSAGE, 'error_type': 'LLMUnavailable',
                    'http_status': last.status if last else 0, 'detail': detail})
    yield 'data: [DONE]\n\n'
