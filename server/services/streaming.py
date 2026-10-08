"""Streaming service — LLM endpoint calls via httpx (analysis) and MLflow (impact)."""

import asyncio
import json
import logging
import os
import re
from typing import Any, AsyncGenerator, Dict, List

import httpx
from mlflow.deployments import get_deploy_client

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_S = float(os.getenv('COMPARE_ANALYSIS_TIMEOUT_S', '300'))
DEFAULT_CONNECT_TIMEOUT_S = float(os.getenv('COMPARE_ANALYSIS_CONNECT_TIMEOUT_S', '30'))

_PRICING_USD: Dict[str, Dict[str, float]] = {
    'databricks-claude-haiku-4-5': {'input': 1.0, 'output': 5.0},
    # Sonnet 4.6: 42.857 / 214.286 DBU per 1M tokens (workspace console, 2026-10-07) x 0.078 EUR/DBU,
    # in USD via _EUR_PER_USD — the former 3 / 15 USD list price understated the cost by ~20%.
    'databricks-claude-sonnet-4-6': {'input': 3.634, 'output': 18.168},
    # Sonnet 5.5: 28.571 / 142.857 DBU per 1M tokens (workspace console, confirmed 2026-10-07).
    'databricks-claude-sonnet-5-5': {'input': 2.422, 'output': 12.112},
    'databricks-claude-opus-4-6':   {'input': 15.0, 'output': 75.0},
    'databricks-claude-haiku-4-5':  {'input': 0.8,  'output': 4.0},
    # Confirmed 2026-07-29 from the workspace's Serving Endpoints console
    # (DBUs per 1M tokens) x the contracted rate (0.078 EUR/DBU), converted
    # to USD via _EUR_PER_USD below:
    #   gpt-5-4-mini:          in=21.428 DBU, out=128.572 DBU
    #   gpt-5-mini:            in=6.427  DBU, out=28.571  DBU
    #   gemini-3-1-flash-lite: in=6.428  DBU, out=38.572  DBU
    # Used by the Compare tab's impact judge / document summary / image
    # summary; without these entries unlisted endpoints fall back to Sonnet's
    # rate, wildly overstating cost for a mini-tier model.
    'databricks-gpt-5-4-mini':      {'input': 1.817, 'output': 10.901},
    'databricks-gpt-5-mini':        {'input': 0.545, 'output': 2.422},
    'databricks-gemini-3-1-flash-lite': {'input': 0.545, 'output': 3.270},
    # Re-checked 2026-08-19 (workspace console): Luna's rate DROPPED ~5x since
    # the 2026-07-29 reading (was in=14.286/out=128.571 DBU, i.e. the same
    # output cost as gpt-5-4-mini) to in=2.857/out=25.714 DBU — now cheaper
    # than BOTH gpt-5-4-mini and gpt-5-mini on both axes. The stale rate was
    # overstating every uat-test Luna call's cost_eur by ~5x; update this
    # entry again if the console rate moves.
    # 2026-10-08 (console): Luna output down again, 25.714 -> 17.143 DBU; input unchanged at 2.857.
    'databricks-gpt-5-6-luna':      {'input': 0.242, 'output': 1.453},
    # GPT-6 Luna, 2026-10-08 (console): in=1.428571 DBU, out=7.142857 DBU.
    'databricks-gpt-6-luna':        {'input': 0.121, 'output': 0.606},
    # Gemini 3.8 Flash, 2026-10-08 (console): in=10.714285 DBU, out=53.571425 DBU.
    'databricks-gemini-3-8-flash':  {'input': 0.908, 'output': 4.542},
}
_DEFAULT_PRICING = {'input': 3.0, 'output': 15.0}
_EUR_PER_USD = float(os.getenv('EUR_PER_USD', '0.92'))
_MAX_RETRIES_429 = int(os.getenv('COMPARE_ANALYSIS_RETRIES', '1'))
_RETRY_DELAY_429_S = 60.0
# 502/503/504 are transient gateway/serving-endpoint hiccups (cold start,
# brief overload) — worth a couple of short retries, unlike 429 which needs
# a long backoff.
_RETRYABLE_5XX = {500, 502, 503, 504}
_MAX_RETRIES_5XX = int(os.getenv('COMPARE_ANALYSIS_RETRIES_5XX', '2'))
_RETRY_DELAY_5XX_S = 2.0

