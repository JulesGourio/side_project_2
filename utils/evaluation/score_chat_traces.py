# Databricks notebook source
# MAGIC %md
# MAGIC # Qualibot — quality scoring of the live chat traces
# MAGIC
# MAGIC The app writes one MLflow trace per chat turn (`server/services/chat/mlflow_turns.py`): question, answer, steps,
# MAGIC passages sent to the model, tags, and the user's thumbs up / down as a `user_vote` assessment. This job scores those
# MAGIC traces **after the fact, in batches** — never inside a chat turn — and writes the verdicts back on the traces and to
# MAGIC `results_table`.
# MAGIC
# MAGIC | Family | Scorers | Cost |
# MAGIC |---|---|---|
# MAGIC | free (code, on every turn) | `language_match`, `has_citation`, `degraded_turn`, `used_fallback_model`, `first_token_s`, `first_token_in_budget`, `turn_cost_eur` | none |
# MAGIC | judges (LLM, on a sample) | `retrieval_groundedness`, `numeric_fidelity`, `honest_limits`, `off_topic_refusal`, `retrieval_sufficiency`, `relevance_to_query`, `retrieval_relevance` | tokens of `judge_model` |
# MAGIC
# MAGIC **Protecting the token quota** (the judge shares the model endpoint's limits with the chat): the plan is printed with an
# MAGIC estimate of input tokens first (`dry_run` = stop there); the run refuses to start above `max_input_tokens`; judge calls are
# MAGIC throttled by `max_workers` calls in flight and `calls_per_second` starting per second, 429s are retried with doubling waits.
# MAGIC Turns with a thumbs down or a degraded status are read by every judge, whatever the sample.
# MAGIC `sample_rates` (JSON, e.g. `{"retrieval_groundedness": 0.25}`) overrides `chat_scorers.SAMPLE_RATES`; the target
# MAGIC rates are `chat_scorers.TARGET_SAMPLE_RATES`.
# MAGIC
# MAGIC ## Technical debt
# MAGIC 1. Judge prompts are MLflow's; their token cost per turn is not measured yet.
# MAGIC 2. `databricks:/<endpoint>` judges need MLflow >= 3.4; with an older MLflow leave `judge_model` empty (managed judge).

# COMMAND ----------

# MAGIC %pip install -q "mlflow[databricks]>=3.4" "databricks-sdk>=0.102"
# MAGIC dbutils.library.restartPython()

# COMMAND ----------

# MAGIC %md
# MAGIC ## Configuration

# COMMAND ----------

import json
import logging
import sys
from datetime import datetime, timedelta, timezone

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
logger = logging.getLogger('score_chat_traces')

dbutils.widgets.removeAll()
dbutils.widgets.text('app_code_path', '/Workspace/.bundle/qualibot/dev/files')
dbutils.widgets.text('experiment', '')
dbutils.widgets.text('warehouse_id', '', 'SQL warehouse that reads the Unity Catalog trace tables')
dbutils.widgets.text('days', '7')
dbutils.widgets.text('min_date', '', 'Earliest trace kept, YYYY-MM-DD (empty = no bound)')
dbutils.widgets.text('limit', '200')
dbutils.widgets.dropdown('skip_scored', 'true', ['true', 'false'])
dbutils.widgets.dropdown('dry_run', 'true', ['true', 'false'])
dbutils.widgets.text('judge_model', 'databricks:/databricks-gpt-5-6-luna')
dbutils.widgets.text('sample_rates', '{}')
dbutils.widgets.dropdown('chunk_relevance', 'true', ['true', 'false'])
dbutils.widgets.text('max_input_tokens', '2000000')
dbutils.widgets.text('max_workers', '2')
dbutils.widgets.text('calls_per_second', '2')
dbutils.widgets.text('results_table', 'dev_proj.qualibot.chat_trace_scores')

APP = dbutils.widgets.get('app_code_path').strip().rstrip('/')
EXPERIMENT = dbutils.widgets.get('experiment').strip()
if not EXPERIMENT:
    raise ValueError('The experiment parameter is required')
WAREHOUSE_ID = dbutils.widgets.get('warehouse_id').strip()
DAYS = max(1, int(dbutils.widgets.get('days') or 7))
MIN_DATE = dbutils.widgets.get('min_date').strip()
LIMIT = max(1, int(dbutils.widgets.get('limit') or 200))
SKIP_SCORED = dbutils.widgets.get('skip_scored') == 'true'
DRY_RUN = dbutils.widgets.get('dry_run') == 'true'
JUDGE = dbutils.widgets.get('judge_model').strip()
RATE_OVERRIDES = json.loads(dbutils.widgets.get('sample_rates') or '{}')
CHUNK_RELEVANCE = dbutils.widgets.get('chunk_relevance') == 'true'
MAX_INPUT_TOKENS = int(dbutils.widgets.get('max_input_tokens') or 2_000_000)
RESULTS = dbutils.widgets.get('results_table').strip()

sys.path.insert(0, f'{APP}/utils/evaluation')
import chat_scorers

