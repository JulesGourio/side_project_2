"""Image description of the parsing pipeline (utils/parsing_pipeline/llm_describe.py): the endpoint chain, the passes,
the retries, the timeouts, the per-run circuit breaker, the tokens and the attempt counter, against a fake client with
no waits. An image that no endpoint describes stays PENDING."""
import asyncio
import os
import sys
import types

import pytest

PARSING = os.path.join(os.path.dirname(__file__), "..", "utils", "parsing_pipeline")
sys.path.insert(0, PARSING)
for key, value in {"PIPELINE_WRITE_ENV": "dev", "REFERENCE_READ_ENV": "ref",
                   "PARSING_IMAGES_VOLUME": "img"}.items():
    os.environ.setdefault(key, value)

pytest.importorskip("tenacity")

import llm_describe
from llm_describe import (EndpointBreaker, _AsyncRateLimiter, _EmptyAnswer, describe_one_image, endpoint_chain,
                          error_kind, one_per_image, round_wait_s)


def _resp(content, finish="stop", prompt=10, out=5):
    return types.SimpleNamespace(
        choices=[types.SimpleNamespace(message=types.SimpleNamespace(content=content), finish_reason=finish)],
        usage=types.SimpleNamespace(prompt_tokens=prompt, completion_tokens=out),
    )


class FakeClient:
    """Outcomes per endpoint, consumed in order; the last one repeats. An outcome is a response, an exception, or
    "hang" (never answers, so the call timeout applies)."""

    def __init__(self, script):
        self.script = {k: list(v) for k, v in script.items()}
        self.calls = []
        self.chat = types.SimpleNamespace(completions=types.SimpleNamespace(create=self._create))

    async def _create(self, model, messages, max_tokens, temperature):
        self.calls.append(model)
        outcomes = self.script[model]
        outcome = outcomes.pop(0) if len(outcomes) > 1 else outcomes[0]
        if outcome == "hang":
            await asyncio.sleep(3600)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


@pytest.fixture(autouse=True)
def _no_retry_waits(monkeypatch):
    monkeypatch.setattr(llm_describe, "_RETRY_WAIT_MIN_S", 0.0)
    monkeypatch.setattr(llm_describe, "_RETRY_WAIT_MAX_S", 0.0)


def _row(volume_path=__file__, **extra):
    return {"IDDOC": 1, "image_id": 0, "volume_path": volume_path, "label": "picture", "context_text": "",
            "division": "AS", "niveau_plus_1": "Procédés", **extra}


def _describe(client, endpoints, max_retries=2, rounds=1, round_waits_s=(), timeout=5.0, row=None,
              breaker=None, max_attempts=None):
    async def scenario():
        limiter = _AsyncRateLimiter(rate_per_minute=6_000_000)
        return await describe_one_image(client, row or _row(), asyncio.Semaphore(4), endpoints, 100, 1.0,
                                        max_retries, limiter, breaker or EndpointBreaker(100), timeout=timeout,
                                        rounds=rounds, round_waits_s=round_waits_s, max_attempts=max_attempts)
    return asyncio.run(scenario())


def test_the_chain_keeps_the_primary_first_and_each_endpoint_once():
    assert endpoint_chain("a", " b , a,, c ") == ["a", "b", "c"]
    assert endpoint_chain("a", "") == ["a"]
    assert endpoint_chain("a", None) == ["a"]


def test_the_waits_grow_between_passes_and_the_first_pass_has_none():
    assert [round_wait_s(i, (5.0, 15.0, 30.0)) for i in range(4)] == [0.0, 5.0, 15.0, 30.0]
    assert round_wait_s(1, ()) == 0.0


def test_the_error_kinds_name_what_failed():
    def with_status(code):
        exc = RuntimeError(f"HTTP {code}")
        exc.status_code = code
        return exc

    assert error_kind(RuntimeError("429 REQUEST_LIMIT_EXCEEDED")) == "rate_limit"
    assert error_kind(with_status(429)) == "rate_limit"
    assert error_kind(with_status(503)) == "http_5xx"
    assert error_kind(with_status(404)) == "http_4xx"
    assert error_kind(asyncio.TimeoutError()) == "timeout"
    assert error_kind(type("APITimeoutError", (Exception,), {})("")) == "timeout"
    assert error_kind(_EmptyAnswer("empty")) == "empty_answer"
    assert error_kind(RuntimeError("boom")) == "other"