# Cached per-endpoint format (agent vs chat_completion) to avoid probe on every call.
_endpoint_format_cache: Dict[str, str] = {}

# GPT-5.6 family (Luna/Terra/Sol, endpoint names containing "gpt-5-6") rejects
# any non-default temperature — only the implicit default (1) is accepted;
# sending 0 (COMPARE_TEMPERATURE's default) 400s. Confirmed empirically
# 2026-07-29 against databricks-gpt-5-6-luna and -terra.
# Bare "gpt-5-mini" (distinct from "gpt-5-4-mini", which DOES accept
# temperature=0) has the exact same restriction — confirmed 2026-08-20: every
# /compare/summarize call on an image was 400ing in production, since
# COMPARE_SUMMARY_IMAGE_ENDPOINT defaults to databricks-gpt-5-mini and this
# regex didn't cover it. The `\b` keeps this from also matching
# "gpt-5-4-mini" (no bare "gpt-5-mini" substring in that name).
# Claude 5 generation (Sonnet 5 / 5.5, Opus 5 / 5.5, Fable) rejects non-default sampling
# parameters too (Anthropic API: temperature != default -> 400), so the same rule applies.
# Reasoning models that refuse a non-default temperature (GPT-6 assumed like GPT-5.6: not verified).
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


# Shown once 502/503/504 retries are exhausted — friendlier than surfacing the
# raw gateway status code to the end user.
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


def _extract_text(chunk: dict, raw_data: str) -> str | None:
    """Extract text delta from any known chunk format."""
    # Native agent format
    if chunk.get('type') == 'response.output_text.delta':
        delta = chunk.get('delta', '')
        return _fix_mojibake(delta) if delta else None
    # OpenAI chat.completion.chunk
    return _parse_text_delta(raw_data)


def _collect_sources(chunk: dict, sources: list) -> None:
    """Best-effort extraction of retrieval context from any chunk format."""
    # Direct retrieval_context field
    ctx = chunk.get('retrieval_context')
    if isinstance(ctx, list):
        for item in ctx:
            s = _normalise_source(item)
            if s:
                sources.append(s)
        return

    # Databricks trace spans (return_trace=True)
    trace = chunk.get('databricks_trace') or chunk.get('trace') or {}
    spans = trace.get('spans') or (trace.get('data') or {}).get('spans') or []
    for span in (spans if isinstance(spans, list) else []):
        attrs = span.get('attributes') or {}
        outputs = span.get('outputs') or attrs.get('outputs') or {}
        if isinstance(outputs, str):
            try:
                outputs = json.loads(outputs)
            except Exception:
                outputs = {}
        for key in ('retrieved_context', 'retrieved_chunks', 'documents', 'results'):
            retrieved = outputs.get(key) or []
            if isinstance(retrieved, list):
                for item in retrieved:
                    s = _normalise_source(item)
                    if s:
                        sources.append(s)

    # Custom data field
    custom = chunk.get('custom_data') or {}
    for key in ('retrieval_context', 'sources'):
        for item in (custom.get(key) or []):
            s = _normalise_source(item)
            if s:
                sources.append(s)

    # LangChain-style event streams
    if chunk.get('event') in ('on_retriever_end', 'on_tool_end'):
        data = chunk.get('data') or {}
        output = data.get('output') or data.get('documents') or []
        if isinstance(output, list):
            for item in output:
                s = _normalise_source(item)
                if s:
                    sources.append(s)


