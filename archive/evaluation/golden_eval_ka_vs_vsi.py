# Databricks notebook source
# MAGIC %md
# MAGIC # Qualibot — golden evaluation, Chat KA vs Chat VSI variants (DEV)
# MAGIC
# MAGIC **Run all.** The notebook runs every attempt listed in the *Plan* cell that is not yet in
# MAGIC `results_table`, one after the other, and appends its rows as soon as it finishes (an
# MAGIC interrupted batch keeps what is done). Attempts already saved are skipped, so re-running
# MAGIC the notebook never repeats them. To add an attempt: add a line to the plan, Run all.
# MAGIC
# MAGIC Each case goes through **the deployed app's own code**, the steps of one WebSocket chat
# MAGIC turn (`server/routers/chat.py::_run_chat_ws`): history trim, translation bridge, date,
# MAGIC engine, citation markers, catalog sources, translation back. App settings come from the
# MAGIC deployed folder (`app.yaml`, then `target_config.env`); each attempt only overrides the
# MAGIC `CHAT_VSI_*` settings of its plan line. Calls run with the notebook user's identity.
# MAGIC
# MAGIC Scores: `Correctness`, `ExpectationsGuidelines` (MLflow judges), `golden_doc_recall`,
# MAGIC `latency_s`. The last cell prints the leaderboard and every attempt against `ka`;
# MAGIC `golden_eval_queries.sql` has more comparisons.
# MAGIC
# MAGIC Before running: deploy the branch to DEV (`deploy_qualibot.ps1 -AppEnv dev`).

# COMMAND ----------

dbutils.widgets.text('app_code_path', '/Workspace/Shared/.bundle/qualibot/dev/files')
APP = dbutils.widgets.get('app_code_path').strip().rstrip('/')

# COMMAND ----------

# MAGIC %pip install -q -r $APP/requirements.txt fastapi pyyaml "mlflow[databricks]>=3.6" "databricks-sdk>=0.102"
# MAGIC dbutils.library.restartPython()

# COMMAND ----------

# DBTITLE 1,Plan — one line per attempt (eval_id must be unique; saved attempts are skipped)
# engine 'ka' = the division's Knowledge Assistant; engine 'vsi' = Chat VSI, `variant`
# 'baseline' (chat_vsi.py as delivered) or 'rerank' (chat_vsi_rerank.py), with `env` settings:
#   CHAT_VSI_RERANK_ENABLED  true | false     reranker on the Vector Search queries
#   CHAT_VSI_RERANK_TOP_K    12               passages kept per query after reranking
#   CHAT_VSI_RERANK_MERGE    rerank | union   union = also keep the baseline's raw results
#   CHAT_VSI_RERANK_COLUMNS  chunk_text       columns the reranker reads
#   CHAT_VSI_CONTEXT_BUDGET_CHARS  0          context size cap in characters (0 = off)
#   CHAT_VSI_REF_LOOKUP      off | on         filtered search on the REFs named in the question
#   CHAT_VSI_INSTRUCTIONS    ka | v2 | v3     v2 = compact VSI prompt, v3 = ka + VSI addendum
#   CHAT_VSI_LLM_ENDPOINT    (app.yaml)       answer model
#   CHAT_VSI_ANSWER_MAX_TOKENS / CHAT_VSI_REWRITE_MAX_TOKENS  2000 / 120  output ceilings (thinking included)
UNION = {'CHAT_VSI_RERANK_MERGE': 'union'}
V2 = {'CHAT_VSI_INSTRUCTIONS': 'v2'}
CTX = {'CHAT_VSI_RERANK_COLUMNS': 'REF,semantic_headers,chunk_text'}
V3 = {'CHAT_VSI_INSTRUCTIONS': 'v3'}
# Sonnet 5.5 thinks by default and the thinking counts in max_tokens: with the baseline's
# 2000 / 120 ceilings the answers were truncated (first s55 batch, 2026-10-07, deleted).
S55 = {'CHAT_VSI_LLM_ENDPOINT': 'databricks-claude-sonnet-5-5',      # rewrite + answer model
       'CHAT_VSI_ANSWER_MAX_TOKENS': '16000', 'CHAT_VSI_REWRITE_MAX_TOKENS': '4000'}
