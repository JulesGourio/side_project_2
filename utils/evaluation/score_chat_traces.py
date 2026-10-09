# Databricks notebook source
# MAGIC %md
# MAGIC # Qualibot — scoring of real chat turns as MLflow traces (prototype)
# MAGIC
# MAGIC Every chat turn is already logged in Lakebase: the question, the search queries, every passage
# MAGIC sent to the model (`chat_retrieved_chunks`, with its text), the answer, the user's vote
# MAGIC (`docs/lakebase_schema.md`). This notebook replays those turns as MLflow traces — no model or
# MAGIC Vector Search call, the logged data is the trace — then scores them with MLflow GenAI scorers:
# MAGIC
# MAGIC | Scorer | Question it answers | Kind |
# MAGIC |---|---|---|
# MAGIC | `relevance_to_query` | Does the answer address the question? | judge |
# MAGIC | `retrieval_groundedness` | Is every claim of the answer supported by the passages sent? | judge |
# MAGIC | `retrieval_relevance` (widget `chunk_relevance`) | Is each passage sent relevant to the question? One judge call per passage | judge |
# MAGIC | `safety` | Nothing harmful | judge |
# MAGIC | `language`, `honest_limits` | Same language as the question; says so when the documents do not answer | judge (guidelines) |
# MAGIC | `cited_documents`, `first_token_s`, `user_vote` | Documents cited, time to the first word, thumbs up / down | logged values, free |
# MAGIC
# MAGIC Judge: `judge_model` (default GPT-5.6 Luna, our own endpoint: we see its tokens and pay its
# MAGIC price; empty = the Databricks managed judge). Traces and scores land in the MLflow experiment
# MAGIC `experiment`; one row per turn and scorer in `results_table`, joined to Lakebase by
# MAGIC `vsi_trace_id`. Replaces `score_production_qa.py` (archived), which compared the former
# MAGIC Knowledge Assistant with a naive RAG.
# MAGIC
# MAGIC Run all, on the workspace of the app whose turns you score (Lakebase is read with the
# MAGIC notebook's own identity), on serverless.
# MAGIC
# MAGIC ## Technical debt
# MAGIC 1. Prototype: run by hand, no job yet. Judge prompts are MLflow's; their cost per turn is still to measure.
# MAGIC 2. A `databricks:/<endpoint>` judge needs MLflow ≥ 3.4; with an older MLflow, leave `judge_model` empty.
# MAGIC 3. The app does not write MLflow traces itself: scores exist only for the turns replayed here.

# COMMAND ----------

# MAGIC %pip install -q "mlflow[databricks]>=3.4" psycopg2-binary "databricks-sdk>=0.102"
# MAGIC dbutils.library.restartPython()

# COMMAND ----------

# MAGIC %md
# MAGIC ## Configuration
# MAGIC `selection`: `recent` (latest answered turns), `down` (thumbed-down turns only), `voted` (every turn with a vote).
# MAGIC `skip_scored` leaves out the turns already in `results_table`, so the notebook can be re-run on the same window.

# COMMAND ----------

import json
import logging
import re
from datetime import datetime, timezone

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
logger = logging.getLogger('score_chat_traces')

dbutils.widgets.text('lakebase_project_id', 'qualibot')
dbutils.widgets.text('lakebase_branch', 'production')
dbutils.widgets.text('lakebase_endpoint', 'primary')
dbutils.widgets.text('lakebase_database', 'doccompare')
dbutils.widgets.text('days', '7')
dbutils.widgets.text('limit', '50')
dbutils.widgets.dropdown('selection', 'recent', ['recent', 'down', 'voted'])
dbutils.widgets.dropdown('skip_scored', 'true', ['true', 'false'])
dbutils.widgets.text('judge_model', 'databricks:/databricks-gpt-5-6-luna')
dbutils.widgets.dropdown('chunk_relevance', 'false', ['false', 'true'])
dbutils.widgets.text('experiment', '/Shared/qualibot/chat_trace_scores')
dbutils.widgets.text('results_table', 'dev_landingzone.qualibot.chat_trace_scores')