def _normalise_source(raw: Any) -> 'dict | None':
    """Normalise a raw retrieved-chunk object to {doc_uri, content?, score?}."""
    if not isinstance(raw, dict):
        return None
    result: Dict[str, Any] = {}
    for key in ('doc_uri', 'uri', 'url', 'source', 'file_path', 'path', 'id'):
        if raw.get(key):
            result['doc_uri'] = str(raw[key])
            break
    if 'doc_uri' not in result:
        return None
    for key in ('content', 'text', 'page_content', 'chunk_text', 'body'):
        if raw.get(key):
            result['content'] = str(raw[key])[:600]
            break
    for key in ('score', 'relevance_score', 'similarity_score', '_score'):
        if isinstance(raw.get(key), (int, float)):
            result['score'] = round(float(raw[key]), 3)
            break
    return result


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
    # Temperature is sent alongside 'thinking', not instead of it. This was an
    # 'elif' until 2026-08-18, on the assumption that extended thinking forces
    # temperature=1 — it does not on this endpoint (verified: thinking +
    # temperature=0.0 returns 200 on databricks-claude-sonnet-4-6). Since
    # COMPARE_THINKING_BUDGET defaults to 4000, that branch meant the analysis
    # path silently never sent COMPARE_TEMPERATURE at all, leaving every report
    # at the endpoint's default sampling. Note temperature and top_p are
    # mutually exclusive here (400 if both are set), so only temperature is sent.
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


