# Databricks notebook source
# MAGIC %md
# MAGIC # Qualibot — retrieval evaluation of the Chat VSI configurations (no answer, no judge)
# MAGIC
# MAGIC Measures only the search: for each question, are the expected documents among the passages
# MAGIC the configuration would hand to the LLM? Deterministic apart from the French rewrite, ~100x
# MAGIC cheaper than a golden run (no answer generated, no judge), so it runs on every question we
# MAGIC have with an expected document:
# MAGIC
# MAGIC | Source | Table | Expected documents |
# MAGIC |---|---|---|
# MAGIC | `golden` | `dev_landingzone.qualibot.qualibot_eval_golden` | `expected_retrieved_context` |
# MAGIC | `synthetic` | `uat_landingzone.qualibot.synthetic_retrieval_questions_v2` | `positive_refs` (FULL), `partial_refs` reported apart |
# MAGIC | `feedback` | `uat_landingzone.qualibot.feedback_failure_cases` | `extracted_expected_ref` (user-named document) |
# MAGIC
# MAGIC Caveat: synthetic positives were judged among the UAT **KA**'s own top-K, so they lean
# MAGIC toward what the KA retrieves; compare configurations with each other, per source.
# MAGIC
# MAGIC Same steps as the app (`chat_vsi_variants.retrieve_documents`): history, translation bridge,
# MAGIC date, French rewrite, the variant's search. Each configuration's questions run in parallel;
# MAGIC configurations already saved in `results_table` are skipped. Run all.

# COMMAND ----------

dbutils.widgets.text('app_code_path', '/Workspace/Shared/.bundle/qualibot/dev/files')
APP = dbutils.widgets.get('app_code_path').strip().rstrip('/')

# COMMAND ----------

# MAGIC %pip install -q -r $APP/requirements.txt fastapi pyyaml "databricks-sdk>=0.102"
# MAGIC dbutils.library.restartPython()

# COMMAND ----------

# DBTITLE 1,Configurations (search only: the answer model doesn't matter here, the rewrite model does)
UNION = {'CHAT_VSI_RERANK_MERGE': 'union'}
CTX = {'CHAT_VSI_RERANK_COLUMNS': 'REF,semantic_headers,chunk_text'}
CONFIGS = {
    'baseline':           dict(variant='baseline'),
    'rerank':             dict(variant='rerank'),
    'union':              dict(variant='rerank', env=UNION),
    'union-ctx':          dict(variant='rerank', env={**UNION, **CTX}),
    'union-ctx-k20':      dict(variant='rerank', env={**UNION, **CTX, 'CHAT_VSI_RERANK_TOP_K': '20'}),
    'union-ctx-ref':      dict(variant='rerank', env={**UNION, **CTX, 'CHAT_VSI_REF_LOOKUP': 'on'}),
    'union-ctx-b35k':     dict(variant='rerank', env={**UNION, **CTX, 'CHAT_VSI_RERANK_TOP_K': '25',
                                                       'CHAT_VSI_CONTEXT_BUDGET_CHARS': '35000'}),
    'union-ctx-b25k':     dict(variant='rerank', env={**UNION, **CTX, 'CHAT_VSI_RERANK_TOP_K': '25',
                                                       'CHAT_VSI_CONTEXT_BUDGET_CHARS': '25000'}),
}

# COMMAND ----------

# DBTITLE 1,Parameters
dbutils.widgets.text('app_code_path', '/Workspace/Shared/.bundle/qualibot/dev/files')
dbutils.widgets.text('configs', ','.join(CONFIGS))                 # which configurations to measure
dbutils.widgets.text('sources', 'golden,synthetic,feedback')
dbutils.widgets.dropdown('rerun_existing', 'false', ['false', 'true'])
dbutils.widgets.text('max_parallel', '8')
dbutils.widgets.text('golden_table', 'dev_landingzone.qualibot.qualibot_eval_golden')
dbutils.widgets.text('synthetic_table', 'uat_landingzone.qualibot.synthetic_retrieval_questions_v2')
dbutils.widgets.text('feedback_table', 'uat_landingzone.qualibot.feedback_failure_cases')
dbutils.widgets.text('results_table', 'dev_landingzone.qualibot.eval_retrieval_runs')

APP = dbutils.widgets.get('app_code_path').strip().rstrip('/')
RUN = [c.strip() for c in dbutils.widgets.get('configs').split(',') if c.strip()]
SOURCES = {s.strip() for s in dbutils.widgets.get('sources').split(',') if s.strip()}
RERUN = dbutils.widgets.get('rerun_existing') == 'true'
MAX_PARALLEL = max(1, int(dbutils.widgets.get('max_parallel') or 8))
RESULTS = dbutils.widgets.get('results_table').strip()
assert not [c for c in RUN if c not in CONFIGS], f'unknown configs — pick from {list(CONFIGS)}'

