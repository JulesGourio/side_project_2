# Databricks notebook source
# MAGIC %md
# MAGIC # Qualibot — load test of the chat (how many questions at once before it breaks)
# MAGIC
# MAGIC Sends real questions to the deployed DEV app, the way the browser does (WebSocket
# MAGIC `/api/chat/ws`), by **steps of simultaneous questions** (widget `levels`, e.g. 5, 10, 20, 40,
# MAGIC 80). At each step, `level` workers each ask `rounds` questions one after the other, so the
# MAGIC app always has `level` questions in progress. Everything the app does is exercised: the
# MAGIC instance and its queue (32 answers at once, `CHAT_VSI_MAX_CONCURRENT_ANSWERS`), the rewrite,
# MAGIC Vector Search, GPT-6 Luna and its fallback GPT-5.6 Luna, the translation bridge, Lakebase.
# MAGIC
# MAGIC Measured per question: time to the first word, total time, outcome (answer / error shown to
# MAGIC the user / no answer within `timeout_s`). The run stops at the first step where more than
# MAGIC `stop_error_pct` % of the questions fail. Results: `results_table`, one row per question.
# MAGIC Which model answered and every retry are in the app logs (`chat_vsi_llm:` lines).
# MAGIC
# MAGIC **Mode** (`mode`): `app` = through the app (the real test); `engine` = the app's engine
# MAGIC called in this notebook (same rewrite, search, models and limits, without the app instance:
# MAGIC tells whether a limit comes from the models / Vector Search or from the app); `auto` = `app`
# MAGIC if this notebook can open the app's WebSocket, else `engine` (the app may refuse a notebook
# MAGIC token: the first cell says which mode ran).
# MAGIC
# MAGIC Cost ≈ 0.003 € per question (rewrite + answer + translation): the defaults (5, 10, 20, 40,
# MAGIC 80 × 4 rounds = 620 questions) ≈ 2 €. Questions: the golden set + real DEV questions,
# MAGIC languages and divisions as asked. In `app` mode every question is saved in the app's history
# MAGIC under your user, in sessions named `loadtest-<run>-…`.

# COMMAND ----------

dbutils.widgets.text('app_code_path', '/Workspace/Shared/.bundle/qualibot/dev/files')
APP = dbutils.widgets.get('app_code_path').strip().rstrip('/')

# COMMAND ----------

# MAGIC %pip install -q -r $APP/requirements.txt fastapi pyyaml websockets "databricks-sdk>=0.102"
# MAGIC dbutils.library.restartPython()

# COMMAND ----------

dbutils.widgets.text('app_code_path', '/Workspace/Shared/.bundle/qualibot/dev/files')
dbutils.widgets.dropdown('mode', 'auto', ['auto', 'app', 'engine'])
dbutils.widgets.text('app_name', 'qualibot')
dbutils.widgets.text('levels', '5,10,20,40,80')        # questions in progress at once, step after step
dbutils.widgets.text('rounds', '4')                    # questions per worker at each step
dbutils.widgets.text('timeout_s', '300')               # a question without an outcome after this = timeout
dbutils.widgets.text('stop_error_pct', '50')           # stop after the first step above this failure rate
dbutils.widgets.text('golden_table', 'dev_landingzone.qualibot.qualibot_eval_golden')
dbutils.widgets.text('chat_table', 'dev_landingzone.qualibot.chat_messages')
dbutils.widgets.text('results_table', 'dev_landingzone.qualibot.eval_load_runs')

import os, re, shlex, sys, json, time, random, asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import yaml
from databricks.sdk import WorkspaceClient
from pyspark.sql import functions as F

APP = dbutils.widgets.get('app_code_path').strip().rstrip('/')
LEVELS = [int(x) for x in dbutils.widgets.get('levels').split(',') if x.strip()]
ROUNDS = max(1, int(dbutils.widgets.get('rounds')))
TIMEOUT_S = float(dbutils.widgets.get('timeout_s'))
STOP_PCT = float(dbutils.widgets.get('stop_error_pct'))
RESULTS = dbutils.widgets.get('results_table').strip()
RUN_ID = datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')

w = WorkspaceClient()
HOST = w.config.host.rstrip('/')


def token() -> str:
    return w.config.authenticate()['Authorization'].split(' ', 1)[1]


def run_async(coro):
    """asyncio.run in its own thread: the notebook may already have an event loop."""
    with ThreadPoolExecutor(max_workers=1) as ex:
        return ex.submit(asyncio.run, coro).result()

# COMMAND ----------

_DIVISION_PREFIX = re.compile(r'^\[Division: (?:AS|IS)\][\s\S]*?\n\n')
QUESTIONS = []
for r in spark.table(dbutils.widgets.get('golden_table')).select('inputs').collect():
    msgs = json.loads(r['inputs'])['messages']
    QUESTIONS.append({'division': 'ALL', 'content': msgs[-1]['content']})