async def stream_chat(
    host: str,
    token: str,
    endpoint_name: str,
    messages: List[Dict[str, str]],
) -> AsyncGenerator[str, None]:
    """Stream a Mosaic AI agent endpoint via httpx SSE.

    Tries agent format (input) first, then chat format (messages).

    Emits:
      data: {"type": "response.output_text.delta", "delta": "..."}
        — only from items whose id starts with "msg_bdrk_" (real agent turns,
          not internal tool-call routing messages)
      data: {"type": "sources", "sources": [{"title": "...", "url": "..."}]}
        — collected from response.output_text.annotation.added events
      data: [DONE]
    """
    url = f'{host}/serving-endpoints/{endpoint_name}/invocations'
    hdrs = {'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'}
    timeout = httpx.Timeout(DEFAULT_TIMEOUT_S, connect=DEFAULT_CONNECT_TIMEOUT_S)
    _KEEPALIVE_S = 15.0

    cached = _endpoint_format_cache.get(endpoint_name)
    formats = [cached] if cached else ['agent', 'chat']
    retries_429 = 0
    retries_5xx = 0

    for fmt in formats:
        payload: Dict[str, Any] = (
            {'input': messages, 'stream': True, 'databricks_options': {'return_trace': True}}
            if fmt == 'agent'
            else {'messages': messages, 'stream': True}
        )
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                async with client.stream('POST', url, json=payload, headers=hdrs) as resp:
                    if resp.status_code == 429 and retries_429 < _MAX_RETRIES_429:
                        # Same transparent-wait behaviour as stream_analysis:
                        # keepalives hold the SSE connection open through the
                        # gateway while we back off, then the format is
                        # re-queued for another attempt.
                        await resp.aread()
                        retries_429 += 1
                        wait_s = _RETRY_DELAY_429_S * retries_429
                        logger.error('%s returned 429 (chat retry %d/%d) — waiting %.0fs',
                                     endpoint_name, retries_429, _MAX_RETRIES_429, wait_s)
                        deadline = asyncio.get_event_loop().time() + wait_s
                        while asyncio.get_event_loop().time() < deadline:
                            yield ': keepalive\n\n'
                            await asyncio.sleep(min(15.0, max(0.1, deadline - asyncio.get_event_loop().time())))
                        formats.append(fmt)
                        continue
                    if resp.status_code in _RETRYABLE_5XX and retries_5xx < _MAX_RETRIES_5XX:
                        await resp.aread()
                        retries_5xx += 1
                        wait_s = _RETRY_DELAY_5XX_S * retries_5xx
                        logger.error('%s returned %d (chat retry %d/%d) — waiting %.0fs',
                                     endpoint_name, resp.status_code, retries_5xx, _MAX_RETRIES_5XX, wait_s)
                        deadline = asyncio.get_event_loop().time() + wait_s
                        while asyncio.get_event_loop().time() < deadline:
                            yield ': keepalive\n\n'
                            await asyncio.sleep(min(15.0, max(0.1, deadline - asyncio.get_event_loop().time())))
                        formats.append(fmt)
                        continue
                    if resp.status_code in (400, 422):
                        await resp.aread()
                        if not cached:
                            continue
                        yield f'data: {json.dumps({"type": "error", "error": f"Endpoint returned {resp.status_code}", "error_type": "HTTPError", "http_status": resp.status_code})}\n\n'
                        yield 'data: [DONE]\n\n'
                        return
                    if resp.status_code != 200:
                        body = await resp.aread()
                        logger.error('%s returned %d: %s', endpoint_name, resp.status_code,
                                     body.decode('utf-8', errors='replace'))
                        error_msg = (
                            _TIRED_MESSAGE if resp.status_code in _RETRYABLE_5XX
                            else f"Chat endpoint error {resp.status_code}"
                        )
                        yield f'data: {json.dumps({"type": "error", "error": error_msg, "error_type": "HTTPError", "http_status": resp.status_code})}\n\n'
                        yield 'data: [DONE]\n\n'
                        return

                    _endpoint_format_cache[endpoint_name] = fmt
                    logger.info('stream_chat: %s format, endpoint=%s', fmt, endpoint_name)

                    sources: List[Dict[str, str]] = []
                    seen_sources: set = set()
                    # Inline citations: every url_citation occurrence with the
                    # character offset (into the answer text) where the marker
                    # should appear, plus the 1-based source number it points to.
                    # This is what the Databricks Agent UI uses to render the
                    # superscript [1][2] markers exactly where each fact is cited.
                    citations: List[Dict[str, Any]] = []
                    source_num: Dict[str, int] = {}
                    # Map document URL -> REF, harvested from the RETRIEVER trace.
                    # The newer KA endpoint puts the URL (not the REF) in the
                    # citation annotation, so we relabel each source's title with
                    # its REF for the "Sources" chips, keeping the URL as the link.
                    url_to_ref: Dict[str, str] = {}
                    cit_logged = False
                    # Running length of the answer text streamed so far. The KA
                    # endpoint does NOT send start_index/end_index on citations;
                    # instead each annotation.added event arrives interleaved in
                    # the stream right after the cited span, so the marker
                    # position is the accumulated text length at that moment
                    # (this is how the Databricks Playground places them).
                    text_len = 0
                    buffer = ''
                    delta_count = 0
                    tool_name: str = ''
                    tool_query: str = ''
                    tool_result: str = ''
                    reasoning_steps: List[str] = []
                    current_reasoning: str = ''
                    # The Knowledge Assistant endpoint does not emit a
                    # `response.completed` event with the MLflow trace in its
                    # stream, so capture the serving request id from the response
                    # headers as the trace identifier (overridden below if a real
                    # trace ever arrives).
                    trace_id: str = (
                        resp.headers.get('x-databricks-request-id')
                        or resp.headers.get('x-request-id')
                        or resp.headers.get('databricks-request-id')
                        or ''
                    )
                    logger.info('stream_chat response headers: %s | request-id=%s',
                                ','.join(resp.headers.keys()), trace_id)
                    q: asyncio.Queue = asyncio.Queue()

                    async def _produce():
                        try:
                            async for raw_chunk in resp.aiter_text():
                                await q.put(('chunk', raw_chunk))
                        except Exception as exc:
                            logger.error('stream_chat producer: %s', exc, exc_info=True)
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
                            buffer += item
                            while '\n' in buffer:
                                line, buffer = buffer.split('\n', 1)
                                line = line.strip()
                                if not line or not line.startswith('data: '):
                                    continue
                                data = line[6:].strip()
                                if data == '[DONE]':
                                    logger.info('stream_chat done: %d deltas, %d sources, trace_id=%s', delta_count, len(sources), trace_id)
                                    if current_reasoning.strip():
                                        reasoning_steps.append(current_reasoning.strip())
                                    for s in sources:
                                        ref = url_to_ref.get(s.get('doc_uri') or '')
                                        if ref:
                                            s['title'] = ref
                                    if sources or citations:
                                        yield f'data: {json.dumps({"type": "sources", "sources": sources, "citations": citations})}\n\n'
                                    meta: Dict[str, Any] = {
                                        'type': 'metadata',
                                        'trace_id': trace_id,
                                        'tool_name': tool_name,
                                        'tool_query': tool_query,
                                        'tool_result': tool_result,
                                        'reasoning_steps': reasoning_steps,
                                    }
                                    yield f'data: {json.dumps(meta)}\n\n'
                                    yield 'data: [DONE]\n\n'
                                    return
                                try:
                                    chunk_obj = json.loads(data)
                                    t = chunk_obj.get('type', '')

                                    if t == 'response.output_text.delta':
                                        delta = _fix_mojibake(chunk_obj.get('delta', ''))
                                        if delta:
                                            text_len += len(delta)
                                            delta_count += 1
                                            if delta_count == 1:
                                                logger.info('stream_chat first delta, item_id=%s', chunk_obj.get('item_id', ''))
                                            elif delta_count % 20 == 0:
                                                logger.info('stream_chat delta #%d', delta_count)
                                            yield f'data: {json.dumps({"type": "response.output_text.delta", "delta": delta})}\n\n'

                                    elif t == 'response.output_text.annotation.added':
                                        ann = chunk_obj.get('annotation', {})
                                        if not cit_logged:
                                            # One-shot raw dump so we can confirm whether the
                                            # endpoint provides start_index/end_index offsets.
                                            logger.info('stream_chat annotation raw: %s', json.dumps(ann)[:400])
                                            cit_logged = True
                                        if ann.get('type') == 'url_citation':
                                            raw_title = (ann.get('title') or '').strip()
                                            raw_url = (ann.get('url') or '').strip()
                                            # The newer KA puts the URL in title/url; the older
                                            # one puts the REF in title and leaves url empty.
                                            if raw_title.startswith('http'):
                                                http_link = raw_title.split('#')[0]
                                            elif raw_url.startswith('http'):
                                                http_link = raw_url.split('#')[0]
                                            else:
                                                http_link = None
                                            key = raw_title or raw_url
                                            if key:
                                                if key not in seen_sources:
                                                    seen_sources.add(key)
                                                    sources.append({
                                                        # Title is provisional; relabeled to the
                                                        # REF from the trace before emitting.
                                                        'title': raw_title or http_link or key,
                                                        'url': http_link,
                                                        'doc_uri': http_link,
                                                    })
                                                    source_num[key] = len(sources)
                                                    logger.info('stream_chat annotation source #%d: %s', source_num[key], raw_title or http_link)
                                                # Marker position: use end_index/start_index if
                                                # the endpoint ever sends them, otherwise the
                                                # accumulated text length now (the KA endpoint
                                                # emits the annotation right after the cited
                                                # span, with no explicit offset).
                                                end_idx = ann.get('end_index')
                                                start_idx = ann.get('start_index')
                                                pos = (end_idx if isinstance(end_idx, int)
                                                       else start_idx if isinstance(start_idx, int)
                                                       else text_len)
                                                citations.append({'n': source_num[key], 'pos': pos})

                                    elif t == 'response.output_item.done':
                                        item = chunk_obj.get('item', {})
                                        if item.get('type') == 'function_call' and item.get('name'):
                                            tool_name = item['name']
                                            try:
                                                args = json.loads(item.get('arguments', '{}'))
                                                tool_query = args.get('ka_query') or args.get('query') or ''
                                            except Exception:
                                                pass
                                        logger.info('stream_chat output_item type=%s id=%s', item.get('type'), item.get('id', '')[:36])
                                        # The KA attaches its MLflow trace here. Harvest
                                        # doc_uri -> REF from the RETRIEVER span so citation
                                        # chips can show the REF (the annotation only carries
                                        # the URL). REF is embedded in each chunk's
                                        # page_content as "[Source: <REF> | Title: …]".
                                        trace = (chunk_obj.get('databricks_output') or {}).get('trace') or {}
                                        # The KA agent format only attaches the real MLflow trace
                                        # here (never at response.completed, despite the header
                                        # comment above assuming otherwise) — grab the real
                                        # trace_id now, overriding the request-id header fallback.
                                        real_trace_id = (trace.get('info') or {}).get('trace_id')
                                        if real_trace_id:
                                            trace_id = real_trace_id
                                        for span in (trace.get('data') or {}).get('spans', []):
                                            sattrs = span.get('attributes', {})
                                            # span attributes are JSON-encoded, so the value is
                                            # the string '"RETRIEVER"' (with quotes).
                                            if (sattrs.get('mlflow.spanType') or '').strip('"') != 'RETRIEVER':
                                                continue
                                            out_raw = sattrs.get('mlflow.spanOutputs', '')
                                            try:
                                                docs = json.loads(out_raw) if isinstance(out_raw, str) else out_raw
                                            except Exception:
                                                continue
                                            for it in (docs if isinstance(docs, list) else []):
                                                if not isinstance(it, dict):
                                                    continue
                                                md = it.get('metadata') or {}
                                                du = md.get('doc_uri') or it.get('doc_uri')
                                                m = re.search(r'\[Source:\s*([^|\]]+)', it.get('page_content') or '')
                                                if du and m:
                                                    url_to_ref[du.split('#')[0]] = m.group(1).strip()
                                        if url_to_ref:
                                            logger.info('stream_chat trace url_to_ref: %d entries', len(url_to_ref))

                                    elif t == 'response.reasoning_summary_text.delta':
                                        delta = chunk_obj.get('delta', '').strip()
                                        if delta:
                                            current_reasoning += delta
                                            # Surface the KA's internal vector-search
                                            # failures (notably the request-id
                                            # self-collision "Request id …-0 already
                                            # running" on parallel sub-queries) in the
                                            # app logs, so this stays visible in prod.
                                            if 'Vector search failed' in delta or 'already running' in delta:
                                                logger.warning('stream_chat KA retrieval error on %s: %s',
                                                               endpoint_name, delta[:300])

                                    elif t == 'response.completed':
                                        db_out = chunk_obj.get('databricks_output') or {}
                                        trace = db_out.get('trace') or chunk_obj.get('trace') or {}
                                        trace_info = trace.get('info') or {}
                                        trace_id = (trace_info.get('trace_id')
                                                    or trace_info.get('request_id')
                                                    or trace.get('trace_id')
                                                    or chunk_obj.get('id')
                                                    or trace_id)
                                        spans = (trace.get('data') or {}).get('spans', [])
                                        # Log one line per span for diagnostics
                                        for span in spans:
                                            sattrs = span.get('attributes', {})
                                            out_raw = sattrs.get('mlflow.spanOutputs', '')
                                            in_raw = sattrs.get('mlflow.spanInputs', '')
                                            logger.info('trace span name=%s type=%s in=%d out=%d',
                                                        span.get('name', ''), sattrs.get('mlflow.spanType', ''),
                                                        len(in_raw), len(out_raw))
                                            if out_raw and len(out_raw) < 4000:
                                                logger.info('  span outputs: %s', out_raw)
                                        # Extract data from each span type
                                        for span in spans:
                                            sattrs = span.get('attributes', {})
                                            stype = sattrs.get('mlflow.spanType', '')
                                            sname = span.get('name', '')
                                            out_raw = sattrs.get('mlflow.spanOutputs', '')
                                            if not out_raw:
                                                continue
                                            try:
                                                out_obj = json.loads(out_raw)
                                            except Exception:
                                                continue

                                            if stype == 'TOOL':
                                                # Knowledge Assistant tool returns {"result": "...full RAG answer..."}
                                                # Scores/raw chunks are internal to that sub-endpoint — not exposed here
                                                result_text = out_obj.get('result', '')
                                                if result_text and not tool_result:
                                                    tool_result = str(result_text)[:5000]
                                                    logger.info('trace tool %s result: %d chars', sname, len(result_text))

                                            elif stype == 'RETRIEVER':
                                                # Extract structured chunks if any RETRIEVER returns them
                                                items = out_obj if isinstance(out_obj, list) else \
                                                    out_obj.get('chunks') or out_obj.get('documents') or \
                                                    out_obj.get('results') or []
                                                for item in (items if isinstance(items, list) else []):
                                                    if not isinstance(item, dict):
                                                        continue
                                                    meta = item.get('metadata') or {}
                                                    doc_uri = (meta.get('doc_uri') or meta.get('source') or
                                                               item.get('doc_uri') or item.get('source') or '')
                                                    # Prefer the indexed REF column as the citation name.
                                                    ref_val = (meta.get('REF') or meta.get('ref') or
                                                               item.get('REF') or item.get('ref'))
                                                    title_val = ref_val or meta.get('source') or meta.get('doc_path') or doc_uri
                                                    content_val = (item.get('page_content') or item.get('content') or
                                                                   item.get('text') or '')
                                                    score_val = (item.get('score') or item.get('relevance_score') or
                                                                 meta.get('score') or None)
                                                    key = str(title_val or doc_uri)
                                                    if key and key not in seen_sources:
                                                        seen_sources.add(key)
                                                        # Expose a clickable URL only when the
                                                        # doc_uri is an actual http(s) link, so the
                                                        # client renders a real link; otherwise the
                                                        # numbered chip just shows the REF/reference.
                                                        url_val = (str(doc_uri)[:500]
                                                                   if doc_uri and str(doc_uri).startswith(('http://', 'https://'))
                                                                   else None)
                                                        sources.append({
                                                            'title': str(title_val)[:500] if title_val else None,
                                                            'url': url_val,
                                                            'doc_uri': str(doc_uri)[:500] if doc_uri else None,
                                                            'chunk_content': str(content_val)[:2000] if content_val else None,
                                                            'score': float(score_val) if score_val is not None else None,
                                                        })
                                        # Collect reasoning summary
                                        if current_reasoning.strip():
                                            reasoning_steps.append(current_reasoning.strip())
                                            current_reasoning = ''

                                    elif t == 'error':
                                        # The KA emits this mid-stream (e.g. its own
                                        # rate limit, "code": "429") after already
                                        # returning HTTP 200 — previously fell into
                                        # the else-branch below and was only logged,
                                        # so chat_ws saw 0 deltas + no error message
                                        # and silently closed the socket instead of
                                        # telling the client anything.
                                        err_msg = chunk_obj.get('message') or chunk_obj.get('error') or 'Upstream error'
                                        err_code = chunk_obj.get('code', '')
                                        db_out = chunk_obj.get('databricks_output') or {}
                                        err_trace_id = ((db_out.get('trace') or {}).get('info') or {}).get('trace_id')
                                        logger.warning('stream_chat upstream error on %s: code=%s message=%s trace_id=%s',
                                                        endpoint_name, err_code, err_msg, err_trace_id)
                                        try:
                                            http_status = int(err_code)
                                        except (TypeError, ValueError):
                                            http_status = 0
                                        yield f'data: {json.dumps({"type": "error", "error": err_msg, "error_type": err_code or "UpstreamError", "http_status": http_status})}\n\n'
                                        yield 'data: [DONE]\n\n'
                                        return

                                    else:
                                        logger.info('stream_chat event type=%s: %s', t, json.dumps(chunk_obj)[:400])

                                except json.JSONDecodeError:
                                    pass
                    finally:
                        producer.cancel()

                    logger.info('stream_chat finished without [DONE]: %d deltas, %d sources, trace_id=%s', delta_count, len(sources), trace_id)
                    if current_reasoning.strip():
                        reasoning_steps.append(current_reasoning.strip())
                    for s in sources:
                        ref = url_to_ref.get(s.get('doc_uri') or '')
                        if ref:
                            s['title'] = ref
                    if sources or citations:
                        yield f'data: {json.dumps({"type": "sources", "sources": sources, "citations": citations})}\n\n'
                    meta = {'type': 'metadata', 'trace_id': trace_id, 'tool_name': tool_name, 'tool_query': tool_query, 'tool_result': tool_result, 'reasoning_steps': reasoning_steps}
                    yield f'data: {json.dumps(meta)}\n\n'
                    yield 'data: [DONE]\n\n'
                    return

        except httpx.TimeoutException as exc:
            logger.warning('stream_chat timeout: %s', exc)
            yield f'data: {json.dumps({"type": "error", "error": "Chat request timed out", "error_type": "TimeoutError", "http_status": 0})}\n\n'
            yield 'data: [DONE]\n\n'
            return
        except Exception as exc:
            logger.error('stream_chat error: %s', exc, exc_info=True)
            yield f'data: {json.dumps({"type": "error", "error": str(exc), "error_type": type(exc).__name__, "http_status": 0})}\n\n'
            yield 'data: [DONE]\n\n'
            return

    yield f'data: {json.dumps({"type": "error", "error": "Chat endpoint did not accept any message format", "error_type": "FormatError", "http_status": 0})}\n\n'
    yield 'data: [DONE]\n\n'
