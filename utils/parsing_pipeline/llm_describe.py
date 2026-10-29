"""llm_describe.py — asynchronous vision-LLM description of the extracted images (driver side).

`describe_all_images` runs the images concurrently under a semaphore and a token-bucket rate limiter. An image goes
through the endpoint chain (primary, then the fallbacks) for `rounds` passes. Given one quota per endpoint, the images
are spread over every endpoint at once, each endpoint within its own quota, the others as fallbacks. An endpoint that fails several images in a
row is skipped for the rest of the run (`EndpointBreaker`). An image no endpoint described comes back PENDING with no
description, so a later run describes it again. Nothing raises: failures are returned as results.
All tunables come from config.py.
"""

import asyncio
import base64
import hashlib
from dataclasses import dataclass
from typing import Any, Dict, List, Sequence, Set

from config import (
    IMAGE_DESCRIPTION_PROMPT,
    LLM_OCR_MAX_TOKENS,
    PDF_PAGE_OCR_PROMPT,
)

# Backoff between two attempts on the same endpoint (seconds, exponential between the bounds).
_RETRY_WAIT_MIN_S = 2.0
_RETRY_WAIT_MAX_S = 30.0


class _EmptyAnswer(Exception):
    """The endpoint answered without visible text: a failed attempt, retried like a transport error."""


@dataclass
class _Spend:
    """Tokens of one image over all its attempts; the failed ones are also kept apart."""

    input_tokens: int = 0
    output_tokens: int = 0
    failed_input_tokens: int = 0
    failed_output_tokens: int = 0


def endpoint_chain(primary: str, fallbacks: str) -> List[str]:
    """Primary, then the comma-separated fallbacks in order, each endpoint once (a fallback equal to the primary is a no-op).

    :param primary: Primary endpoint name.
    :param fallbacks: Comma-separated fallback endpoint names.
    """
    chain: List[str] = []
    for endpoint in [primary, *(fallbacks or "").split(",")]:
        endpoint = endpoint.strip()
        if endpoint and endpoint not in chain:
            chain.append(endpoint)
    return chain


def spread_chains(count: int, endpoints: Sequence[str], weights: Dict[str, float]) -> List[List[str]]:
    """One endpoint chain per image: the images start on the endpoints in proportion to their weights, interleaved,
    and each chain then tries the other endpoints in their order.

    :param count: Number of images.
    :param endpoints: Endpoint names, in fallback order.
    :param weights: Share of the images each endpoint starts, e.g. its requests per minute; a missing or non-positive
        weight starts no image.
    :return: ``count`` chains, each holding every endpoint once.
    """
    active = [e for e in endpoints if weights.get(e, 0) > 0] or list(endpoints[:1])
    total = sum(weights.get(e, 1.0) for e in active)
    credit = {e: 0.0 for e in active}
    chains = []
    for _ in range(count):
        # Smooth weighted round robin: the same shares at every prefix of the list, not one block per endpoint.
        for e in active:
            credit[e] += weights.get(e, 1.0)
        first = max(active, key=lambda e: credit[e])
        credit[first] -= total
        chains.append([first, *(e for e in endpoints if e != first)])
    return chains


def _of(value: Any, endpoint: str) -> Any:
    """The entry of ``endpoint`` when ``value`` is a dict by endpoint, else ``value`` itself (shared by every endpoint).

    :param value: One setting, or one setting per endpoint.
    :param endpoint: Endpoint name.
    :return: the setting that applies to the endpoint.
    """
    return value[endpoint] if isinstance(value, dict) else value


def round_wait_s(round_no: int, waits: Sequence[float]) -> float:
    """Seconds slept before pass `round_no` (0-based): none before the first pass, then `waits` in order, the last one repeated.

    :param round_no: Pass number, starting at 0.
    :param waits: Seconds slept before each pass after the first.
    """
    if round_no == 0 or not waits:
        return 0.0
    return float(waits[min(round_no - 1, len(waits) - 1)])


