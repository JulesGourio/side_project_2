# Databricks notebook source
# MAGIC %md
# MAGIC # Qualibot — chat answers side by side, judged by another model
# MAGIC
# MAGIC A **version** = an answer model + the index it searches: `model@label`, the label being one of
# MAGIC `indexes` (as in `retrieval_eval`; `chat` = the app's own index). Everything else is the app's
# MAGIC chat as deployed (`server/services/chat_vsi.py`: rewrite, search, prompt, language reminder).
# MAGIC One **reference** version is compared, question by question, with each **contender** — e.g. the
# MAGIC chat's model against a candidate model, or the chat's index against an index built with another
# MAGIC chunking.
# MAGIC
# MAGIC **Answers as the user sees them** (`translate_back`, on by default when the app's translation
# MAGIC bridge is on): a Spanish/Czech/… question reaches the model in English and the app translates
# MAGIC the answer back (`translation_bridge.translate_answer_back`).
# MAGIC
# MAGIC The **judge** (default GPT-5.6 Luna, never one of the compared models) reads each pair
# MAGIC **twice, in both orders**: a version wins a question only when both readings agree, else
# MAGIC tie. It scores faithfulness / correctness / completeness / citations (0–3) and lists every
# MAGIC claim the documents don't support. When the two versions searched differently, the judge
# MAGIC sees each answer with its own documents.
# MAGIC
# MAGIC Three phases: searches one index after the other, then all answers, then all judgments, in
# MAGIC parallel, by batches of 20. **Every batch is saved as soon as it ends**: searches and answers
# MAGIC in `eval_pairwise_runs_cache` (reused by any later run, whatever its `eval_id`), judgments in
# MAGIC `eval_pairwise_runs`, one line per (question, contender). A run that crashes loses at most
# MAGIC the batch in progress: Run all again and it resumes. Nothing already judged for the same
# MAGIC `eval_id` is re-run. Results of every test so far: `docs/chat_vsi_tests.md`.

# COMMAND ----------

dbutils.widgets.text('app_code_path', '/Workspace/Shared/.bundle/qualibot/dev/files')
dbutils.widgets.text('eval_id', 'chat-check')
dbutils.widgets.text('reference', 'databricks-gpt-6-luna@chat')              # the chat as deployed
# e.g. "databricks-gpt-6-luna@rechunk" with indexes "chat,rechunk=dev_landingzone.qualibot.chunks_test_index"
dbutils.widgets.text('contenders', '')
# Same syntax as retrieval_eval: "label[=catalog.schema.index][|rerank=N][|raw=N][|cap=N]",
# comma-separated; "chat" = the app as deployed.
dbutils.widgets.text('indexes', 'chat')
dbutils.widgets.text('judge', 'databricks-gpt-5-6-luna')
dbutils.widgets.text('answer_max_tokens', '8000')                       # reasoning counts inside it
dbutils.widgets.dropdown('translate_back', 'true', ['true', 'false'])   # as the app: answers back to the question's language
dbutils.widgets.text('n_questions', '40')                               # golden first, then real DEV questions
dbutils.widgets.text('seed', '7')
dbutils.widgets.text('max_parallel', '4')
dbutils.widgets.text('golden_table', 'dev_landingzone.qualibot.qualibot_eval_golden')
dbutils.widgets.text('chat_table', 'dev_landingzone.qualibot.chat_messages')
dbutils.widgets.text('results_table', 'dev_landingzone.qualibot.eval_pairwise_runs')
APP = dbutils.widgets.get('app_code_path').strip().rstrip('/')

# COMMAND ----------

# MAGIC %pip install -q -r $APP/requirements.txt fastapi pyyaml "databricks-sdk>=0.102"
# MAGIC dbutils.library.restartPython()

# COMMAND ----------

import os, re, shlex, sys
import yaml

