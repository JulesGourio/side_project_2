# Databricks notebook source
# MAGIC %md
# MAGIC # Qualibot — two answer models side by side, same passages, judged by a third model
# MAGIC
# MAGIC For each question the search runs **once** (the `u-all` configuration of `retrieval_eval`,
# MAGIC rewrite by a fixed model), then **model A** and **model B** write their answer from exactly
# MAGIC the same passages and the same prompt (the app's own `chat_vsi_prompts.build_prompt`). Only
# MAGIC the writing differs, so the comparison is not blurred by the search.
# MAGIC
# MAGIC A **judge** from another family (default Gemini 3.8 Flash) reads the passages and both
# MAGIC answers, **twice, in both orders** (a judge tends to favour one position): a model wins a
# MAGIC question only when both readings agree, otherwise it is a tie. It also lists every claim of
# MAGIC each answer that the passages do not support (invention check).
# MAGIC
# MAGIC Questions: the golden set and a random sample of real DEV user questions
# MAGIC (`chat_messages`, with the earlier turns of their conversation). Results in
# MAGIC `eval_pairwise_runs`, one line per question; a question already judged for the same
# MAGIC `pair_id` is never re-run. Run all.

# COMMAND ----------

dbutils.widgets.text('app_code_path', '/Workspace/Shared/.bundle/qualibot/dev/files')
dbutils.widgets.text('pair_id', 's55-vs-luna6')                  # name of this comparison
dbutils.widgets.text('model_a', 'databricks-claude-sonnet-5-5')
dbutils.widgets.text('model_b', 'databricks-gpt-6-luna')
dbutils.widgets.text('judge', 'databricks-gemini-3-8-flash')
dbutils.widgets.text('rewrite_model', 'databricks-claude-sonnet-4-6')  # same rewrite, so same passages
dbutils.widgets.text('answer_max_tokens', '8000')                 # reasoning counts inside it
dbutils.widgets.text('sources', 'golden,chat')
dbutils.widgets.text('n_chat_questions', '60')
dbutils.widgets.text('seed', '7')
dbutils.widgets.text('max_parallel', '3')
dbutils.widgets.text('golden_table', 'dev_landingzone.qualibot.qualibot_eval_golden')
dbutils.widgets.text('chat_table', 'dev_landingzone.qualibot.chat_messages')
dbutils.widgets.text('results_table', 'dev_landingzone.qualibot.eval_pairwise_runs')
APP = dbutils.widgets.get('app_code_path').strip().rstrip('/')

# COMMAND ----------

# MAGIC %pip install -q -r $APP/requirements.txt fastapi pyyaml "databricks-sdk>=0.102"
# MAGIC dbutils.library.restartPython()

# COMMAND ----------

# DBTITLE 1,Parameters, app configuration (app.yaml, then target_config.env), search = u-all
import os, re, shlex, sys
import yaml

APP = dbutils.widgets.get('app_code_path').strip().rstrip('/')
PAIR = dbutils.widgets.get('pair_id').strip()
MODEL_A, MODEL_B = dbutils.widgets.get('model_a').strip(), dbutils.widgets.get('model_b').strip()
JUDGE = dbutils.widgets.get('judge').strip()
REWRITE = dbutils.widgets.get('rewrite_model').strip()
MAX_TOKENS = int(dbutils.widgets.get('answer_max_tokens'))
SOURCES = {s.strip() for s in dbutils.widgets.get('sources').split(',') if s.strip()}
N_CHAT = int(dbutils.widgets.get('n_chat_questions'))
SEED = int(dbutils.widgets.get('seed'))
MAX_PARALLEL = max(1, int(dbutils.widgets.get('max_parallel')))
RESULTS = dbutils.widgets.get('results_table').strip()
assert JUDGE not in (MODEL_A, MODEL_B), 'the judge must not be one of the two models'

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
# u-all (retrieval_eval, 2026-10-08): union + ctx + REF + titles + bilingual rewrite + one language.
os.environ.update({
    'CHAT_VSI_VARIANT': 'rerank', 'CHAT_VSI_RERANK_MERGE': 'union',
    'CHAT_VSI_RERANK_COLUMNS': 'REF,semantic_headers,chunk_text', 'CHAT_VSI_REF_LOOKUP': 'on',
    'CHAT_VSI_TITLE_LOOKUP': 'on', 'CHAT_VSI_REWRITE': 'bilingual', 'CHAT_VSI_ONE_LANGUAGE': 'on',
    'CHAT_VSI_REWRITE_ENDPOINT': REWRITE, 'CHAT_VSI_REWRITE_MAX_TOKENS': '1000',
})
sys.path.insert(0, APP)