REF = {'CHAT_VSI_REF_LOOKUP': 'on'}
BUDGET = {'CHAT_VSI_RERANK_TOP_K': '25', 'CHAT_VSI_CONTEXT_BUDGET_CHARS': '35000'}

PLAN = [
    # Already measured on 2026-10-07 — kept here for the record, skipped because saved.
    dict(eval_id='ka', engine='ka'),
    dict(eval_id='baseline', engine='vsi', variant='baseline'),
    dict(eval_id='rerank', engine='vsi', variant='rerank', notes='reranker 50->12'),
    # Batch 2
    dict(eval_id='baseline-run2', engine='vsi', variant='baseline',
         notes='same as baseline: run-to-run spread'),
    dict(eval_id='prompt-v2', engine='vsi', variant='rerank',
         env={'CHAT_VSI_RERANK_ENABLED': 'false', **V2}, notes='baseline search + v2 prompt'),
    dict(eval_id='rerank-union', engine='vsi', variant='rerank', env=UNION,
         notes='reranked + raw results, KA prompt'),
    dict(eval_id='rerank-union-ctx', engine='vsi', variant='rerank', env={**UNION, **CTX},
         notes='union, reranker reads REF + section headers'),
    dict(eval_id='union-v2', engine='vsi', variant='rerank', env={**UNION, **V2},
         notes='union + v2 prompt'),
    dict(eval_id='best-v2', engine='vsi', variant='rerank',
         env={**UNION, **V2, 'CHAT_VSI_RERANK_TOP_K': '25', 'CHAT_VSI_CONTEXT_BUDGET_CHARS': '35000',
              'CHAT_VSI_REF_LOOKUP': 'on'},
         notes='union + v2 + 35k-char budget + REF lookup'),
    # Batch 3 — base = rerank-union-ctx (best of batch 2, 76.2% correct). v2 prompt dropped
    # (-3 questions vs the KA prompt on the same search); budget and REF lookup were only tried
    # with v2. From here on the model is Claude Sonnet 5.5; the 4.6 run2 gives stability + cost.
    dict(eval_id='union-ctx-run2', engine='vsi', variant='rerank', env={**UNION, **CTX},
         notes='rerank-union-ctx again on Sonnet 4.6: stability + measured cost'),
    dict(eval_id='union-ctx-s55', engine='vsi', variant='rerank', env={**UNION, **CTX, **S55},
         notes='rerank-union-ctx on Sonnet 5.5 (reference for the s55 attempts)'),
    dict(eval_id='union-ctx-s55-v3', engine='vsi', variant='rerank', env={**UNION, **CTX, **S55, **V3},
         notes='+ v3 prompt (KA prompt + grounding rules + doc-type glossary)'),
    dict(eval_id='union-ctx-s55-ref', engine='vsi', variant='rerank', env={**UNION, **CTX, **S55, **REF},
         notes='+ REF lookup'),
    dict(eval_id='union-ctx-s55-budget', engine='vsi', variant='rerank', env={**UNION, **CTX, **S55, **BUDGET},
         notes='+ top_k 25 and 35k-char budget'),
    dict(eval_id='union-ctx-s55-all', engine='vsi', variant='rerank', env={**UNION, **CTX, **S55, **V3, **REF, **BUDGET},
         notes='+ v3 + REF lookup + budget'),
    # Next attempts: judged side by side with replay_compare.py first (cheaper than full runs).
    # A line may take repeat=N to average N attempts of the same config.
]

# COMMAND ----------

# DBTITLE 1,Parameters
dbutils.widgets.text('app_code_path', '/Workspace/Shared/.bundle/qualibot/dev/files')
dbutils.widgets.dropdown('division', 'ALL', ['ALL', 'AS', 'IS'])
dbutils.widgets.text('only', '')                  # comma-separated eval_ids to run (empty = whole plan)
dbutils.widgets.dropdown('rerun_existing', 'false', ['false', 'true'])
dbutils.widgets.text('experiment', '')
dbutils.widgets.text('golden_table', 'dev_landingzone.qualibot.qualibot_eval_golden')
dbutils.widgets.text('results_table', 'dev_landingzone.qualibot.eval_golden_runs')

APP = dbutils.widgets.get('app_code_path').strip().rstrip('/')
DIVISION = dbutils.widgets.get('division')
ONLY = {e.strip() for e in dbutils.widgets.get('only').split(',') if e.strip()}
RERUN = dbutils.widgets.get('rerun_existing') == 'true'
GOLDEN = dbutils.widgets.get('golden_table').strip()
RESULTS = dbutils.widgets.get('results_table').strip()

