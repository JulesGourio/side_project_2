# Databricks notebook source
# MAGIC %md
# MAGIC # Qualibot — retrieval evaluation of the chat (no answer, no judge)
# MAGIC
# MAGIC Measures only the search: for each question, are the expected documents among the passages
# MAGIC the chat hands to the LLM? Deterministic apart from the rewrite, ~100x cheaper than an answer
# MAGIC comparison (no answer generated, no judge), so it runs on every question we have with an
# MAGIC expected document:
# MAGIC
# MAGIC | Source | Table | Expected documents |
# MAGIC |---|---|---|
# MAGIC | `golden` | `dev_landingzone.qualibot.qualibot_eval_golden` | `expected_retrieved_context` |
# MAGIC | `synthetic` | `dev_landingzone.qualibot.synthetic_retrieval_questions` | `positive_refs` (FULL), `partial_refs` reported apart |
# MAGIC | `feedback` | `dev_landingzone.qualibot.feedback_failure_cases` | `extracted_expected_ref` (user-named document) |
# MAGIC
# MAGIC Caveat: synthetic positives were judged among the former Knowledge Assistant's own top-K, so
# MAGIC they lean toward what it retrieved; compare runs with each other, per source.
# MAGIC
# MAGIC Same steps as the app: history, translation bridge, date, then `chat_vsi.retrieve_for_turn`
# MAGIC (rewrite, 3 queries, REF and title lookups). What is compared: the **index** (the chat's own,
# MAGIC or one built with another chunking) and the **search sizes** (reranked / raw passages per
# MAGIC query, cap on the merged list) — widget `indexes`. Questions
# MAGIC already saved in `results_table` for a label are skipped. Results of every test so far:
# MAGIC `docs/chat_vsi_tests.md`. Run all.

# COMMAND ----------

dbutils.widgets.text('app_code_path', '/Workspace/Shared/.bundle/qualibot/dev/files')
APP = dbutils.widgets.get('app_code_path').strip().rstrip('/')

# COMMAND ----------

# MAGIC %pip install -q -r $APP/requirements.txt fastapi pyyaml "databricks-sdk>=0.102"
# MAGIC dbutils.library.restartPython()

# COMMAND ----------

# DBTITLE 1,Parameters
dbutils.widgets.text('app_code_path', '/Workspace/Shared/.bundle/qualibot/dev/files')
# What to measure, comma-separated: "label[=catalog.schema.index][|rerank=N][|raw=N][|cap=N]".
# "chat" alone = the app as deployed. Index after a rechunk: "chat,rechunk=dev_landingzone.qualibot.chunks_test_index".
# Search sizes (reranked / raw passages per query, cap on the merged list):
# "chat,rerank-only|raw=0,raw5|raw=5,cap40|cap=40".
dbutils.widgets.text('indexes', 'chat')
dbutils.widgets.text('sources', 'golden,synthetic,feedback')
dbutils.widgets.dropdown('rerun_existing', 'false', ['false', 'true'])
dbutils.widgets.text('max_parallel', '4')   # questions at once (each sends 3-5 Vector Search queries)
dbutils.widgets.text('golden_table', 'dev_landingzone.qualibot.qualibot_eval_golden')
dbutils.widgets.text('synthetic_table', 'dev_landingzone.qualibot.synthetic_retrieval_questions')
dbutils.widgets.text('feedback_table', 'dev_landingzone.qualibot.feedback_failure_cases')
dbutils.widgets.text('results_table', 'dev_landingzone.qualibot.eval_retrieval_runs')

APP = dbutils.widgets.get('app_code_path').strip().rstrip('/')
SOURCES = {s.strip() for s in dbutils.widgets.get('sources').split(',') if s.strip()}
RERUN = dbutils.widgets.get('rerun_existing') == 'true'
MAX_PARALLEL = max(1, int(dbutils.widgets.get('max_parallel') or 4))
RESULTS = dbutils.widgets.get('results_table').strip()

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
# One rewrite model per run: no silent fallback to another model (chat_vsi_llm).
os.environ['CHAT_VSI_REWRITE_FALLBACK_ENDPOINTS'] = os.environ.get('CHAT_VSI_REWRITE_ENDPOINT') or os.environ['CHAT_VSI_LLM_ENDPOINT']

_SIZE_KEYS = {'rerank': 'CHAT_VSI_RERANK_TOP_K', 'raw': 'CHAT_VSI_RAW_TOP_K', 'cap': 'CHAT_VSI_MAX_SEARCH_PASSAGES'}