def error_kind(exc: BaseException) -> str:
    """Short name of a failed call: stored on the image and counted in the run summary.

    :param exc: Exception raised by the call.
    """
    if isinstance(exc, _EmptyAnswer):
        return "empty_answer"
    status = getattr(exc, "status_code", None)
    text = str(exc)
    if status == 429 or "429" in text or "REQUEST_LIMIT_EXCEEDED" in text:
        return "rate_limit"
    # A timeout has an empty message, so the class name is the only hint.
    if isinstance(exc, asyncio.TimeoutError) or "timeout" in type(exc).__name__.lower():
        return "timeout"
    if isinstance(status, int):
        return "http_5xx" if status >= 500 else "http_4xx"
    return "other"


def one_per_image(rows: List[dict]) -> List[dict]:
    """The rows with the first occurrence of each (IDDOC, image_id): an image listed twice would be paid for twice.

    :param rows: Image rows, possibly listed twice.
    :return: rows without duplicates, in their original order.
    """
    seen = set()
    unique = []
    for row in rows:
        key = (row["IDDOC"], row["image_id"])
        if key not in seen:
            seen.add(key)
            unique.append(row)
    return unique


def image_mime_type(image_bytes: bytes) -> str:
    """Media type of an image from its first bytes, as sent in a ``data:`` URL.

    :param image_bytes: content of the image file
    :return: ``image/png``, ``image/gif``, ``image/webp`` or, for anything else, ``image/jpeg``
    """
    if image_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if image_bytes.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if image_bytes[:4] == b"RIFF" and image_bytes[8:12] == b"WEBP":
        return "image/webp"
    return "image/jpeg"


def description_cache_key(prompt: str, image_bytes: bytes) -> str:
    """Key of a description: the image and everything the model is told about it.

    :param prompt: text of the request (context, division, category, label)
    :param image_bytes: content of the image file
    :return: hexadecimal SHA-256 of the prompt and the image
    """
    digest = hashlib.sha256(prompt.encode("utf-8"))
    digest.update(b"\x00")
    digest.update(image_bytes)
    return digest.hexdigest()


def _escape_braces(text: str) -> str:
    """Braces doubled so the text cannot interfere with `str.format`.

    :param text: Text to escape.
    """
    return text.replace("{", "{{").replace("}", "}}")


def build_llm_prompt(
    context_text: str, division: str = "", category: str = "", label: str = ""
) -> str:
    """Vision prompt of an image (an OCR prompt for a scanned page); the nearby text is only a domain hint.

    :param context_text: Text around the image in the document.
    :param division: Division of the document; empty for unknown.
    :param category: Category path of the document; empty for unknown.
    :param label: ``scanned_page`` selects the OCR prompt, any other value the description prompt.
    """
    ctx = (context_text or "").replace(" [...image...] ", " ").strip()[:1200]
    template = PDF_PAGE_OCR_PROMPT if label == "scanned_page" else IMAGE_DESCRIPTION_PROMPT
    return template.format(
        context=_escape_braces(ctx or "(none)"),
        division=_escape_braces(division or "unknown"),
        category=_escape_braces(category or "unknown"),
    )


def is_skip_response(text) -> bool:
    """True if the model refused to describe the image (response starts with SKIP).

    :param text: Answer of the model, or None.
    """
    return bool(text) and str(text).strip().upper().startswith("SKIP")


class EndpointBreaker:
    """Skips an endpoint for the rest of the run after `max_consecutive` failed images in a row on it.

    One instance per run, shared by every chunk. A success resets the count. The images an open endpoint would have
    described stay PENDING for the next run.
    """

    __slots__ = ("_max", "_streak", "_open")

    def __init__(self, max_consecutive: int) -> None:
        """Set up the breaker with no failure yet.

        :param max_consecutive: Failed images in a row on one endpoint that open it.
        """
        self._max = max(1, max_consecutive)
        self._streak: Dict[str, int] = {}
        self._open: Set[str] = set()

    def is_open(self, endpoint: str) -> bool:
        """Whether the endpoint is skipped for the rest of the run.

        :param endpoint: Endpoint name.
        """
        return endpoint in self._open

    def record(self, endpoint: str, ok: bool) -> None:
        """Count one result of an endpoint: a success resets its streak, a failure extends it.

        :param endpoint: Endpoint name.
        :param ok: True for a success.
        """
        if ok:
            self._streak[endpoint] = 0
            return
        self._streak[endpoint] = self._streak.get(endpoint, 0) + 1
        if self._streak[endpoint] >= self._max:
            self._open.add(endpoint)

    @property
    def open_endpoints(self) -> List[str]:
        return sorted(self._open)


