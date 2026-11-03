# Databricks notebook source
# MAGIC %md
# MAGIC # Qualibot: chat stress test (PROD)
# MAGIC
# MAGIC Sends the golden questions to the PROD chat engine and measures what a user would get: time to the first word,
# MAGIC total time, errors. Each question runs the same steps as the app's chat route: history window and date, question
# MAGIC translation, rewrite, Vector Search, REF and citation lookups, answer on the same models with the same retries,
# MAGIC fallbacks and concurrency limit, citation markers, catalog sources, translation back. The code is the deployed
# MAGIC PROD app code, loaded in this notebook. Nothing is written: no Lakebase row, no MLflow trace, no table.
# MAGIC
# MAGIC **Parameters** (first code cell)
# MAGIC - `MODE = "rate"`: for each value of `QUESTIONS_PER_MINUTE`, questions are sent at that pace for `DURATION_MIN`
# MAGIC   minutes, whether the previous ones are answered or not, as real users do. One step per value, in order.
# MAGIC - `MODE = "burst"`: `BURST_SIZE` questions sent at the same instant.
# MAGIC - `LLM_FOR_TRANSLATION = False`: before the test, every golden question gets its translation or rewrite once, with
# MAGIC   the app's own calls; the test then reuses them, so the search runs on the same queries as in the app, without
# MAGIC   loading the translation model. `True`: each question calls it live, as the app does.
# MAGIC - `LLM_FOR_GENERATION = False`: no answer, the question stops after the search (passages ready for the model).
# MAGIC   `True`: the answer is generated, as in the app.
# MAGIC
# MAGIC The run stops after a step where more than half of the questions failed. The LLM calls run on the endpoints real
# MAGIC users share.
# MAGIC
# MAGIC Run on serverless, in the PROD workspace, with your own account.

# COMMAND ----------

# MAGIC %pip install -q -r /Workspace/.bundle/qualibot/qualibot-prod/files/requirements.txt fastapi pyyaml matplotlib "databricks-sdk>=0.102"

# COMMAND ----------

dbutils.library.restartPython()

# COMMAND ----------

MODE = "rate"                              # "rate": questions sent at a fixed pace; "burst": all at the same instant
QUESTIONS_PER_MINUTE = [30, 60, 120, 240]  # "rate": one step per value, in questions per minute
DURATION_MIN = 1                           # "rate": length of each step, in minutes
BURST_SIZE = 50                            # "burst": questions sent at the same instant
LLM_FOR_TRANSLATION = False                # False: translations made once before the test, then reused
LLM_FOR_GENERATION = True                  # False: search only, no answer generated

# COMMAND ----------

import asyncio
import copy
import json
import logging
import os
import re
import shlex
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import yaml
from databricks.sdk import WorkspaceClient

APP_CODE = '/Workspace/.bundle/qualibot/qualibot-prod/files'
GOLDEN_TABLE = 'prod_proj.qualibot.eval_golden_retrieval'
TIMEOUT_S = 300            # a question with no end after this delay counts as a timeout
STOP_FAILED_PCT = 50       # no further step after a step with more failures than this

logger = logging.getLogger('stress_test_chat')
logger.setLevel(logging.INFO)
if not logger.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter('%(asctime)s %(message)s', datefmt='%H:%M:%S'))
    logger.addHandler(_handler)
    logger.propagate = False

if MODE not in ('rate', 'burst'):
    raise ValueError('MODE must be "rate" or "burst".')
RATES = list(QUESTIONS_PER_MINUTE) if isinstance(QUESTIONS_PER_MINUTE, (list, tuple)) else [QUESTIONS_PER_MINUTE]

w = WorkspaceClient()
HOST = w.config.host.rstrip('/')


def token() -> str:
    """The notebook user's token, renewed by the SDK when it expires.

    :return: a bearer token
    """
    return w.config.authenticate()['Authorization'].split(' ', 1)[1]