APP = dbutils.widgets.get('app_code_path').strip().rstrip('/')
EVAL_ID = dbutils.widgets.get('eval_id').strip()
JUDGE = dbutils.widgets.get('judge').strip()
MAX_TOKENS = int(dbutils.widgets.get('answer_max_tokens'))
N_QUESTIONS = int(dbutils.widgets.get('n_questions'))
SEED = int(dbutils.widgets.get('seed'))
MAX_PARALLEL = max(1, int(dbutils.widgets.get('max_parallel')))
RESULTS = dbutils.widgets.get('results_table').strip()

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
REWRITE = os.environ.get('CHAT_VSI_REWRITE_ENDPOINT') or os.environ['CHAT_VSI_LLM_ENDPOINT']
# One rewrite model for every search: no silent fallback to another model (chat_vsi_llm).
os.environ['CHAT_VSI_REWRITE_FALLBACK_ENDPOINTS'] = REWRITE

_SIZE_KEYS = {'rerank': 'CHAT_VSI_RERANK_TOP_K', 'raw': 'CHAT_VSI_RAW_TOP_K', 'rawon': 'CHAT_VSI_RAW_ON',
              'cap': 'CHAT_VSI_MAX_SEARCH_PASSAGES'}


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
            ok = re.fullmatch(r'(question|fr|en)(\+(question|fr|en))*', v) if k == 'rawon' else v.isdigit()
            assert k in _SIZE_KEYS and ok, f'{item}: options are rerank=N, raw=N, cap=N, rawon=fr+en'
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
BY_SIGNATURE = {c['signature']: c for c in CONFIGS.values()}


def version(spec):
    """'model@label' — label: one of `indexes` (default chat). 'index' = the search signature
    (index + sizes), the cache key of its searches."""
    model, _, label = spec.strip().partition('@')
    label = label.strip() or 'chat'
    assert label in CONFIGS, f'{spec}: unknown label — pick from {list(CONFIGS)}'
    return {'label': spec.strip(), 'model': model.strip(), 'index': CONFIGS[label]['signature']}


REFERENCE = version(dbutils.widgets.get('reference'))
CONTENDERS = [version(s) for s in dbutils.widgets.get('contenders').split(',') if s.strip()]
assert JUDGE not in {REFERENCE['model']} | {c['model'] for c in CONTENDERS}, 'the judge must not be a compared model'

# COMMAND ----------

import asyncio, json, time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import httpx
from databricks.sdk import WorkspaceClient
from pyspark.sql import functions as F

for _m in [m for m in sys.modules if m == 'server' or m.startswith('server.')]:
    del sys.modules[_m]
from server.routers.chat import TRANSLATE_BRIDGE_ENABLED, _strip_division, _trim_history, _with_today_date  # app code
from server.services import chat_vsi, chat_vsi_llm
from server.services import translation_bridge as _tb
from server.services.streaming import _cost_eur

assert hasattr(chat_vsi, 'retrieve_for_turn'), (
    f'Stale app code in {APP}: chat_vsi.py has no retrieve_for_turn. '
    'Copy the latest zip, run deploy_qualibot.ps1 -AppEnv dev -SyncOnly, then Run all again.')

# One translation client per event loop (each question runs in its own loop).
_tb._get_http_client = lambda: httpx.AsyncClient(timeout=_tb._TIMEOUT_S)

w = WorkspaceClient()
HOST = w.config.host.rstrip('/')


def token() -> str:
    return w.config.authenticate()['Authorization'].split(' ', 1)[1]