_ids = [p['eval_id'] for p in PLAN]
assert len(_ids) == len(set(_ids)), 'duplicate eval_id in PLAN'
# `repeat` (default 1): how many saved attempts the eval_id should have. One run of 21 questions
# moves by ±2 questions between identical runs (2026-10-07), so compare configs on averages.
_done = {}
if spark.catalog.tableExists(RESULTS):
    _done = {r['eval_id']: r['n'] for r in spark.sql(
        f'SELECT eval_id, count(DISTINCT attempt_ts) AS n FROM {RESULTS} GROUP BY eval_id').collect()}
TODO = []
for p in PLAN:
    if ONLY and p['eval_id'] not in ONLY:
        continue
    missing = 1 if RERUN else max(0, p.get('repeat', 1) - _done.get(p['eval_id'], 0))
    TODO += [p] * missing
print('saved attempts:', {k: v for k, v in _done.items() if k in _ids})
print('to run        :', [p['eval_id'] for p in TODO])

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
else:
    print(f'WARNING: {_target} missing — app.yaml defaults only (deploy the app first).')
os.environ['MLFLOW_GENAI_EVAL_MAX_WORKERS'] = '3'   # KA rate limit ~3-4 questions/min/user
sys.path.insert(0, APP)

# The deployed app's CHAT_VSI_* settings; every attempt starts from these.
# Only the app's infrastructure (indexes, answer model): its search / prompt options and its
# fallback models (CHAT_VSI_VARIANT, CHAT_VSI_RERANK_*, CHAT_VSI_LLM_FALLBACK_ENDPOINTS…) would
# leak into every configuration measured here, and a fallback would mix two models in one run.
APP_VSI_ENV = {k: v for k, v in os.environ.items()
               if re.match(r'CHAT_VSI_(INDEX_\w+|ENABLED|NUM_RESULTS|LLM_ENDPOINT)$', k)}


def apply_attempt_env(plan: dict) -> None:
    for k in [k for k in os.environ if k.startswith('CHAT_VSI_')]:
        del os.environ[k]
    os.environ.update(APP_VSI_ENV)
    os.environ['CHAT_VSI_VARIANT'] = plan.get('variant', 'baseline')
    os.environ.update({k: str(v) for k, v in (plan.get('env') or {}).items()})


for k in ['CHAT_ENDPOINT_ALL', 'CHAT_TRANSLATE_BRIDGE_ENABLED', 'CHAT_VSI_INDEX_ALL', 'CHAT_VSI_LLM_ENDPOINT']:
    print(f'{k:32} = {os.environ.get(k, "")}')

# COMMAND ----------

# DBTITLE 1,App code and helpers
import asyncio, glob, hashlib, json, threading, time
from datetime import datetime, timezone

import mlflow
from databricks.sdk import WorkspaceClient
from mlflow.genai.scorers import Correctness, ExpectationsGuidelines, scorer

from server.routers.chat import (  # app code, unchanged
    TRANSLATE_BRIDGE_ENABLED, _apply_citation_markers, _endpoint_for_division, _number_sources,
    _trim_history, _with_today_date,
)
from server.services.chat_vsi import index_for_division, llm_endpoint
from server.services.chat_vsi_variants import stream_chat_vsi, variant_settings, vsi_variant
from server.services.doc_catalog import augment_sources, canon_ref
from server.services.streaming import stream_chat
from server.services.translation_bridge import translate_answer_back, translate_question_to_en

# Each question runs in its own event loop (asyncio.run in a thread): the bridge's shared
# httpx client is bound to the first loop and fails in the others ("Event loop is closed",
# empty error) — the question was then searched untranslated. One client per call here.
import httpx as _httpx
from server.services import translation_bridge as _tb
_tb._get_http_client = lambda: _httpx.AsyncClient(timeout=_tb._TIMEOUT_S)

w = WorkspaceClient()
HOST = w.config.host.rstrip('/')
ME = w.current_user.me().user_name
EXPERIMENT = dbutils.widgets.get('experiment').strip() or f'/Users/{ME}/qualibot-golden-ka-vs-vsi'
KA_ENDPOINT = _endpoint_for_division(DIVISION)
APP_CODE_HASH = hashlib.sha1(b''.join(open(f, 'rb').read() for f in sorted(
    glob.glob(f'{APP}/server/**/*.py', recursive=True) + glob.glob(f'{APP}/server/config/**/*.md', recursive=True)))).hexdigest()[:12]