def test_a_transient_failure_is_retried_on_the_same_endpoint():
    client = FakeClient({"primary": [RuntimeError("503 busy"), RuntimeError("timeout"), _resp("A pump with a label.")]})
    out = _describe(client, ["primary"], max_retries=5)
    assert out["status"] == "DONE" and out["description"] == "A pump with a label."
    assert out["last_error_kind"] is None
    assert client.calls == ["primary"] * 3


def test_a_failing_primary_hands_over_to_the_fallback():
    client = FakeClient({"primary": [RuntimeError("500")], "fallback": [_resp("Fallback text.")]})
    out = _describe(client, ["primary", "fallback"], max_retries=2)
    assert out["status"] == "DONE" and out["description"] == "Fallback text."
    assert client.calls == ["primary", "primary", "fallback"]


def test_an_image_no_endpoint_describes_stays_pending_with_no_description():
    client = FakeClient({"primary": [RuntimeError("429 REQUEST_LIMIT_EXCEEDED")],
                         "fallback": [RuntimeError("500 internal")]})
    out = _describe(client, ["primary", "fallback"], max_retries=2, rounds=2, round_waits_s=(0.0,))
    assert out["status"] == "PENDING" and out["description"] is None
    assert client.calls == ["primary"] * 2 + ["fallback"] * 2 + ["primary"] * 2 + ["fallback"] * 2
    assert out["last_error"].startswith("fallback: RuntimeError")
    assert out["last_error_kind"] == "other"


def test_an_empty_answer_is_a_failed_attempt_and_is_retried():
    client = FakeClient({"primary": [_resp(""), _resp("   "), _resp("Text after two empty answers.")]})
    out = _describe(client, ["primary"], max_retries=5)
    assert out["status"] == "DONE" and out["description"] == "Text after two empty answers."
    assert len(client.calls) == 3


def test_a_length_cut_without_text_ends_pending_after_the_retries():
    client = FakeClient({"primary": [_resp("", finish="length")]})
    out = _describe(client, ["primary"], max_retries=2)
    assert out["status"] == "PENDING" and out["description"] is None
    assert len(client.calls) == 2 and "max_tokens=100 exhausted" in out["last_error"]
    assert out["last_error_kind"] == "empty_answer"


def test_a_refusal_to_describe_is_final_and_not_retried():
    client = FakeClient({"primary": [_resp("SKIP: decorative logo")]})
    out = _describe(client, ["primary"], max_retries=5)
    assert out["status"] == "SKIPPED" and out["description"] == "SKIP: decorative logo"
    assert client.calls == ["primary"]


def test_a_hanging_endpoint_times_out_and_the_fallback_answers():
    client = FakeClient({"primary": ["hang"], "fallback": [_resp("Answer after the timeout.")]})
    out = _describe(client, ["primary", "fallback"], max_retries=1, timeout=0.01)
    assert out["status"] == "DONE" and out["description"] == "Answer after the timeout."
    assert client.calls == ["primary", "fallback"]


def test_an_unreadable_image_stays_pending_without_a_call():
    client = FakeClient({"primary": [_resp("never used")]})
    out = _describe(client, ["primary"], row=_row(volume_path="/nonexistent/image.jpg"))
    assert out["status"] == "PENDING" and out["description"] is None
    assert "failed to read image" in out["last_error"] and out["last_error_kind"] == "read_error"
    assert client.calls == []


def test_the_breaker_skips_an_endpoint_after_consecutive_failed_images():
    client = FakeClient({"primary": [RuntimeError("500")], "fallback": [_resp("Fallback.")]})
    breaker = EndpointBreaker(2)
    outs = [_describe(client, ["primary", "fallback"], max_retries=2, breaker=breaker) for _ in range(3)]
    assert [o["status"] for o in outs] == ["DONE"] * 3
    assert breaker.is_open("primary") and breaker.open_endpoints == ["primary"]
    # Images 1 and 2: two attempts each on the primary, then the fallback. Image 3: the fallback only.
    assert client.calls == ["primary", "primary", "fallback"] * 2 + ["fallback"]


def test_a_success_resets_the_failure_streak_of_the_breaker():
    client = FakeClient({"primary": [RuntimeError("500"), _resp("Fine."), RuntimeError("500")]})
    breaker = EndpointBreaker(2)
    for _ in range(3):
        _describe(client, ["primary"], max_retries=1, breaker=breaker)
    assert not breaker.is_open("primary")