async def search(messages, division, index):
    """The app's steps up to the prompt: history, translation, date, search on `index`."""
    tok = token()
    messages = _trim_history([{'role': m['role'], 'content': m['content']} for m in messages])
    user_content = next((m['content'] for m in reversed(messages) if m['role'] == 'user'), '')
    ctx = None
    if TRANSLATE_BRIDGE_ENABLED and user_content:
        en_question, ctx = await _tb.translate_question_to_en(user_content, HOST, tok)
        if ctx.needs_translation:
            messages = [dict(m) for m in messages]
            messages[-1]['content'] = en_question
    div = chat_vsi.normalize_division(division)
    conversation = chat_vsi._clean_history(_with_today_date(messages))
    apply_config(BY_SIGNATURE[index])              # read at call time, same for every thread of this phase
    found = await chat_vsi.retrieve_for_turn(HOST, tok, div, conversation)
    documents = chat_vsi.group_documents(found['rows'])
    language = _tb.answer_language(ctx, user_content) if user_content else None
    return {'prompt': chat_vsi.build_prompt(div, conversation, documents, language), 'documents': documents}


async def generate(model, prompt):
    """The app's answer call (chat_vsi_llm), pinned to one model; keeps the raw text with its [n] markers."""
    started, first, text, usage = time.monotonic(), None, [], {}
    async for chunk in chat_vsi_llm.stream_answer(HOST, token(), [model], prompt, MAX_TOKENS, 'Chat'):
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


JUDGE_PROMPT = """You evaluate two answers of an assistant for Latécoère quality documentation.
The assistant must answer ONLY from the numbered documents it received, cite them with [n], and
answer in the language of the user's question. You see the conversation, the documents, and two
answers (ANSWER 1, ANSWER 2). {documents_note}

Judge, in this order of importance:
1. faithful: every factual claim is supported by the documents (no invented value, step, role,
   reference, slide content, or general knowledge presented as documented);
2. correct and useful: it really answers the question, with the key facts the documents give;
3. complete but concise;
4. citations: the [n] markers point to documents that support the sentence;
5. language of the question.
If the documents do not contain the answer, the best answer says so instead of improvising.

Return ONLY this JSON:
{{"better": "1" | "2" | "tie",
  "scores": {{"1": {{"faithful": 0-3, "correct": 0-3, "complete": 0-3, "citations": 0-3}},
             "2": {{"faithful": 0-3, "correct": 0-3, "complete": 0-3, "citations": 0-3}}}},
  "unsupported_1": ["claim of answer 1 not supported by its documents", ...],
  "unsupported_2": [...],
  "reason": "two sentences"}}

<conversation>
{conversation}
</conversation>

{documents}

<answer_1>
{answer_1}
</answer_1>

<answer_2>
{answer_2}
</answer_2>"""


def documents_text(documents, max_chars=100_000):
    out = '\n\n'.join(f'[{i}] Document {ref}\n' + '\n\n'.join(d['passages'])
                      for i, (ref, d) in enumerate(documents, 1))
    return out[:max_chars]


async def judge_once(conversation, docs1, docs2, a1, a2):
    if docs1 == docs2:
        note, docs = 'Both answers received the same documents.', f'<documents>\n{docs1}\n</documents>'
    else:
        note = 'Each answer received its own documents: check each answer against its own set.'
        docs = (f'<documents_of_answer_1>\n{docs1}\n</documents_of_answer_1>\n\n'
                f'<documents_of_answer_2>\n{docs2}\n</documents_of_answer_2>')
    prompt = JUDGE_PROMPT.format(documents_note=note, conversation=conversation, documents=docs,
                                 answer_1=a1, answer_2=a2)
    payload = {'messages': [{'role': 'user', 'content': prompt}], 'max_tokens': 6000}
    async with httpx.AsyncClient(timeout=240) as client:
        for attempt in range(4):
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


CACHE = RESULTS + '_cache'


def cache_load(kind):
    """{key tuple: payload} of what an earlier run already saved (searches, answers)."""
    if not spark.catalog.tableExists(CACHE):
        return {}
    out = {}
    for r in spark.table(CACHE).filter(F.col('kind') == kind).orderBy('saved_at').select('key', 'payload').collect():
        out[tuple(json.loads(r['key']))] = json.loads(r['payload'])
    return out