def load_engine() -> dict[str, Any]:
    """The PROD app code, with the environment the app starts with (``app.yaml``, then ``target_config.env``).

    :return: the functions of the chat route and of the engine, by name
    """
    with open(f'{APP_CODE}/app.yaml', encoding='utf-8-sig') as f:
        for item in (yaml.safe_load(f) or {}).get('env') or []:
            if 'value' in item:
                os.environ[item['name']] = str(item['value'])
    target_env = f'{APP_CODE}/target_config.env'
    if os.path.exists(target_env):
        for line in open(target_env, encoding='utf-8'):
            m = re.match(r'\s*export\s+([A-Z0-9_]+)=(.*)$', line)
            if m:
                os.environ[m.group(1)] = (shlex.split(m.group(2)) or [''])[0]
    os.environ['ENV'] = 'production'
    if APP_CODE not in sys.path:
        sys.path.insert(0, APP_CODE)
    for mod in [m for m in sys.modules if m.split('.')[0] in ('server', 'utils')]:
        del sys.modules[mod]
    from server.routers.chat.helpers import (
        _apply_citation_markers, _clean_messages, _number_sources, _trim_history, _with_today_date,
    )
    from server.services.chat import translation_bridge
    from server.services.chat.config import TRANSLATE_BRIDGE_ENABLED
    from server.services.chat.engine import normalize_division, retrieval, stream_chat
    from server.services.chat.turn_log import TurnLog
    from server.services.infra.doc_catalog import augment_sources, with_document_info
    from server.services.infra.http_clients import close_shared_clients
    if not LLM_FOR_TRANSLATION:
        retrieval.translate_query = _cached('translate_query', retrieval.translate_query, lambda a: (a[2], a[3]))
        retrieval.rewrite_queries = _cached('rewrite_queries', retrieval.rewrite_queries, lambda a: json.dumps(a[2]))
        translation_bridge.translate_question_to_en = _cached(
            'translate_question_to_en', translation_bridge.translate_question_to_en, lambda a: a[0])
    return dict(locals())


if 'TRANSLATIONS' not in globals():
    TRANSLATIONS: dict[tuple, tuple[str, Any]] = {}   # kept across runs of the notebook: the golden set does not change
    TRANSLATED: set[str] = set()


def _cached(name: str, call, key_of) -> Any:
    """An app translation call that runs once per question, then gives back the same result (or the same failure).

    :param name: name of the call, part of the cache key
    :param call: the app's coroutine function
    :param key_of: positional arguments -> the part of the key that identifies the question
    :return: a coroutine function with the same signature
    """
    async def wrapper(*args, **kwargs) -> Any:
        """See ``_cached``.

        :param args: positional arguments of the app call
        :param kwargs: keyword arguments of the app call
        :return: what the first call returned
        """
        key = (name, key_of(args))
        if key not in TRANSLATIONS:
            try:
                TRANSLATIONS[key] = ('ok', await call(*args, **kwargs))
            except Exception as exc:  # noqa: BLE001 - the failure is replayed as the app would have met it
                TRANSLATIONS[key] = ('error', exc)
        kind, value = TRANSLATIONS[key]
        if kind == 'error':
            raise value
        # The bridge's context is changed later in the turn: each question gets its own copy.
        return (value[0], copy.copy(value[1])) if name == 'translate_question_to_en' else value
    return wrapper


GOLDEN = [{'content': r['question'], 'division': (r['division'] or 'ALL').upper()}
          for r in spark.table(GOLDEN_TABLE).select('question', 'division').collect() if r['question']]
if not GOLDEN:
    raise RuntimeError(f'{GOLDEN_TABLE} has no question.')
logger.info('%d golden questions | mode %s | LLM translation %s, generation %s | %s', len(GOLDEN), MODE,
            'live' if LLM_FOR_TRANSLATION else 'made once before', LLM_FOR_GENERATION,
            f'steps {RATES} questions/min, {DURATION_MIN} min each' if MODE == 'rate' else f'{BURST_SIZE} at once')

# COMMAND ----------

def _facts(log, t0: float, status: str, first: float | None, error: str = '') -> dict[str, Any]:
    """The measures of one question.

    :param log: the turn log the engine filled
    :param t0: ``time.monotonic()`` when the question was sent
    :param status: ``ok``, ``degraded`` (answered, a step did not run as configured), ``error`` or ``timeout``
    :param first: seconds to the first word the user sees, None when there was none
    :param error: what failed, empty when nothing did
    :return: one result row, without the step and the send time
    """
    return {'status': status, 'ttft_s': first, 'total_s': time.monotonic() - t0, 'error': error or None,
            'model': log.data.get('answer_endpoint'), 'cost_eur': log.data.get('cost_eur'),
            'steps_ms': dict(log.timings_ms), 'warnings': ','.join(log.codes) or None}


