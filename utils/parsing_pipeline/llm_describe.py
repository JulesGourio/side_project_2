"""llm_describe.py — asynchronous vision-LLM description of the extracted images (driver side).

`describe_all_images` runs the images concurrently under a semaphore and a token-bucket rate limiter. An image goes
through the endpoint chain (primary, then the fallbacks) for `rounds` passes; when no endpoint answers it comes back
PENDING with no description, so the next run describes it again. Nothing raises: failures are returned as results.
All tunables come from config.py.
"""

import asyncio
import base64
from typing import List, Sequence

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


def endpoint_chain(primary: str, fallbacks: str) -> List[str]:
    """Primary, then the comma-separated fallbacks in order, each endpoint once (a fallback equal to the primary is a no-op)."""
    chain: List[str] = []
    for endpoint in [primary, *(fallbacks or "").split(",")]:
        endpoint = endpoint.strip()
        if endpoint and endpoint not in chain:
            chain.append(endpoint)
    return chain


def round_wait_s(round_no: int, waits: Sequence[float]) -> float:
    """Seconds slept before pass `round_no` (0-based): none before the first pass, then `waits` in order, the last one repeated."""
    if round_no == 0 or not waits:
        return 0.0
    return float(waits[min(round_no - 1, len(waits) - 1)])


def _escape_braces(text: str) -> str:
    """Braces doubled so the text cannot interfere with `str.format`."""
    return text.replace("{", "{{").replace("}", "}}")


def build_llm_prompt(context_text: str, division: str = "", category: str = "", label: str = "") -> str:
    """Vision prompt of an image (an OCR prompt for a scanned page); the nearby text is only a domain hint."""
    ctx = (context_text or "").replace(" [...image...] ", " ").strip()[:1200]
    template = PDF_PAGE_OCR_PROMPT if label == "scanned_page" else IMAGE_DESCRIPTION_PROMPT
    return template.format(context=_escape_braces(ctx or "(none)"), division=_escape_braces(division or "unknown"),
                           category=_escape_braces(category or "unknown"))


def is_skip_response(text) -> bool:
    """True if the model refused to describe the image (response starts with SKIP)."""
    return bool(text) and str(text).strip().upper().startswith("SKIP")


class _AsyncRateLimiter:
    """Asyncio token-bucket limiter: `rate_per_minute` requests per minute on average, one initial burst token.

    One token is acquired per LLM call (and per retry) to honour the endpoint quota.
    """
    __slots__ = ("_rate", "_tokens", "_updated_at", "_lock")

    def __init__(self, rate_per_minute: float) -> None:
        self._rate: float = rate_per_minute / 60.0   # tokens / second
        self._tokens: float = 1.0
        self._updated_at: float = 0.0                # set on first acquire
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        """Block until one token is available, then consume it."""
        while True:
            async with self._lock:
                now = asyncio.get_running_loop().time()
                if self._updated_at == 0.0:
                    self._updated_at = now
                # Capped at one minute of tokens.
                self._tokens = min(self._rate * 60, self._tokens + (now - self._updated_at) * self._rate)
                self._updated_at = now
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return
                wait_s = (1.0 - self._tokens) / self._rate
            await asyncio.sleep(wait_s)


def safe_requests_per_minute(itpm_budget: int, otpm_budget: int, qph_budget: int,
                             avg_in: int, avg_out: int) -> float:
    """Safe requests per minute: the minimum over the three quota axes (input tokens, output tokens, requests).

    The bottleneck is usually the output (low OTPM plus reasoning tokens). Always >= 1.
    """
    return max(1.0, min(itpm_budget / max(1, avg_in), otpm_budget / max(1, avg_out), qph_budget / 60.0))


def _error_text(exc: BaseException) -> str:
    # The type name is kept because a timeout has an empty message.
    return f"{type(exc).__name__}: {str(exc)[:200]}"


def _answer_of(resp, max_tokens: int):
    """(text, input tokens, output tokens) of a chat completion; _EmptyAnswer when it has no visible text."""
    usage = getattr(resp, "usage", None)
    choice = resp.choices[0]
    content = choice.message.content
    # Some endpoints (e.g. Gemini) return the content as a list of parts.
    if isinstance(content, list):
        content = "".join(p.get("text", "") if isinstance(p, dict) else str(p) for p in content)
    content = (content or "").strip()
    in_tok = getattr(usage, "prompt_tokens", 0) if usage else 0
    out_tok = getattr(usage, "completion_tokens", 0) if usage else 0
    if not content:
        # GPT-5 reasoning tokens can use up max_tokens with no visible content.
        if choice.finish_reason == "length":
            raise _EmptyAnswer(f"max_tokens={max_tokens} exhausted by reasoning "
                               f"(output_tokens={out_tok}, finish_reason=length)")
        raise _EmptyAnswer(f"empty response (finish_reason={choice.finish_reason}, output_tokens={out_tok})")
    return content, in_tok, out_tok