# COMMAND ----------

# DBTITLE 1,App code: search once, then each model answers from the same prompt
import asyncio, json, time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import httpx
from databricks.sdk import WorkspaceClient

for _m in [m for m in sys.modules if m == 'server' or m.startswith('server.')]:
    del sys.modules[_m]
from server.routers.chat import TRANSLATE_BRIDGE_ENABLED, _trim_history, _with_today_date  # app code
from server.services import chat_vsi as base, chat_vsi_prompts, chat_vsi_rerank
from server.services import translation_bridge as _tb
from server.services.doc_catalog import canon_ref
from server.services.streaming import _cost_eur, stream_analysis

# One translation client per event loop (each question runs in its own loop).
_tb._get_http_client = lambda: httpx.AsyncClient(timeout=_tb._TIMEOUT_S)

w = WorkspaceClient()
HOST = w.config.host.rstrip('/')


def token() -> str:
    return w.config.authenticate()['Authorization'].split(' ', 1)[1]


async def search(messages, division):
    """The app's steps up to the prompt: history, translation, date, u-all search."""
    tok = token()
    messages = _trim_history([{'role': m['role'], 'content': m['content']} for m in messages])
    user_content = next((m['content'] for m in reversed(messages) if m['role'] == 'user'), '')
    if TRANSLATE_BRIDGE_ENABLED and user_content:
        en_question, ctx = await _tb.translate_question_to_en(user_content, HOST, tok)
        if ctx.needs_translation:
            messages = [dict(m) for m in messages]
            messages[-1]['content'] = en_question
    div = base.normalize_division(division)
    conversation = base._clean_history(_with_today_date(messages))
    found = await chat_vsi_rerank.retrieve_for_turn(HOST, tok, base.index_for_division(div), REWRITE, conversation)
    documents = base.group_documents(found['rows'])
    return {'prompt': chat_vsi_prompts.build_prompt(div, conversation, documents), 'documents': documents}


async def generate(model, prompt):
    """Same call as stream_chat_vsi_rerank; keeps the raw text with its [n] markers."""
    started, first, text, usage = time.monotonic(), None, [], {}
    async for chunk in stream_analysis(HOST, token(), model, prompt, max_tokens=MAX_TOKENS, thinking_budget=0,
                                       temperature=0.0, operation='Chat'):
        if not chunk.startswith('data: '):
            continue
        data = chunk[6:].strip()
        if data == '[DONE]':
            break
        try:
            ev = json.loads(data)
        except json.JSONDecodeError:
            continue
        if ev.get('type') == 'response.output_text.delta':
            first = first if first is not None else time.monotonic() - started
            text.append(ev.get('delta', ''))
        elif ev.get('type') == 'usage':
            usage = ev
        elif ev.get('type') == 'error':
            raise RuntimeError(ev.get('error'))
    answer = ''.join(text).strip()
    if not answer:
        raise RuntimeError('empty answer')
    return {'answer': answer, 'latency_s': round(time.monotonic() - started, 1),
            'first_token_s': round(first, 1) if first is not None else None,
            'input_tokens': usage.get('input_tokens'), 'output_tokens': usage.get('output_tokens'),
            'cost_eur': usage.get('cost_eur')}

# COMMAND ----------