def parse_configs(text):
    """'label[=index][|rerank=N][|raw=N][|cap=N]', comma-separated -> {label: config}.
    No index = the app's (CHAT_VSI_INDEX); no option = the app's search sizes."""
    out = {}
    for item in [x.strip() for x in text.split(',') if x.strip()]:
        head, *opts = [p.strip() for p in item.split('|') if p.strip()]
        label, _, index = head.partition('=')
        index = index.strip() or os.environ['CHAT_VSI_INDEX']
        env = {}
        for o in opts:
            k, _, v = o.partition('=')
            assert k in _SIZE_KEYS and v.isdigit(), f'{item}: options are rerank=N, raw=N, cap=N'
            env[_SIZE_KEYS[k]] = v
        out[label.strip()] = {'index': index, 'env': env, 'signature': '|'.join([index] + sorted(opts))}
    return out


def apply_config(cfg):
    """Point the engine at the config's index and search sizes (read at call time)."""
    os.environ['CHAT_VSI_INDEX'] = cfg['index']
    for key in _SIZE_KEYS.values():
        os.environ.pop(key, None)
    os.environ.update(cfg['env'])


CONFIGS = parse_configs(dbutils.widgets.get('indexes'))
RUN = list(CONFIGS)
print('configs:', {k: v['signature'] for k, v in CONFIGS.items()})

# A question counts as measured for a label once it ran without error: errors are retried on the
# next run, the rest is never repeated (unless rerun_existing = true).
_ok = set()
if spark.catalog.tableExists(RESULTS) and not RERUN:
    _ok = {(r['config'], r['source'], r['case_id'])
           for r in spark.sql(f'SELECT DISTINCT config, source, case_id FROM {RESULTS} WHERE error IS NULL').collect()}

# COMMAND ----------

# DBTITLE 1,App code — the search step of one chat turn
import asyncio, json, time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from databricks.sdk import WorkspaceClient

# Re-run after a -SyncOnly: drop the app modules Python kept from the previous run.
for _m in [m for m in sys.modules if m == 'server' or m.startswith('server.')]:
    del sys.modules[_m]

from server.services import chat_vsi
assert hasattr(chat_vsi, 'retrieve_for_turn'), (
    f'Stale app code in {APP}: chat_vsi.py has no retrieve_for_turn. '
    'Copy the latest zip, run deploy_qualibot.ps1 -AppEnv dev -SyncOnly, then Run all again.')

from server.routers.chat import TRANSLATE_BRIDGE_ENABLED, _strip_division, _trim_history, _with_today_date  # app code
from server.services.doc_catalog import canon_ref
from server.services.translation_bridge import translate_question_to_en

# Each question runs in its own event loop (asyncio.run in a thread): the bridge's shared
# httpx client is bound to the first loop and fails in the others ("Event loop is closed",
# empty error) — the question was then searched untranslated. One client per call here.
import httpx as _httpx
from server.services import translation_bridge as _tb
_tb._get_http_client = lambda: _httpx.AsyncClient(timeout=_tb._TIMEOUT_S)

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
    conversation = chat_vsi._clean_history(_with_today_date(messages))
    found = await chat_vsi.retrieve_for_turn(HOST, tok, chat_vsi.normalize_division(division), conversation)
    docs = chat_vsi.group_documents(found['rows'])
    return {'docs': [canon_ref(ref) for ref, _ in docs], 'passages': len(found['rows']),
            'chars': sum(len(r.get('chunk_text') or '') for r in found['rows']),
            'fr_query': found.get('fr_query') or '', 'reranked': True,
            'named': found.get('named') or [], 'titled': found.get('titled') or [],
            'en_query': found.get('en_query') or '', 'latency_s': round(time.monotonic() - started, 2)}


def run_case(case: dict) -> dict:
    # Up to 3 tries: under parallel load the endpoints answer 429 / time out now and then.
    for wait in (5, 20, None):
        try:
            return asyncio.run(search(case['messages'], case['division']))
        except Exception as exc:
            out = {'error': f'{type(exc).__name__}: {str(exc)[:300]}'}
            if wait is None:
                return out
            time.sleep(wait)

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
                          # Questions of the former Knowledge Assistant carry its "[Division: AS] (system
                          # routing note…)" prefix: the app no longer sends it, the eval mustn't either.
                          'messages': [{'role': 'user', 'content': _strip_division(r['question'])}],
                          'expected': expected, 'partial': []})
_by_source = {}
for c in CASES:
    _by_source[c['source']] = _by_source.get(c['source'], 0) + 1
print(len(CASES), 'cases with an expected document:', _by_source)
TODO = {name: [c for c in CASES if (name, c['source'], c['case_id']) not in _ok] for name in RUN}
print('to run:', {name: len(cases) for name, cases in TODO.items() if cases} or 'nothing, all saved')

# COMMAND ----------

