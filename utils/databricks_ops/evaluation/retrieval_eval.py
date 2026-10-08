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
REF = {'CHAT_VSI_REF_LOOKUP': 'on'}                  # REF named in the conversation -> filtered search
TITLE = {'CHAT_VSI_TITLE_LOOKUP': 'on'}              # catalog title matching the question -> filtered search
BI = {'CHAT_VSI_REWRITE': 'bilingual'}               # French + English rewrite (3 queries instead of 2)
ONE_LANG = {'CHAT_VSI_ONE_LANGUAGE': 'on'}           # one language variant per document
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
    # Wave 3 (2026-10-07) — each one = union-ctx + one change, then all of them together.
    'u-1lang':            dict(variant='rerank', env={**UNION, **CTX, **ONE_LANG}),
    'u-1lang-k20-cap3':   dict(variant='rerank', env={**UNION, **CTX, **ONE_LANG, 'CHAT_VSI_RERANK_TOP_K': '20',
                                                       'CHAT_VSI_MAX_PASSAGES_PER_DOC': '3'}),
    'u-title':            dict(variant='rerank', env={**UNION, **CTX, **TITLE}),
    'u-bi':               dict(variant='rerank', env={**UNION, **CTX, **BI}),
    'u-all':              dict(variant='rerank', env={**UNION, **CTX, **REF, **TITLE, **BI, **ONE_LANG}),
    # Same as u-bi, rewrite by GPT-5.6 Luna instead of the answer model (faster first token if it
    # holds up). Reasoning model: it needs a large ceiling or it returns an empty rewrite.
    # u-all without the one-language rule (u-1lang alone lost 1.5 points on 2026-10-08).
    'u-bi-title-ref':     dict(variant='rerank', env={**UNION, **CTX, **REF, **TITLE, **BI}),
    'u-bi-luna':          dict(variant='rerank', env={**UNION, **CTX, **BI,
                                                       'CHAT_VSI_REWRITE_ENDPOINT': 'databricks-gpt-5-6-luna',
                                                       'CHAT_VSI_REWRITE_MAX_TOKENS': '2000'}),
    # u-all with the rewrite by GPT-6 Luna (2026-10-08): the rewrite is the only Sonnet call left
    # per question, and it costs more than the GPT-6 Luna answer itself.
    'u-all-luna6':        dict(variant='rerank', env={**UNION, **CTX, **REF, **TITLE, **BI, **ONE_LANG,
                                                       'CHAT_VSI_REWRITE_ENDPOINT': 'databricks-gpt-6-luna',
                                                       'CHAT_VSI_REWRITE_MAX_TOKENS': '2000'}),
}

# COMMAND ----------

# DBTITLE 1,Parameters
dbutils.widgets.text('app_code_path', '/Workspace/Shared/.bundle/qualibot/dev/files')
dbutils.widgets.text('configs', ','.join(CONFIGS))                 # which configurations to measure
dbutils.widgets.text('sources', 'golden,synthetic,feedback')
dbutils.widgets.dropdown('rerun_existing', 'false', ['false', 'true'])
dbutils.widgets.text('max_parallel', '4')   # questions at once (each sends 2-7 Vector Search queries)
dbutils.widgets.text('golden_table', 'dev_landingzone.qualibot.qualibot_eval_golden')
dbutils.widgets.text('synthetic_table', 'uat_landingzone.qualibot.synthetic_retrieval_questions_v2')
dbutils.widgets.text('feedback_table', 'uat_landingzone.qualibot.feedback_failure_cases')
dbutils.widgets.text('results_table', 'dev_landingzone.qualibot.eval_retrieval_runs')
# Test indexes built by rechunk_experiment.py (e.g. "v2a,v2b"): each adds idx-<v> and idx-<v>-clean
# (union-ctx) and idx-<v>-all (u-all, the chat's configuration since 2026-10-08), plus the reference
# idx-v1 / idx-v1-all, every question on the ALL index of the variant.
dbutils.widgets.text('index_variants', '')
# The chat's rewrite model since 2026-10-08 (no Claude in the chatbot). Runs before 2026-10-08 used
# databricks-claude-sonnet-4-6: compare old rows with u-all-luna6, not across rewrite models.
dbutils.widgets.text('rewrite_model', 'databricks-gpt-6-luna')
dbutils.widgets.text('index_schema', 'dev_landingzone.qualibot')

APP = dbutils.widgets.get('app_code_path').strip().rstrip('/')
RUN = [c.strip() for c in dbutils.widgets.get('configs').split(',') if c.strip()]
_IDX_SCHEMA = dbutils.widgets.get('index_schema').strip()
_VARIANTS = [v.strip() for v in dbutils.widgets.get('index_variants').split(',') if v.strip()]
if _VARIANTS:
    def _on_index(name, extra=None):
        index = f'{_IDX_SCHEMA}.chunks_index_{name}'
        return dict(variant='rerank', env={**UNION, **CTX, 'CHAT_VSI_INDEX_ALL': index,
                                           'CHAT_VSI_INDEX_AS': index, 'CHAT_VSI_INDEX_IS': index, **(extra or {})})
    _ALL = {**REF, **TITLE, **BI, **ONE_LANG}
    CONFIGS['idx-v1'] = _on_index('v1')
    CONFIGS['idx-v1-all'] = _on_index('v1', _ALL)
    for _v in _VARIANTS:
        CONFIGS[f'idx-{_v}'] = _on_index(_v)
        CONFIGS[f'idx-{_v}-clean'] = _on_index(_v, {'CHAT_VSI_SKIP_NOISE': 'on'})
        CONFIGS[f'idx-{_v}-all'] = _on_index(_v, _ALL)
    RUN += [c for c in CONFIGS if c.startswith('idx-') and c not in RUN]