# DBTITLE 1,Judge: both orders, invention check
JUDGE_PROMPT = """You evaluate two answers of an assistant for Latécoère quality documentation.
The assistant must answer ONLY from the numbered documents below, cite them with [n], and answer
in the language of the user's question. You see the conversation, the documents the assistant
received, and two answers (ANSWER 1, ANSWER 2) written from exactly the same documents.

Judge, in this order of importance:
1. faithful: every factual claim is supported by the documents (no invented value, step, role,
   reference, slide content or general knowledge presented as documented);
2. correct and useful: it really answers the question, with the key facts the documents give;
3. complete but concise;
4. citations: the [n] markers point to documents that support the sentence;
5. language of the question.
If the documents do not contain the answer, the best answer says so instead of improvising.

Return ONLY this JSON:
{{"better": "1" | "2" | "tie",
  "scores": {{"1": {{"faithful": 0-3, "correct": 0-3, "complete": 0-3, "citations": 0-3}},
             "2": {{"faithful": 0-3, "correct": 0-3, "complete": 0-3, "citations": 0-3}}}},
  "unsupported_1": ["claim of answer 1 not supported by the documents", ...],
  "unsupported_2": [...],
  "reason": "two sentences"}}

<conversation>
{conversation}
</conversation>

<documents>
{documents}
</documents>

<answer_1>
{answer_1}
</answer_1>

<answer_2>
{answer_2}
</answer_2>"""


def _documents_text(documents, max_chars=120_000):
    out = '\n\n'.join(f'[{i}] Document {ref}\n' + '\n\n'.join(d['passages'])
                      for i, (ref, d) in enumerate(documents, 1))
    return out[:max_chars]


async def judge_once(conversation, docs_text, a1, a2):
    prompt = JUDGE_PROMPT.format(conversation=conversation, documents=docs_text, answer_1=a1, answer_2=a2)
    payload = {'messages': [{'role': 'user', 'content': prompt}], 'max_tokens': 4000}
    async with httpx.AsyncClient(timeout=180) as client:
        for attempt in range(3):
            resp = await client.post(f'{HOST}/serving-endpoints/{JUDGE}/invocations', json=payload,
                                     headers={'Authorization': f'Bearer {token()}'})
            if resp.status_code == 429 or resp.status_code >= 500:
                await asyncio.sleep(10 * (attempt + 1))
                continue
            break
        resp.raise_for_status()
    body = resp.json()
    content = body['choices'][0]['message']['content']
    if isinstance(content, list):
        content = ''.join(b.get('text', '') for b in content if isinstance(b, dict))
    m = re.search(r'\{.*\}', content or '', re.S)
    if not m:
        raise RuntimeError(f'judge returned no JSON: {(content or "")[:200]}')
    usage = body.get('usage') or {}
    verdict = json.loads(m.group(0))
    verdict['_cost'] = _cost_eur(JUDGE, usage.get('prompt_tokens') or 0, usage.get('completion_tokens') or 0)
    return verdict


async def compare(case):
    s = await search(case['messages'], case['division'])
    a, b = await asyncio.gather(generate(MODEL_A, s['prompt']), generate(MODEL_B, s['prompt']))
    conversation = '\n'.join(f"{m['role']}: {m['content']}" for m in case['messages'])
    docs_text = _documents_text(s['documents'])
    v1, v2 = await asyncio.gather(judge_once(conversation, docs_text, a['answer'], b['answer']),   # A first
                                  judge_once(conversation, docs_text, b['answer'], a['answer']))  # B first
    pick1 = {'1': 'A', '2': 'B'}.get(str(v1.get('better')), 'tie')
    pick2 = {'1': 'B', '2': 'A'}.get(str(v2.get('better')), 'tie')
    final = pick1 if pick1 == pick2 else 'tie'

    def scores(v, slot):
        return (v.get('scores') or {}).get(slot) or {}

    sa = [scores(v1, '1'), scores(v2, '2')]
    sb = [scores(v1, '2'), scores(v2, '1')]
    avg = lambda ss, k: round(sum(float(x.get(k, 0) or 0) for x in ss) / 2, 2)
    return {
        'documents': [canon_ref(ref) for ref, _ in s['documents']],
        'answer_a': a['answer'], 'answer_b': b['answer'],
        'pick_a_first': pick1, 'pick_b_first': pick2, 'winner': final,
        **{f'a_{k}': avg(sa, k) for k in ('faithful', 'correct', 'complete', 'citations')},
        **{f'b_{k}': avg(sb, k) for k in ('faithful', 'correct', 'complete', 'citations')},
        'a_unsupported': json.dumps((v1.get('unsupported_1') or []) + (v2.get('unsupported_2') or []), ensure_ascii=False),
        'b_unsupported': json.dumps((v1.get('unsupported_2') or []) + (v2.get('unsupported_1') or []), ensure_ascii=False),
        'n_unsupported_a': (len(v1.get('unsupported_1') or []) + len(v2.get('unsupported_2') or [])) / 2,
        'n_unsupported_b': (len(v1.get('unsupported_2') or []) + len(v2.get('unsupported_1') or [])) / 2,
        'reason_a_first': v1.get('reason'), 'reason_b_first': v2.get('reason'),
        **{f'a_{k}': a[k] for k in ('latency_s', 'first_token_s', 'input_tokens', 'output_tokens', 'cost_eur')},
        **{f'b_{k}': b[k] for k in ('latency_s', 'first_token_s', 'input_tokens', 'output_tokens', 'cost_eur')},
        'judge_cost_eur': round(v1['_cost'] + v2['_cost'], 6),
    }