LAKEBASE_PROJECT_ID = dbutils.widgets.get('lakebase_project_id').strip()
LAKEBASE_BRANCH = dbutils.widgets.get('lakebase_branch').strip()
LAKEBASE_ENDPOINT = dbutils.widgets.get('lakebase_endpoint').strip()
LAKEBASE_DATABASE = dbutils.widgets.get('lakebase_database').strip()
DAYS = max(1, int(dbutils.widgets.get('days') or 7))
LIMIT = max(1, int(dbutils.widgets.get('limit') or 50))
SELECTION = dbutils.widgets.get('selection')
SKIP_SCORED = dbutils.widgets.get('skip_scored') == 'true'
JUDGE = dbutils.widgets.get('judge_model').strip()
CHUNK_RELEVANCE = dbutils.widgets.get('chunk_relevance') == 'true'
EXPERIMENT = dbutils.widgets.get('experiment').strip()
RESULTS = dbutils.widgets.get('results_table').strip()

logger.info('Lakebase %s/%s/%s, last %d days, %s, at most %d turns, judge %s',
            LAKEBASE_PROJECT_ID, LAKEBASE_BRANCH, LAKEBASE_DATABASE, DAYS, SELECTION, LIMIT, JUDGE or 'managed')

# COMMAND ----------

# MAGIC %md
# MAGIC ## Inputs
# MAGIC The turns come straight from the app's Lakebase, not from a Delta copy, so the notebook sees the
# MAGIC turns of the app it runs next to as soon as they are answered. Only answered turns (`ok`,
# MAGIC `degraded`) are scored; the latest vote on the answer comes with them.

# COMMAND ----------

import psycopg2
import psycopg2.extras
from databricks.sdk import WorkspaceClient

w = WorkspaceClient()
branch_path = f'projects/{LAKEBASE_PROJECT_ID}/branches/{LAKEBASE_BRANCH}'
endpoints = list(w.postgres.list_endpoints(parent=branch_path))
if not endpoints:
    raise RuntimeError(f'No Lakebase endpoint found for {branch_path}')
credential = w.postgres.generate_database_credential(endpoint=f'{branch_path}/endpoints/{LAKEBASE_ENDPOINT}')
me = w.current_user.me()

already_scored = set()
if SKIP_SCORED and spark.catalog.tableExists(RESULTS):
    already_scored = {r[0] for r in spark.table(RESULTS).select('vsi_trace_id').distinct().collect()}

vote_filter = {'recent': '', 'down': "AND v.vote = 'down'", 'voted': 'AND v.vote IS NOT NULL'}[SELECTION]
turns_sql = f"""
    SELECT t.trace_id, t.created_at, t.status, t.warnings, t.division, t.question, t.question_lang,
           t.fr_query, t.answer_endpoint, t.llm_fallback, t.cited_refs, t.first_token_ms, t.total_ms,
           m.content AS answer, v.vote, v.comment AS vote_comment
    FROM chat_turns t
    JOIN chat_messages m ON m.id = t.assistant_message_id
    LEFT JOIN LATERAL (SELECT f.vote, f.comment FROM chat_feedbacks f
                       WHERE f.message_id = t.assistant_message_id
                       ORDER BY f.created_at DESC LIMIT 1) v ON TRUE
    WHERE t.status IN ('ok', 'degraded') AND t.trace_id LIKE 'vsi-%%'
      AND t.created_at > NOW() - make_interval(days => %s) {vote_filter}
    ORDER BY t.created_at DESC
    LIMIT %s
"""
chunks_sql = """
    SELECT trace_id, prompt_rank, doc_number, ref, url, semantic_headers, chunk_text, cited
    FROM chat_retrieved_chunks
    WHERE kept AND trace_id = ANY(%s)
    ORDER BY trace_id, prompt_rank
"""
conn = psycopg2.connect(host=endpoints[0].status.hosts.host, port=5432, database=LAKEBASE_DATABASE,
                        user=me.user_name or me.display_name, password=credential.token, sslmode='require')
try:
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(turns_sql, (DAYS, LIMIT + len(already_scored)))
        TURNS = {r['trace_id']: dict(r) for r in cur.fetchall() if r['trace_id'] not in already_scored}
        TURNS = dict(list(TURNS.items())[:LIMIT])
        cur.execute(chunks_sql, (list(TURNS),))
        CHUNKS = {}
        for r in cur.fetchall():
            CHUNKS.setdefault(r['trace_id'], []).append(dict(r))
