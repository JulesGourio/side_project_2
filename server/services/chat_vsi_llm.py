"""Chat VSI — LLM calls that keep answering: retries, fallback models, continuation, load limits.

Used for the answer (``stream_answer``) and for the search-query rewrite (``complete``). Settings, read at call time:

- ``CHAT_VSI_LLM_FALLBACK_ENDPOINTS`` — comma-separated endpoints tried, in order, after
  ``CHAT_VSI_LLM_ENDPOINT`` (default: none, the primary is only retried). In production: GPT-6 Luna, then GPT-5.6 Luna;
- ``CHAT_VSI_REWRITE_FALLBACK_ENDPOINTS`` — same for the rewrite (default: the answer chain);
- ``CHAT_VSI_FIRST_TOKEN_TIMEOUT_S`` (90) — no answer text after this long: the endpoint is
  given up for the next one (a reasoning model thinks before its first word: GPT-6 Luna
  p50 is about 7 s);
- ``CHAT_VSI_STALL_TIMEOUT_S`` (60) — the stream stops sending text this long: given up;
- ``CHAT_VSI_LLM_DEADLINE_S`` (240) — no new attempt starts after this long;
- ``CHAT_VSI_LLM_ROUNDS`` (3) — passes over the endpoint chain, waiting 2 s then 8 s between
  passes (or the endpoint's ``Retry-After``, when shorter than the deadline);
- ``CHAT_VSI_COOLDOWN_S`` (30) — an endpoint that answered 429 / 5xx / timed out is put last
  for every turn during this long (404: 10 minutes), so a quota burst on GPT-6 Luna sends the
  next questions straight to the backup instead of each paying a failed call first;
- ``CHAT_VSI_MAX_CONCURRENT_ANSWERS`` (32, 0 = no limit) — answers generated at once by this
  app instance; the others wait their turn (keepalives sent) instead of all hitting the
  per-minute token quota (1M input tokens/min per endpoint ≈ 40 questions of 25k tokens);
- ``CHAT_VSI_REASONING_MIN_TOKENS`` (6000) — output ceiling floor on reasoning models (their
  thinking counts inside it): a small answer budget would truncate GPT-6 Luna when the
  chain falls back to it. The rewrite uses a floor of 1000.

What is retried where:

- before any answer text: 429, 5xx, timeouts, network errors, an empty answer, a refused
  request (400, e.g. a parameter the model rejects) or a missing endpoint (404) move on to the
  next endpoint; 400/401/403/404 are not retried on the same endpoint in later rounds;
- after part of the answer was streamed (connection dropped, stream stalled, mid-stream
  error): the next endpoint — the same one when it is the only one — is asked to continue the
  text where it stopped, at most twice; the user sees one answer;
- when everything failed: one ``error`` event with a plain message.

The output is the event stream of ``streaming.stream_analysis`` (text deltas, ``usage``,
``warning``, ``error``, ``[DONE]``, keepalive comments) plus one ``llm`` event before
``[DONE]``: the endpoint that answered, every attempt, the wait for a free slot
(``queue_wait_ms``), ``truncated``, ``continuations`` and ``interrupted`` (part of the answer
out, no endpoint could finish it) — saved in Lakebase ``chat_turns`` by the chat engine.
"""

import asyncio
import json
import logging
import os
import random
import time
from typing import Any, AsyncGenerator, Dict, List, Optional, Tuple

import httpx

from .streaming import _cost_eur, _parse_text_delta, supports_temperature

logger = logging.getLogger(__name__)

_KEEPALIVE_S = 15.0
_ROUND_WAITS_S = (2.0, 8.0, 20.0)
_NOT_FOUND_COOLDOWN_S = 600.0
_MAX_CONTINUATIONS = 2
_REWRITE_REASONING_MIN_TOKENS = 1000
CONTINUE_PROMPT = ('Your previous answer was cut off by a technical problem. Continue it exactly where it '
                   'stops: no preamble, do not repeat what is already written, same language, same citation '
                   'markers.')
# The only failure texts a user ever sees (chat.py sends TIRED_MESSAGE for every failed turn); the
# technical cause goes to the turn log, never to the browser.
TIRED_MESSAGE = 'Qualibot is a bit tired right now. Please wait a moment and try again.'
TIRED_CUT_NOTE = '\n\n_(Qualibot got tired before the end of this answer. Please wait a moment and ask again.)_'
NO_MODEL_ANSWERED = 'every answer model failed'


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)) or default)
    except ValueError:
        return default