class _AsyncRateLimiter:
    """Asyncio token-bucket limiter: `rate_per_minute` requests per minute on average, one initial burst token.

    One token is acquired per LLM call (and per retry) to honour the endpoint quota.
    """

    __slots__ = ("_rate", "_tokens", "_updated_at", "_lock")

    def __init__(self, rate_per_minute: float) -> None:
        """Set up the limiter with one token in the bucket.

        :param rate_per_minute: Requests allowed per minute, on average.
        """
        self._rate: float = rate_per_minute / 60.0  # tokens / second
        self._tokens: float = 1.0
        self._updated_at: float = 0.0  # set on first acquire
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        """Block until one token is available, then consume it."""
        while True:
            async with self._lock:
                now = asyncio.get_running_loop().time()
                if self._updated_at == 0.0:
                    self._updated_at = now
                # Capped at one minute of tokens.
                self._tokens = min(
                    self._rate * 60, self._tokens + (now - self._updated_at) * self._rate
                )
                self._updated_at = now
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return
                wait_s = (1.0 - self._tokens) / self._rate
            await asyncio.sleep(wait_s)


def safe_requests_per_minute(
    itpm_budget: int, otpm_budget: int, qph_budget: int, avg_in: int, avg_out: int
) -> float:
    """Safe requests per minute: the minimum over the three quota axes (input tokens, output tokens, requests).

    The bottleneck is usually the output (low OTPM plus reasoning tokens). Always >= 1.

    :param itpm_budget: Input tokens per minute allowed.
    :param otpm_budget: Output tokens per minute allowed.
    :param qph_budget: Requests per hour allowed.
    :param avg_in: Average input tokens per image.
    :param avg_out: Average output tokens per image.
    """
    return max(
        1.0, min(itpm_budget / max(1, avg_in), otpm_budget / max(1, avg_out), qph_budget / 60.0)
    )


def _error_text(exc: BaseException) -> str:
    """Type name and message of an error, cut to 200 characters.

    :param exc: Exception to describe.
    """
    # The type name is kept because a timeout has an empty message.
    return f"{type(exc).__name__}: {str(exc)[:200]}"


def _usage_of(resp) -> tuple[int, int]:
    """(input tokens, output tokens) of a chat completion; (0, 0) when the endpoint sent no usage.

    :param resp: Chat completion response.
    :return: input and output tokens; ``(0, 0)`` when the endpoint sent no usage.
    """
    usage = getattr(resp, "usage", None)
    if not usage:
        return 0, 0
    return int(getattr(usage, "prompt_tokens", 0) or 0), int(
        getattr(usage, "completion_tokens", 0) or 0
    )


def _answer_of(resp, max_tokens: int) -> str:
    """The text of a chat completion; _EmptyAnswer when it has no visible text.

    :param resp: Chat completion response.
    :param max_tokens: Output token budget of the call, reported in the error.
    """
    choice = resp.choices[0]
    content = choice.message.content
    # Some endpoints (e.g. Gemini) return the content as a list of parts.
    if isinstance(content, list):
        content = "".join(p.get("text", "") if isinstance(p, dict) else str(p) for p in content)
    content = (content or "").strip()
    if not content:
        _, out_tok = _usage_of(resp)
        # GPT-5 reasoning tokens can use up max_tokens with no visible content.
        if choice.finish_reason == "length":
            raise _EmptyAnswer(
                f"max_tokens={max_tokens} exhausted by reasoning "
                f"(output_tokens={out_tok}, finish_reason=length)"
            )
        raise _EmptyAnswer(
            f"empty response (finish_reason={choice.finish_reason}, output_tokens={out_tok})"
        )
    return content