MAX_WORKERS = max(1, int(dbutils.widgets.get('max_workers') or 2))
CALLS_PER_SECOND = float(dbutils.widgets.get('calls_per_second') or 2)
RATES = {**chat_scorers.SAMPLE_RATES, **{k: float(v) for k, v in RATE_OVERRIDES.items()}}
logger.info('experiment %s, last %d days, at most %d traces, judge %s, rates %s, %d workers, %.1f calls/s, dry_run=%s',
            EXPERIMENT, DAYS, LIMIT, JUDGE or 'managed', RATES, MAX_WORKERS, CALLS_PER_SECOND, DRY_RUN)

# COMMAND ----------

# MAGIC %md
# MAGIC ## Inputs
# MAGIC Answered turns of the experiment (`ok`, `degraded`) of the last `days`, newest first. A trace already carrying the tag
# MAGIC `quality_scored` is left out when `skip_scored` is on, so the job can run every night on the same window.

# COMMAND ----------

import os

import mlflow
from mlflow.entities import AssessmentSourceType

if WAREHOUSE_ID:
    os.environ['MLFLOW_TRACING_SQL_WAREHOUSE_ID'] = WAREHOUSE_ID

mlflow.set_tracking_uri('databricks')
experiment = mlflow.get_experiment_by_name(EXPERIMENT)
if experiment is None:
    raise RuntimeError(f'Experiment {EXPERIMENT} does not exist: run the create_chat_experiment job first')
since = datetime.now(timezone.utc) - timedelta(days=DAYS)
if MIN_DATE:
    since = max(since, datetime.fromisoformat(MIN_DATE).replace(tzinfo=timezone.utc))
since_ms = int(since.timestamp() * 1000)
found = mlflow.search_traces(locations=[experiment.experiment_id], filter_string=f'timestamp > {since_ms}',
                             order_by=['timestamp DESC'], max_results=LIMIT * 3, return_type='list')


def _vote(trace) -> str:
    """Thumbs vote of the user on a trace.

    :param trace: Trace of one chat turn.
    :return: ``up``, ``down``, or an empty string when the user did not vote.
    """
    for a in trace.info.assessments or []:
        if a.name == 'user_vote' and getattr(a, 'feedback', None) is not None:
            return 'up' if a.feedback.value else 'down'
    return ''


TRACES = {}
for tr in found:
    tags = tr.info.tags or {}
    if tags.get('status') not in ('ok', 'degraded') or (SKIP_SCORED and tags.get('quality_scored')):
        continue
    TRACES[tr.info.trace_id] = tr
    if len(TRACES) >= LIMIT:
        break
META = [{'trace_id': i, 'status': (t.info.tags or {}).get('status', ''), 'vote': _vote(t)} for i, t in TRACES.items()]
logger.info('%d traces to score (%d seen, %d thumbs down)', len(TRACES), len(found), sum(m['vote'] == 'down' for m in META))
if not TRACES:
    dbutils.notebook.exit('no_traces')

# COMMAND ----------

# MAGIC %md
# MAGIC ## Data Preparation
# MAGIC ### Prep 1 — which judge reads which trace, and what it will cost
# MAGIC The passages sent are re-read by every context judge (about 4 characters a token), so the estimate below is
# MAGIC what the run asks of the judge endpoint's input-token quota. Above `max_input_tokens` the run stops here.

# COMMAND ----------

if not CHUNK_RELEVANCE:
    RATES.pop('retrieval_relevance', None)


def _passage_chars(trace) -> int:
    """Total characters of the passages sent to the model.

    :param trace: Trace of one chat turn.
    :return: characters of the page content of the retrieval span, 0 when there is none.
    """
    for span in trace.data.spans:
        if span.name == 'retrieval':
            return sum(len((d.get('page_content') if isinstance(d, dict) else getattr(d, 'page_content', '')) or '')
                       for d in (span.outputs or []))
    return 0


PLAN = chat_scorers.plan(META, RATES)
CHARS = {i: _passage_chars(t) for i, t in TRACES.items()}
ESTIMATE = chat_scorers.estimated_input_tokens(PLAN, CHARS)
for name, ids in PLAN.items():
    logger.info('%-24s reads %4d traces', name, len(ids))
logger.info('estimated input tokens: %s (limit %s)', f'{ESTIMATE:,}', f'{MAX_INPUT_TOKENS:,}')
if DRY_RUN:
    dbutils.notebook.exit(f'dry_run: {len(TRACES)} traces, ~{ESTIMATE} input tokens')
if ESTIMATE > MAX_INPUT_TOKENS:
    raise RuntimeError(f'Estimated {ESTIMATE:,} input tokens > max_input_tokens {MAX_INPUT_TOKENS:,}: '
                       'lower `limit`, `days` or the sample rates')

# COMMAND ----------