def cache_save(kind, results):
    rows = [(EVAL_ID, kind, json.dumps(list(k), ensure_ascii=False), json.dumps(v, ensure_ascii=False),
             datetime.now(timezone.utc)) for k, v in results.items() if 'error' not in v]
    if rows:
        spark.createDataFrame(rows, 'eval_id string, kind string, key string, payload string, saved_at timestamp') \
            .write.mode('append').saveAsTable(CACHE)


import logging

logger = logging.getLogger("pairwise_answers")
logger.setLevel(logging.INFO)
if not logger.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S"))
    logger.addHandler(_handler)
    logger.propagate = False


def in_batches(fn, keys, on_batch, size=20):
    """in_threads by batches; on_batch(dict) saves each batch as soon as it ends."""
    out = {}
    for i in range(0, len(keys), size):
        batch = keys[i:i + size]
        done_batch = dict(zip(batch, in_threads(fn, batch)))
        on_batch(done_batch)
        out.update(done_batch)
        logger.info(f'  {min(i + size, len(keys))}/{len(keys)} saved')
    return out


def in_threads(fn, items):
    def safe(item):
        for attempt in range(2):
            try:
                return asyncio.run(fn(item))
            except Exception as exc:
                err = {'error': f'{type(exc).__name__}: {str(exc)[:300]}'}
                time.sleep(10)
        return err
    with ThreadPoolExecutor(max_workers=MAX_PARALLEL) as pool:
        return list(pool.map(safe, items))

# COMMAND ----------

CASES = []
for r in spark.table(dbutils.widgets.get('golden_table')).select('dataset_record_id', 'inputs').collect():
    CASES.append({'source': 'golden', 'case_id': r['dataset_record_id'], 'division': 'ALL',
                  'messages': json.loads(r['inputs'])['messages']})
CASES = CASES[:N_QUESTIONS]
n_chat = N_QUESTIONS - len(CASES)
if n_chat > 0:
    msgs = spark.table(dbutils.widgets.get('chat_table'))
    if 'deleted' in msgs.columns:
        msgs = msgs.filter(~F.coalesce(F.col('deleted'), F.lit(False)))
    users = msgs.filter((F.col('role') == 'user') & F.length('content').between(15, 1500))
    picked = (users.withColumn('_k', F.lower(F.trim('content'))).dropDuplicates(['_k'])
              .orderBy(F.rand(SEED)).limit(n_chat)
              .select('id', 'session_id', 'created_at', 'content',
                      (F.col('division') if 'division' in msgs.columns else F.lit('ALL')).alias('division'))
              .collect())
    sessions = [r['session_id'] for r in picked if r['session_id']]
    history = {}
    for h in (msgs.filter(F.col('session_id').isin(sessions))
              .select('session_id', 'created_at', 'role', 'content').orderBy('created_at').collect()):
        history.setdefault(h['session_id'], []).append(h)
    for r in picked:
        earlier = [h for h in history.get(r['session_id'], []) if h['created_at'] < r['created_at']][-4:]
        # Turns of the former Knowledge Assistant carry its "[Division: …]" routing prefix.
        messages = [{'role': h['role'], 'content': _strip_division(h['content'])} for h in earlier] + \
                   [{'role': 'user', 'content': _strip_division(r['content'])}]
        while messages and messages[0]['role'] != 'user':
            messages.pop(0)
        CASES.append({'source': 'chat', 'case_id': str(r['id']), 'division': (r['division'] or 'ALL').upper(),
                      'messages': messages})

done = set()
if spark.catalog.tableExists(RESULTS):
    done = {(r['source'], r['case_id'], r['contender']) for r in spark.table(RESULTS)
            .filter((F.col('eval_id') == EVAL_ID) & F.col('error').isNull())
            .select('source', 'case_id', 'contender').collect()}
