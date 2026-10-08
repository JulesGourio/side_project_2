# Databricks notebook source
# MAGIC %md
# MAGIC # Qualibot — replay questions on several chat configurations, side by side
# MAGIC
# MAGIC For judging by eye instead of running the whole golden evaluation: pick a few questions
# MAGIC (golden cases, filtered by text, and/or your own question), pick configurations, Run all.
# MAGIC Each configuration answers every picked question **in parallel**, through the deployed
# MAGIC app's own chat turn (same steps as `golden_eval_ka_vs_vsi.py`). Answers already saved by
# MAGIC the golden evaluation can be shown too, without re-running them (`stored_evals`).
# MAGIC
# MAGIC The last cell renders one card per question: the expected facts / documents, then one
# MAGIC column per configuration with the answer, the documents it cites (green = expected,
# MAGIC grey = other, red = expected but missing), the time and the cost. Nothing is saved.
# MAGIC
# MAGIC Before running: deploy the branch to DEV (`deploy_qualibot.ps1 -AppEnv dev`).

# COMMAND ----------

dbutils.widgets.text('app_code_path', '/Workspace/Shared/.bundle/qualibot/dev/files')
APP = dbutils.widgets.get('app_code_path').strip().rstrip('/')

# COMMAND ----------

# MAGIC %pip install -q -r $APP/requirements.txt fastapi pyyaml markdown "databricks-sdk>=0.102"
# MAGIC dbutils.library.restartPython()

# COMMAND ----------

# DBTITLE 1,Configurations you can pick (widget `configs`)
UNION_CTX = {'CHAT_VSI_RERANK_MERGE': 'union', 'CHAT_VSI_RERANK_COLUMNS': 'REF,semantic_headers,chunk_text'}
S55 = {'CHAT_VSI_LLM_ENDPOINT': 'databricks-claude-sonnet-5-5',
       'CHAT_VSI_ANSWER_MAX_TOKENS': '16000', 'CHAT_VSI_REWRITE_MAX_TOKENS': '4000'}
CONFIGS = {
    'ka':                dict(engine='ka'),
    'baseline':          dict(engine='vsi', variant='baseline'),
    'union-ctx':         dict(engine='vsi', variant='rerank', env=UNION_CTX),                      # Sonnet 4.6
    'union-ctx-s55':     dict(engine='vsi', variant='rerank', env={**UNION_CTX, **S55}),
    'union-ctx-s55-ref': dict(engine='vsi', variant='rerank', env={**UNION_CTX, **S55, 'CHAT_VSI_REF_LOOKUP': 'on'}),
    'union-ctx-budget':  dict(engine='vsi', variant='rerank',
                              env={**UNION_CTX, 'CHAT_VSI_RERANK_TOP_K': '25', 'CHAT_VSI_CONTEXT_BUDGET_CHARS': '35000'}),
}

# COMMAND ----------

# DBTITLE 1,Parameters
dbutils.widgets.text('app_code_path', '/Workspace/Shared/.bundle/qualibot/dev/files')
dbutils.widgets.text('configs', 'ka,union-ctx,union-ctx-s55')            # names from CONFIGS, run now
dbutils.widgets.text('stored_evals', '')                                  # eval_ids already saved, shown as-is
dbutils.widgets.text('question_filter', 'APO|CMP|slide 15|OPEX')          # parts of golden questions, '|'-separated; empty = all
dbutils.widgets.text('extra_question', '')                                # your own question (single turn)
dbutils.widgets.dropdown('division', 'ALL', ['ALL', 'AS', 'IS'])
dbutils.widgets.text('max_parallel', '4')
dbutils.widgets.text('golden_table', 'dev_landingzone.qualibot.qualibot_eval_golden')
dbutils.widgets.text('results_table', 'dev_landingzone.qualibot.eval_golden_runs')

APP = dbutils.widgets.get('app_code_path').strip().rstrip('/')
RUN = [c.strip() for c in dbutils.widgets.get('configs').split(',') if c.strip()]
STORED = [c.strip() for c in dbutils.widgets.get('stored_evals').split(',') if c.strip()]
FILTERS = [f.strip().lower() for f in dbutils.widgets.get('question_filter').split('|') if f.strip()]
EXTRA = dbutils.widgets.get('extra_question').strip()
DIVISION = dbutils.widgets.get('division')
MAX_PARALLEL = max(1, int(dbutils.widgets.get('max_parallel') or 4))
GOLDEN = dbutils.widgets.get('golden_table').strip()
RESULTS = dbutils.widgets.get('results_table').strip()
unknown = [c for c in RUN if c not in CONFIGS]
assert not unknown, f'unknown configs {unknown} — pick from {list(CONFIGS)}'

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
    """CHAT_VSI_* settings are read at call time, so one configuration at a time (its
    questions run in parallel)."""
    for k in [k for k in os.environ if k.startswith('CHAT_VSI_')]:
        del os.environ[k]
    os.environ.update(APP_VSI_ENV)
    os.environ['CHAT_VSI_VARIANT'] = cfg.get('variant', 'baseline')
    os.environ.update({k: str(v) for k, v in (cfg.get('env') or {}).items()})