def test_an_open_endpoint_without_fallback_leaves_the_image_pending_as_circuit_open():
    client = FakeClient({"primary": [RuntimeError("500")]})
    breaker = EndpointBreaker(1)
    first = _describe(client, ["primary"], max_retries=1, breaker=breaker)
    second = _describe(client, ["primary"], max_retries=1, breaker=breaker)
    assert first["last_error_kind"] == "other"
    assert second["status"] == "PENDING" and second["last_error_kind"] == "circuit_open"
    assert client.calls == ["primary"]   # the second image never reached the endpoint


def test_the_tokens_of_failed_attempts_are_counted_apart():
    client = FakeClient({"primary": [_resp("", finish="length", prompt=100, out=4000), _resp("Good.", prompt=50, out=20)]})
    out = _describe(client, ["primary"], max_retries=5)
    assert out["status"] == "DONE"
    assert (out["input_tokens"], out["output_tokens"]) == (150, 4020)
    assert (out["failed_input_tokens"], out["failed_output_tokens"]) == (100, 4000)


def test_a_transport_failure_has_no_tokens_to_count():
    client = FakeClient({"primary": [RuntimeError("reset"), _resp("A.", prompt=10, out=2)]})
    out = _describe(client, ["primary"], max_retries=5)
    assert (out["input_tokens"], out["output_tokens"]) == (10, 2)
    assert (out["failed_input_tokens"], out["failed_output_tokens"]) == (0, 0)


def test_the_attempt_counter_goes_up_once_per_run_whatever_the_outcome():
    client = FakeClient({"primary": [_resp("Done.")]})
    assert _describe(client, ["primary"], row=_row(attempts=2))["attempts"] == 3
    failing = FakeClient({"primary": [RuntimeError("500")]})
    pending = _describe(failing, ["primary"], max_retries=1, row=_row(attempts=None))
    assert pending["attempts"] == 1 and pending["status"] == "PENDING"


def test_one_row_per_image_keeps_the_first_occurrence():
    rows = [{"IDDOC": 1, "image_id": 0, "tag": "a"}, {"IDDOC": 1, "image_id": 0, "tag": "b"},
            {"IDDOC": 1, "image_id": 1, "tag": "c"}]
    assert [r["tag"] for r in one_per_image(rows)] == ["a", "c"]


def test_describe_all_images_leaves_every_undescribed_image_pending(monkeypatch):
    client = FakeClient({"primary": [RuntimeError("500")]})

    async def close():
        return None

    client.close = close
    fake_openai = types.ModuleType("openai")
    fake_openai.AsyncOpenAI = lambda **kwargs: client
    monkeypatch.setitem(sys.modules, "openai", fake_openai)

    rows = [{**_row(), "image_id": i} for i in range(3)]
    out = asyncio.run(llm_describe.describe_all_images(
        rows, "host", "token", ["primary"], 100, 1.0, 1, 2, 6_000_000, EndpointBreaker(100),
        rounds=2, round_waits_s=(0.0,)))
    assert [r["status"] for r in out] == ["PENDING"] * 3
    assert all(r["description"] is None for r in out)
    assert len(client.calls) == 6   # 3 images x 2 passes x 1 attempt


def test_an_image_that_reaches_the_cap_is_set_aside_with_its_last_error_kind():
    client = FakeClient({"primary": [RuntimeError("500")]})
    out = _describe(client, ["primary"], max_retries=1, row=_row(attempts=9), breaker=EndpointBreaker(100),
                    rounds=1, max_attempts=10)
    assert out["status"] == "FAILED_LIMIT" and out["attempts"] == 10
    assert out["last_error_kind"] == "other" and out["description"] is None


def test_the_cap_is_not_reached_before_the_limit():
    client = FakeClient({"primary": [RuntimeError("500")]})
    async def scenario():
        limiter = _AsyncRateLimiter(rate_per_minute=6_000_000)
        return await describe_one_image(client, _row(attempts=8), asyncio.Semaphore(4), ["primary"], 100, 1.0, 1,
                                        limiter, EndpointBreaker(100), max_attempts=10)
    out = asyncio.run(scenario())
    assert out["status"] == "PENDING" and out["attempts"] == 9


def test_the_cap_counts_runs_not_retries_inside_one_run():
    client = FakeClient({"primary": [RuntimeError("500")]})

    async def scenario():
        limiter = _AsyncRateLimiter(rate_per_minute=6_000_000)
        return await describe_one_image(client, _row(), asyncio.Semaphore(4), ["primary"], 100, 1.0, 5, limiter,
                                        EndpointBreaker(100), rounds=2, max_attempts=10)
    out = asyncio.run(scenario())
    assert out["status"] == "PENDING" and out["attempts"] == 1
    assert len(client.calls) == 10   # 5 retries x 2 passes, all in this run


