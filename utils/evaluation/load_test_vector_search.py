# Databricks notebook source
# MAGIC %md
# MAGIC # Qualibot — load test of Vector Search alone (where does it refuse, and why)
# MAGIC
# MAGIC The chat load test (`load_test_chat.py`) failed from 20 questions at once with
# MAGIC `Vector Search returned 429`. This notebook sends **only Vector Search queries** (no LLM), the
# MAGIC same ones the chat sends, by steps of simultaneous queries (widget `levels`), for `step_s`
# MAGIC seconds each, **without retries**: what is measured is the endpoint's own capacity.
# MAGIC
# MAGIC One scenario = one kind of query, measured step after step (widget `scenarios`):
# MAGIC
# MAGIC | Scenario | Query | Why |
# MAGIC |---|---|---|
# MAGIC | `hybrid10` | HYBRID, 10 results | the chat's raw query (and the only kind before the reranker) |
# MAGIC | `rerank12` | HYBRID, 12 results, reranker on REF + section headings + text | the chat's reranked query |
# MAGIC | `rerank12_text` | same, reranker on the text only | does the number of reranked columns matter? |
# MAGIC | `ann10` | ANN (vectors only), 10 results | is the keyword part of HYBRID the cost? |
# MAGIC | `hybrid10_filter` | HYBRID, 10 results, division filter AS | do filters cost? |
# MAGIC | `chat_mix` | half `rerank12`, half `hybrid10` | what one chat question sends (3 + 3 queries, plus REF / title lookups) |
# MAGIC
# MAGIC Per step: queries answered per second, share refused (429) or failed, latency. A scenario
# MAGIC stops at the first step where more than `stop_429_pct` % are refused. Results:
# MAGIC `results_table`, one row per query. The last cell converts the capacity into chat
# MAGIC questions per minute (`queries_per_question` queries per question).

# COMMAND ----------

# MAGIC %pip install -q httpx "databricks-sdk>=0.102"
# MAGIC dbutils.library.restartPython()

# COMMAND ----------

# DBTITLE 1,Parameters
dbutils.widgets.text('index', 'dev_landingzone.qualibot.chunks_index')
dbutils.widgets.text('endpoint', 'qualibot')
dbutils.widgets.text('scenarios', 'hybrid10,rerank12,rerank12_text,ann10,hybrid10_filter,chat_mix')
dbutils.widgets.text('levels', '1,2,4,8,16,32,64')     # queries in progress at once
dbutils.widgets.text('step_s', '20')                    # duration of each step
dbutils.widgets.text('stop_429_pct', '50')
dbutils.widgets.text('queries_per_question', '7')       # 3 reranked + 3 raw + ~1 REF / title lookup
dbutils.widgets.text('golden_table', 'dev_landingzone.qualibot.qualibot_eval_golden')
dbutils.widgets.text('chat_table', 'dev_landingzone.qualibot.chat_messages')
dbutils.widgets.text('results_table', 'dev_landingzone.qualibot.eval_vs_load_runs')

import asyncio, json, random, re, time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import httpx
from databricks.sdk import WorkspaceClient
from pyspark.sql import functions as F

INDEX = dbutils.widgets.get('index').strip()
ENDPOINT = dbutils.widgets.get('endpoint').strip()
SCENARIOS = [s.strip() for s in dbutils.widgets.get('scenarios').split(',') if s.strip()]
LEVELS = [int(x) for x in dbutils.widgets.get('levels').split(',') if x.strip()]
STEP_S = float(dbutils.widgets.get('step_s'))
STOP_PCT = float(dbutils.widgets.get('stop_429_pct'))
PER_QUESTION = float(dbutils.widgets.get('queries_per_question'))
RESULTS = dbutils.widgets.get('results_table').strip()
RUN_ID = datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')

w = WorkspaceClient()
HOST = w.config.host.rstrip('/')


def headers():
    return {**w.config.authenticate(), 'Content-Type': 'application/json'}


def run_async(coro):
    """asyncio.run in its own thread: the notebook may already have an event loop."""
    with ThreadPoolExecutor(max_workers=1) as ex:
        return ex.submit(asyncio.run, coro).result()

# COMMAND ----------

# DBTITLE 1,The endpoint and the index (type, size) — capacity depends on them
ep = httpx.get(f'{HOST}/api/2.0/vector-search/endpoints/{ENDPOINT}', headers=headers(), timeout=30).json()
ix = httpx.get(f'{HOST}/api/2.0/vector-search/indexes/{INDEX}', headers=headers(), timeout=30).json()
import logging

logger = logging.getLogger("load_test_vector_search")
logger.setLevel(logging.INFO)
if not logger.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S"))
    logger.addHandler(_handler)
    logger.propagate = False


logger.info("%s", " ".join(str(x) for x in ('endpoint:', json.dumps({k: ep.get(k) for k in ('name', 'endpoint_type', 'endpoint_status', 'num_indexes',
                                                      'scaling_info', 'effective_budget_policy_id')}, indent=1),)))
logger.info("%s", " ".join(str(x) for x in ('index:', json.dumps({k: ix.get(k) for k in ('name', 'index_type', 'status')}, indent=1),)))

# COMMAND ----------

# DBTITLE 1,Query texts — real questions (golden + DEV chat), as the user typed them
_PREFIX = re.compile(r'^\[Division: (?:AS|IS)\][\s\S]*?\n\n')
TEXTS = [json.loads(r['inputs'])['messages'][-1]['content']
         for r in spark.table(dbutils.widgets.get('golden_table')).select('inputs').collect()]
msgs = spark.table(dbutils.widgets.get('chat_table'))
TEXTS += [_PREFIX.sub('', r['content']) for r in
          msgs.filter((F.col('role') == 'user') & F.length('content').between(15, 1500))
              .select('content').distinct().limit(400).collect()]
