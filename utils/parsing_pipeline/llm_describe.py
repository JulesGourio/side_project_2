"""llm_describe.py — asynchronous vision-LLM description of the extracted images (driver side).

`describe_all_images` runs the images concurrently under a semaphore and a token-bucket rate limiter; each call retries
on transient errors and never raises, it returns a status of DONE / SKIPPED / ERROR. All tunables come from config.py.
"""

import asyncio
import base64
from typing import List

from config import (
    IMAGE_DESCRIPTION_PROMPT,
    LLM_OCR_MAX_TOKENS,
    PDF_PAGE_OCR_PROMPT,
)


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


async def describe_one_image(client, row_data: dict, semaphore: asyncio.Semaphore, model: str, max_tokens: int,
                             temperature: float, max_retries: int, rate_limiter: _AsyncRateLimiter,
                             timeout: float = 90.0) -> dict:
    """Describe one image (read from the volume, sent as base64); the result is `row_data` plus
    description / input_tokens / output_tokens / status."""
    from tenacity import AsyncRetrying, stop_after_attempt, wait_exponential

    def result(description, status, in_tok=0, out_tok=0):
        return {**row_data, "description": description, "input_tokens": in_tok, "output_tokens": out_tok,
                "status": status}

    # A full-page transcription runs longer than a short image description: it gets its own, larger budget.
    if row_data.get("label") == "scanned_page":
        max_tokens = max(max_tokens, LLM_OCR_MAX_TOKENS)

    try:
        with open(row_data["volume_path"], "rb") as f:
            image_b64 = base64.b64encode(f.read()).decode()
    except Exception as e:
        return result(f"[ERROR: failed to read image: {str(e)[:200]}]", "ERROR")

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

    try:
        async for attempt in AsyncRetrying(
            stop=stop_after_attempt(max_retries),
            wait=wait_exponential(multiplier=2, min=2, max=30), reraise=True,
        ):
            with attempt:
                # The token is acquired inside the retry loop so each retry waits for the bucket too: no 429 cascade.
                await rate_limiter.acquire()
                async with semaphore:
                    resp = await asyncio.wait_for(
                        client.chat.completions.create(
                            model=model, messages=messages, max_tokens=max_tokens, temperature=temperature,
                        ), timeout=timeout,
                    )
        usage = getattr(resp, "usage", None)
        content = resp.choices[0].message.content
        finish_reason = resp.choices[0].finish_reason
        # Some endpoints (e.g. Gemini) return the content as a list of parts.
        if isinstance(content, list):
            content = "".join(p.get("text", "") if isinstance(p, dict) else str(p) for p in content)
        content = (content or "").strip()
        in_tok = getattr(usage, "prompt_tokens", 0) if usage else 0
        out_tok = getattr(usage, "completion_tokens", 0) if usage else 0
        if is_skip_response(content):
            return result(content, "SKIPPED", in_tok, out_tok)
        if not content and finish_reason == "length":
            # GPT-5 reasoning tokens used up max_tokens: no visible content.
            return result(f"[ERROR: max_tokens={max_tokens} exhausted by reasoning (output_tokens={out_tok}, "
                          f"finish_reason=length) — increase LLM_MAX_TOKENS]", "ERROR", in_tok, out_tok)
        if not content:
            return result(f"[ERROR: empty response (finish_reason={finish_reason}, output_tokens={out_tok})]",
                          "ERROR", in_tok, out_tok)
        return result(content, "DONE", in_tok, out_tok)
    except Exception as e:
        err = str(e)[:200]
        if "429" in err or "REQUEST_LIMIT_EXCEEDED" in err:
            desc = f"[RATE_LIMITED after {max_retries} retries: {err}]"
        elif "timeout" in err.lower() or isinstance(e, asyncio.TimeoutError):
            desc = f"[TIMEOUT after {max_retries} retries: {err}]"
        else:
            desc = f"[ERROR after {max_retries} retries: {err}]"
        return result(desc, "ERROR")


async def describe_all_images(rows: List[dict], ws_host: str, ws_token: str, model: str, max_tokens: int,
                              temperature: float, max_retries: int, max_concurrent: int,
                              requests_per_minute: float) -> List[dict]:
    """Describe all images concurrently, at most `max_concurrent` at a time and `requests_per_minute` on average."""
    from openai import AsyncOpenAI

    rate_limiter = _AsyncRateLimiter(requests_per_minute)
    client = AsyncOpenAI(api_key=ws_token, base_url=f"https://{ws_host}/serving-endpoints")
    semaphore = asyncio.Semaphore(max_concurrent)
    try:
        results = await asyncio.gather(
            *[describe_one_image(client, r, semaphore, model, max_tokens, temperature, max_retries, rate_limiter)
              for r in rows],
            return_exceptions=True,
        )
    finally:
        try:
            await client.close()
        except Exception:
            pass

    return [
        {**rows[i], "description": f"[System Error: {str(res)[:150]}]", "input_tokens": 0, "output_tokens": 0,
         "status": "ERROR"} if isinstance(res, Exception) else res
        for i, res in enumerate(results)
    ]