def test_without_a_cap_an_image_stays_pending_however_often_it_failed():
    client = FakeClient({"primary": [RuntimeError("500")]})
    out = _describe(client, ["primary"], max_retries=1, row=_row(attempts=50))
    assert out["status"] == "PENDING" and out["attempts"] == 51


def test_a_rate_limit_does_not_open_the_breaker():
    client = FakeClient({"primary": [RuntimeError("429 REQUEST_LIMIT_EXCEEDED")]})
    breaker = EndpointBreaker(1)
    for _ in range(3):
        _describe(client, ["primary"], max_retries=1, breaker=breaker)
    assert not breaker.is_open("primary")
    assert len(client.calls) == 3


def test_an_image_skipped_only_by_an_open_endpoint_does_not_use_an_attempt():
    client = FakeClient({"primary": [RuntimeError("500")]})
    breaker = EndpointBreaker(1)
    _describe(client, ["primary"], max_retries=1, breaker=breaker)
    skipped = _describe(client, ["primary"], max_retries=1, breaker=breaker, row=_row(attempts=3))
    assert skipped["last_error_kind"] == "circuit_open" and skipped["attempts"] == 3
    assert len(client.calls) == 1


def test_a_described_image_clears_its_error_kind_and_counts_the_attempt():
    client = FakeClient({"primary": [_resp("A pump.")]})
    out = _describe(client, ["primary"], row=_row(attempts=4, last_error_kind="timeout"))
    assert out["status"] == "DONE" and out["attempts"] == 5 and out["last_error_kind"] is None


def test_describe_all_images_sets_the_capped_image_aside(monkeypatch):
    client = FakeClient({"primary": [RuntimeError("500")]})

    async def close():
        return None

    client.close = close
    fake_openai = types.ModuleType("openai")
    fake_openai.AsyncOpenAI = lambda **kwargs: client
    monkeypatch.setitem(sys.modules, "openai", fake_openai)
    rows = [{**_row(attempts=9), "image_id": 0}, {**_row(attempts=2), "image_id": 1}]
    out = asyncio.run(llm_describe.describe_all_images(
        rows, "host", "token", ["primary"], 100, 1.0, 1, 2, 6_000_000, EndpointBreaker(100),
        rounds=1, max_attempts=10))
    assert [r["status"] for r in out] == ["FAILED_LIMIT", "PENDING"]
    assert [r["attempts"] for r in out] == [10, 3]


def test_a_quota_error_never_reaches_the_attempt_limit():
    client = FakeClient({"primary": [RuntimeError("429 REQUEST_LIMIT_EXCEEDED")]})
    out = _describe(client, ["primary"], max_retries=1, row=_row(attempts=9), max_attempts=10)
    assert out["status"] == "PENDING" and out["attempts"] == 9
    assert out["last_error_kind"] == "rate_limit"


def test_a_quota_error_followed_by_another_failure_in_the_run_counts():
    client = FakeClient({"primary": [RuntimeError("429 REQUEST_LIMIT_EXCEEDED")],
                         "fallback": [RuntimeError("500")]})
    out = _describe(client, ["primary", "fallback"], max_retries=1, row=_row(attempts=9), max_attempts=10)
    assert out["status"] == "FAILED_LIMIT" and out["attempts"] == 10
    assert out["last_error_kind"] == "other"


def test_a_quota_error_alone_leaves_the_attempt_count_unchanged():
    client = FakeClient({"primary": [RuntimeError("429 REQUEST_LIMIT_EXCEEDED")]})
    out = _describe(client, ["primary"], max_retries=1, row=_row(attempts=3))
    assert out["status"] == "PENDING" and out["attempts"] == 3
    assert out["description"] is None


def _described_with_cache(client, cache, row=None):
    async def scenario():
        limiter = _AsyncRateLimiter(rate_per_minute=6_000_000)
        return await describe_one_image(client, row or _row(), asyncio.Semaphore(4), ["primary"], 100, 1.0, 2, limiter,
                                        EndpointBreaker(100), timeout=5.0, cache=cache)
    return asyncio.run(scenario())


def test_a_description_is_reused_for_the_same_image_and_the_same_request():
    from llm_describe import description_cache_key
    first = _described_with_cache(FakeClient({"primary": [_resp("A wiring diagram.")]}), {})
    assert first["status"] == "DONE" and first["from_cache"] is False and first["cache_key"]
    cache = {first["cache_key"]: {"description": first["description"], "status": first["status"]}}
    silent = FakeClient({"primary": [_resp("must not be asked")]})
    again = _described_with_cache(silent, cache)
    assert again["description"] == "A wiring diagram." and again["from_cache"] is True
    assert silent.calls == [] and again["input_tokens"] == 0 and again["output_tokens"] == 0
    assert description_cache_key("prompt", b"image") != description_cache_key("prompt", b"image2")
    assert description_cache_key("prompt", b"image") != description_cache_key("prompt two", b"image")