finally:
    conn.close()

logger.info('%d turns to score (%d already scored left out), %d passages',
            len(TURNS), len(already_scored), sum(len(v) for v in CHUNKS.values()))
if not TURNS:
    dbutils.notebook.exit('no_turns')

# COMMAND ----------

# MAGIC %md
# MAGIC ## Data Transformations
# MAGIC ### Tr. 1 — logged turn → MLflow trace
# MAGIC The retriever span returns exactly the passages the model received, in prompt order, so
# MAGIC `retrieval_groundedness` judges the answer against what the model actually saw. The span
# MAGIC functions only read the logged turn: replaying costs nothing and gives the same trace every time.

# COMMAND ----------

import mlflow
from mlflow.entities import Document, SpanType


@mlflow.trace(span_type=SpanType.RETRIEVER, name='retrieve')
def retrieve(query: str, vsi_trace_id: str):
    return [Document(page_content=c['chunk_text'] or '',
                     metadata={'doc_uri': c['url'] or c['ref'], 'ref': c['ref'], 'doc_number': c['doc_number'],
                               'cited': bool(c['cited']), 'semantic_headers': c['semantic_headers'] or ''})
            for c in CHUNKS.get(vsi_trace_id, [])]


@mlflow.trace(span_type=SpanType.LLM, name='answer')
def answer(question: str, passages: int, model: str, vsi_trace_id: str) -> str:
    return TURNS[vsi_trace_id]['answer']


@mlflow.trace(span_type=SpanType.CHAIN, name='chat_turn')
def chat_turn(question: str, vsi_trace_id: str) -> str:
    t = TURNS[vsi_trace_id]
    mlflow.update_current_trace(tags={
        'vsi_trace_id': vsi_trace_id, 'division': t['division'] or '', 'status': t['status'],
        'vote': t['vote'] or '', 'answer_endpoint': t['answer_endpoint'] or '',
        'cited_count': str(len(t['cited_refs'] or [])),
        'first_token_ms': '' if t['first_token_ms'] is None else str(t['first_token_ms']),
        'warnings': ','.join(t['warnings'] or []),
    })
    docs = retrieve(t['fr_query'] or question, vsi_trace_id)
    return answer(question, len(docs), t['answer_endpoint'] or '', vsi_trace_id)


DATA = [{'inputs': {'question': t['question'], 'vsi_trace_id': tid}} for tid, t in TURNS.items()]

# COMMAND ----------

# MAGIC %md
# MAGIC ### Tr. 2 — scorers
# MAGIC Judge scorers get the judge model explicitly: the managed judge does not report its tokens and
# MAGIC cost far more per call than our Luna endpoint. The free scorers read the logged values from the
# MAGIC trace tags, so the vote and the latency sit next to the judges' verdicts in the MLflow UI.

# COMMAND ----------

from mlflow.entities import Feedback
from mlflow.genai.scorers import (Guidelines, RelevanceToQuery, RetrievalGroundedness, RetrievalRelevance,
                                  Safety, scorer)

judge = {'model': JUDGE} if JUDGE else {}
SCORERS = [
    RelevanceToQuery(**judge),
    RetrievalGroundedness(**judge),
    Safety(**judge),
    Guidelines(name='language', **judge, guidelines=[
        'The response is written in the language of the user question, unless the user explicitly asks '
        'for another language.']),
    Guidelines(name='honest_limits', **judge, guidelines=[
        'When the retrieved documents do not contain the answer, the response says so plainly instead of '
        'answering from general knowledge or guessing.']),
]
if CHUNK_RELEVANCE:
    SCORERS.append(RetrievalRelevance(**judge))


def _tag(trace, name):
    return (trace.info.tags or {}).get(name, '')


@scorer
def cited_documents(trace) -> Feedback:
    n = int(_tag(trace, 'cited_count') or 0)
    return Feedback(value=n, rationale=f'{n} documents cited')


@scorer
def first_token_s(trace) -> Feedback:
    ms = _tag(trace, 'first_token_ms')
    return Feedback(value=round(int(ms) / 1000, 1) if ms else None, rationale='time to the first word (logged)')


