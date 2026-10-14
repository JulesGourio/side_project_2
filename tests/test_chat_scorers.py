"""Tests for the chat quality scorers (utils/evaluation/chat_scorers.py).

Coverage:
  - the sample draw is stable, respects the rate, and thumbs-down / degraded turns are always judged
  - the token estimate counts the passages once per context judge
  - judge calls are spaced, bounded in parallel, retried on 429 only, and a failure does not stop the batch
  - free scorers on real traces written by mlflow_turns, by calling each scorer directly (no model call)
"""

import os
import sys

import pytest

pytest.importorskip('mlflow')

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'utils', 'evaluation'))
import chat_scorers


def test_sampling_is_stable_and_close_to_the_rate():
    ids = [f'tr-{i}' for i in range(2000)]
    first = [i for i in ids if chat_scorers.sampled(i, 'retrieval_groundedness', 0.25)]
    again = [i for i in ids if chat_scorers.sampled(i, 'retrieval_groundedness', 0.25)]
    assert first == again and 400 < len(first) < 600
    assert all(chat_scorers.sampled(i, 'x', 1.0) for i in ids[:10]) and not any(chat_scorers.sampled(i, 'x', 0.0) for i in ids[:10])
    other = [i for i in ids if chat_scorers.sampled(i, 'honest_limits', 0.25)]
    assert other != first                                           # judges draw independently


def test_plan_always_reads_thumbs_down_and_degraded():
    traces = [{'trace_id': f'tr-{i}', 'status': 'ok', 'vote': ''} for i in range(400)]
    traces += [{'trace_id': 'down', 'status': 'ok', 'vote': 'down'}, {'trace_id': 'bad', 'status': 'degraded', 'vote': ''}]
    plan = chat_scorers.plan(traces, {'retrieval_groundedness': 0.0, 'honest_limits': 0.1})
    assert plan['retrieval_groundedness'] == ['down', 'bad']
    assert {'down', 'bad'} <= set(plan['honest_limits']) and len(plan['honest_limits']) < 80


def test_estimate_counts_the_passages_for_context_judges_only():
    plan = {'retrieval_groundedness': ['a', 'b'], 'relevance_to_query': ['a', 'b']}
    assert chat_scorers.estimated_input_tokens(plan, {'a': 40_000, 'b': 80_000}) == 10_000 + 20_000 + 2 * 1500


def test_default_rates_cover_every_judge_and_start_at_100_percent():
    judges = chat_scorers.judge_scorers('databricks:/some-endpoint')
    assert set(judges) == set(chat_scorers.SAMPLE_RATES) == set(chat_scorers.TARGET_SAMPLE_RATES)
    assert set(chat_scorers.SAMPLE_RATES.values()) == {1.0}
    assert all(0 < r < 1 for r in chat_scorers.TARGET_SAMPLE_RATES.values())
    assert chat_scorers.CONTEXT_JUDGES <= set(judges)
    assert 'retrieval_relevance' not in chat_scorers.judge_scorers('', retrieval_relevance=False)


def test_throttle_spaces_the_calls():
    now, slept = [0.0], []
    throttle = chat_scorers.Throttle(2.0, clock=lambda: now[0], sleep=lambda s: slept.append(s))
    for _ in range(3):
        throttle.wait()
    assert slept == [0.5, 1.0]


def test_rate_limited_calls_are_retried_with_doubling_waits():
    waits, calls = [], []

    class Limited(Exception):
        status_code = 429

    def flaky():
        calls.append(1)
        if len(calls) < 3:
            raise Limited('slow down')
        return 'ok'
    assert chat_scorers.with_backoff(flaky, retries=3, base_s=2.0, sleep=waits.append) == 'ok'
    assert waits == [2.0, 4.0]


def test_other_errors_are_not_retried():
    with pytest.raises(ValueError):
        chat_scorers.with_backoff(lambda: (_ for _ in ()).throw(ValueError('bad')), retries=3, sleep=lambda s: None)


