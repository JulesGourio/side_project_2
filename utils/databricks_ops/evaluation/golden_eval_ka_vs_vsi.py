# Databricks notebook source
# MAGIC %md
# MAGIC # Qualibot — golden evaluation, Chat KA vs Chat VSI (DEV by default)
# MAGIC
# MAGIC Runs every case of the golden dataset through **the app's own code**, the same steps as
# MAGIC one WebSocket chat turn (`server/routers/chat.py::_run_chat_ws`), for each engine:
# MAGIC
# MAGIC 1. `_trim_history` on the conversation;
# MAGIC 2. translation bridge (`translate_question_to_en`) when the question is neither French nor English;
# MAGIC 3. `_with_today_date`, then the engine: `stream_chat` (KA of the division) or `stream_chat_vsi`;
# MAGIC 4. `_apply_citation_markers`, `augment_sources`, `_number_sources`;
# MAGIC 5. `translate_answer_back`.
# MAGIC
# MAGIC The app's configuration is loaded first from the deployed code folder (`app.yaml` env, then
# MAGIC `target_config.env`), so the KA endpoints, VSI indexes, LLM and translation settings are the
# MAGIC ones the deployed DEV app uses. Calls run with the notebook user's identity.
# MAGIC
# MAGIC Scorers, the same for both engines: `Correctness`, `ExpectationsGuidelines` (MLflow built-in
# MAGIC judges), `golden_doc_recall` (share of the expected documents the answer references) and
# MAGIC `latency_s`. One MLflow run per engine in `experiment`.
# MAGIC
# MAGIC **Before running**: deploy the branch to DEV (`deploy_qualibot.ps1 -AppEnv dev`) so
# MAGIC `app_code_path` holds the current code. Widgets:
# MAGIC
# MAGIC | Widget | Default | Meaning |
# MAGIC |---|---|---|
# MAGIC | `engines` | `ka,vsi` | Engines to evaluate |
# MAGIC | `division` | `ALL` | Division used for every case (`ALL`, `AS`, `IS`) |
# MAGIC | `vsi_llm_endpoint` | empty = app config | Override `CHAT_VSI_LLM_ENDPOINT` to compare models |
# MAGIC | `run_tag` | empty | Suffix of the MLflow run names |
# MAGIC | `experiment` | empty = `/Users/<you>/qualibot-golden-ka-vs-vsi` | MLflow experiment |
# MAGIC | `golden_table` | `dev_landingzone.qualibot.qualibot_eval_golden` | Golden dataset |
# MAGIC | `app_code_path` | `/Workspace/Shared/.bundle/qualibot/dev/files` | Deployed app code |
# MAGIC | `results_table` | `dev_landingzone.qualibot.eval_golden_results` | Delta table, one row per case × engine × run (appended) |

# COMMAND ----------

dbutils.widgets.text('app_code_path', '/Workspace/Shared/.bundle/qualibot/dev/files')
APP = dbutils.widgets.get('app_code_path').strip().rstrip('/')

# COMMAND ----------

# MAGIC %pip install -q -r $APP/requirements.txt fastapi pyyaml "mlflow[databricks]>=3.6" "databricks-sdk>=0.102"
# MAGIC dbutils.library.restartPython()

# COMMAND ----------

# DBTITLE 1,Parameters
dbutils.widgets.text('app_code_path', '/Workspace/Shared/.bundle/qualibot/dev/files')
dbutils.widgets.text('engines', 'ka,vsi')
dbutils.widgets.dropdown('division', 'ALL', ['ALL', 'AS', 'IS'])
dbutils.widgets.text('vsi_llm_endpoint', '')
dbutils.widgets.text('run_tag', '')
dbutils.widgets.text('experiment', '')
dbutils.widgets.text('golden_table', 'dev_landingzone.qualibot.qualibot_eval_golden')
dbutils.widgets.text('results_table', 'dev_landingzone.qualibot.eval_golden_results')

APP = dbutils.widgets.get('app_code_path').strip().rstrip('/')
ENGINES = [e.strip() for e in dbutils.widgets.get('engines').split(',') if e.strip()]
DIVISION = dbutils.widgets.get('division')
VSI_LLM = dbutils.widgets.get('vsi_llm_endpoint').strip()
RUN_TAG = dbutils.widgets.get('run_tag').strip()
GOLDEN = dbutils.widgets.get('golden_table').strip()
RESULTS = dbutils.widgets.get('results_table').strip()
assert ENGINES and set(ENGINES) <= {'ka', 'vsi'}, ENGINES

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