SOURCES = {s.strip() for s in dbutils.widgets.get('sources').split(',') if s.strip()}
RERUN = dbutils.widgets.get('rerun_existing') == 'true'
MAX_PARALLEL = max(1, int(dbutils.widgets.get('max_parallel') or 4))
RESULTS = dbutils.widgets.get('results_table').strip()
assert not [c for c in RUN if c not in CONFIGS], f'unknown configs — pick from {list(CONFIGS)}'

# A question counts as measured for a configuration once it ran without error: errors are retried
# on the next run, the rest is never repeated (unless rerun_existing = true).
_ok = set()
if spark.catalog.tableExists(RESULTS) and not RERUN:
    _ok = {(r['config'], r['source'], r['case_id'])
           for r in spark.sql(f'SELECT DISTINCT config, source, case_id FROM {RESULTS} WHERE error IS NULL').collect()}

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
# Only the app's infrastructure (indexes, answer model): its search / prompt options and its
# fallback models (CHAT_VSI_VARIANT, CHAT_VSI_RERANK_*, CHAT_VSI_LLM_FALLBACK_ENDPOINTS…) would
# leak into every configuration measured here, and a fallback would mix two models in one run.
APP_VSI_ENV = {k: v for k, v in os.environ.items()
               if re.match(r'CHAT_VSI_(INDEX_\w+|ENABLED|NUM_RESULTS|LLM_ENDPOINT)$', k)}


def apply_config_env(cfg: dict) -> None:
    for k in [k for k in os.environ if k.startswith('CHAT_VSI_')]:
        del os.environ[k]
    os.environ.update(APP_VSI_ENV)
    # No answer here: the "LLM" only rewrites (baseline variant included), pinned to one model.
    os.environ['CHAT_VSI_LLM_ENDPOINT'] = dbutils.widgets.get('rewrite_model').strip()
    os.environ['CHAT_VSI_REWRITE_ENDPOINT'] = dbutils.widgets.get('rewrite_model').strip()
    os.environ['CHAT_VSI_VARIANT'] = cfg.get('variant', 'baseline')
    os.environ.update({k: str(v) for k, v in (cfg.get('env') or {}).items()})
    # One rewrite model per configuration: no silent fallback to another model (chat_vsi_llm).
    os.environ['CHAT_VSI_REWRITE_FALLBACK_ENDPOINTS'] = os.environ['CHAT_VSI_REWRITE_ENDPOINT']

# COMMAND ----------

# DBTITLE 1,App code — the search step of one chat turn
import asyncio, json, time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from databricks.sdk import WorkspaceClient

# Re-run after a -SyncOnly: drop the app modules Python kept from the previous run.
for _m in [m for m in sys.modules if m == 'server' or m.startswith('server.')]:
    del sys.modules[_m]

from server.services import chat_vsi_rerank
assert hasattr(chat_vsi_rerank, 'retrieve_for_turn'), (
    f'Stale app code in {APP}: chat_vsi_rerank.py has no retrieve_for_turn. '
    'Copy the latest zip, run deploy_qualibot.ps1 -AppEnv dev -SyncOnly, then Run all again.')

from server.routers.chat import TRANSLATE_BRIDGE_ENABLED, _trim_history, _with_today_date  # app code
from server.services.chat_vsi import group_documents
from server.services.chat_vsi_variants import retrieve_documents, variant_settings
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
    found = await retrieve_documents(HOST, tok, division, _with_today_date(messages))
    docs = group_documents(found['rows'])
    return {'docs': [canon_ref(ref) for ref, _ in docs], 'passages': len(found['rows']),
            'chars': sum(len(r.get('chunk_text') or '') for r in found['rows']),
            'fr_query': found.get('fr_query') or '', 'reranked': bool(found.get('reranked')),
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
                          'messages': [{'role': 'user', 'content': r['question']}], 'expected': expected, 'partial': []})
_by_source = {}
for c in CASES:
    _by_source[c['source']] = _by_source.get(c['source'], 0) + 1
print(len(CASES), 'cases with an expected document:', _by_source)
TODO = {name: [c for c in CASES if (name, c['source'], c['case_id']) not in _ok] for name in RUN}
print('to run:', {name: len(cases) for name, cases in TODO.items() if cases} or 'nothing, all saved')

# COMMAND ----------

# DBTITLE 1,Run — one configuration after the other, its missing questions in parallel, saved as it ends
_SCHEMA = """config string, settings string, run_ts timestamp, source string, case_id string, division string,
question string, query_type string, expected array<string>, partial array<string>, retrieved array<string>,
recall double, hit double, first_rank int, partial_recall double, n_docs int, n_passages int, chars int,
fr_query string, reranked boolean, named array<string>, latency_s double, error string,
en_query string, titled array<string>"""

for name, cases in TODO.items():
    if not cases:
        continue
    apply_config_env(CONFIGS[name])
    settings = json.dumps(variant_settings())
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

# DBTITLE 1,Results — per configuration and source, on the questions every configuration answered
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

# Cases one configuration finds and another doesn't (edit the two names)
A, B = 'union-ctx', 'u-all'
display(spark.sql(f"""
SELECT a.source, left(a.question, 90) AS question, a.expected,
       array_intersect(a.expected, a.retrieved) AS found_by_{A.replace('-', '_')},
       array_intersect(b.expected, b.retrieved) AS found_by_{B.replace('-', '_')}
FROM common a JOIN common b ON a.source = b.source AND a.case_id = b.case_id
WHERE a.config = '{A}' AND b.config = '{B}' AND a.recall <> b.recall
ORDER BY b.recall - a.recall DESC"""))