msgs = spark.table(dbutils.widgets.get('chat_table'))
if 'deleted' in msgs.columns:
    msgs = msgs.filter(~F.coalesce(F.col('deleted'), F.lit(False)))
division_col = F.col('division') if 'division' in msgs.columns else F.lit('ALL')
for r in (msgs.filter((F.col('role') == 'user') & F.length('content').between(15, 1500))
          .withColumn('_k', F.lower(F.trim('content'))).dropDuplicates(['_k'])
          .select('content', division_col.alias('division')).limit(400).collect()):
    QUESTIONS.append({'division': (r['division'] or 'ALL').upper(), 'content': _DIVISION_PREFIX.sub('', r['content'])})
random.Random(7).shuffle(QUESTIONS)
import logging

logger = logging.getLogger("load_test_chat")
logger.setLevel(logging.INFO)
if not logger.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S"))
    logger.addHandler(_handler)
    logger.propagate = False


logger.info("%s", " ".join(str(x) for x in (len(QUESTIONS), 'questions;', sum(LEVELS) * ROUNDS, 'to send over the steps', LEVELS,)))

# COMMAND ----------

import websockets

APP_URL = (w.apps.get(dbutils.widgets.get('app_name').strip()).url or '').rstrip('/')
WS_URL = re.sub(r'^http', 'ws', APP_URL) + '/api/chat/ws'


async def ask_app(q, session_id):
    t0 = time.monotonic()
    first = None
    async with websockets.connect(WS_URL, additional_headers={'Authorization': f'Bearer {token()}'},
                                  max_size=None, open_timeout=60) as ws:
        await ws.send(json.dumps({'messages': [{'role': 'user', 'content': q['content']}],
                                  'division': q['division'], 'session_id': session_id}))
        while True:
            m = json.loads(await ws.recv())
            if m['type'] == 'delta' and first is None:
                first = time.monotonic() - t0
            elif m['type'] == 'done':
                return {'status': 'ok', 'ttft_s': first, 'total_s': time.monotonic() - t0,
                        'answer_chars': len(m.get('content') or ''), 'n_sources': len(m.get('sources') or [])}
            elif m['type'] == 'error':
                return {'status': 'error', 'ttft_s': first, 'total_s': time.monotonic() - t0, 'error': m.get('error')}


ENGINE = {}


def load_engine():
    """The app's code and configuration in this notebook (app.yaml, then target_config.env)."""
    with open(f'{APP}/app.yaml', encoding='utf-8-sig') as f:
        for item in (yaml.safe_load(f) or {}).get('env') or []:
            if 'value' in item:
                os.environ[item['name']] = str(item['value'])
    target = f'{APP}/target_config.env'
    if os.path.exists(target):
        for line in open(target, encoding='utf-8'):
            m = re.match(r"\s*export\s+([A-Z0-9_]+)=(.*)$", line)
            if m:
                os.environ[m.group(1)] = (shlex.split(m.group(2)) or [''])[0]
    sys.path.insert(0, APP)
    for mod in [m for m in sys.modules if m == 'server' or m.startswith('server.')]:
        del sys.modules[mod]
    import httpx
    from server.routers.chat import TRANSLATE_BRIDGE_ENABLED, _trim_history, _with_today_date
    from server.services import chat_vsi, translation_bridge
    translation_bridge._get_http_client = lambda: httpx.AsyncClient(timeout=translation_bridge._TIMEOUT_S)
    ENGINE.update(chat_vsi=chat_vsi, tb=translation_bridge, bridge=TRANSLATE_BRIDGE_ENABLED,
                  trim=_trim_history, date=_with_today_date)


async def ask_engine(q, session_id):
    e, tok = ENGINE, token()
    t0 = time.monotonic()
    first, text = None, []
    messages = e['trim']([{'role': 'user', 'content': q['content']}])
    ctx = None
    if e['bridge']:
        en, ctx = await e['tb'].translate_question_to_en(q['content'], HOST, tok)
        if ctx.needs_translation:
            messages[-1]['content'] = en
    language = e['tb'].answer_language(ctx, q['content'])
    async for chunk in e['chat_vsi'].stream_chat_vsi(HOST, tok, q['division'], e['date'](messages), language):
        if not chunk.startswith('data: ') or chunk.strip() == 'data: [DONE]':
            continue
        ev = json.loads(chunk[6:])
        if ev.get('type') == 'response.output_text.delta':
            first = first if first is not None else time.monotonic() - t0
            text.append(ev.get('delta', ''))
        elif ev.get('type') == 'error':
            return {'status': 'error', 'ttft_s': first, 'total_s': time.monotonic() - t0, 'error': ev.get('error')}
    if not text:
        return {'status': 'error', 'ttft_s': None, 'total_s': time.monotonic() - t0, 'error': 'no answer'}
    return {'status': 'ok', 'ttft_s': first, 'total_s': time.monotonic() - t0, 'answer_chars': len(''.join(text))}


