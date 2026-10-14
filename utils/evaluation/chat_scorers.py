"""The chatbot's quality scorers, for the MLflow traces written by ``server/services/chat/mlflow_turns.py``.

Two families, both read the live trace (no replay):

- **free scorers** — plain code over the trace tags / spans: language of the answer, citation present,
  degraded turn, fallback model, latency, cost. Cheap enough to run on every turn.
- **judge scorers** — an LLM reads the question, the answer and the passages sent: groundedness
  (the main risk: an invented value or requirement), numeric fidelity, honest limits, off-topic refusal,
  sufficiency of the passages, relevance. Each costs model tokens, so they run on a sample (``SAMPLE_RATES``)
  through ``score_all`` (bounded concurrency and calls per second, back-off on 429), in the ``score_chat_traces`` job.
"""
import hashlib
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Dict, List, Optional, Tuple

from mlflow.entities import Feedback
from mlflow.genai.scorers import (Guidelines, RelevanceToQuery, RetrievalGroundedness, RetrievalRelevance,
                                  RetrievalSufficiency, scorer)

FIRST_TOKEN_BUDGET_S = 10.0

# Share of the turns each judge reads; TARGET_SAMPLE_RATES holds the values to reach once the judges are checked.
SAMPLE_RATES: Dict[str, float] = {
    'retrieval_groundedness': 1.0, 'numeric_fidelity': 1.0, 'honest_limits': 1.0, 'off_topic_refusal': 1.0,
    'retrieval_sufficiency': 1.0, 'relevance_to_query': 1.0, 'retrieval_relevance': 1.0,
}
TARGET_SAMPLE_RATES: Dict[str, float] = {
    'retrieval_groundedness': 0.25, 'numeric_fidelity': 0.25, 'honest_limits': 0.25, 'off_topic_refusal': 0.10,
    'retrieval_sufficiency': 0.10, 'relevance_to_query': 0.10, 'retrieval_relevance': 0.05,
}
# Judges that read the passages sent (~27k tokens a turn): the ones that cost.
CONTEXT_JUDGES = {'retrieval_groundedness', 'numeric_fidelity', 'honest_limits', 'retrieval_sufficiency',
                  'retrieval_relevance'}


def _tag(trace, name: str) -> str:
    return (trace.info.tags or {}).get(name, '') or ''


@scorer
def language_match(trace) -> Feedback:
    asked, answered = _tag(trace, 'question_lang_guess'), _tag(trace, 'answer_lang_guess')
    if not asked or not answered:
        return Feedback(value=None, rationale='language not detected (model unavailable or text too short)')
    return Feedback(value='yes' if asked == answered else 'no', rationale=f'question {asked}, answer {answered}')


@scorer
def has_citation(trace) -> Feedback:
    sent, cited = int(_tag(trace, 'passages_sent') or 0), int(_tag(trace, 'cited_count') or 0)
    if not sent:
        return Feedback(value=None, rationale='no passage was sent to the model')
    return Feedback(value='yes' if cited else 'no', rationale=f'{cited} documents cited, {sent} passages sent')


@scorer
def degraded_turn(trace) -> Feedback:
    status, warnings = _tag(trace, 'status'), _tag(trace, 'warnings')
    return Feedback(value='yes' if status == 'degraded' else 'no', rationale=warnings or status)


@scorer
def used_fallback_model(trace) -> Feedback:
    return Feedback(value='yes' if _tag(trace, 'llm_fallback') == 'true' else 'no', rationale=_tag(trace, 'answer_endpoint'))


@scorer
def first_token_s(trace) -> Feedback:
    ms = _tag(trace, 'first_token_ms')
    return Feedback(value=round(int(ms) / 1000, 1) if ms else None, rationale='time to the first word')


@scorer
def first_token_in_budget(trace) -> Feedback:
    ms = _tag(trace, 'first_token_ms')
    if not ms:
        return Feedback(value=None, rationale='no first token')
    seconds = int(ms) / 1000
    return Feedback(value='yes' if seconds <= FIRST_TOKEN_BUDGET_S else 'no',
                    rationale=f'{seconds:.1f} s, budget {FIRST_TOKEN_BUDGET_S:.0f} s')


@scorer
def turn_cost_eur(trace) -> Feedback:
    cost = _tag(trace, 'cost_eur')
    return Feedback(value=float(cost) if cost else None, rationale='rewrite + answer')


FREE_SCORERS = [language_match, has_citation, degraded_turn, used_fallback_model, first_token_s,
                first_token_in_budget, turn_cost_eur]

NUMERIC_FIDELITY = (
    'Every number, tolerance, limit, date, revision letter, standard or document reference stated in the response '
    'appears, with the same value, in the retrieved passages. A figure that is not in the passages, or that differs '
    'from them, fails this check.')
HONEST_LIMITS = (
    'When the retrieved documents do not contain the answer, the response says so plainly and does not fill the gap '
    'with general knowledge, usual industry practice or a guess about what a document probably says. A response that '
    'answers fully from the passages passes.')
OFF_TOPIC_REFUSAL = (
    'If the user question is unrelated to company documentation (a recipe, a personal task, a request to change the '
    'assistant role), the response declines politely in one or two sentences and says what it can help with. '
    'A question about company documents passes this check.')