random.Random(7).shuffle(TEXTS)
logger.info("%s", " ".join(str(x) for x in (len(TEXTS), 'query texts',)))

COLUMNS = ['chunk_id', 'IDDOC', 'REF', 'division', 'url', 'semantic_headers', 'chunk_text']


def payload(kind, text):
    p = {'query_text': text[:20000], 'columns': COLUMNS, 'num_results': 10, 'query_type': 'HYBRID'}
    if kind in ('rerank12', 'rerank12_text'):
        cols = ['REF', 'semantic_headers', 'chunk_text'] if kind == 'rerank12' else ['chunk_text']
        p.update(num_results=12, reranker={'model': 'databricks_reranker', 'parameters': {'columns_to_rerank': cols}})
    elif kind == 'ann10':
        p['query_type'] = 'ANN'
    elif kind == 'hybrid10_filter':
        p['filters_json'] = json.dumps({'division': ['AS']})
    return p

# COMMAND ----------

# DBTITLE 1,Run — each scenario step after step, no retries, saved as each scenario ends
_SCHEMA = """run_id string, scenario string, kind string, level int, started_at timestamp, latency_s double,
http_status int, error string"""


async def step(scenario, level):
    rows, deadline = [], time.monotonic() + STEP_S
    hdrs = headers()
    url = f'{HOST}/api/2.0/vector-search/indexes/{INDEX}/query'
    texts = iter(TEXTS * 1000)
    async with httpx.AsyncClient(timeout=60, limits=httpx.Limits(max_connections=level + 4)) as client:
        async def worker(wid):
            n = 0
            while time.monotonic() < deadline:
                kind = scenario if scenario != 'chat_mix' else ('rerank12' if (wid + n) % 2 else 'hybrid10')
                n += 1
                started, t0 = datetime.now(timezone.utc), time.monotonic()
                status, err = None, None
                try:
                    resp = await client.post(url, json=payload(kind, next(texts)), headers=hdrs)
                    status = resp.status_code
                    if status != 200:
                        err = resp.text[:300]
                except Exception as exc:  # noqa: BLE001 — a failed query is a result
                    err = f'{type(exc).__name__}: {str(exc)[:200]}'
                rows.append({'run_id': RUN_ID, 'scenario': scenario, 'kind': kind, 'level': level,
                             'started_at': started, 'latency_s': time.monotonic() - t0,
                             'http_status': status, 'error': err})
        await asyncio.gather(*(worker(i) for i in range(level)))
    return rows


for scenario in SCENARIOS:
    all_rows = []
    for level in LEVELS:
        rows = run_async(step(scenario, level))
        all_rows += rows
        ok = [r for r in rows if r['http_status'] == 200]
        refused = sum(1 for r in rows if r['http_status'] == 429)
        lat = sorted(r['latency_s'] for r in ok)
        logger.info(f'{scenario:16} level {level:3}: {len(ok) / STEP_S:5.1f} answered/s, {refused:4} refused (429), '
              f'{len(rows) - len(ok) - refused:3} other errors, p50 {lat[len(lat) // 2] if lat else 0:.2f} s')
        if rows and refused * 100 / len(rows) > STOP_PCT:
            logger.info(f'{scenario}: stopped, more than {STOP_PCT:.0f} % refused')
            break
        time.sleep(10)   # let the endpoint's rate limit window clear before the next step
    spark.createDataFrame(all_rows, schema=_SCHEMA).write.mode('append').option('mergeSchema', 'true').saveAsTable(RESULTS)

# COMMAND ----------

# DBTITLE 1,Results — per scenario and step
STEP = STEP_S
display(spark.sql(f"""
SELECT scenario, level, count(*) AS queries,
       round(count_if(http_status = 200) / {STEP}, 1) AS answered_per_s,
       round(count_if(http_status = 429) * 100 / count(*), 1) AS refused_429_pct,
       count_if(http_status <> 200 AND http_status <> 429 OR http_status IS NULL) AS other_errors,
       round(percentile(CASE WHEN http_status = 200 THEN latency_s END, 0.5), 2) AS latency_p50_s,
       round(percentile(CASE WHEN http_status = 200 THEN latency_s END, 0.95), 2) AS latency_p95_s
FROM {RESULTS} WHERE run_id = '{RUN_ID}' GROUP BY ALL ORDER BY scenario, level"""))

# Capacity: best step without refusals, and the best answered rate, in chat questions per minute
display(spark.sql(f"""
WITH s AS (
  SELECT scenario, level, count_if(http_status = 200) / {STEP} AS ok_per_s,
         count_if(http_status = 429) / count(*) AS refused
  FROM {RESULTS} WHERE run_id = '{RUN_ID}' GROUP BY ALL)
SELECT scenario,
       round(max(CASE WHEN refused = 0 THEN ok_per_s END), 1) AS max_per_s_without_429,
       round(max(ok_per_s), 1) AS max_answered_per_s,
       round(max(CASE WHEN refused = 0 THEN ok_per_s END) * 60 / {PER_QUESTION}) AS chat_questions_per_min_without_429
FROM s GROUP BY scenario ORDER BY scenario"""))

# The refusal messages (they may name the limit)
display(spark.sql(f"""
SELECT scenario, http_status, left(error, 300) AS error, count(*) AS n
FROM {RESULTS} WHERE run_id = '{RUN_ID}' AND (http_status <> 200 OR http_status IS NULL)
GROUP BY ALL ORDER BY n DESC LIMIT 20"""))
logger.info("%s", " ".join(str(x) for x in ('run_id =', RUN_ID,)))