async def _ask_endpoint(
    client,
    endpoint: str,
    messages,
    semaphore: asyncio.Semaphore,
    rate_limiter: _AsyncRateLimiter,
    max_tokens: int,
    temperature: float,
    max_retries: int,
    timeout: float,
    spend: _Spend,
) -> str:
    """One endpoint, up to `max_retries` attempts with backoff. Returns the text, or raises the last error.

    The tokens of every attempt that returned usage are added to `spend`, the unusable ones also to its failed part.

    :param client: Async OpenAI client.
    :param endpoint: Serving endpoint name.
    :param messages: Chat messages of the request.
    :param semaphore: Limits the calls in flight.
    :param rate_limiter: Token bucket shared by the run.
    :param max_tokens: Output token budget of each call.
    :param temperature: Sampling temperature.
    :param max_retries: Attempts on this endpoint.
    :param timeout: Seconds to wait for one call.
    :param spend: Token tally of the image, updated in place.
    """
    from tenacity import AsyncRetrying, stop_after_attempt, wait_exponential

    text = None
    async for attempt in AsyncRetrying(
        stop=stop_after_attempt(max_retries),
        wait=wait_exponential(multiplier=2, min=_RETRY_WAIT_MIN_S, max=_RETRY_WAIT_MAX_S),
        reraise=True,
    ):
        with attempt:
            # The token is acquired inside the retry loop so each retry waits for the bucket too: no 429 cascade.
            await rate_limiter.acquire()
            async with semaphore:
                resp = await asyncio.wait_for(
                    client.chat.completions.create(
                        model=endpoint,
                        messages=messages,
                        max_tokens=max_tokens,
                        temperature=temperature,
                    ),
                    timeout=timeout,
                )
            in_tok, out_tok = _usage_of(resp)
            spend.input_tokens += in_tok
            spend.output_tokens += out_tok
            try:
                text = _answer_of(resp, max_tokens)
            except _EmptyAnswer:
                spend.failed_input_tokens += in_tok
                spend.failed_output_tokens += out_tok
                raise
    return text


def _settle(status: str, attempts: int, max_attempts: int | None) -> str:
    """FAILED_LIMIT once a still-PENDING image has used up its attempts (None: no limit).

    :param status: Status of the image.
    :param attempts: Attempts made so far.
    :param max_attempts: Attempts allowed; None for no limit.
    """
    if status == "PENDING" and max_attempts is not None and attempts >= max_attempts:
        return "FAILED_LIMIT"
    return status