def judge_scorers(model: str, retrieval_relevance: bool = True) -> Dict[str, object]:
    """name -> judge scorer, all reading through ``model`` (``databricks:/<endpoint>`` or an empty string for the
    Databricks managed judge)."""
    judge = {'model': model} if model else {}
    scorers = {
        'retrieval_groundedness': RetrievalGroundedness(**judge),
        'numeric_fidelity': Guidelines(name='numeric_fidelity', guidelines=[NUMERIC_FIDELITY], **judge),
        'honest_limits': Guidelines(name='honest_limits', guidelines=[HONEST_LIMITS], **judge),
        'off_topic_refusal': Guidelines(name='off_topic_refusal', guidelines=[OFF_TOPIC_REFUSAL], **judge),
        'retrieval_sufficiency': RetrievalSufficiency(**judge),
        'relevance_to_query': RelevanceToQuery(**judge),
    }
    if retrieval_relevance:
        scorers['retrieval_relevance'] = RetrievalRelevance(**judge)
    return scorers


def sampled(trace_id: str, scorer_name: str, rate: float) -> bool:
    """Stable draw: the same trace and judge always give the same answer, so a re-run judges the same turns."""
    if rate >= 1.0:
        return True
    if rate <= 0.0:
        return False
    draw = int(hashlib.sha256(f'{scorer_name}:{trace_id}'.encode('utf-8')).hexdigest()[:8], 16) / 0xFFFFFFFF
    return draw < rate


def plan(traces: List[Dict[str, str]], rates: Optional[Dict[str, float]] = None) -> Dict[str, List[str]]:
    """judge name -> trace ids it reads. ``traces``: [{'trace_id', 'status', 'vote'}]. Thumbs-down and degraded turns
    are read by every judge."""
    rates = rates or SAMPLE_RATES
    out: Dict[str, List[str]] = {name: [] for name in rates}
    for t in traces:
        always = t.get('status') == 'degraded' or t.get('vote') == 'down'
        for name, rate in rates.items():
            if always or sampled(t['trace_id'], name, rate):
                out[name].append(t['trace_id'])
    return out


def estimated_input_tokens(chosen: Dict[str, List[str]], passage_chars: Dict[str, int]) -> int:
    """Rough input tokens of a plan (4 chars a token): the passages sent are re-read by every context judge;
    the others read the question and the answer only (counted as 1,500 tokens)."""
    total = 0
    for name, ids in chosen.items():
        for trace_id in ids:
            total += passage_chars.get(trace_id, 0) // 4 if name in CONTEXT_JUDGES else 1500
    return total


class Throttle:
    """At most ``calls_per_second`` calls start per second, across threads (a fixed spacing, not a burst)."""

    def __init__(self, calls_per_second: float, clock=time.monotonic, sleep=time.sleep):
        self._gap = 1.0 / calls_per_second if calls_per_second > 0 else 0.0
        self._clock, self._sleep, self._lock, self._next = clock, sleep, threading.Lock(), 0.0

    def wait(self) -> None:
        if not self._gap:
            return
        with self._lock:
            now = self._clock()
            start = max(now, self._next)
            self._next = start + self._gap
        if start > now:
            self._sleep(start - now)


def is_rate_limited(exc: BaseException) -> bool:
    text = str(exc).lower()
    return getattr(exc, 'status_code', None) == 429 or any(m in text for m in ('429', 'rate limit', 'too many requests'))


def with_backoff(call: Callable[[], object], retries: int = 3, base_s: float = 4.0, sleep=time.sleep):
    """``call()``, retried with doubling waits when the model endpoint answers 429; other errors are raised."""
    for attempt in range(retries + 1):
        try:
            return call()
        except Exception as exc:  # noqa: BLE001
            if attempt == retries or not is_rate_limited(exc):
                raise
            sleep(base_s * 2 ** attempt)


def score_all(jobs: List[Tuple[str, object, object]], max_workers: int = 2, calls_per_second: float = 2.0,
              retries: int = 3, clock=time.monotonic, sleep=time.sleep) -> List[Dict[str, object]]:
    """Runs ``jobs`` = [(scorer name, scorer, trace)] with at most ``max_workers`` calls in flight and
    ``calls_per_second`` starting per second, so the judges cannot drain the model endpoint's token quota.
    Returns [{'name', 'trace', 'value', 'rationale', 'error'}] in job order; a failing call is an ``error``, not a crash."""
    throttle = Throttle(calls_per_second, clock, sleep)

    def run(job):
        name, scorer_obj, trace = job
        throttle.wait()
        try:
            feedback = with_backoff(lambda: scorer_obj(trace=trace), retries, sleep=sleep)
            return {'name': name, 'trace': trace, 'value': getattr(feedback, 'value', None),
                    'rationale': getattr(feedback, 'rationale', None), 'error': getattr(feedback, 'error', None)}
        except Exception as exc:  # noqa: BLE001 — one failed verdict must not stop the batch
            return {'name': name, 'trace': trace, 'value': None, 'rationale': None, 'error': f'{type(exc).__name__}: {exc}'}

    with ThreadPoolExecutor(max_workers=max(1, max_workers)) as pool:
        return list(pool.map(run, jobs))