def test_another_context_for_the_same_image_is_described_again():
    first = _described_with_cache(FakeClient({"primary": [_resp("A wiring diagram.")]}), {})
    cache = {first["cache_key"]: {"description": first["description"], "status": first["status"]}}
    client = FakeClient({"primary": [_resp("A different description.")]})
    other = _described_with_cache(client, cache, row=_row(context_text="Another page about wiring"))
    assert other["from_cache"] is False and other["description"] == "A different description." and client.calls


def test_the_images_start_on_the_endpoints_in_proportion_to_their_rates():
    chains = llm_describe.spread_chains(6, ["a", "b"], {"a": 200.0, "b": 100.0})
    assert [c[0] for c in chains].count("a") == 4 and [c[0] for c in chains].count("b") == 2
    assert all(sorted(c) == ["a", "b"] for c in chains)   # every chain keeps the other endpoint as fallback
    assert [c[0] for c in chains[:3]] != ["a", "a", "a"]  # interleaved, not one block per endpoint


def test_an_endpoint_without_rate_starts_no_image_but_stays_a_fallback():
    chains = llm_describe.spread_chains(3, ["a", "b"], {"a": 0.0, "b": 50.0})
    assert chains == [["b", "a"]] * 3


def _fake_openai(monkeypatch, client):
    async def close():
        return None

    client.close = close
    fake_openai = types.ModuleType("openai")
    fake_openai.AsyncOpenAI = lambda **kwargs: client
    monkeypatch.setitem(sys.modules, "openai", fake_openai)


def test_with_one_quota_per_endpoint_both_endpoints_describe_images(monkeypatch):
    client = FakeClient({"primary": [_resp("primary text")], "fallback": [_resp("fallback text")]})
    _fake_openai(monkeypatch, client)
    rows = [{**_row(), "image_id": i} for i in range(4)]
    out = asyncio.run(llm_describe.describe_all_images(
        rows, "host", "token", ["primary", "fallback"], 100, 1.0, 1,
        {"primary": 2, "fallback": 2}, {"primary": 6_000_000, "fallback": 6_000_000}, EndpointBreaker(100)))
    assert [r["status"] for r in out] == ["DONE"] * 4
    assert sorted(client.calls) == ["fallback", "fallback", "primary", "primary"]


def test_with_one_quota_per_endpoint_a_failing_endpoint_hands_its_images_to_the_other(monkeypatch):
    client = FakeClient({"primary": [RuntimeError("500")], "fallback": [_resp("fallback text")]})
    _fake_openai(monkeypatch, client)
    rows = [{**_row(), "image_id": i} for i in range(4)]
    out = asyncio.run(llm_describe.describe_all_images(
        rows, "host", "token", ["primary", "fallback"], 100, 1.0, 1,
        {"primary": 2, "fallback": 2}, {"primary": 6_000_000, "fallback": 6_000_000}, EndpointBreaker(100)))
    assert [r["status"] for r in out] == ["DONE"] * 4
    assert all(r["description"] == "fallback text" for r in out)


def test_with_one_shared_quota_every_image_starts_on_the_primary(monkeypatch):
    client = FakeClient({"primary": [_resp("primary text")], "fallback": [_resp("fallback text")]})
    _fake_openai(monkeypatch, client)
    rows = [{**_row(), "image_id": i} for i in range(3)]
    out = asyncio.run(llm_describe.describe_all_images(
        rows, "host", "token", ["primary", "fallback"], 100, 1.0, 1, 2, 6_000_000, EndpointBreaker(100)))
    assert [r["status"] for r in out] == ["DONE"] * 3
    assert client.calls == ["primary"] * 3


def test_a_costly_call_waits_for_as_many_tokens_as_it_takes():
    async def scenario():
        limiter = _AsyncRateLimiter(rate_per_minute=600)   # 10 tokens per second, one in the bucket at start
        loop = asyncio.get_running_loop()
        start = loop.time()
        await limiter.acquire()            # the initial token
        await limiter.acquire(cost=3.0)    # three more: about 0.3 s
        return loop.time() - start
    assert 0.2 < asyncio.run(scenario()) < 1.0