# COMMAND ----------

# DBTITLE 1,App code — one chat turn, the same steps as _run_chat_ws
import asyncio, json, time
from concurrent.futures import ThreadPoolExecutor

from databricks.sdk import WorkspaceClient

from server.routers.chat import (  # app code, unchanged
    TRANSLATE_BRIDGE_ENABLED, _apply_citation_markers, _endpoint_for_division, _number_sources,
    _trim_history, _with_today_date,
)
from server.services.chat_vsi import llm_endpoint
from server.services.chat_vsi_variants import stream_chat_vsi
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
KA_ENDPOINT = _endpoint_for_division(DIVISION)


def token() -> str:
    return w.config.authenticate()['Authorization'].split(' ', 1)[1]


async def chat_turn(engine: str, messages: list) -> dict:
    started = time.monotonic()
    host, tok = HOST, token()
    messages = _trim_history([{'role': m['role'], 'content': m['content']} for m in messages])
    user_content = next((m['content'] for m in reversed(messages) if m['role'] == 'user'), '')
    translate_ctx, for_engine = None, messages
    if TRANSLATE_BRIDGE_ENABLED and user_content:
        en_question, translate_ctx = await translate_question_to_en(user_content, host, tok)
        if translate_ctx.needs_translation:
            for_engine = [dict(m) for m in messages]
            for i in range(len(for_engine) - 1, -1, -1):
                if for_engine[i]['role'] == 'user':
                    for_engine[i]['content'] = en_question
                    break
    stream = (stream_chat_vsi(host, tok, DIVISION, _with_today_date(for_engine)) if engine == 'vsi'
              else stream_chat(host, tok, KA_ENDPOINT, _with_today_date(for_engine)))
    text, sources, citations, first, meta = [], [], [], None, {}
    async for chunk in stream:
        if not chunk.startswith('data: '):
            continue
        raw = chunk[6:].strip()
        if raw == '[DONE]':
            break
        ev = json.loads(raw)
        kind = ev.get('type')
        if kind == 'response.output_text.delta':
            first = first if first is not None else time.monotonic() - started
            text.append(ev.get('delta', ''))
        elif kind == 'sources':
            sources, citations = ev.get('sources') or sources, ev.get('citations') or citations
        elif kind == 'metadata':
            meta = ev
        elif kind == 'error':
            raise RuntimeError(ev.get('error'))
    if not text:
        raise RuntimeError('No answer was produced.')
    clean = ''.join(text)
    final = _apply_citation_markers(clean, citations)
    sources = augment_sources(clean, sources)
    _number_sources(sources, citations)
    if translate_ctx:
        final = await translate_answer_back(final, translate_ctx, host, tok)
    return {'answer': final, 'refs': sorted({canon_ref(s['title']) for s in sources if s.get('title')}),
            'latency_s': round(time.monotonic() - started, 1),
            'first_token_s': round(first, 1) if first is not None else None,
            'cost_eur': (meta.get('usage') or {}).get('cost_eur'), 'search': meta.get('tool_name') or ''}


def answer(engine: str, messages: list) -> dict:
    try:
        return asyncio.run(chat_turn(engine, messages))
    except Exception as exc:  # shown in the card, the other answers still render
        return {'error': f'{type(exc).__name__}: {exc}'}

# COMMAND ----------

# DBTITLE 1,Questions
_golden = [{'id': r['dataset_record_id'], 'messages': json.loads(r['inputs'])['messages'],
            'expectations': json.loads(r['expectations'])}
           for r in spark.table(GOLDEN).select('dataset_record_id', 'inputs', 'expectations').collect()]
QUESTIONS = [g for g in _golden
             if not FILTERS or any(f in g['messages'][-1]['content'].lower() for f in FILTERS)]
if EXTRA:
    QUESTIONS.append({'id': 'extra', 'messages': [{'role': 'user', 'content': EXTRA}], 'expectations': {}})
print(len(QUESTIONS), 'questions:')
for q in QUESTIONS:
    print(' -', q['messages'][-1]['content'][:100])

# COMMAND ----------

# DBTITLE 1,Run — one configuration after the other, its questions in parallel
RESULTS_BY = {}          # (question id, column name) -> result
for name in RUN:
    cfg = CONFIGS[name]
    apply_config_env(cfg)
    workers = min(MAX_PARALLEL, 2) if cfg['engine'] == 'ka' else MAX_PARALLEL   # KA is rate-limited
    t0 = time.monotonic()
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {q['id']: pool.submit(answer, cfg['engine'], q['messages']) for q in QUESTIONS}
        for qid, fut in futures.items():
            RESULTS_BY[(qid, name)] = fut.result()
    model = KA_ENDPOINT if cfg['engine'] == 'ka' else llm_endpoint()
    print(f'{name:22} {model:32} {len(QUESTIONS)} questions in {time.monotonic() - t0:.0f} s')