def _endpoints_from(name: str) -> List[str]:
    return [e.strip() for e in os.getenv(name, '').split(',') if e.strip()]


def _dedup(endpoints: List[str]) -> List[str]:
    out: List[str] = []
    for e in endpoints:
        if e and e not in out:
            out.append(e)
    return out


def answer_chain(primary: str) -> List[str]:
    """The answer endpoints, in order: the primary, then CHAT_VSI_LLM_FALLBACK_ENDPOINTS."""
    return _dedup([primary] + _endpoints_from('CHAT_VSI_LLM_FALLBACK_ENDPOINTS'))


def rewrite_chain(rewrite_endpoint: str, answer_endpoint: str) -> List[str]:
    """The rewrite endpoints: the rewrite model, then CHAT_VSI_REWRITE_FALLBACK_ENDPOINTS
    (default: the answer chain)."""
    fallbacks = _endpoints_from('CHAT_VSI_REWRITE_FALLBACK_ENDPOINTS') or answer_chain(answer_endpoint)
    return _dedup([rewrite_endpoint] + fallbacks)


def first_token_timeout_s() -> float:
    return _env_float('CHAT_VSI_FIRST_TOKEN_TIMEOUT_S', 90.0)


def stall_timeout_s() -> float:
    return _env_float('CHAT_VSI_STALL_TIMEOUT_S', 60.0)


def deadline_s() -> float:
    return _env_float('CHAT_VSI_LLM_DEADLINE_S', 240.0)


def rounds() -> int:
    return max(1, int(_env_float('CHAT_VSI_LLM_ROUNDS', 3)))


def cooldown_s() -> float:
    return _env_float('CHAT_VSI_COOLDOWN_S', 30.0)


def max_concurrent_answers() -> int:
    return int(_env_float('CHAT_VSI_MAX_CONCURRENT_ANSWERS', 32))


def effective_max_tokens(endpoint: str, max_tokens: int, floor: Optional[int] = None) -> int:
    """``max_tokens``, raised to the reasoning floor on models that think inside it."""
    if supports_temperature(endpoint):
        return max_tokens
    return max(max_tokens, int(_env_float('CHAT_VSI_REASONING_MIN_TOKENS', 6000)) if floor is None else floor)


# --- Endpoint health, shared by every turn of this app instance ---

_cooling_until: Dict[str, float] = {}


def _cool(endpoint: str, seconds: float) -> None:
    if seconds > 0:
        _cooling_until[endpoint] = max(_cooling_until.get(endpoint, 0.0), time.monotonic() + seconds)


def _cooling_left(endpoint: str) -> float:
    return max(0.0, _cooling_until.get(endpoint, 0.0) - time.monotonic())


def order_by_health(endpoints: List[str]) -> List[str]:
    """Endpoints not cooling down first (chain order kept), then the cooling ones, soonest back first."""
    ready = [e for e in endpoints if _cooling_left(e) <= 0]
    cooling = sorted((e for e in endpoints if _cooling_left(e) > 0), key=_cooling_left)
    return ready + cooling


def reset_health() -> None:
    """Forget every cooldown (tests)."""
    _cooling_until.clear()


_semaphores: Dict[int, Tuple[int, asyncio.Semaphore]] = {}


def _semaphore() -> Optional[asyncio.Semaphore]:
    """One semaphore per event loop (notebooks run each question in its own loop)."""
    limit = max_concurrent_answers()
    if limit <= 0:
        return None
    loop_id = id(asyncio.get_running_loop())
    current = _semaphores.get(loop_id)
    if current is None or current[0] != limit:
        current = (limit, asyncio.Semaphore(limit))
        _semaphores[loop_id] = current
    return current[1]


# --- One streamed call ---

class LlmFailure(Exception):
    """One attempt failed. ``kind``: rate_limit, server, timeout, network, refused, auth,
    not_found, empty, stream_error."""

    def __init__(self, kind: str, detail: str = '', status: int = 0, retry_after: Optional[float] = None):
        super().__init__(f'{kind} {status or ""} {detail}'.strip())
        self.kind = kind
        self.detail = detail
        self.status = status
        self.retry_after = retry_after


# Kinds not worth another try on the same endpoint during this turn.
_FINAL_KINDS = {'refused', 'auth', 'not_found'}


def _client(timeout: httpx.Timeout) -> httpx.AsyncClient:
    """Separate so tests can swap the transport."""
    return httpx.AsyncClient(timeout=timeout)


def _retry_after(resp: httpx.Response) -> Optional[float]:
    try:
        return float(resp.headers.get('retry-after', ''))
    except ValueError:
        return None