async def _ask_endpoint(client, endpoint: str, messages, semaphore: asyncio.Semaphore, rate_limiter: _AsyncRateLimiter,
                        max_tokens: int, temperature: float, max_retries: int, timeout: float):
    """One endpoint, up to `max_retries` attempts with backoff. Returns the answer, or raises the last error."""
    from tenacity import AsyncRetrying, stop_after_attempt, wait_exponential

    answer = None
    async for attempt in AsyncRetrying(
        stop=stop_after_attempt(max_retries),
        wait=wait_exponential(multiplier=2, min=_RETRY_WAIT_MIN_S, max=_RETRY_WAIT_MAX_S), reraise=True,
    ):
        with attempt:
            # The token is acquired inside the retry loop so each retry waits for the bucket too: no 429 cascade.
            await rate_limiter.acquire()
            async with semaphore:
                resp = await asyncio.wait_for(
                    client.chat.completions.create(model=endpoint, messages=messages, max_tokens=max_tokens,
                                                   temperature=temperature),
                    timeout=timeout,
                )
            answer = _answer_of(resp, max_tokens)
    return answer


async def describe_one_image(client, row_data: dict, semaphore: asyncio.Semaphore, endpoints: Sequence[str],
                             max_tokens: int, temperature: float, max_retries: int, rate_limiter: _AsyncRateLimiter,
                             timeout: float = 90.0, rounds: int = 1, round_waits_s: Sequence[float] = ()) -> dict:
    """Describe one image: each endpoint in turn, `rounds` passes over the chain, `round_waits_s` before each later pass.

    Returns `row_data` plus description / input_tokens / output_tokens / status / last_error. Status DONE, or SKIPPED
    when the model refused; PENDING with no description when no endpoint answered in any pass.
    """
    def result(description, status, in_tok=0, out_tok=0, last_error=""):
        return {**row_data, "description": description, "input_tokens": in_tok, "output_tokens": out_tok,
                "status": status, "last_error": last_error}

    # A full-page transcription runs longer than a short image description: it gets its own, larger budget.
    if row_data.get("label") == "scanned_page":
        max_tokens = max(max_tokens, LLM_OCR_MAX_TOKENS)

    try:
        with open(row_data["volume_path"], "rb") as f:
            image_b64 = base64.b64encode(f.read()).decode()
    except Exception as e:
        return result(None, "PENDING", last_error=f"failed to read image: {_error_text(e)}")

    category_parts = [p for p in (row_data.get("niveau_plus_1", ""), row_data.get("niveau_plus_2", "")) if p]
    messages = [{"role": "user", "content": [
        {"type": "text", "text": build_llm_prompt(
            row_data.get("context_text", ""),
            division=row_data.get("division", ""),
            category=" > ".join(category_parts),
            label=row_data.get("label", ""),
        )},
        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"}},
    ]}]

    last_error = ""
    for round_no in range(max(1, rounds)):
        wait_s = round_wait_s(round_no, round_waits_s)
        if wait_s:
            await asyncio.sleep(wait_s)
        for endpoint in endpoints:
            try:
                text, in_tok, out_tok = await _ask_endpoint(
                    client, endpoint, messages, semaphore, rate_limiter, max_tokens, temperature, max_retries, timeout)
            except Exception as e:
                last_error = f"{endpoint}: {_error_text(e)}"
                continue
            return result(text, "SKIPPED" if is_skip_response(text) else "DONE", in_tok, out_tok)
    return result(None, "PENDING", last_error=last_error)


async def describe_all_images(rows: List[dict], ws_host: str, ws_token: str, endpoints: Sequence[str], max_tokens: int,
                              temperature: float, max_retries: int, max_concurrent: int, requests_per_minute: float,
                              rounds: int = 1, round_waits_s: Sequence[float] = ()) -> List[dict]:
    """Describe all images concurrently, at most `max_concurrent` at a time and `requests_per_minute` on average."""
    from openai import AsyncOpenAI

    rate_limiter = _AsyncRateLimiter(requests_per_minute)
    client = AsyncOpenAI(api_key=ws_token, base_url=f"https://{ws_host}/serving-endpoints")
    semaphore = asyncio.Semaphore(max_concurrent)
    try:
        results = await asyncio.gather(
            *[describe_one_image(client, r, semaphore, endpoints, max_tokens, temperature, max_retries, rate_limiter,
                                 rounds=rounds, round_waits_s=round_waits_s)
              for r in rows],
            return_exceptions=True,
        )
    finally:
        try:
            await client.close()
        except Exception:
            pass

    return [
        {**rows[i], "description": None, "input_tokens": 0, "output_tokens": 0, "status": "PENDING",
         "last_error": f"unexpected: {_error_text(res)}"} if isinstance(res, Exception) else res
        for i, res in enumerate(results)
    ]