async def describe_one_image(
    client,
    row_data: dict,
    semaphore: asyncio.Semaphore,
    endpoints: Sequence[str],
    max_tokens: int,
    temperature: float,
    max_retries: int,
    rate_limiter: _AsyncRateLimiter,
    breaker: EndpointBreaker,
    timeout: float = 90.0,
    rounds: int = 1,
    round_waits_s: Sequence[float] = (),
    max_attempts: int | None = None,
    cache: Dict[str, dict] | None = None,
) -> dict:
    """Describe one image: each endpoint in turn, `rounds` passes over the chain, `round_waits_s` before each later pass.

    Returns `row_data` plus description, status, the tokens spent (`input_tokens`, `output_tokens`, and the failed
    part `failed_*`), `attempts` and `last_error` / `last_error_kind`. Status DONE, or SKIPPED when the model refused.
    PENDING with no description when no endpoint answered; FAILED_LIMIT instead once the image has `max_attempts`
    attempts. An attempt is a run in which the image failed for a reason other than a quota (429), or could not be
    read: a 429 is a property of the quota, not of the image, and an image skipped only because its endpoints are open
    in this run is not counted either.

    :param client: Async OpenAI client.
    :param row_data: Image row: IDDOC, image_id, volume_path, label, context and category columns.
    :param semaphore: Limits the calls in flight: one for every endpoint, or a dict by endpoint.
    :param endpoints: Endpoint names, tried in order.
    :param max_tokens: Output token budget of each call.
    :param temperature: Sampling temperature.
    :param max_retries: Attempts per endpoint.
    :param rate_limiter: Token bucket shared by the run: one for every endpoint, or a dict by endpoint.
    :param breaker: Circuit breaker shared by the run.
    :param timeout: Seconds to wait for one call.
    :param rounds: Passes over the endpoint chain.
    :param round_waits_s: Seconds slept before each pass after the first.
    :param max_attempts: Attempts after which a still-pending image is FAILED_LIMIT; None for no limit.
    :param cache: Descriptions already made, by ``description_cache_key``: an image with the same content and the same
        request reuses its description without any call; None to describe every image.
    """
    spend = _Spend()
    other_failure = False
    cache_key = ""
    from_cache = False

    def result(description, status, last_error="", last_kind=None) -> dict:
        """Outcome of the image, as the row written back to the table.

        :param description: Description text, or None when no endpoint answered.
        :param status: Status to record, before the attempt limit applies.
        :param last_error: Message of the last failure; empty when none.
        :param last_kind: Kind of the last failure, or None.
        """
        counted = status != "PENDING" or other_failure or last_kind == "read_error"
        attempts = int(row_data.get("attempts") or 0) + (1 if counted else 0)
        status = _settle(status, attempts, max_attempts)
        return {
            **row_data,
            "description": description,
            "status": status,
            "input_tokens": spend.input_tokens,
            "output_tokens": spend.output_tokens,
            "failed_input_tokens": spend.failed_input_tokens,
            "failed_output_tokens": spend.failed_output_tokens,
            "attempts": attempts,
            "cache_key": cache_key,
            "from_cache": from_cache,
            "last_error": last_error,
            "last_error_kind": last_kind if status in ("PENDING", "FAILED_LIMIT") else None,
        }

    # A full-page transcription runs longer than a short image description: it gets its own, larger budget.
    if row_data.get("label") == "scanned_page":
        max_tokens = max(max_tokens, LLM_OCR_MAX_TOKENS)

    try:
        with open(row_data["volume_path"], "rb") as f:
            image_bytes = f.read()
        image_b64 = base64.b64encode(image_bytes).decode()
    except Exception as e:
        return result(
            None,
            "PENDING",
            last_error=f"failed to read image: {_error_text(e)}",
            last_kind="read_error",
        )

    category_parts = [
        p for p in (row_data.get("niveau_plus_1", ""), row_data.get("niveau_plus_2", "")) if p
    ]
    prompt = build_llm_prompt(
        row_data.get("context_text", ""),
        division=row_data.get("division", ""),
        category=" > ".join(category_parts),
        label=row_data.get("label", ""),
    )
    cache_key = description_cache_key(prompt, image_bytes)
    cached = (cache or {}).get(cache_key)
    if cached and cached.get("description"):
        from_cache = True
        return result(cached["description"], cached["status"])
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:{image_mime_type(image_bytes)};base64,{image_b64}"},
                },
            ],
        }
    ]

    last_error, last_kind = "", "no_endpoint"
    for round_no in range(max(1, rounds)):
        wait_s = round_wait_s(round_no, round_waits_s)
        if wait_s:
            await asyncio.sleep(wait_s)
        for endpoint in endpoints:
            if breaker.is_open(endpoint):
                last_error, last_kind = (
                    f"{endpoint}: skipped, failing earlier in this run",
                    "circuit_open",
                )
                continue
            try:
                text = await _ask_endpoint(
                    client,
                    endpoint,
                    messages,
                    _of(semaphore, endpoint),
                    _of(rate_limiter, endpoint),
                    max_tokens,
                    temperature,
                    max_retries,
                    timeout,
                    spend,
                )
            except Exception as e:
                last_kind = error_kind(e)
                last_error = f"{endpoint}: {_error_text(e)}"
                # A 429 is a quota condition, not an outage: it neither opens the breaker nor counts as an attempt.
                if last_kind != "rate_limit":
                    breaker.record(endpoint, ok=False)
                    other_failure = True
                continue
            breaker.record(endpoint, ok=True)
            return result(text, "SKIPPED" if is_skip_response(text) else "DONE")
    return result(None, "PENDING", last_error=last_error, last_kind=last_kind)