# DBTITLE 1,Run — one index after the other, its missing questions in parallel, saved as it ends
_SCHEMA = """config string, settings string, run_ts timestamp, source string, case_id string, division string,
question string, query_type string, expected array<string>, partial array<string>, retrieved array<string>,
recall double, hit double, first_rank int, partial_recall double, n_docs int, n_passages int, chars int,
fr_query string, reranked boolean, named array<string>, latency_s double, error string,
en_query string, titled array<string>"""

for name, cases in TODO.items():
    if not cases:
        continue
    apply_config(CONFIGS[name])
    settings = json.dumps(chat_vsi.settings())
    t0 = time.monotonic()
    with ThreadPoolExecutor(max_workers=MAX_PARALLEL) as pool:
        outs = list(pool.map(run_case, cases))
    run_ts, rows = datetime.now(timezone.utc), []
    for case, out in zip(cases, outs):
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
            'en_query': out.get('en_query'), 'titled': out.get('titled') or [],
        })
    spark.createDataFrame(rows, schema=_SCHEMA).write.mode('append').option('mergeSchema', 'true').saveAsTable(RESULTS)
    errors = [r['error'] for r in rows if r['error']]
    print(f'{name:18} {len(rows)} cases in {time.monotonic() - t0:.0f} s, {len(errors)} errors')
    for e in sorted(set(errors))[:3]:
        print('    ', e)

# COMMAND ----------

# DBTITLE 1,Results — per index and source, on the questions every index answered
# One row per (configuration, question): its latest error-free run, else its latest error.
# `common` keeps only the questions with no error in ANY configuration shown, so every
# configuration is scored on the same questions (an error would otherwise just drop a question).
_shown = ', '.join(f"'{c}'" for c in RUN)
spark.sql(f"""
CREATE OR REPLACE TEMPORARY VIEW latest_retrieval AS
SELECT * EXCEPT (rn),
       -- expected documents among the first 5 distinct documents of the context: compares
       -- configurations at the same size, whatever the size of their whole context
       size(array_intersect(slice(array_distinct(retrieved), 1, 5), expected)) / size(expected) AS recall_at5
FROM (
  SELECT *, row_number() OVER (PARTITION BY config, source, case_id
                               ORDER BY error IS NULL DESC, run_ts DESC) AS rn
  FROM {RESULTS} WHERE config IN ({_shown})) WHERE rn = 1""")
spark.sql(f"""
CREATE OR REPLACE TEMPORARY VIEW common AS
SELECT * FROM latest_retrieval WHERE (source, case_id) IN (
  SELECT source, case_id FROM latest_retrieval GROUP BY source, case_id
  HAVING count_if(error IS NULL) = {len(RUN)})""")

display(spark.sql("""
SELECT config, count(*) AS questions, count_if(error IS NOT NULL) AS still_in_error, first(error, true) AS an_error
FROM latest_retrieval GROUP BY config HAVING still_in_error > 0"""))

display(spark.sql("""
SELECT config, source, count(*) AS cases,
       round(avg(recall) * 100, 1)         AS recall_pct,
       round(avg(recall_at5) * 100, 1)     AS recall_top5_pct,
       round(avg(hit) * 100, 1)            AS at_least_one_pct,
       round(avg(partial_recall) * 100, 1) AS partial_recall_pct,
       round(percentile(first_rank, 0.5))  AS median_rank_first_hit,
       round(avg(size(array_distinct(retrieved))), 1) AS avg_docs,
       round(avg(chars) / 4)               AS avg_context_tokens,
       round(percentile(latency_s, 0.5), 1) AS search_p50_s
FROM common GROUP BY ALL ORDER BY source, recall_pct DESC"""))

display(spark.sql("""
SELECT config, count(*) AS cases, round(avg(recall) * 100, 1) AS recall_pct,
       round(avg(recall_at5) * 100, 1) AS recall_top5_pct, round(avg(hit) * 100, 1) AS at_least_one_pct,
       round(avg(chars) / 4) AS avg_context_tokens, round(percentile(latency_s, 0.5), 1) AS search_p50_s
FROM common GROUP BY config ORDER BY recall_pct DESC"""))

# Cases one index finds and the other doesn't (the first two labels of `indexes`)
A, B = (RUN + RUN)[:2]
if A != B:
    display(spark.sql(f"""
    SELECT a.source, left(a.question, 90) AS question, a.expected,
           array_intersect(a.expected, a.retrieved) AS found_by_{A.replace('-', '_')},
           array_intersect(b.expected, b.retrieved) AS found_by_{B.replace('-', '_')}
    FROM common a JOIN common b ON a.source = b.source AND a.case_id = b.case_id
    WHERE a.config = '{A}' AND b.config = '{B}' AND a.recall <> b.recall
    ORDER BY b.recall - a.recall DESC"""))