def run_case(case):
    for attempt in range(2):
        try:
            return asyncio.run(compare(case))
        except Exception as exc:
            err = f'{type(exc).__name__}: {str(exc)[:300]}'
            time.sleep(10)
    return {'error': err}

# COMMAND ----------

# DBTITLE 1,Questions: golden + a random sample of real DEV questions (with their earlier turns)
from pyspark.sql import functions as F, Window

CASES = []
if 'golden' in SOURCES:
    for r in spark.table(dbutils.widgets.get('golden_table')).select('dataset_record_id', 'inputs').collect():
        CASES.append({'source': 'golden', 'case_id': r['dataset_record_id'], 'division': 'ALL',
                      'messages': json.loads(r['inputs'])['messages']})
if 'chat' in SOURCES and N_CHAT > 0:
    msgs = spark.table(dbutils.widgets.get('chat_table'))
    if 'deleted' in msgs.columns:
        msgs = msgs.filter(~F.coalesce(F.col('deleted'), F.lit(False)))
    users = (msgs.filter((F.col('role') == 'user') & F.length('content').between(15, 1500))
             .withColumn('_k', F.lower(F.trim('content')))
             .dropDuplicates(['_k'])
             .orderBy(F.rand(SEED)).limit(N_CHAT)
             .select('id', 'session_id', 'created_at', 'content',
                     (F.col('division') if 'division' in msgs.columns else F.lit('ALL')).alias('division')))
    picked = users.collect()
    sessions = [r['session_id'] for r in picked if r['session_id']]
    history = {}
    for h in (msgs.filter(F.col('session_id').isin(sessions))
              .select('session_id', 'created_at', 'role', 'content').orderBy('created_at').collect()):
        history.setdefault(h['session_id'], []).append(h)
    for r in picked:
        earlier = [h for h in history.get(r['session_id'], []) if h['created_at'] < r['created_at']][-4:]
        messages = [{'role': h['role'], 'content': h['content']} for h in earlier] + \
                   [{'role': 'user', 'content': r['content']}]
        while messages and messages[0]['role'] != 'user':
            messages.pop(0)
        CASES.append({'source': 'chat', 'case_id': str(r['id']), 'division': (r['division'] or 'ALL').upper(),
                      'messages': messages})

done = set()
if spark.catalog.tableExists(RESULTS):
    done = {(r['source'], r['case_id']) for r in spark.table(RESULTS)
            .filter((F.col('pair_id') == PAIR) & F.col('error').isNull()).select('source', 'case_id').collect()}
TODO = [c for c in CASES if (c['source'], c['case_id']) not in done]
print(len(CASES), 'questions,', len(TODO), 'to run for', PAIR, f'({MODEL_A} vs {MODEL_B}, judge {JUDGE})')

# COMMAND ----------