async def ask(question: dict[str, str], generate: bool = LLM_FOR_GENERATION) -> dict[str, Any]:
    """One question through the engine, step by step as the app's chat route runs it, without the save.

    :param question: ``{'content', 'division'}``
    :param generate: False stops after the search, before the answer
    :return: see ``_facts``
    """
    log, t0 = E['TurnLog'](), time.monotonic()
    msgs = E['_trim_history'](E['_clean_messages']([{'role': 'user', 'content': question['content']}]))
    tok, tb, ctx = token(), E['translation_bridge'], None
    if E['TRANSLATE_BRIDGE_ENABLED']:
        with log.timed('translate_in'):
            en, ctx = await tb.translate_question_to_en(question['content'], HOST, tok,
                                                        record=log.model_io.setdefault('translate_in', {}))
        if ctx.error:
            log.issue('translate_in_failed', 'translate_in', ctx.error)
        if ctx.needs_translation:
            msgs = [{'role': 'user', 'content': en}]
    if not generate:
        try:
            await E['retrieval'].retrieve_for_turn(HOST, tok, E['normalize_division'](question['division']),
                                                   E['retrieval']._clean_history(E['_with_today_date'](msgs)), log=log)
        except Exception as exc:  # noqa: BLE001 - a failed search is a result
            return _facts(log, t0, 'error', None, f'{type(exc).__name__}: {str(exc)[:200]}')
        return _facts(log, t0, log.finish('ok'), time.monotonic() - t0)

    stream = E['stream_chat'](HOST, tok, question['division'], E['_with_today_date'](msgs),
                              answer_language=tb.answer_language(ctx, question['content']), log=log)
    text, sources, citations, first = [], [], [], None
    try:
        async for chunk in stream:
            if chunk.startswith('data: [DONE]'):
                if not text:
                    log.fail('llm', 'EmptyAnswer', 'stream ended with no text')
                    return _facts(log, t0, 'error', first, 'empty answer')
                final = E['_apply_citation_markers'](''.join(text), citations)
                sources = await asyncio.to_thread(E['augment_sources'], ''.join(text), sources)
                E['_number_sources'](sources, citations)
                E['with_document_info'](sources)
                if ctx:
                    with log.timed('translate_back'):
                        await tb.translate_answer_back(final, ctx, HOST, tok,
                                                       record=log.model_io.setdefault('translate_back', {}))
                status = log.finish('ok')
                return _facts(log, t0, status, first if first is not None else time.monotonic() - t0)
            if not chunk.startswith('data: '):
                continue
            try:
                ev = json.loads(chunk[6:].strip())
            except json.JSONDecodeError:
                continue
            if ev.get('type') == 'response.output_text.delta':
                text.append(ev.get('delta', ''))
                # A translated question shows no text before the translated answer.
                if first is None and not (ctx and ctx.needs_translation):
                    first = time.monotonic() - t0
            elif ev.get('type') == 'sources':
                sources = ev.get('sources') if isinstance(ev.get('sources'), list) else []
                citations = ev.get('citations') if isinstance(ev.get('citations'), list) else []
            elif ev.get('type') == 'error':
                return _facts(log, t0, 'error', first, ev.get('error', ''))
        return _facts(log, t0, 'error', first, 'stream ended before the answer was complete')
    finally:
        await stream.aclose()


async def ask_timed(question: dict[str, str]) -> dict[str, Any]:
    """``ask`` bounded by ``TIMEOUT_S``; an exception is a result, never raised.

    :param question: ``{'content', 'division'}``
    :return: see ``_facts``
    """
    t0 = time.monotonic()
    try:
        return await asyncio.wait_for(ask(question), TIMEOUT_S)
    except asyncio.TimeoutError:
        return {'status': 'timeout', 'total_s': TIMEOUT_S, 'error': f'no end after {TIMEOUT_S} s'}
    except Exception as exc:  # noqa: BLE001
        return {'status': 'error', 'total_s': time.monotonic() - t0, 'error': f'{type(exc).__name__}: {str(exc)[:200]}'}


async def translate_all() -> None:
    """Each golden question's translation or rewrite, made once with the app's calls (8 at a time), before the test."""
    gate = asyncio.Semaphore(8)

    async def one(q: dict[str, str]) -> None:
        """Run the question up to the search: its translation calls fill ``TRANSLATIONS``.

        :param q: golden question
        """
        async with gate:
            await ask(q, generate=False)

    todo = [q for q in GOLDEN if q['content'] not in TRANSLATED]
    if todo:
        logger.info('translating %d questions once, before the test', len(todo))
        await asyncio.gather(*(one(q) for q in todo))
        TRANSLATED.update(q['content'] for q in todo)
    failed = sum(kind == 'error' for kind, _ in TRANSLATIONS.values())
    logger.info('translations ready: %d calls stored, %d failed (replayed as failures)', len(TRANSLATIONS), failed)