async def ask(mode, q, session_id):
    try:
        coro = ask_app(q, session_id) if mode == 'app' else ask_engine(q, session_id)
        return await asyncio.wait_for(coro, TIMEOUT_S)
    except asyncio.TimeoutError:
        return {'status': 'timeout', 'total_s': TIMEOUT_S, 'error': f'no outcome after {TIMEOUT_S:.0f} s'}
    except Exception as exc:  # noqa: BLE001 — a refused connection is a result too
        return {'status': 'error', 'error': f'{type(exc).__name__}: {str(exc)[:300]}'}

# COMMAND ----------

MODE = dbutils.widgets.get('mode')
if MODE in ('auto', 'app'):
    probe = run_async(ask('app', QUESTIONS[0], f'loadtest-{RUN_ID}-probe'))
    logger.info("%s", " ".join(str(x) for x in ('app probe:', probe,)))
    if probe['status'] != 'ok':
        if MODE == 'app':
            raise RuntimeError(f'The app refused this notebook ({probe.get("error")}). Run with mode=engine, '
                               'or from your PC (see operations_dev.md, block U).')
        MODE = 'engine'
    else:
        MODE = 'app'
if MODE == 'engine':
    load_engine()
logger.info("%s", " ".join(str(x) for x in (f'MODE = {MODE}', f'({WS_URL})' if MODE == 'app' else '(engine called in this notebook)',)))

# COMMAND ----------

_SCHEMA = """run_id string, mode string, level int, worker int, seq int, division string, question string,
started_at timestamp, ttft_s double, total_s double, status string, error string, answer_chars int, n_sources int"""
_next = iter(range(10**9))


async def step(level):
    rows = []

    async def worker(wid):
        for seq in range(ROUNDS):
            q = QUESTIONS[next(_next) % len(QUESTIONS)]
            started = datetime.now(timezone.utc)
            out = await ask(MODE, q, f'loadtest-{RUN_ID}-{level}-{wid}-{seq}')
            rows.append({'run_id': RUN_ID, 'mode': MODE, 'level': level, 'worker': wid, 'seq': seq,
                         'division': q['division'], 'question': q['content'][:500], 'started_at': started,
                         'ttft_s': out.get('ttft_s'), 'total_s': out.get('total_s'), 'status': out['status'],
                         'error': out.get('error'), 'answer_chars': out.get('answer_chars'),
                         'n_sources': out.get('n_sources')})

    t0 = time.monotonic()
    await asyncio.gather(*(worker(i) for i in range(level)))
    return rows, time.monotonic() - t0


for level in LEVELS:
    rows, wall = run_async(step(level))
    spark.createDataFrame(rows, schema=_SCHEMA).write.mode('append').option('mergeSchema', 'true').saveAsTable(RESULTS)
    failed = sum(1 for r in rows if r['status'] != 'ok')
    ttft = sorted(r['ttft_s'] for r in rows if r['ttft_s'] is not None)
    p50 = ttft[len(ttft) // 2] if ttft else None
    logger.info(f'level {level:3}: {len(rows)} questions in {wall:.0f} s ({len(rows) / wall * 60:.0f}/min), '
          f'{failed} failed, first word p50 {p50 and round(p50, 1)} s')
    if failed * 100 / len(rows) > STOP_PCT:
        logger.info(f'Stopped: more than {STOP_PCT:.0f} % failed at level {level}.')
        break

# COMMAND ----------

display(spark.sql(f"""
SELECT level, count(*) AS questions,
       round(avg(CASE WHEN status = 'ok' THEN 1 ELSE 0 END) * 100, 1) AS ok_pct,
       count_if(status = 'error') AS errors, count_if(status = 'timeout') AS timeouts,
       round(percentile(ttft_s, 0.5), 1) AS first_word_p50_s, round(percentile(ttft_s, 0.95), 1) AS first_word_p95_s,
       round(percentile(total_s, 0.5), 1) AS total_p50_s, round(percentile(total_s, 0.95), 1) AS total_p95_s,
       round(count(*) / ((max(unix_timestamp(started_at)) - min(unix_timestamp(started_at)) + max(total_s)) / 60), 1)
         AS questions_per_min
FROM {RESULTS} WHERE run_id = '{RUN_ID}' GROUP BY level ORDER BY level"""))

# What failed, by message
display(spark.sql(f"""
SELECT level, status, left(error, 200) AS error, count(*) AS n
FROM {RESULTS} WHERE run_id = '{RUN_ID}' AND status <> 'ok' GROUP BY ALL ORDER BY level, n DESC"""))
logger.info("%s", " ".join(str(x) for x in ('run_id =', RUN_ID,)))