# DBTITLE 1,Run (questions in parallel), saved at the end
_SCHEMA = """pair_id string, run_ts timestamp, model_a string, model_b string, judge string, source string,
case_id string, question string, documents array<string>, answer_a string, answer_b string,
pick_a_first string, pick_b_first string, winner string,
a_faithful double, a_correct double, a_complete double, a_citations double,
b_faithful double, b_correct double, b_complete double, b_citations double,
a_unsupported string, b_unsupported string, n_unsupported_a double, n_unsupported_b double,
reason_a_first string, reason_b_first string,
a_latency_s double, a_first_token_s double, a_input_tokens long, a_output_tokens long, a_cost_eur double,
b_latency_s double, b_first_token_s double, b_input_tokens long, b_output_tokens long, b_cost_eur double,
judge_cost_eur double, error string"""
_FIELDS = [f.strip().split(' ')[0] for f in _SCHEMA.replace('\n', ' ').split(',')]

t0 = time.monotonic()
with ThreadPoolExecutor(max_workers=MAX_PARALLEL) as pool:
    outs = list(pool.map(run_case, TODO))
run_ts = datetime.now(timezone.utc)
rows = []
for case, out in zip(TODO, outs):
    row = {k: None for k in _FIELDS}
    row.update({k: v for k, v in out.items() if k in row})
    row.update({'pair_id': PAIR, 'run_ts': run_ts, 'model_a': MODEL_A, 'model_b': MODEL_B, 'judge': JUDGE,
                'source': case['source'], 'case_id': case['case_id'],
                'question': case['messages'][-1]['content'], 'error': out.get('error')})
    for k in ('a_input_tokens', 'a_output_tokens', 'b_input_tokens', 'b_output_tokens'):
        row[k] = int(row[k]) if row[k] is not None else None
    rows.append(row)
if rows:
    spark.createDataFrame(rows, schema=_SCHEMA).write.mode('append').option('mergeSchema', 'true').saveAsTable(RESULTS)
errors = [r['error'] for r in rows if r['error']]
print(f'{len(rows)} questions in {time.monotonic() - t0:.0f} s, {len(errors)} errors')
for e in sorted(set(errors))[:5]:
    print('   ', e)

# COMMAND ----------

# DBTITLE 1,Results
spark.sql(f"""CREATE OR REPLACE TEMPORARY VIEW pair AS
SELECT * EXCEPT (rn) FROM (SELECT *, row_number() OVER (PARTITION BY pair_id, source, case_id
                                                      ORDER BY error IS NULL DESC, run_ts DESC) AS rn
                         FROM {RESULTS} WHERE pair_id = '{PAIR}') WHERE rn = 1 AND error IS NULL""")

# Who wins (a win counts only when both orders agree), quality scores (0-3), inventions, speed, cost
display(spark.sql("""
SELECT source, count(*) AS questions,
       count_if(winner = 'A') AS a_wins, count_if(winner = 'B') AS b_wins, count_if(winner = 'tie') AS ties,
       round(avg(CASE WHEN pick_a_first = pick_b_first THEN 1 ELSE 0 END) * 100) AS judge_consistent_pct,
       round(avg(a_faithful), 2) AS a_faithful, round(avg(b_faithful), 2) AS b_faithful,
       round(avg(a_correct), 2) AS a_correct, round(avg(b_correct), 2) AS b_correct,
       round(avg(n_unsupported_a), 2) AS a_inventions, round(avg(n_unsupported_b), 2) AS b_inventions
FROM pair GROUP BY ROLLUP(source) ORDER BY source"""))

display(spark.sql("""
SELECT max(model_a) AS model_a, max(model_b) AS model_b,
       percentile(a_first_token_s, 0.5) AS a_first_token_p50, percentile(b_first_token_s, 0.5) AS b_first_token_p50,
       percentile(a_latency_s, 0.5) AS a_latency_p50, percentile(b_latency_s, 0.5) AS b_latency_p50,
       round(avg(a_cost_eur), 4) AS a_eur_per_q, round(avg(b_cost_eur), 4) AS b_eur_per_q,
       round(avg(judge_cost_eur), 4) AS judge_eur_per_q
FROM pair"""))

# Questions to read by eye: one model clearly better, or the judge changed its mind with the order
display(spark.sql("""
SELECT source, winner, pick_a_first, pick_b_first, left(question, 120) AS question,
       n_unsupported_a, n_unsupported_b, reason_a_first, answer_a, answer_b, a_unsupported, b_unsupported
FROM pair ORDER BY winner = 'tie', pick_a_first = pick_b_first, source"""))