# MAGIC %md
# MAGIC ## Data Transformations
# MAGIC ### Tr. 1 — free scorers on every trace, then each judge on its own sample
# MAGIC Every (scorer, trace) pair of the plan is one job for `score_all`: bounded parallelism, calls spaced out, back-off on 429,
# MAGIC a refused call is recorded as an error and the batch goes on. The verdicts are written on the original traces (they show as
# MAGIC columns in the Traces tab) and collected for `results_table`.

# COMMAND ----------

from mlflow.entities import AssessmentSource

JUDGES = chat_scorers.judge_scorers(JUDGE, retrieval_relevance=CHUNK_RELEVANCE)
STARTED = datetime.now(timezone.utc)
JOBS = [(f.name, f, t) for f in chat_scorers.FREE_SCORERS for t in TRACES.values()]
JOBS += [(name, JUDGES[name], TRACES[i]) for name, ids in PLAN.items() if name in JUDGES for i in ids]
logger.info('%d scorer calls (%d free, %d judge)', len(JOBS), len(chat_scorers.FREE_SCORERS) * len(TRACES),
            len(JOBS) - len(chat_scorers.FREE_SCORERS) * len(TRACES))
RESULTS_RAW = chat_scorers.score_all(JOBS, max_workers=MAX_WORKERS, calls_per_second=CALLS_PER_SECOND)

JUDGE_NAMES = set(JUDGES)
for r in RESULTS_RAW:
    if r['error'] or r['value'] is None:
        continue
    source = AssessmentSource(source_type=AssessmentSourceType.LLM_JUDGE if r['name'] in JUDGE_NAMES
                              else AssessmentSourceType.CODE, source_id=(JUDGE if r['name'] in JUDGE_NAMES else 'chat_scorers'))
    mlflow.log_feedback(trace_id=r['trace'].info.trace_id, name=r['name'], value=r['value'], rationale=r['rationale'],
                        source=source)
for trace_id in TRACES:
    mlflow.set_trace_tag(trace_id, 'quality_scored', STARTED.strftime('%Y-%m-%d'))

# COMMAND ----------

# MAGIC ## Quality Checks
# MAGIC Every scorer that ran must have produced a verdict on its traces; a scorer that failed on many of them usually means
# MAGIC the judge model was refused (name, permission) or throttled beyond its retries. Verdicts of a free scorer that could not
# MAGIC decide (language not detected, no passage sent) are empty, not errors.

# COMMAND ----------

with mlflow.start_run(run_name=f'chat-quality-{STARTED:%Y%m%d-%H%M}') as run:
    mlflow.log_params({'judge': JUDGE or 'managed', 'traces': len(TRACES), 'estimated_input_tokens': ESTIMATE,
                       'rates': json.dumps(RATES), 'max_workers': MAX_WORKERS, 'calls_per_second': CALLS_PER_SECOND})
RUN_ID = run.info.run_id

rows, failures, ran = [], {}, {}
for r in RESULTS_RAW:
    trace = r['trace']
    tags = trace.info.tags or {}
    ran[r['name']] = ran.get(r['name'], 0) + 1
    if r['error']:
        failures[r['name']] = failures.get(r['name'], 0) + 1
        logger.warning('%s failed on %s: %s', r['name'], trace.info.trace_id, r['error'])
        continue
    rows.append({'chat_trace_id': tags.get('chat_trace_id'), 'mlflow_trace_id': trace.info.trace_id, 'run_id': RUN_ID,
                 'scored_at': STARTED, 'scorer': r['name'], 'value': None if r['value'] is None else str(r['value']),
                 'rationale': (r['rationale'] or '')[:2000] or None, 'division': tags.get('division'),
                 'status': tags.get('status'), 'vote': _vote(trace) or None, 'judge': JUDGE or 'managed'})
logger.info('verdicts per scorer: %s; failed: %s', ran, failures or 'none')

# COMMAND ----------

# MAGIC %md
# MAGIC ## Outputs
# MAGIC One row per turn and scorer, appended to `results_table` (long format: a new scorer needs no new column). The summary sets
# MAGIC each judge against the users' votes: a judge that does not separate thumbed-down answers from the others is not measuring
# MAGIC what users complain about.

# COMMAND ----------

from pyspark.sql import functions as F

schema = ('chat_trace_id string, mlflow_trace_id string, run_id string, scored_at timestamp, scorer string, '
          'value string, rationale string, division string, status string, vote string, judge string')
if rows:
    spark.createDataFrame(rows, schema).write.mode('append').saveAsTable(RESULTS)
    logger.info('%d rows appended to %s', len(rows), RESULTS)

display(spark.table(RESULTS).filter(F.col('run_id') == RUN_ID)
        .groupBy('scorer', F.coalesce('vote', F.lit('none')).alias('vote'))
        .agg(F.count('*').alias('turns'),
             F.round(F.avg(F.when(F.lower('value') == 'yes', 1).when(F.lower('value') == 'no', 0)) * 100, 1).alias('yes_pct'),
             F.round(F.avg(F.expr('try_cast(value AS double)')), 2).alias('avg_value'))
        .orderBy('scorer', 'vote'))