def _failure_for_status(resp: httpx.Response, body: str) -> LlmFailure:
    status = resp.status_code
    kind = ('rate_limit' if status == 429 else 'server' if status >= 500 else 'auth' if status in (401, 403)
            else 'not_found' if status == 404 else 'refused')
    return LlmFailure(kind, body[:300], status, _retry_after(resp))


def _payload(endpoint: str, messages: List[Dict[str, Any]], max_tokens: int, stream: bool) -> Dict[str, Any]:
    payload: Dict[str, Any] = {'messages': messages, 'max_tokens': max_tokens}
    if stream:
        payload.update({'stream': True, 'stream_options': {'include_usage': True}})
    if supports_temperature(endpoint):
        payload['temperature'] = 0.0
    return payload


async def stream_once(host: str, token: str, endpoint: str, messages: List[Dict[str, Any]],
                      max_tokens: int) -> AsyncGenerator[Tuple[str, Any], None]:
    """One streamed call. Yields ('text', str), ('usage', dict), ('truncated', None) and
    ('keepalive', None) while waiting; raises ``LlmFailure``."""
    first_deadline = time.monotonic() + first_token_timeout_s()
    stall = stall_timeout_s()
    timeout = httpx.Timeout(max(first_token_timeout_s(), stall) + 30, connect=15.0)
    got_text, last_text_at = False, 0.0
    try:
        async with _client(timeout) as client:
            async with client.stream('POST', f'{host}/serving-endpoints/{endpoint}/invocations',
                                     json=_payload(endpoint, messages, max_tokens, True),
                                     headers={'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'}) as resp:
                if resp.status_code != 200:
                    body = (await resp.aread()).decode('utf-8', errors='replace')
                    raise _failure_for_status(resp, body)
                lines = resp.aiter_lines().__aiter__()
                pending: Optional[asyncio.Task] = None
                try:
                    while True:
                        if pending is None:
                            pending = asyncio.ensure_future(lines.__anext__())
                        now = time.monotonic()
                        budget = (last_text_at + stall - now) if got_text else (first_deadline - now)
                        if budget <= 0:
                            raise LlmFailure('timeout', 'stream stalled' if got_text else 'no answer text')
                        done, _ = await asyncio.wait({pending}, timeout=min(_KEEPALIVE_S, budget))
                        if not done:
                            yield 'keepalive', None
                            continue
                        task, pending = pending, None
                        try:
                            line = task.result()
                        except StopAsyncIteration:
                            break
                        line = line.strip()
                        if not line.startswith('data: '):
                            continue
                        data = line[6:].strip()
                        if data == '[DONE]':
                            break
                        try:
                            chunk = json.loads(data)
                        except json.JSONDecodeError:
                            continue
                        if isinstance(chunk, dict) and chunk.get('error'):
                            err = chunk['error']
                            msg = err.get('message') if isinstance(err, dict) else str(err)
                            code = err.get('code') if isinstance(err, dict) else ''
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
                            got_text, last_text_at = True, time.monotonic()
                            yield 'text', text
                finally:
                    if pending is not None:
                        pending.cancel()
    except LlmFailure:
        raise
    except httpx.TimeoutException as exc:
        raise LlmFailure('timeout', str(exc)[:200]) from exc
    except httpx.HTTPError as exc:
        raise LlmFailure('network', f'{type(exc).__name__}: {exc}'[:300]) from exc


# --- The answer: chain, rounds, continuation ---

def _event(payload: Dict[str, Any]) -> str:
    return f'data: {json.dumps(payload)}\n\n'


def _cooldown_for(kind: str) -> float:
    if kind == 'not_found':
        return _NOT_FOUND_COOLDOWN_S
    return cooldown_s() if kind in ('rate_limit', 'server', 'timeout', 'network') else 0.0


async def stream_answer(host: str, token: str, endpoints: List[str], messages: List[Dict[str, Any]],
                        max_tokens: int, operation: str = 'Chat') -> AsyncGenerator[str, None]:
    """Stream the answer of the first endpoint of ``endpoints`` that works (see module doc)."""
    sem = _semaphore()
    queued_at = time.monotonic()
    if sem is not None and sem.locked():
        logger.warning('chat_vsi_llm: %d answers already running — this one waits', max_concurrent_answers())
    if sem is not None:
        acquire = asyncio.ensure_future(sem.acquire())
        try:
            while True:
                done, _ = await asyncio.wait({acquire}, timeout=_KEEPALIVE_S)
                if done:
                    break
                yield ': keepalive\n\n'
        except BaseException:
            if acquire.done() and not acquire.cancelled() and acquire.exception() is None:
                sem.release()
            else:
                acquire.cancel()
            raise
    released = False

    def _release():
        nonlocal released
        if sem is not None and not released:
            released = True
            sem.release()
    queue_wait_ms = int((time.monotonic() - queued_at) * 1000)
    try:
        async for event in _stream_answer(host, token, endpoints, messages, max_tokens, operation, queue_wait_ms):
            # Callers stop reading at [DONE] or at an error: free the slot before they do,
            # not when the abandoned generator is garbage-collected.
            if event.startswith(('data: [DONE]', 'data: {"type": "error"')):
                _release()
            yield event
    finally:
        _release()