def token() -> str:
    return w.config.authenticate()['Authorization'].split(' ', 1)[1]


def run_async(coro):
    """Run a coroutine from sync code, even inside the notebook's running event loop."""
    out = {}

    def _target():
        try:
            out['v'] = asyncio.run(coro)
        except BaseException as exc:          # re-raised in the caller's thread below
            out['e'] = exc
    t = threading.Thread(target=_target)
    t.start()
    t.join()
    if 'e' in out:
        raise out['e']
    return out['v']


_golden = spark.table(GOLDEN).select('dataset_record_id', 'inputs', 'expectations').collect()
GOLDEN_ROWS = [{'inputs': json.loads(r['inputs']), 'expectations': json.loads(r['expectations'])} for r in _golden]
GOLDEN_IDS = {json.loads(r['inputs'])['messages'][-1]['content']: r['dataset_record_id'] for r in _golden}
print(f'{len(GOLDEN_ROWS)} golden cases | division {DIVISION} | KA {KA_ENDPOINT} | code {APP_CODE_HASH} | experiment {EXPERIMENT}')

# COMMAND ----------

# DBTITLE 1,One chat turn, the same steps as _run_chat_ws
async def chat_turn(engine: str, messages: list) -> dict:
    started = time.monotonic()
    host, tok = HOST, token()
    messages = _trim_history([{'role': m['role'], 'content': m['content']} for m in messages])
    user_content = next((m['content'] for m in reversed(messages) if m['role'] == 'user'), '')

    translate_ctx, messages_for_engine = None, messages
    if TRANSLATE_BRIDGE_ENABLED and user_content:
        en_question, translate_ctx = await translate_question_to_en(user_content, host, tok)
        if translate_ctx.needs_translation:
            messages_for_engine = [dict(m) for m in messages]
            for i in range(len(messages_for_engine) - 1, -1, -1):
                if messages_for_engine[i]['role'] == 'user':
                    messages_for_engine[i]['content'] = en_question
                    break

    stream = (stream_chat_vsi(host, tok, DIVISION, _with_today_date(messages_for_engine)) if engine == 'vsi'
              else stream_chat(host, tok, KA_ENDPOINT, _with_today_date(messages_for_engine)))
    text, sources, citations, first_token_s, meta = [], [], [], None, {}
    async for chunk in stream:
        if not chunk.startswith('data: '):
            continue
        raw = chunk[6:].strip()
        if raw == '[DONE]':
            break
        event = json.loads(raw)
        kind = event.get('type')
        if kind == 'response.output_text.delta':
            if first_token_s is None:
                first_token_s = time.monotonic() - started
            text.append(event.get('delta', ''))
        elif kind == 'sources':
            sources = event.get('sources') or sources
            citations = event.get('citations') or citations
        elif kind == 'metadata':
            meta = event
        elif kind == 'error':
            raise RuntimeError(event.get('error'))
    if not text:
        raise RuntimeError('No answer was produced.')

    clean = ''.join(text)
    final = _apply_citation_markers(clean, citations)
    sources = augment_sources(clean, sources)
    _number_sources(sources, citations)
    if translate_ctx:
        final = await translate_answer_back(final, translate_ctx, host, tok)
    return {
        'response': final,
        'refs': sorted({canon_ref(s['title']) for s in sources if s.get('title')}),
        'latency_s': round(time.monotonic() - started, 2),
        'first_token_s': round(first_token_s, 2) if first_token_s is not None else None,
        'question_lang': translate_ctx.lang_code if translate_ctx else '',
        # VSI only: the documents handed to the LLM (cited or not) and how they were searched.
        'retrieved_refs': sorted({canon_ref(r.strip()) for r in (meta.get('tool_result') or '').split(',') if r.strip()})
                          if engine == 'vsi' else [],
        'search': (meta.get('tool_name') or '') if engine == 'vsi' else '',
        # VSI 'rerank' variant only: tokens and cost of the answer generation (streaming.py rates).
        'usage': meta.get('usage') or {},
    }