@scorer
def user_vote(trace) -> Feedback:
    vote = _tag(trace, 'vote') or 'none'
    return Feedback(value=vote, rationale='latest thumbs up / down on the answer')


SCORERS += [cited_documents, first_token_s, user_vote]

# COMMAND ----------

# MAGIC %md
# MAGIC ### Tr. 3 — evaluate
# MAGIC One MLflow run per execution: the traces, their scores and the aggregated metrics are in the run.

# COMMAND ----------

mlflow.set_experiment(EXPERIMENT)
run_name = f"chat-traces-{SELECTION}-{datetime.now(timezone.utc):%Y%m%d-%H%M}"
with mlflow.start_run(run_name=run_name) as run:
    mlflow.log_params({'selection': SELECTION, 'days': DAYS, 'turns': len(DATA), 'judge': JUDGE or 'managed',
                       'lakebase_database': LAKEBASE_DATABASE})
    results = mlflow.genai.evaluate(data=DATA, predict_fn=chat_turn, scorers=SCORERS)
RUN_ID = run.info.run_id
logger.info('Run %s: %s', RUN_ID, json.dumps(results.metrics, default=str, indent=1))

# COMMAND ----------

# MAGIC %md
# MAGIC ## Quality Checks
# MAGIC Every turn must come back as a trace with its judge verdicts; a scorer that failed on many turns
# MAGIC usually means the judge model was refused (name, permission, MLflow version).

# COMMAND ----------

traces = mlflow.search_traces(run_id=RUN_ID)


def _assessments(row):
    for a in row.get('assessments') or []:
        if isinstance(a, dict):
            fb = a.get('feedback') or {}
            yield (a.get('assessment_name') or a.get('name'), fb.get('value') if isinstance(fb, dict) else None,
                   a.get('rationale'), (fb.get('error') if isinstance(fb, dict) else None))
        else:
            yield a.name, getattr(a, 'value', None), getattr(a, 'rationale', None), getattr(a, 'error', None)


rows, failures = [], {}
scored_at = datetime.now(timezone.utc)
for _, tr in traces.iterrows():
    tags = tr.get('tags') or {}
    vsi = tags.get('vsi_trace_id') or (tr.get('request') or {}).get('vsi_trace_id')
    for name, value, rationale, error in _assessments(tr):
        if error:
            failures[name] = failures.get(name, 0) + 1
        rows.append({'vsi_trace_id': vsi, 'mlflow_trace_id': tr.get('trace_id'), 'run_id': RUN_ID,
                     'scored_at': scored_at, 'scorer': name, 'value': None if value is None else str(value),
                     'rationale': None if rationale is None else str(rationale)[:2000],
                     'division': tags.get('division'), 'status': tags.get('status'), 'vote': tags.get('vote') or None,
                     'judge': JUDGE or 'managed'})

logger.info('%d traces, %d scores, failed scorers: %s', len(traces), len(rows), failures or 'none')
if len(traces) < len(DATA):
    logger.warning('%d turns sent, %d traces back', len(DATA), len(traces))

# COMMAND ----------

# MAGIC %md
# MAGIC ## Outputs
# MAGIC One row per turn and scorer, appended to `results_table` (long format: a new scorer needs no new
# MAGIC column). The summary compares each judge's verdicts with the users' votes: a judge that does not
# MAGIC separate thumbed-down answers from the others is not measuring what users complain about.

# COMMAND ----------

from pyspark.sql import functions as F

schema = ('vsi_trace_id string, mlflow_trace_id string, run_id string, scored_at timestamp, scorer string, '
          'value string, rationale string, division string, status string, vote string, judge string')
if rows:
    spark.createDataFrame(rows, schema).write.mode('append').saveAsTable(RESULTS)
    logger.info('%d rows appended to %s', len(rows), RESULTS)

display(spark.table(RESULTS).filter(F.col('run_id') == RUN_ID)
        .groupBy('scorer', F.coalesce('vote', F.lit('none')).alias('vote'))
        .agg(F.count('*').alias('turns'),
             F.round(F.avg(F.when(F.lower('value') == 'yes', 1).when(F.lower('value') == 'no', 0)) * 100, 1)
             .alias('yes_pct'),
             F.round(F.avg(F.col('value').cast('double')), 2).alias('avg_value'))
        .orderBy('scorer', 'vote'))