async def _stream_answer(host: str, token: str, endpoints: List[str], messages: List[Dict[str, Any]],
                         max_tokens: int, operation: str, queue_wait_ms: int = 0) -> AsyncGenerator[str, None]:
    started = time.monotonic()
    chain = _dedup(endpoints)
    given_up: set = set()                       # endpoints not retried this turn
    attempts: List[Dict[str, Any]] = []
    written = ''                                 # answer text already sent
    continuations = 0
    usage = {'input_tokens': 0, 'output_tokens': 0, 'thinking_tokens': 0, 'cost_eur': 0.0}
    answered_by = None
    truncated = False
    last: Optional[LlmFailure] = None

    for round_no in range(rounds()):
        if round_no:
            wait = _ROUND_WAITS_S[min(round_no - 1, len(_ROUND_WAITS_S) - 1)]
            if last is not None and last.retry_after:
                wait = max(wait, min(last.retry_after, 30.0))
            if time.monotonic() - started + wait >= deadline_s():
                break
            logger.warning('chat_vsi_llm: every endpoint failed (round %d) — new round in %.0f s', round_no, wait)
            end = time.monotonic() + wait
            while time.monotonic() < end:
                yield ': keepalive\n\n'
                await asyncio.sleep(min(_KEEPALIVE_S, max(0.05, end - time.monotonic())))
        candidates = [e for e in order_by_health(chain) if e not in given_up]
        if not candidates:
            break
        for endpoint in candidates:
            if time.monotonic() - started >= deadline_s():
                break
            call_messages = messages
            if written:
                call_messages = list(messages) + [{'role': 'assistant', 'content': written},
                                                  {'role': 'user', 'content': CONTINUE_PROMPT}]
            t0 = time.monotonic()
            got = ''
            try:
                async for kind, value in stream_once(host, token, endpoint, call_messages,
                                                     effective_max_tokens(endpoint, max_tokens)):
                    if kind == 'keepalive':
                        yield ': keepalive\n\n'
                    elif kind == 'text':
                        if written and not got:
                            value = value.lstrip() if written.endswith((' ', '\n')) else value
                        got += value
                        yield _event({'type': 'response.output_text.delta', 'delta': value})
                    elif kind == 'usage':
                        for k in ('input_tokens', 'output_tokens', 'thinking_tokens'):
                            usage[k] += value.get(k) or 0
                        usage['cost_eur'] = round(usage['cost_eur'] + (value.get('cost_eur') or 0.0), 6)
                    elif kind == 'truncated':
                        truncated = True
                if not got and not written:
                    raise LlmFailure('empty', 'no answer text')
                attempts.append({'endpoint': endpoint, 'outcome': 'ok' if not written else 'continued',
                                 's': round(time.monotonic() - t0, 1)})
                written += got
                answered_by = endpoint
                break
            except LlmFailure as exc:
                last = exc
                written += got
                attempts.append({'endpoint': endpoint, 'outcome': exc.kind, 'status': exc.status,
                                 's': round(time.monotonic() - t0, 1), 'after_chars': len(written)})
                logger.warning('chat_vsi_llm: %s failed (%s %s, %d chars written): %s',
                               endpoint, exc.kind, exc.status or '', len(written), exc.detail[:200])
                _cool(endpoint, _cooldown_for(exc.kind))
                if exc.kind in _FINAL_KINDS:
                    given_up.add(endpoint)
                if written:
                    continuations += 1
                    if continuations > _MAX_CONTINUATIONS:
                        break
        if answered_by or continuations > _MAX_CONTINUATIONS:
            break

    if answered_by and len(chain) > 1 and answered_by != chain[0]:
        logger.warning('chat_vsi_llm: answered by fallback %s (primary %s)', answered_by, chain[0])
    if truncated:
        yield _event({'type': 'warning', 'warning': 'output_truncated',
                      'detail': f'The model hit the output limit ({max_tokens} tokens) — the end may be missing.'})
    if usage['input_tokens'] or usage['output_tokens']:
        yield _event({'type': 'usage', **usage, 'total_tokens': usage['input_tokens'] + usage['output_tokens']})
    yield _event({'type': 'llm', 'endpoint': answered_by, 'fallback': bool(answered_by and answered_by != chain[0]),
                  'attempts': attempts, 'queue_wait_ms': queue_wait_ms, 'truncated': truncated,
                  'continuations': continuations, 'interrupted': bool(written and not answered_by)})
    if not answered_by and not written:
        logger.error('chat_vsi_llm: no endpoint answered (%s) after %.0f s: %s', ', '.join(chain),
                     time.monotonic() - started, attempts)
        yield _event({'type': 'error', 'error': NO_MODEL_ANSWERED, 'error_type': 'LLMUnavailable',
                      'http_status': last.status if last else 0})
    elif not answered_by:
        # Part of the answer is out but no endpoint could finish it: say so at the end.
        logger.error('chat_vsi_llm: answer cut after %d chars, no endpoint could continue: %s', len(written), attempts)
        yield _event({'type': 'response.output_text.delta',
                      'delta': TIRED_CUT_NOTE})
    yield 'data: [DONE]\n\n'