TODO = [(c, v) for c in CASES for v in CONTENDERS if (c['source'], c['case_id'], v['label']) not in done]
logger.info("%s", " ".join(str(x) for x in (len(CASES), 'questions,', len(TODO), 'comparisons to run | reference', REFERENCE['label'], '| judge', JUDGE,)))

# COMMAND ----------

cases_todo = {(c['source'], c['case_id']): c for c, _ in TODO}
# Cache key: (index, rewrite model, source, case_id) — the same search is reused by later runs.
SEARCH = {(k[0],) + k[2:]: v for k, v in cache_load('search').items() if k[1] == REWRITE}
for index in sorted({REFERENCE['index']} | {v['index'] for _, v in TODO}):
    keys = [k for k in cases_todo if (index,) + k not in SEARCH]
    logger.info(f'{index}: {len(cases_todo) - len(keys)} searches already saved, {len(keys)} to run')
    if not keys:
        continue
    got = in_batches(lambda k, index=index: search(cases_todo[k]['messages'], cases_todo[k]['division'], index), keys,
                     lambda b, index=index: cache_save('search', {(index, REWRITE) + k: v for k, v in b.items()}))
    SEARCH.update({(index,) + k: v for k, v in got.items()})

# COMMAND ----------

# A job = (model, index, source, case_id).
jobs = sorted({(REFERENCE['model'], REFERENCE['index']) + k for k in cases_todo}
              | {(v['model'], v['index'], c['source'], c['case_id']) for c, v in TODO})


async def _answer(job):
    model, index, source, case_id = job
    s = SEARCH[(index, source, case_id)]
    if 'error' in s:
        raise RuntimeError('search failed: ' + s['error'])
    return await generate(model, s['prompt'])

# Cache key: (model, index, source, case_id, answer_max_tokens).
ANSWERS = {k[:4]: v for k, v in cache_load('answer').items() if k[4] == MAX_TOKENS}
_missing = [j for j in jobs if j not in ANSWERS]
logger.info("%s", " ".join(str(x) for x in (len(jobs) - len(_missing), 'answers already saved,', len(_missing), 'to generate',)))
ANSWERS.update(in_batches(_answer, _missing,
                          lambda b: cache_save('answer', {j + (MAX_TOKENS,): v for j, v in b.items()})))

# As the app shows them: translated back to the question's language (cache kind 'shown').
TRANSLATE_BACK = dbutils.widgets.get('translate_back') == 'true' and TRANSLATE_BRIDGE_ENABLED
if TRANSLATE_BACK:
    async def _shown(job):
        a = ANSWERS[job]
        if 'error' in a:
            raise RuntimeError(a['error'])
        question = cases_todo[job[2:]]['messages'][-1]['content']
        _, ctx = await _tb.translate_question_to_en(question, HOST, token())
        text = await _tb.translate_answer_back(a['answer'], ctx, HOST, token())
        return {**a, 'answer': text, 'translated': text != a['answer']}

    SHOWN = {k[:4]: v for k, v in cache_load('shown').items() if k[4] == MAX_TOKENS}
    _todo = [j for j in jobs if j not in SHOWN and 'error' not in ANSWERS.get(j, {'error': ''})]
    logger.info("%s", " ".join(str(x) for x in (len(_todo), 'answers to pass through the translation back',)))
    SHOWN.update(in_batches(_shown, _todo,
                            lambda b: cache_save('shown', {j + (MAX_TOKENS,): v for j, v in b.items()})))
    for j in jobs:
        if j in SHOWN and 'error' not in SHOWN[j]:
            ANSWERS[j] = SHOWN[j]
    logger.info("%s", " ".join(str(x) for x in (sum(1 for j in jobs if ANSWERS.get(j, {}).get('translated')), 'answers translated back',)))
else:
    logger.info('answers judged as generated (translate_back off or translation bridge off in the app config)')
logger.info("%s", " ".join(str(x) for x in (sum(1 for a in ANSWERS.values() if 'error' not in a), '/', len(ANSWERS), 'answers',)))

# COMMAND ----------