_done = set()
if spark.catalog.tableExists(RESULTS):
    # A configuration counts as measured only if at least one of its questions ran without error.
    _done = {r['config'] for r in spark.sql(f'SELECT DISTINCT config FROM {RESULTS} WHERE error IS NULL').collect()}
TODO = [c for c in RUN if RERUN or c not in _done]
print('already saved:', sorted(_done & set(RUN)), '| to run:', TODO)

# COMMAND ----------

# DBTITLE 1,App configuration (app.yaml env, then target_config.env) — before importing the app
import os, re, shlex, sys
import yaml

with open(f'{APP}/app.yaml', encoding='utf-8-sig') as f:
    for item in (yaml.safe_load(f) or {}).get('env') or []:
        if 'value' in item:
            os.environ[item['name']] = str(item['value'])
_target = f'{APP}/target_config.env'
if os.path.exists(_target):
    for line in open(_target, encoding='utf-8'):
        m = re.match(r"\s*export\s+([A-Z0-9_]+)=(.*)$", line)
        if m:
            os.environ[m.group(1)] = (shlex.split(m.group(2)) or [''])[0]
sys.path.insert(0, APP)
APP_VSI_ENV = {k: v for k, v in os.environ.items() if k.startswith('CHAT_VSI_')}


def apply_config_env(cfg: dict) -> None:
    for k in [k for k in os.environ if k.startswith('CHAT_VSI_')]:
        del os.environ[k]
    os.environ.update(APP_VSI_ENV)
    os.environ['CHAT_VSI_VARIANT'] = cfg.get('variant', 'baseline')
    os.environ.update({k: str(v) for k, v in (cfg.get('env') or {}).items()})

# COMMAND ----------

# DBTITLE 1,App code — the search step of one chat turn
import asyncio, json, time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from databricks.sdk import WorkspaceClient

from server.routers.chat import TRANSLATE_BRIDGE_ENABLED, _trim_history, _with_today_date  # app code
from server.services.chat_vsi import group_documents
from server.services.chat_vsi_variants import retrieve_documents, variant_settings
from server.services.doc_catalog import canon_ref
from server.services.translation_bridge import translate_question_to_en

w = WorkspaceClient()
HOST = w.config.host.rstrip('/')


def token() -> str:
    return w.config.authenticate()['Authorization'].split(' ', 1)[1]


async def search(messages: list, division: str) -> dict:
    started = time.monotonic()
    tok = token()
    messages = _trim_history([{'role': m['role'], 'content': m['content']} for m in messages])
    user_content = next((m['content'] for m in reversed(messages) if m['role'] == 'user'), '')
    if TRANSLATE_BRIDGE_ENABLED and user_content:
        en_question, ctx = await translate_question_to_en(user_content, HOST, tok)
        if ctx.needs_translation:
            messages = [dict(m) for m in messages]
            messages[-1]['content'] = en_question
    found = await retrieve_documents(HOST, tok, division, _with_today_date(messages))
    docs = group_documents(found['rows'])
    return {'docs': [canon_ref(ref) for ref, _ in docs], 'passages': len(found['rows']),
            'chars': sum(len(r.get('chunk_text') or '') for r in found['rows']),
            'fr_query': found.get('fr_query') or '', 'reranked': bool(found.get('reranked')),
            'named': found.get('named') or [], 'latency_s': round(time.monotonic() - started, 2)}


def run_case(case: dict) -> dict:
    try:
        return asyncio.run(search(case['messages'], case['division']))
    except Exception as exc:
        return {'error': f'{type(exc).__name__}: {str(exc)[:300]}'}

# COMMAND ----------

# DBTITLE 1,Cases — every question with an expected document
def _refs(values):
    return sorted({canon_ref(v) for v in values or [] if v})


CASES = []
if 'golden' in SOURCES:
    for r in spark.table(dbutils.widgets.get('golden_table')).select('dataset_record_id', 'inputs', 'expectations').collect():
        exp = json.loads(r['expectations'])
        expected = _refs(d.get('doc_uri') for d in exp.get('expected_retrieved_context') or [])
        if expected:
            CASES.append({'source': 'golden', 'case_id': r['dataset_record_id'], 'division': 'ALL',
                          'messages': json.loads(r['inputs'])['messages'], 'expected': expected, 'partial': []})
if 'synthetic' in SOURCES:
    for r in spark.table(dbutils.widgets.get('synthetic_table')).collect():
        expected = _refs(r['positive_refs'])
        if expected:
            CASES.append({'source': 'synthetic', 'case_id': r['question_id'], 'division': (r['division'] or 'ALL').upper(),
                          'messages': [{'role': 'user', 'content': r['question']}], 'expected': expected,
                          'partial': _refs(r['partial_refs']), 'query_type': r['query_type']})
if 'feedback' in SOURCES:
    for r in spark.table(dbutils.widgets.get('feedback_table')).where('extracted_expected_ref IS NOT NULL').collect():
        expected = _refs(re.split(r'[,;\s]+', r['extracted_expected_ref']))
        if expected and r['question']:
            CASES.append({'source': 'feedback', 'case_id': str(r['message_id']), 'division': (r['division'] or 'ALL').upper(),
                          'messages': [{'role': 'user', 'content': r['question']}], 'expected': expected, 'partial': []})