# --- Short non-streamed calls (search-query rewrite) ---

def _message_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return ''.join(b.get('text', '') for b in content
                       if isinstance(b, dict) and b.get('type') in (None, 'text', 'output_text'))
    return ''


async def complete(host: str, token: str, endpoints: List[str], messages: List[Dict[str, Any]],
                   max_tokens: int, timeout_s: float = 30.0,
                   info: Optional[Dict[str, Any]] = None) -> Tuple[str, str]:
    """(text, endpoint) of the first endpoint that returns non-empty text; two passes over the
    chain, ``timeout_s`` per call and 2 x ``timeout_s`` in all. Raises the last ``LlmFailure``
    when none does. ``info``, when given, receives ``attempts`` (every call: endpoint, outcome,
    status, seconds) and the ``usage`` of the call that answered."""
    info = {} if info is None else info
    attempts = info.setdefault('attempts', [])
    last: Optional[LlmFailure] = None
    given_up: set = set()
    deadline = time.monotonic() + 2 * timeout_s
    for round_no in range(2):
        if round_no:
            await asyncio.sleep(1.0 + random.random())
        for endpoint in [e for e in order_by_health(_dedup(endpoints)) if e not in given_up]:
            left = deadline - time.monotonic()
            if left < 2:
                raise last or LlmFailure('timeout', 'no time left')
            t0 = time.monotonic()
            try:
                async with _client(httpx.Timeout(min(timeout_s, left), connect=10.0)) as client:
                    resp = await client.post(
                        f'{host}/serving-endpoints/{endpoint}/invocations',
                        json=_payload(endpoint, messages,
                                      effective_max_tokens(endpoint, max_tokens, _REWRITE_REASONING_MIN_TOKENS), False),
                        headers={'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'})
                if resp.status_code != 200:
                    raise _failure_for_status(resp, resp.text)
                body = resp.json()
                text = _message_text(body['choices'][0]['message'].get('content')).strip()
                if not text:
                    raise LlmFailure('empty', 'empty completion')
                u = body.get('usage') or {}
                inp, out = u.get('prompt_tokens') or 0, u.get('completion_tokens') or 0
                info['usage'] = {'input_tokens': inp, 'output_tokens': out, 'cost_eur': _cost_eur(endpoint, inp, out)}
                attempts.append({'endpoint': endpoint, 'outcome': 'ok', 's': round(time.monotonic() - t0, 1)})
                return text, endpoint
            except httpx.TimeoutException as exc:
                last = LlmFailure('timeout', str(exc)[:200])
            except httpx.HTTPError as exc:
                last = LlmFailure('network', str(exc)[:200])
            except (KeyError, IndexError, ValueError) as exc:
                last = LlmFailure('stream_error', f'unexpected reply: {exc}'[:200])
            except LlmFailure as exc:
                last = exc
            attempts.append({'endpoint': endpoint, 'outcome': last.kind, 'status': last.status,
                             's': round(time.monotonic() - t0, 1)})
            logger.warning('chat_vsi_llm: %s failed for a short call (%s %s)', endpoint, last.kind, last.status or '')
            _cool(endpoint, _cooldown_for(last.kind))
            if last.kind in _FINAL_KINDS:
                given_up.add(endpoint)
    raise last or LlmFailure('empty', 'no endpoint')