def test_score_all_keeps_going_after_a_failure_and_never_exceeds_the_worker_cap():
    import threading
    live, peak, lock = [0], [0], threading.Lock()

    def judge(trace):
        with lock:
            live[0] += 1
            peak[0] = max(peak[0], live[0])
        try:
            if trace == 'bad':
                raise RuntimeError('endpoint refused')
            return type('F', (), {'value': 'yes', 'rationale': 'fine', 'error': None})()
        finally:
            with lock:
                live[0] -= 1

    jobs = [('j', judge, t) for t in ('a', 'bad', 'b', 'c', 'd', 'e')]
    results = chat_scorers.score_all(jobs, max_workers=2, calls_per_second=0, retries=0)
    assert [r['trace'] for r in results] == ['a', 'bad', 'b', 'c', 'd', 'e']
    assert results[1]['error'].startswith('RuntimeError') and results[1]['value'] is None
    assert [r['value'] for r in results if not r['error']] == ['yes'] * 5
    assert peak[0] <= 2


class _Info:
    def __init__(self, tags):
        self.tags = tags


class _Trace:
    def __init__(self, **tags):
        self.info = _Info(tags)


def test_free_scorers_on_tags():
    ok = _Trace(question_lang_guess='fr', answer_lang_guess='fr', passages_sent='3', cited_count='2', status='ok',
                llm_fallback='false', first_token_ms='4200', cost_eur='0.0041')
    bad = _Trace(question_lang_guess='fr', answer_lang_guess='en', passages_sent='3', cited_count='0', status='degraded',
                 warnings='llm_fallback', llm_fallback='true', answer_endpoint='gpt-5-6-luna', first_token_ms='15000')
    value = lambda s, t: s(trace=t).value  # noqa: E731
    assert [value(chat_scorers.language_match, t) for t in (ok, bad)] == ['yes', 'no']
    assert [value(chat_scorers.has_citation, t) for t in (ok, bad)] == ['yes', 'no']
    assert [value(chat_scorers.degraded_turn, t) for t in (ok, bad)] == ['no', 'yes']
    assert [value(chat_scorers.used_fallback_model, t) for t in (ok, bad)] == ['no', 'yes']
    assert [value(chat_scorers.first_token_s, t) for t in (ok, bad)] == [4.2, 15.0]
    assert [value(chat_scorers.first_token_in_budget, t) for t in (ok, bad)] == ['yes', 'no']
    assert value(chat_scorers.turn_cost_eur, ok) == 0.0041
    unknown = _Trace(passages_sent='0')
    assert value(chat_scorers.language_match, unknown) is None and value(chat_scorers.has_citation, unknown) is None


def test_free_scorers_run_on_traces_written_by_the_app(tmp_path, monkeypatch):
    pytest.importorskip('sqlalchemy')
    pytest.importorskip('alembic')
    import mlflow
    from server.services.chat import mlflow_turns
    from server.services.chat.turn_log import TurnLog

    old = mlflow.get_tracking_uri()
    mlflow.set_tracking_uri(f'sqlite:///{tmp_path}/m.db')
    try:
        experiment_id = mlflow.create_experiment('chat', artifact_location=str(tmp_path))
        monkeypatch.setattr(mlflow_turns, '_experiment', {'chat': experiment_id})
        monkeypatch.setattr(mlflow_turns, '_language_guess', lambda text: 'fr')
        log = TurnLog()
        log.data.update({'question': 'Quelle est la procédure ?', 'division': 'AS', 'passages_sent': 2,
                         'citations_count': 1, 'cost_eur': 0.004})
        log.timings_ms.update({'total': 3000, 'first_token': 1500})
        log.answer = 'Voir QP-1518 [1].'
        log.finish('ok')
        mlflow_turns._write_trace(log, 'chat')
        mlflow.flush_trace_async_logging()
        traces = mlflow.search_traces(locations=[experiment_id], return_type='list')
        assert len(traces) == 1
        jobs = [(f.name, f, traces[0]) for f in chat_scorers.FREE_SCORERS]
        results = chat_scorers.score_all(jobs, max_workers=2, calls_per_second=0, retries=0)
        verdicts = {r['name']: r['value'] for r in results if not r['error']}
        assert not [r for r in results if r['error']]
    finally:
        mlflow.set_tracking_uri(old)
    assert verdicts['language_match'] == 'yes' and verdicts['has_citation'] == 'yes'
    assert verdicts['first_token_s'] == 1.5 and verdicts['turn_cost_eur'] == 0.004