@mlflow.trace(name='ka')
def ka_predict(messages: list) -> dict:
    return run_async(chat_turn('ka', messages))


@mlflow.trace(name='vsi')
def vsi_predict(messages: list) -> dict:
    return run_async(chat_turn('vsi', messages))


PREDICT = {'ka': ka_predict, 'vsi': vsi_predict}

# COMMAND ----------

# DBTITLE 1,Scorers
@scorer
def golden_doc_recall(outputs, expectations):
    """Share of the expected documents (golden expected_retrieved_context) the answer references."""
    expected = {canon_ref(d['doc_uri']) for d in (expectations.get('expected_retrieved_context') or []) if d.get('doc_uri')}
    if not expected:
        return None
    return round(len(expected & set(outputs.get('refs') or [])) / len(expected), 3)


@scorer
def latency_s(outputs):
    """End-to-end time of the turn, translation included (seconds)."""
    return outputs.get('latency_s')


SCORERS = [Correctness(), ExpectationsGuidelines(), golden_doc_recall, latency_s]


def _as_float(v):
    """Judge verdicts ('yes'/'no', True/False) and numeric scores as 1.0 / 0.0 / number."""
    if v is None:
        return None
    if isinstance(v, (bool, int, float)):
        return float(v)
    return {'yes': 1.0, 'no': 0.0, 'true': 1.0, 'false': 0.0}.get(str(v).strip().lower())


def assessments(run_id: str) -> dict:
    out = {}
    for t in mlflow.search_traces(run_id=run_id, return_type='list'):
        try:
            q = json.loads(t.data.request)['messages'][-1]['content']
        except Exception:
            continue
        out[q] = {a.name: getattr(getattr(a, 'feedback', None), 'value', getattr(a, 'value', None))
                  for a in (t.info.assessments or [])}
    return out


_SCHEMA = """eval_id string, attempt_ts timestamp, notes string, engine string, division string,
ka_endpoint string, vsi_llm_endpoint string, vsi_index string, vsi_variant string, vsi_settings string,
app_code_hash string, retrieved_refs array<string>, search string, mlflow_run_id string, golden_table string,
dataset_record_id string, question string, turns int, case_kind string, golden_refs array<string>,
answer_refs array<string>, correctness double, guidelines double, doc_recall double, latency_s double,
first_token_s double, question_lang string, answer string, input_tokens double, output_tokens double,
thinking_tokens double, cost_eur double"""

# COMMAND ----------