_by_source = {}
for c in CASES:
    _by_source[c['source']] = _by_source.get(c['source'], 0) + 1
print(len(CASES), 'cases with an expected document:', _by_source)

# COMMAND ----------

# DBTITLE 1,Run — one configuration after the other, its questions in parallel, saved as it ends
_SCHEMA = """config string, settings string, run_ts timestamp, source string, case_id string, division string,
question string, query_type string, expected array<string>, partial array<string>, retrieved array<string>,
recall double, hit double, first_rank int, partial_recall double, n_docs int, n_passages int, chars int,
fr_query string, reranked boolean, named array<string>, latency_s double, error string"""

for name in TODO:
    apply_config_env(CONFIGS[name])
    settings = json.dumps(variant_settings())
    t0 = time.monotonic()
    with ThreadPoolExecutor(max_workers=MAX_PARALLEL) as pool:
        outs = list(pool.map(run_case, CASES))
    run_ts, rows = datetime.now(timezone.utc), []
    for case, out in zip(CASES, outs):
        got = out.get('docs') or []
        hits = [d for d in got if d in case['expected']]
        rows.append({
            'config': name, 'settings': settings, 'run_ts': run_ts, 'source': case['source'],
            'case_id': case['case_id'], 'division': case['division'],
            'question': case['messages'][-1]['content'], 'query_type': case.get('query_type'),
            'expected': case['expected'], 'partial': case['partial'], 'retrieved': got,
            'recall': None if 'error' in out else len(set(hits)) / len(case['expected']),
            'hit': None if 'error' in out else float(bool(hits)),
            'first_rank': next((i + 1 for i, d in enumerate(got) if d in case['expected']), None),
            'partial_recall': (len(set(got) & set(case['partial'])) / len(case['partial'])) if case['partial'] and 'error' not in out else None,
            'n_docs': len(got), 'n_passages': out.get('passages'), 'chars': out.get('chars'),
            'fr_query': out.get('fr_query'), 'reranked': out.get('reranked'), 'named': out.get('named') or [],
            'latency_s': out.get('latency_s'), 'error': out.get('error'),
        })
    spark.createDataFrame(rows, schema=_SCHEMA).write.mode('append').option('mergeSchema', 'true').saveAsTable(RESULTS)
    errors = [r['error'] for r in rows if r['error']]
    print(f'{name:18} {len(rows)} cases in {time.monotonic() - t0:.0f} s, {len(errors)} errors')
    for e in sorted(set(errors))[:3]:
        print('    ', e)

# COMMAND ----------

# DBTITLE 1,Results — per configuration and source (latest run of each configuration)
spark.sql(f"""
CREATE OR REPLACE TEMPORARY VIEW latest_retrieval AS
SELECT r.* FROM {RESULTS} r
JOIN (SELECT config, max(run_ts) AS ts FROM {RESULTS} GROUP BY config) l ON r.config = l.config AND r.run_ts = l.ts""")

display(spark.sql("""
SELECT config, source, count(*) AS cases,
       round(avg(recall) * 100, 1)         AS recall_pct,
       round(avg(hit) * 100, 1)            AS at_least_one_pct,
       round(avg(partial_recall) * 100, 1) AS partial_recall_pct,
       round(percentile(first_rank, 0.5))  AS median_rank_first_hit,
       round(avg(n_docs), 1)               AS avg_docs,
       round(avg(chars) / 4)               AS avg_context_tokens,
       round(percentile(latency_s, 0.5), 1) AS search_p50_s,
       sum(CASE WHEN error IS NOT NULL THEN 1 ELSE 0 END) AS errors
FROM latest_retrieval GROUP BY ALL ORDER BY source, recall_pct DESC"""))

display(spark.sql("""
SELECT config, count(*) AS cases, round(avg(recall) * 100, 1) AS recall_pct, round(avg(hit) * 100, 1) AS at_least_one_pct,
       round(avg(chars) / 4) AS avg_context_tokens
FROM latest_retrieval GROUP BY config ORDER BY recall_pct DESC"""))

# Cases one configuration finds and another doesn't (edit the two names)
A, B = 'baseline', 'union-ctx'
display(spark.sql(f"""
SELECT a.source, left(a.question, 90) AS question, a.expected,
       array_intersect(a.expected, a.retrieved) AS found_by_{A.replace('-', '_')},
       array_intersect(b.expected, b.retrieved) AS found_by_{B.replace('-', '_')}
FROM latest_retrieval a JOIN latest_retrieval b ON a.source = b.source AND a.case_id = b.case_id
WHERE a.config = '{A}' AND b.config = '{B}' AND a.recall <> b.recall
ORDER BY b.recall - a.recall DESC"""))