def _docs(index, source, case_id):
    s = SEARCH[(index, source, case_id)]
    return documents_text(s['documents']) if 'error' not in s else ''


async def _judge(item):
    case, v = item
    k = (case['source'], case['case_id'])
    ref = ANSWERS[(REFERENCE['model'], REFERENCE['index']) + k]
    con = ANSWERS[(v['model'], v['index']) + k]
    for a in (ref, con):
        if 'error' in a:
            raise RuntimeError('answer failed: ' + a['error'])
    conversation = '\n'.join(f"{m['role']}: {m['content']}" for m in case['messages'])
    d_ref, d_con = _docs(REFERENCE['index'], *k), _docs(v['index'], *k)
    v1, v2 = await asyncio.gather(judge_once(conversation, d_ref, d_con, ref['answer'], con['answer']),  # ref first
                                  judge_once(conversation, d_con, d_ref, con['answer'], ref['answer']))  # contender first
    pick1 = {'1': 'ref', '2': 'contender'}.get(str(v1.get('better')), 'tie')
    pick2 = {'1': 'contender', '2': 'ref'}.get(str(v2.get('better')), 'tie')
    sc = lambda v, slot: (v.get('scores') or {}).get(slot) or {}
    avg = lambda pair, key: round(sum(float(x.get(key, 0) or 0) for x in pair) / 2, 2)
    s_ref, s_con = [sc(v1, '1'), sc(v2, '2')], [sc(v1, '2'), sc(v2, '1')]
    uns_ref = (v1.get('unsupported_1') or []) + (v2.get('unsupported_2') or [])
    uns_con = (v1.get('unsupported_2') or []) + (v2.get('unsupported_1') or [])
    return {
        'winner': pick1 if pick1 == pick2 else 'tie', 'pick_ref_first': pick1, 'pick_contender_first': pick2,
        **{f'ref_{m}': avg(s_ref, m) for m in ('faithful', 'correct', 'complete', 'citations')},
        **{f'con_{m}': avg(s_con, m) for m in ('faithful', 'correct', 'complete', 'citations')},
        'ref_inventions': len(uns_ref) / 2, 'con_inventions': len(uns_con) / 2,
        'ref_unsupported': json.dumps(uns_ref, ensure_ascii=False),
        'con_unsupported': json.dumps(uns_con, ensure_ascii=False),
        'reason': v1.get('reason'),
        'ref_answer': ref['answer'], 'con_answer': con['answer'],
        **{f'ref_{m}': ref[m] for m in ('latency_s', 'first_token_s', 'input_tokens', 'output_tokens', 'cost_eur')},
        **{f'con_{m}': con[m] for m in ('latency_s', 'first_token_s', 'input_tokens', 'output_tokens', 'cost_eur')},
        'judge_cost_eur': round(v1['_cost'] + v2['_cost'], 6),
    }

_SCHEMA = """eval_id string, run_ts timestamp, judge string, reference string, contender string,
contender_model string, contender_search string, source string, case_id string, question string,
winner string, pick_ref_first string, pick_contender_first string,
ref_faithful double, ref_correct double, ref_complete double, ref_citations double,
con_faithful double, con_correct double, con_complete double, con_citations double,
ref_inventions double, con_inventions double, ref_unsupported string, con_unsupported string, reason string,
ref_answer string, con_answer string,
ref_latency_s double, ref_first_token_s double, ref_input_tokens long, ref_output_tokens long, ref_cost_eur double,
con_latency_s double, con_first_token_s double, con_input_tokens long, con_output_tokens long, con_cost_eur double,
judge_cost_eur double, error string"""
_FIELDS = [f.strip().split(' ')[0] for f in _SCHEMA.replace('\n', ' ').split(',')]