if VSI_LLM:
    os.environ['CHAT_VSI_LLM_ENDPOINT'] = VSI_LLM
os.environ['MLFLOW_GENAI_EVAL_MAX_WORKERS'] = '3'   # KA rate limit ~3-4 questions/min/user
sys.path.insert(0, APP)

for k in ['CHAT_ENDPOINT', 'CHAT_ENDPOINT_ALL', 'CHAT_ENDPOINT_AS', 'CHAT_ENDPOINT_IS',
          'CHAT_TRANSLATE_BRIDGE_ENABLED', 'CHAT_TRANSLATE_ENDPOINT', 'CHAT_MAX_HISTORY',
          'CHAT_VSI_INDEX_ALL', 'CHAT_VSI_INDEX_AS', 'CHAT_VSI_INDEX_IS', 'CHAT_VSI_LLM_ENDPOINT',
          'CHAT_VSI_NUM_RESULTS']:
    print(f'{k:32} = {os.environ.get(k, "")}')

# COMMAND ----------

# DBTITLE 1,App code and helpers
import asyncio, json, threading, time

import mlflow
from databricks.sdk import WorkspaceClient
from mlflow.genai.scorers import Correctness, ExpectationsGuidelines, scorer

from server.routers.chat import (  # app code, unchanged
    TRANSLATE_BRIDGE_ENABLED, _apply_citation_markers, _endpoint_for_division, _number_sources,
    _trim_history, _with_today_date,
)
from server.services.chat_vsi import index_for_division, llm_endpoint, stream_chat_vsi
from server.services.doc_catalog import augment_sources, canon_ref
from server.services.streaming import stream_chat
from server.services.translation_bridge import translate_answer_back, translate_question_to_en

w = WorkspaceClient()
HOST = w.config.host.rstrip('/')
ME = w.current_user.me().user_name
EXPERIMENT = dbutils.widgets.get('experiment').strip() or f'/Users/{ME}/qualibot-golden-ka-vs-vsi'
KA_ENDPOINT = _endpoint_for_division(DIVISION)
assert 'ka' not in ENGINES or KA_ENDPOINT, f'no KA endpoint configured for division {DIVISION}'


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
print(f'{len(GOLDEN_ROWS)} golden cases | engines {ENGINES} | division {DIVISION} | '
      f'KA {KA_ENDPOINT} | VSI index {index_for_division(DIVISION)} + {llm_endpoint()} | experiment {EXPERIMENT}')

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
    text, sources, citations, first_token_s = [], [], [], None
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
    }


@mlflow.trace(name='ka')
def ka_predict(messages: list) -> dict:
    return run_async(chat_turn('ka', messages))


@mlflow.trace(name='vsi')
def vsi_predict(messages: list) -> dict:
    return run_async(chat_turn('vsi', messages))


PREDICT = {'ka': ka_predict, 'vsi': vsi_predict}
LABEL = {'ka': f'KA {KA_ENDPOINT}', 'vsi': f'VSI {llm_endpoint()}'}

# Smoke test on the first case before spending a whole run.
_probe = GOLDEN_ROWS[0]['inputs']['messages']
for e in ENGINES:
    out = PREDICT[e](_probe)
    print(f"[{e}] {out['latency_s']} s, refs={out['refs']}\n{out['response'][:300]}\n")

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

# COMMAND ----------

# DBTITLE 1,Runs
mlflow.set_experiment(EXPERIMENT)
answers, runs = {}, {}
for engine in ENGINES:
    answers[engine] = {}

    def _keep(messages: list, _engine=engine) -> dict:
        out = PREDICT[_engine](messages)
        answers[_engine][messages[-1]['content']] = out
        return out
    name = f'{LABEL[engine]} — {DIVISION}' + (f' — {RUN_TAG}' if RUN_TAG else '')
    with mlflow.start_run(run_name=name):
        mlflow.log_params({'engine': engine, 'division': DIVISION, 'golden_table': GOLDEN,
                           'ka_endpoint': KA_ENDPOINT, 'vsi_index': index_for_division(DIVISION),
                           'vsi_llm_endpoint': llm_endpoint(), 'cases': len(GOLDEN_ROWS)})
        runs[engine] = mlflow.genai.evaluate(data=GOLDEN_ROWS, predict_fn=_keep, scorers=SCORERS)
    print(name, runs[engine].run_id)