if STORED:
    by_q = {q['messages'][-1]['content']: q['id'] for q in QUESTIONS}
    for eval_id in STORED:
        rows = spark.sql(f"""
            SELECT question, answer, answer_refs, latency_s, first_token_s, cost_eur, search FROM {RESULTS} r
            WHERE eval_id = '{eval_id}' AND attempt_ts = (SELECT max(attempt_ts) FROM {RESULTS} WHERE eval_id = '{eval_id}')
        """).collect()
        for r in rows:
            if r['question'] in by_q:
                RESULTS_BY[(by_q[r['question']], f'{eval_id} (saved)')] = {
                    'answer': r['answer'], 'refs': list(r['answer_refs'] or []), 'latency_s': r['latency_s'],
                    'first_token_s': r['first_token_s'], 'cost_eur': r['cost_eur'], 'search': r['search'] or ''}
COLUMNS = RUN + [f'{e} (saved)' for e in STORED]

# COMMAND ----------

# DBTITLE 1,Side-by-side view
import html
import markdown as md


def _answer_html(text: str) -> str:
    text = re.sub(r'⟦(\d+)⟧', r'<sup class="cite">[\1]</sup>', text or '')
    return md.markdown(text, extensions=['tables'])


def _chips(refs, golden):
    refs = set(refs or [])
    out = [f'<span class="chip {"ok" if r in golden else "other"}">{html.escape(r)}</span>' for r in sorted(refs)]
    out += [f'<span class="chip miss" title="expected, not cited">{html.escape(r)}</span>' for r in sorted(golden - refs)]
    return ''.join(out) or '<span class="muted">no document</span>'


CSS = """<style>
.rc{font-family:system-ui,sans-serif;color:#1f2328}
.rc .card{border:1px solid #d0d7de;border-radius:8px;margin:0 0 22px;padding:14px;background:#fff}
.rc .q{font-size:16px;font-weight:600;margin-bottom:6px}
.rc .hist{font-size:12px;color:#57606a;margin-bottom:6px;white-space:pre-wrap}
.rc .exp{background:#f6f8fa;border-radius:6px;padding:8px 10px;font-size:13px;margin-bottom:10px}
.rc .cols{display:grid;grid-template-columns:repeat(var(--n),minmax(280px,1fr));gap:10px;overflow-x:auto}
.rc .col{border:1px solid #eaeef2;border-radius:6px;padding:10px;font-size:13px;max-height:560px;overflow:auto}
.rc .col h4{margin:0 0 6px;font-size:13px;display:flex;justify-content:space-between;gap:8px}
.rc .meta{font-weight:400;color:#57606a;font-size:12px}
.rc .chip{display:inline-block;border-radius:10px;padding:1px 7px;margin:2px 3px 2px 0;font-size:11px}
.rc .chip.ok{background:#dafbe1;color:#116329}.rc .chip.other{background:#eaeef2;color:#424a53}
.rc .chip.miss{background:#ffebe9;color:#a40e26;text-decoration:line-through}
.rc .err{color:#a40e26}.rc .muted{color:#8c959f}.rc sup.cite{color:#0969da;font-size:10px}
.rc table{border-collapse:collapse;font-size:12px}.rc td,.rc th{border:1px solid #d0d7de;padding:3px 5px}
</style>"""

cards = []
for q in QUESTIONS:
    exp = q['expectations']
    golden = {canon_ref(d['doc_uri']) for d in exp.get('expected_retrieved_context') or [] if d.get('doc_uri')}
    history = '\n'.join(f"{m['role']}: {m['content'][:300]}" for m in q['messages'][:-1])
    expected = ''.join(f'<li>{html.escape(f)}</li>' for f in exp.get('expected_facts') or [])
    if exp.get('expected_response'):
        expected += f"<li><i>expected answer:</i> {html.escape(exp['expected_response'])}</li>"
    cols = []
    for name in COLUMNS:
        r = RESULTS_BY.get((q['id'], name))
        if r is None:
            cols.append(f'<div class="col"><h4>{html.escape(name)}</h4><span class="muted">not available</span></div>')
            continue
        if 'error' in r:
            cols.append(f'<div class="col"><h4>{html.escape(name)}</h4><div class="err">{html.escape(r["error"])}</div></div>')
            continue
        cost = f" · {r['cost_eur']:.3f} €" if r.get('cost_eur') is not None else ''
        meta = f"{r.get('latency_s')} s (1st word {r.get('first_token_s')} s){cost}"
        cols.append(f'<div class="col"><h4>{html.escape(name)} <span class="meta">{meta}</span></h4>'
                    f'<div>{_chips(r.get("refs"), golden)}</div>{_answer_html(r.get("answer"))}'
                    f'<div class="meta">{html.escape(r.get("search") or "")}</div></div>')
    cards.append(f'<div class="card"><div class="q">{html.escape(q["messages"][-1]["content"])}</div>'
                 + (f'<div class="hist">{html.escape(history)}</div>' if history else '')
                 + (f'<div class="exp"><b>Expected</b><ul>{expected}</ul>{"Documents: " + ", ".join(sorted(golden)) if golden else ""}</div>'
                    if expected or golden else '')
                 + f'<div class="cols" style="--n:{len(COLUMNS)}">{"".join(cols)}</div></div>')

displayHTML(CSS + '<div class="rc">' + ''.join(cards) + '</div>')