def save_judgments(batch):
    run_ts, rows = datetime.now(timezone.utc), []
    for i, out in batch.items():
        case, v = TODO[i]
        row = {k: None for k in _FIELDS}
        row.update({k: val for k, val in out.items() if k in row})
        row.update({'eval_id': EVAL_ID, 'run_ts': run_ts, 'judge': JUDGE, 'reference': REFERENCE['label'],
                    'contender': v['label'], 'contender_model': v['model'], 'contender_search': v['index'],
                    'source': case['source'], 'case_id': case['case_id'],
                    'question': case['messages'][-1]['content'], 'error': out.get('error')})
        for k in ('ref_input_tokens', 'ref_output_tokens', 'con_input_tokens', 'con_output_tokens'):
            row[k] = int(row[k]) if row[k] is not None else None
        for k in _FIELDS:
            if isinstance(row[k], int) and not k.endswith('_tokens'):
                row[k] = float(row[k])
        rows.append(row)
    if rows:
        spark.createDataFrame(rows, schema=_SCHEMA).write.mode('append').option('mergeSchema', 'true').saveAsTable(RESULTS)
    for e in sorted({r['error'] for r in rows if r['error']})[:3]:
        logger.info("%s", " ".join(str(x) for x in ('   ', e,)))


t0 = time.monotonic()
outs = in_batches(lambda i: _judge(TODO[i]), list(range(len(TODO))), save_judgments)
logger.info(f'{len(outs)} judgments in {time.monotonic() - t0:.0f} s, {sum(1 for o in outs.values() if "error" in o)} errors')

# COMMAND ----------

spark.sql(f"""CREATE OR REPLACE TEMPORARY VIEW pair AS
SELECT * EXCEPT (rn) FROM (SELECT *, row_number() OVER (PARTITION BY eval_id, source, case_id, contender
                                                      ORDER BY error IS NULL DESC, run_ts DESC) AS rn
                         FROM {RESULTS} WHERE eval_id = '{EVAL_ID}') WHERE rn = 1 AND error IS NULL""")

# Quality: a win counts only when both orders agree; scores 0-3; inventions = unsupported claims per answer
display(spark.sql("""
SELECT contender, count(*) AS questions,
       count_if(winner = 'contender') AS contender_wins, count_if(winner = 'ref') AS reference_wins,
       count_if(winner = 'tie') AS ties,
       round(avg(con_faithful), 2) AS con_faithful, round(avg(ref_faithful), 2) AS ref_faithful,
       round(avg(con_correct), 2) AS con_correct, round(avg(ref_correct), 2) AS ref_correct,
       round(avg(con_inventions), 2) AS con_inventions, round(avg(ref_inventions), 2) AS ref_inventions,
       round(avg(CASE WHEN pick_ref_first = pick_contender_first THEN 1 ELSE 0 END) * 100) AS judge_consistent_pct
FROM pair GROUP BY contender ORDER BY contender_wins - reference_wins DESC"""))

# Speed and cost (input tokens = the size of the context each search gives)
display(spark.sql("""
SELECT contender, percentile(con_first_token_s, 0.5) AS first_token_p50, percentile(con_latency_s, 0.5) AS latency_p50,
       round(avg(con_input_tokens)) AS input_tokens, round(avg(con_output_tokens)) AS output_tokens,
       round(avg(con_cost_eur), 4) AS eur_per_question,
       percentile(ref_first_token_s, 0.5) AS ref_first_token_p50, round(avg(ref_cost_eur), 4) AS ref_eur_per_question,
       round(avg(judge_cost_eur), 4) AS judge_eur
FROM pair GROUP BY contender ORDER BY contender"""))

# To read by eye: clear wins first, then the cases where the judge changed its mind with the order
display(spark.sql("""
SELECT contender, source, winner, pick_ref_first, pick_contender_first, left(question, 120) AS question,
       con_inventions, ref_inventions, reason, con_answer, ref_answer, con_unsupported, ref_unsupported
FROM pair ORDER BY winner = 'tie', pick_ref_first = pick_contender_first, contender"""))