# COMMAND ----------

# DBTITLE 1,Summary — metrics per engine, then case by case
import pandas as pd

metrics = pd.DataFrame({LABEL[e]: r.metrics for e, r in runs.items()})
_means = [i for i in metrics.index if i.endswith('/mean')]
display(metrics.loc[_means] if _means else metrics)


def assessments(run_id):
    out = {}
    for t in mlflow.search_traces(run_id=run_id, return_type='list'):
        try:
            q = json.loads(t.data.request)['messages'][-1]['content']
        except Exception:
            continue
        out[q] = {a.name: getattr(getattr(a, 'feedback', None), 'value', getattr(a, 'value', None))
                  for a in (t.info.assessments or [])}
    return out


per_engine = {e: assessments(r.run_id) for e, r in runs.items()}
rows = []
for case in GOLDEN_ROWS:
    q = case['inputs']['messages'][-1]['content']
    row = {'question': q[:100], 'turns': len(case['inputs']['messages']),
           'golden_refs': ', '.join(sorted({canon_ref(d['doc_uri']) for d in case['expectations'].get('expected_retrieved_context') or []}))}
    for e in runs:
        a = per_engine[e].get(q) or {}
        row[f'{e}_correct'] = a.get('correctness')
        row[f'{e}_guidelines'] = a.get('expectations_guidelines')
        row[f'{e}_recall'] = a.get('golden_doc_recall')
        row[f'{e}_latency_s'] = (answers[e].get(q) or {}).get('latency_s')
        row[f'{e}_refs'] = ', '.join((answers[e].get(q) or {}).get('refs') or [])
    rows.append(row)
display(pd.DataFrame(rows))

# COMMAND ----------

# DBTITLE 1,Save the results (one row per case × engine) for SQL comparisons
from datetime import datetime, timezone


def _as_float(v):
    """Judge verdicts ('yes'/'no', True/False) and numeric scores as 1.0 / 0.0 / number."""
    if v is None:
        return None
    if isinstance(v, bool):
        return float(v)
    if isinstance(v, (int, float)):
        return float(v)
    return {'yes': 1.0, 'no': 0.0, 'true': 1.0, 'false': 0.0}.get(str(v).strip().lower())


RUN_TS = datetime.now(timezone.utc)
records = []
for case in GOLDEN_ROWS:
    q = case['inputs']['messages'][-1]['content']
    exp = case['expectations']
    for e, r in runs.items():
        a = per_engine[e].get(q) or {}
        out = answers[e].get(q) or {}
        records.append({
            'run_ts': RUN_TS, 'run_tag': RUN_TAG, 'run_id': r.run_id, 'engine': e, 'division': DIVISION,
            'ka_endpoint': KA_ENDPOINT if e == 'ka' else None,
            'vsi_llm_endpoint': llm_endpoint() if e == 'vsi' else None,
            'vsi_index': index_for_division(DIVISION) if e == 'vsi' else None,
            'dataset_record_id': GOLDEN_IDS.get(q), 'question': q, 'turns': len(case['inputs']['messages']),
            'case_kind': 'expected_response' if 'expected_response' in exp else 'expected_facts',
            'golden_refs': sorted({canon_ref(d['doc_uri']) for d in exp.get('expected_retrieved_context') or [] if d.get('doc_uri')}),
            'answer_refs': out.get('refs') or [],
            'correctness': _as_float(a.get('correctness')),
            'guidelines': _as_float(a.get('expectations_guidelines')),
            'doc_recall': _as_float(a.get('golden_doc_recall')),
            'latency_s': out.get('latency_s'), 'first_token_s': out.get('first_token_s'),
            'question_lang': out.get('question_lang') or '',
            'answer': out.get('response'),
        })

_schema = """run_ts timestamp, run_tag string, run_id string, engine string, division string,
ka_endpoint string, vsi_llm_endpoint string, vsi_index string, dataset_record_id string, question string,
turns int, case_kind string, golden_refs array<string>, answer_refs array<string>, correctness double,
guidelines double, doc_recall double, latency_s double, first_token_s double, question_lang string, answer string"""
spark.createDataFrame(records, schema=_schema).write.mode('append').option('mergeSchema', 'true').saveAsTable(RESULTS)
print(f'{len(records)} rows appended to {RESULTS} (run_ts {RUN_TS.isoformat()})')