# DBTITLE 1,Run the plan — each attempt is saved as soon as it ends
mlflow.set_experiment(EXPERIMENT)
for plan in TODO:
    engine, eval_id = plan['engine'], plan['eval_id']
    apply_attempt_env(plan)
    settings = variant_settings() if engine == 'vsi' else {}
    print(f'\n=== {eval_id} ({engine}) {json.dumps(settings)}')

    # Smoke test on the first case: a wrong setting fails here, not after 21 questions.
    probe = PREDICT[engine](GOLDEN_ROWS[0]['inputs']['messages'])
    print(f"smoke test ok: {probe['latency_s']} s, search={probe['search'] or '-'}")

    answers = {}

    def _keep(messages: list, _engine=engine) -> dict:
        out = PREDICT[_engine](messages)
        answers[messages[-1]['content']] = out
        return out
    with mlflow.start_run(run_name=f'{eval_id} — {DIVISION}'):
        mlflow.log_params({'eval_id': eval_id, 'engine': engine, 'division': DIVISION, 'app_code_hash': APP_CODE_HASH,
                           'ka_endpoint': KA_ENDPOINT if engine == 'ka' else '',
                           'vsi_settings': json.dumps(settings)[:500], 'cases': len(GOLDEN_ROWS)})
        result = mlflow.genai.evaluate(data=GOLDEN_ROWS, predict_fn=_keep, scorers=SCORERS)
    judged = assessments(result.run_id)

    attempt_ts, records = datetime.now(timezone.utc), []
    for case in GOLDEN_ROWS:
        q = case['inputs']['messages'][-1]['content']
        exp, a, out = case['expectations'], judged.get(q) or {}, answers.get(q) or {}
        records.append({
            'eval_id': eval_id, 'attempt_ts': attempt_ts, 'notes': plan.get('notes', ''), 'engine': engine,
            'division': DIVISION, 'ka_endpoint': KA_ENDPOINT if engine == 'ka' else None,
            'vsi_llm_endpoint': llm_endpoint() if engine == 'vsi' else None,
            'vsi_index': index_for_division(DIVISION) if engine == 'vsi' else None,
            'vsi_variant': vsi_variant() if engine == 'vsi' else None,
            'vsi_settings': json.dumps(settings) if engine == 'vsi' else None,
            'app_code_hash': APP_CODE_HASH,
            'retrieved_refs': out.get('retrieved_refs') or [], 'search': out.get('search') or '',
            'mlflow_run_id': result.run_id, 'golden_table': GOLDEN,
            'dataset_record_id': GOLDEN_IDS.get(q), 'question': q, 'turns': len(case['inputs']['messages']),
            'case_kind': 'refusal_or_not_in_docs' if 'expected_response' in exp else 'facts',
            'golden_refs': sorted({canon_ref(d['doc_uri']) for d in exp.get('expected_retrieved_context') or [] if d.get('doc_uri')}),
            'answer_refs': out.get('refs') or [],
            'correctness': _as_float(a.get('correctness')),
            'guidelines': _as_float(a.get('expectations_guidelines')),
            'doc_recall': _as_float(a.get('golden_doc_recall')),
            'latency_s': _as_float(out.get('latency_s')), 'first_token_s': _as_float(out.get('first_token_s')),
            'question_lang': out.get('question_lang') or '',
            'answer': out.get('response'),
            'input_tokens': _as_float((out.get('usage') or {}).get('input_tokens')),
            'output_tokens': _as_float((out.get('usage') or {}).get('output_tokens')),
            'thinking_tokens': _as_float((out.get('usage') or {}).get('thinking_tokens')),
            'cost_eur': _as_float((out.get('usage') or {}).get('cost_eur')),
        })
    spark.createDataFrame(records, schema=_SCHEMA).write.mode('append').option('mergeSchema', 'true').saveAsTable(RESULTS)
    ok = [r['correctness'] for r in records if r['correctness'] is not None]
    print(f'{eval_id}: saved {len(records)} rows, correct {sum(ok)}/{len(ok)}')

# COMMAND ----------

# DBTITLE 1,Leaderboard — average over every saved attempt of each eval_id, then vs ka
display(spark.sql(f"""
WITH per_attempt AS (
  SELECT eval_id, attempt_ts, max(notes) AS notes, max(coalesce(vsi_llm_endpoint, ka_endpoint)) AS model,
         avg(correctness) AS c, avg(guidelines) AS g, avg(doc_recall) AS d,
         percentile(latency_s, 0.5) AS p50, percentile(first_token_s, 0.5) AS ft, avg(cost_eur) AS eur
  FROM {RESULTS} GROUP BY eval_id, attempt_ts)
SELECT eval_id, max(model) AS model, count(*) AS attempts,
       round(avg(c) * 100, 1) AS correct_pct_avg, round(min(c) * 100, 1) AS correct_min, round(max(c) * 100, 1) AS correct_max,
       round(avg(g) * 100, 1) AS guidelines_pct, round(avg(d) * 100, 1) AS doc_recall_pct,
       round(avg(p50), 1) AS latency_p50_s, round(avg(ft), 1) AS first_token_s, round(avg(eur), 4) AS eur_per_q,
       max(notes) AS notes
FROM per_attempt GROUP BY eval_id ORDER BY correct_pct_avg DESC"""))

display(spark.sql(f"""
WITH q AS (SELECT eval_id, question, avg(correctness) AS c, avg(doc_recall) AS d FROM {RESULTS} GROUP BY eval_id, question)
SELECT b.eval_id,
       round(sum(b.c - a.c), 1)                                      AS net_questions_vs_ka,
       round(avg(b.d - a.d) * 100, 1)                                AS doc_recall_gain_pts,
       concat_ws(' | ', collect_list(CASE WHEN b.c < a.c - 0.5 THEN left(a.question, 50) END)) AS mostly_lost,
       concat_ws(' | ', collect_list(CASE WHEN b.c > a.c + 0.5 THEN left(a.question, 50) END)) AS mostly_won
FROM q a JOIN q b ON a.question = b.question
WHERE a.eval_id = 'ka' AND b.eval_id <> 'ka'
GROUP BY b.eval_id ORDER BY net_questions_vs_ka DESC"""))
