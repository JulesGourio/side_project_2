# Databricks notebook source
# MAGIC %md
# MAGIC # Qualibot — golden evaluation of the Chat engines (KA and/or the integrated VSI module)
# MAGIC
# MAGIC Golden dataset: `dev_landingzone.qualibot.qualibot_eval_golden` (21 cases, built by Jules).
# MAGIC
# MAGIC - **ka**: the app's own `stream_chat()` against `qualibot_ALL_v2`.
# MAGIC - **vsi**: the app's own `stream_chat_vsi()` (server/services/chat_vsi.py), division ALL.
# MAGIC
# MAGIC Both go through the same post-processing as `chat_ws` (`_apply_citation_markers`,
# MAGIC `augment_sources`, `_number_sources`).
# MAGIC Scorers, same for every engine: built-in `Correctness`, built-in `ExpectationsGuidelines`,
# MAGIC `golden_doc_recall`.
# MAGIC
# MAGIC App code is imported unchanged from `/Workspace/Shared/qualibot-custom` (the synced branch).
# MAGIC Widgets: `engines` (comma-separated: `ka`, `vsi`), `run_tag` (suffix of the MLflow run names).

# COMMAND ----------

# MAGIC %pip install -q -r /Workspace/Shared/qualibot-custom/requirements.txt fastapi python-dotenv "mlflow[databricks]>=3.6" "databricks-sdk>=0.102"
# MAGIC dbutils.library.restartPython()

# COMMAND ----------

import asyncio, json, os, sys, threading
os.environ['MLFLOW_GENAI_EVAL_MAX_WORKERS'] = '3'   # KA rate limit ~3-4 questions/min/user
sys.path.insert(0, '/Workspace/Shared/qualibot-custom')

import mlflow
from databricks.sdk import WorkspaceClient
from mlflow.genai.scorers import Correctness, ExpectationsGuidelines, scorer

from server.routers.chat import _apply_citation_markers, _number_sources, _trim_history, _with_today_date  # app code
from server.services.chat_vsi import stream_chat_vsi                                                     # app code
from server.services.doc_catalog import augment_sources, canon_ref                                       # app code
from server.services.streaming import stream_chat                                                        # app code

dbutils.widgets.text('engines', 'ka,vsi')
dbutils.widgets.text('run_tag', '')
ENGINES = [e.strip() for e in dbutils.widgets.get('engines').split(',') if e.strip()]
RUN_TAG = dbutils.widgets.get('run_tag').strip()
assert ENGINES and set(ENGINES) <= {'ka', 'vsi'}, ENGINES

w = WorkspaceClient()
HOST = w.config.host.rstrip('/')
def token() -> str:
    return w.config.authenticate()['Authorization'].split(' ', 1)[1]

EXPERIMENT = '/Users/mehdi.lamrani@databricks.com/qualibot-vsi-vs-ka'
GOLDEN = 'dev_landingzone.qualibot.qualibot_eval_golden'
KA_ENDPOINT = 'ka-4d15cb32-endpoint'                  # qualibot_ALL_v2
GOLDEN_ROWS = [{'inputs': json.loads(r['inputs']), 'expectations': json.loads(r['expectations'])}
               for r in spark.table(GOLDEN).select('inputs', 'expectations').collect()]
print(len(GOLDEN_ROWS), 'golden cases | engines:', ENGINES, '| run tag:', RUN_TAG or '-')

def run_async(coro):
    """Run a coroutine from sync code, even inside the notebook's running event loop."""
    out = {}
    def _target():
        try:
            out['v'] = asyncio.run(coro)
        except BaseException as exc:          # surfaced in the caller's thread below
            out['e'] = exc
    t = threading.Thread(target=_target)
    t.start(); t.join()
    if 'e' in out:
        raise out['e']
    return out['v']

# COMMAND ----------

# DBTITLE 1,One chat turn, exactly like chat_ws
async def _turn(stream) -> dict:
    text, sources, citations = [], [], []
    async for chunk in stream:
        if not chunk.startswith('data: '):
            continue
        raw = chunk[6:].strip()
        if raw == '[DONE]':
            break
        o = json.loads(raw)
        if o.get('type') == 'response.output_text.delta':
            text.append(o.get('delta', ''))
        elif o.get('type') == 'sources':
            sources, citations = o.get('sources') or [], o.get('citations') or []
        elif o.get('type') == 'error':
            raise RuntimeError(o.get('error'))
    clean = ''.join(text)
    final = _apply_citation_markers(clean, citations)
    sources = augment_sources(clean, sources)
    _number_sources(sources, citations)
    return {'response': final, 'refs': sorted({canon_ref(s['title']) for s in sources if s.get('title')})}

@mlflow.trace(name='ka')
def ka_predict(messages: list) -> dict:
    return run_async(_turn(stream_chat(HOST, token(), KA_ENDPOINT, _with_today_date(_trim_history(messages)))))

@mlflow.trace(name='vsi')
def vsi_predict(messages: list) -> dict:
    return run_async(_turn(stream_chat_vsi(HOST, token(), 'ALL', _with_today_date(_trim_history(messages)))))

PREDICT = {'ka': ka_predict, 'vsi': vsi_predict}
LABEL = {'ka': 'KA', 'vsi': 'VSI module'}

# COMMAND ----------

# DBTITLE 1,Scorers
@scorer
def golden_doc_recall(outputs, expectations):
    """Share of the expected documents (golden expected_retrieved_context) the answer references."""
    expected = {canon_ref(d['doc_uri']) for d in (expectations.get('expected_retrieved_context') or []) if d.get('doc_uri')}
    if not expected:
        return None
    return round(len(expected & set(outputs.get('refs') or [])) / len(expected), 3)

SCORERS = [Correctness(), ExpectationsGuidelines(), golden_doc_recall]

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
    name = f'{LABEL[engine]} (golden)' + (f' — {RUN_TAG}' if RUN_TAG else '')
    with mlflow.start_run(run_name=name):
        runs[engine] = mlflow.genai.evaluate(data=GOLDEN_ROWS, predict_fn=_keep, scorers=SCORERS)
    print(name, runs[engine].run_id, runs[engine].metrics)

# COMMAND ----------

# DBTITLE 1,Summary
def assessments(run_id):
    out = {}
    for t in mlflow.search_traces(run_id=run_id, return_type='list'):
        try:
            q = json.loads(t.data.request)['messages'][-1]['content']
        except Exception:
            continue
        out[q] = {a.name: str(getattr(a, 'value', None)) for a in (t.info.assessments or [])}
    return out

per_engine = {e: assessments(r.run_id) for e, r in runs.items()}
rows = []
for case in GOLDEN_ROWS:
    q = case['inputs']['messages'][-1]['content']
    row = {'question': q[:120], 'turns': len(case['inputs']['messages']),
           'golden_refs': sorted({canon_ref(d['doc_uri']) for d in case['expectations'].get('expected_retrieved_context') or []})}
    for e in runs:
        row[e] = {'refs': answers[e].get(q, {}).get('refs'), **(per_engine[e].get(q) or {})}
    rows.append(row)
summary = {'experiment': EXPERIMENT, 'run_tag': RUN_TAG,
           'runs': {e: {'run_id': r.run_id, 'metrics': r.metrics} for e, r in runs.items()}, 'rows': rows}
print(json.dumps(summary, ensure_ascii=False, indent=1))
dbutils.notebook.exit(json.dumps(summary, ensure_ascii=False))