async def run() -> list[dict[str, Any]]:
    """Every step, one after the other: send its questions on schedule, then wait for all of them.

    :return: one row per question
    """
    rows, n = [], 0
    steps = [(f'{r}/min', r) for r in RATES] if MODE == 'rate' else [(f'burst {BURST_SIZE}', None)]
    try:
        if not LLM_FOR_TRANSLATION:
            await translate_all()
        t_run = time.monotonic()
        for label, rate in steps:
            count = round(rate * DURATION_MIN) if rate else BURST_SIZE
            gap = 60.0 / rate if rate else 0.0
            t_step = time.monotonic()
            logger.info('step %s: %d questions', label, count)

            async def one(i: int) -> None:
                """Send question ``i`` of the step at its time and record its outcome.

                :param i: rank of the question in the step
                """
                await asyncio.sleep(max(0.0, t_step + i * gap - time.monotonic()))
                q = GOLDEN[(n + i) % len(GOLDEN)]
                sent = time.monotonic() - t_run
                out = await ask_timed(q)
                rows.append({'step': label, 'rate': rate, 'sent_s': sent, 'question': q['content'][:200], **out})

            await asyncio.gather(*(one(i) for i in range(count)))
            n += count
            step_rows = [r for r in rows if r['step'] == label]
            failed = sum(r['status'] not in ('ok', 'degraded') for r in step_rows)
            logger.info('step %s done: %d failed out of %d', label, failed, len(step_rows))
            if failed * 100 > STOP_FAILED_PCT * len(step_rows):
                logger.warning('stopped: more than %d %% failed at step %s', STOP_FAILED_PCT, label)
                break
    finally:
        await E['close_shared_clients']()
        await E['translation_bridge'].shutdown_http_client()
    return rows


E = load_engine()   # fresh modules each run: their HTTP clients and locks belong to the event loop that created them
with ThreadPoolExecutor(max_workers=1) as _ex:   # the notebook may already run an event loop
    DF = pd.DataFrame(_ex.submit(asyncio.run, run()).result())
for _col in ('ttft_s', 'cost_eur', 'steps_ms', 'error', 'model', 'warnings'):
    if _col not in DF:
        DF[_col] = None

# COMMAND ----------

# MAGIC %md
# MAGIC ## Results

# COMMAND ----------

ANSWERED = ('ok', 'degraded')
COLORS = {'ok': '#2E7D5B', 'degraded': '#D99A1E', 'error': '#C8453B', 'timeout': '#5B6770'}
plt.rcParams.update({'figure.dpi': 110, 'axes.spines.top': False, 'axes.spines.right': False, 'axes.grid': True,
                     'grid.alpha': .25, 'axes.titleweight': 'bold', 'axes.titlesize': 12, 'font.size': 10,
                     'legend.frameon': False})
STEPS = list(dict.fromkeys(DF['step']))
DF['end_s'] = DF['sent_s'] + DF['total_s']


def pct(s: pd.Series, p: float) -> float:
    """Percentile ``p`` of ``s``, rounded to 0.1.

    :param s: values in seconds
    :param p: percentile between 0 and 1
    :return: the percentile, NaN when ``s`` holds no value
    """
    s = s.dropna()
    return round(float(s.quantile(p)), 1) if len(s) else np.nan


def in_flight(df: pd.DataFrame, t: np.ndarray) -> np.ndarray:
    """Questions in progress at each instant of ``t``.

    :param df: result rows with ``sent_s`` and ``end_s``
    :param t: instants, in seconds since the start
    :return: one count per instant
    """
    return ((df['sent_s'].to_numpy()[None, :] <= t[:, None]) & (df['end_s'].to_numpy()[None, :] > t[:, None])).sum(1)


summary = []
for step in STEPS:
    d = DF[DF['step'] == step]
    ok = d[d['status'].isin(ANSWERED)]
    span = max(1e-9, d['end_s'].max() - d['sent_s'].min())
    summary.append({'step': step, 'questions': len(d), 'answered %': round(len(ok) * 100 / len(d), 1),
                    'errors': int((d['status'] == 'error').sum()), 'timeouts': int((d['status'] == 'timeout').sum()),
                    'first word p50 s': pct(ok['ttft_s'], .5), 'first word p95 s': pct(ok['ttft_s'], .95),
                    'total p50 s': pct(ok['total_s'], .5), 'total p95 s': pct(ok['total_s'], .95),
                    'answered / min': round(len(ok) / span * 60, 1),
                    'peak in progress': int(in_flight(d, np.arange(d['sent_s'].min(), d['end_s'].max(), .25)).max()),
                    'cost €': round(float(d['cost_eur'].fillna(0).sum()), 2)})