async def describe_all_images(
    rows: List[dict],
    ws_host: str,
    ws_token: str,
    endpoints: Sequence[str],
    max_tokens: int,
    temperature: float,
    max_retries: int,
    max_concurrent: int | Dict[str, int],
    requests_per_minute: float | Dict[str, float],
    breaker: EndpointBreaker,
    rounds: int = 1,
    round_waits_s: Sequence[float] = (),
    max_attempts: int | None = None,
    cache: Dict[str, dict] | None = None,
) -> List[dict]:
    """Describe all images concurrently, at most `max_concurrent` at a time and `requests_per_minute` on average.

    With both given per endpoint (dicts), every endpoint works at once within its own limits: the images start on
    the endpoints in proportion to their requests per minute and fall back on the others. With single values, one
    limit is shared by the whole chain and every image starts on the primary.

    :param rows: Image rows to describe.
    :param ws_host: Workspace host of the endpoints, without scheme.
    :param ws_token: Bearer token of the workspace.
    :param endpoints: Endpoint names, tried in order.
    :param max_tokens: Output token budget of each call.
    :param temperature: Sampling temperature.
    :param max_retries: Attempts per endpoint.
    :param max_concurrent: Calls in flight at once: for the whole chain, or a dict by endpoint.
    :param requests_per_minute: Requests allowed per minute, on average: for the whole chain, or a dict by endpoint.
    :param breaker: Circuit breaker shared by the run.
    :param rounds: Passes over the endpoint chain.
    :param round_waits_s: Seconds slept before each pass after the first.
    :param max_attempts: Attempts after which a still-pending image is FAILED_LIMIT; None for no limit.
    :param cache: Descriptions already made, by ``description_cache_key``; None to describe every image.
    """
    from openai import AsyncOpenAI

    if isinstance(requests_per_minute, dict) and isinstance(max_concurrent, dict):
        rate_limiter = {e: _AsyncRateLimiter(requests_per_minute[e]) for e in endpoints}
        semaphore = {e: asyncio.Semaphore(max_concurrent[e]) for e in endpoints}
        chains = spread_chains(len(rows), endpoints, requests_per_minute)
        in_flight = sum(max_concurrent[e] for e in endpoints)
    else:
        rate_limiter = _AsyncRateLimiter(_of(requests_per_minute, endpoints[0]))
        semaphore = asyncio.Semaphore(_of(max_concurrent, endpoints[0]))
        chains = [list(endpoints)] * len(rows)
        in_flight = _of(max_concurrent, endpoints[0])
    # An image is read into memory as soon as its coroutine starts: only a few more than the calls in flight are
    # started at once, so the memory does not grow with the size of the chunk.
    started = asyncio.Semaphore(2 * in_flight)
    client = AsyncOpenAI(api_key=ws_token, base_url=f"https://{ws_host}/serving-endpoints")

    async def one(row: dict, chain: List[str]) -> dict:
        """Describe one image once a start slot is free.

        :param row: Image row.
        :param chain: Endpoints of this image, in the order they are tried.
        :return: the outcome of ``describe_one_image``.
        """
        async with started:
            return await describe_one_image(
                client,
                row,
                semaphore,
                chain,
                max_tokens,
                temperature,
                max_retries,
                rate_limiter,
                breaker,
                rounds=rounds,
                round_waits_s=round_waits_s,
                max_attempts=max_attempts,
                cache=cache,
            )

    try:
        results = await asyncio.gather(
            *[one(r, chain) for r, chain in zip(rows, chains)],
            return_exceptions=True,
        )
    finally:
        try:
            await client.close()
        except Exception:
            pass

    out = []
    for row, res in zip(rows, results):
        if isinstance(res, Exception):
            attempts = int(row.get("attempts") or 0) + 1
            res = {
                **row,
                "description": None,
                "status": _settle("PENDING", attempts, max_attempts),
                "input_tokens": 0,
                "output_tokens": 0,
                "failed_input_tokens": 0,
                "failed_output_tokens": 0,
                "attempts": attempts,
                "last_error": f"unexpected: {_error_text(res)}",
                "last_error_kind": "other",
            }
        out.append(res)
    return out
