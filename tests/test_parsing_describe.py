"""Image description of the parsing pipeline (utils/parsing_pipeline/llm_describe.py): the endpoint chain, the passes,
the retries and the timeouts, against a fake client with no waits. An image that no endpoint describes stays PENDING."""
import asyncio
import os
import sys
import types

import pytest

PARSING = os.path.join(os.path.dirname(__file__), "..", "utils", "parsing_pipeline")
sys.path.insert(0, PARSING)
for key, value in {"PARSING_CATALOG_SCHEMA": "cat.sch", "PARSING_INTRAQUAL_BRONZE": "bronze.sch",
                   "PARSING_VOLUME_ROOT_PATH": "/Volumes/c/s/root", "PARSING_VOLUME_BASE_PATH": "/Volumes/c/s/img",
                   "PARSING_OFFLINE_MODELS": "/models"}.items():
    os.environ.setdefault(key, value)

pytest.importorskip("tenacity")

import llm_describe
from llm_describe import _AsyncRateLimiter, describe_one_image, endpoint_chain, round_wait_s


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


def _row(volume_path=__file__):
    return {"IDDOC": 1, "image_id": 0, "volume_path": volume_path, "label": "picture", "context_text": "",
            "division": "AS", "niveau_plus_1": "Procédés"}


def _describe(client, endpoints, max_retries=2, rounds=1, round_waits_s=(), timeout=5.0, row=None):
    async def scenario():
        limiter = _AsyncRateLimiter(rate_per_minute=6_000_000)
        return await describe_one_image(client, row or _row(), asyncio.Semaphore(4), endpoints, 100, 1.0,
                                        max_retries, limiter, timeout=timeout, rounds=rounds,
                                        round_waits_s=round_waits_s)
    return asyncio.run(scenario())


def test_the_chain_keeps_the_primary_first_and_each_endpoint_once():
    assert endpoint_chain("a", " b , a,, c ") == ["a", "b", "c"]
    assert endpoint_chain("a", "") == ["a"]
    assert endpoint_chain("a", None) == ["a"]


def test_the_waits_grow_between_passes_and_the_first_pass_has_none():
    assert [round_wait_s(i, (5.0, 15.0, 30.0)) for i in range(4)] == [0.0, 5.0, 15.0, 30.0]
    assert round_wait_s(1, ()) == 0.0


def test_a_transient_failure_is_retried_on_the_same_endpoint():
    client = FakeClient({"primary": [RuntimeError("503 busy"), RuntimeError("timeout"), _resp("A pump with a label.")]})
    out = _describe(client, ["primary"], max_retries=5)
    assert out["status"] == "DONE" and out["description"] == "A pump with a label."
    assert out["input_tokens"] == 10 and out["output_tokens"] == 5
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
    assert "failed to read image" in out["last_error"] and client.calls == []


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
        rows, "host", "token", ["primary"], 100, 1.0, 1, 2, 6_000_000, rounds=2, round_waits_s=(0.0,)))
    assert [r["status"] for r in out] == ["PENDING"] * 3
    assert all(r["description"] is None for r in out)
    assert len(client.calls) == 6   # 3 images x 2 passes x 1 attempt