SUMMARY = pd.DataFrame(summary)
display(SUMMARY)

# Where it breaks: latency and failures against the pace sent.
fig, (a1, a2) = plt.subplots(1, 2, figsize=(13, 4.5))
x = np.arange(len(STEPS))
for col, color in [('first word p50 s', '#90CAF9'), ('first word p95 s', '#1F4E79'),
                   ('total p50 s', '#A5D6A7'), ('total p95 s', '#2E7D5B')]:
    a1.plot(x, SUMMARY[col], marker='o', linewidth=2, color=color, label=col.replace(' s', ''))
a1.set_xticks(x, STEPS)
a1.set_ylabel('seconds')
a1.set_title('Latency per step')
a1.legend()
a2.bar(x, 100 - SUMMARY['answered %'], color='#C8453B', alpha=.85, label='failed %')
a2.set_ylim(0, 100)
a2.set_ylabel('failed %')
a2b = a2.twinx()
a2b.plot(x, SUMMARY['answered / min'], marker='o', color='#1F4E79', linewidth=2, label='answered / min')
if MODE == 'rate':
    a2b.plot(x, RATES[:len(STEPS)], linestyle='--', color='#5B6770', label='sent / min')
a2b.set_ylabel('questions / min')
a2b.grid(False)
a2.set_xticks(x, STEPS)
a2.set_title('Failures and throughput per step')
h1, l1 = a2.get_legend_handles_labels()
h2, l2 = a2b.get_legend_handles_labels()
a2.legend(h1 + h2, l1 + l2, loc='upper left')
plt.tight_layout()
plt.show()

# Over time: questions in progress, and the total time of each question at the moment it ended.
t = np.arange(0, DF['end_s'].max() + .25, .25)
fig, (b1, b2) = plt.subplots(2, 1, figsize=(13, 7), sharex=True)
b1.step(t, in_flight(DF, t), where='post', color='#1F4E79', linewidth=2)
b1.set_ylabel('questions in progress')
b1.set_title('Load over time')
for step in STEPS:
    start = DF.loc[DF['step'] == step, 'sent_s'].min()
    for ax in (b1, b2):
        ax.axvline(start, color='#999', linewidth=.8, linestyle=':')
    b1.annotate(step, (start, 0), xytext=(3, 3), textcoords='offset points', fontsize=9, color='#555')
for status, d in DF.groupby('status'):
    b2.scatter(d['end_s'], d['total_s'], s=16, color=COLORS.get(status, '#999'), alpha=.8, label=status)
b2.set_xlabel('seconds since the start')
b2.set_ylabel('total time (s)')
b2.set_title('Total time of each question')
b2.legend(loc='upper left')
plt.tight_layout()
plt.show()

# Where the time of a question goes, per step (medians).
STAGES = [('translate_in', 'question translation', '#B0BEC5'), ('rewrite', 'rewrite / translation', '#90CAF9'),
          ('search', 'search', '#1F4E79'), ('queue_wait', 'wait for an answer slot', '#C8453B'),
          ('generation', 'answer generation', '#2E7D5B'), ('translate_back', 'answer translation', '#80CBC4')]
T = pd.DataFrame([r if isinstance(r, dict) else {} for r in DF['steps_ms']], index=DF.index)
if 'retrieval' in T:
    T['search'] = T['retrieval'] - (T['rewrite'].fillna(0) if 'rewrite' in T else 0)
    fig, ax = plt.subplots(figsize=(13, 1.6 + .6 * len(STEPS)))
    for i, step in enumerate(STEPS):
        left = 0.0
        for key, label, color in STAGES:
            if key in T:
                v = float(T.loc[DF['step'] == step, key].fillna(0).median()) / 1000
                ax.barh(i, v, left=left, color=color, label=label if i == 0 else None)
                left += v
    ax.set_yticks(range(len(STEPS)), STEPS)
    ax.invert_yaxis()
    ax.set_xlabel('seconds (median of each step)')
    ax.set_title('Where the time of a question goes')
    ax.legend(loc='upper center', bbox_to_anchor=(.5, -.3), ncol=6, fontsize=8)
    plt.tight_layout()
    plt.show()

failures = DF[~DF['status'].isin(ANSWERED)]
if len(failures):
    display(failures.assign(error=failures['error'].fillna('').str.slice(0, 150))
            .groupby(['step', 'status', 'error']).size().rename('questions').reset_index())
