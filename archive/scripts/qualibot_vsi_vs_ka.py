# Databricks notebook source
# MAGIC %md
# MAGIC # Qualibot — VSI v0 vs KA (reference), 5 prefilled questions
# MAGIC
# MAGIC - **KA reference**: the app's own `stream_chat()` against `qualibot_ALL_v2`, then the app's post-processing.
# MAGIC - **VSI v0**: Vector Search HYBRID on the same index (question as-is + French version, merged),
# MAGIC   documents numbered in the prompt, live KA ALL instructions, `databricks-claude-sonnet-4-6`,
# MAGIC   then the same app post-processing.
# MAGIC - **Scorers**: built-in `Correctness` (expected response = KA answer) and `ka_doc_recall`
# MAGIC   (share of the KA's documents also found by VSI).
# MAGIC
# MAGIC App code is imported unchanged from `/Workspace/Shared/qualibot-custom`.

# COMMAND ----------

# MAGIC %pip install -q -r /Workspace/Shared/qualibot-custom/requirements.txt fastapi python-dotenv "mlflow[databricks]>=3.6" "databricks-sdk>=0.102"
# MAGIC dbutils.library.restartPython()

# COMMAND ----------

import asyncio, json, re, sys, threading
sys.path.insert(0, '/Workspace/Shared/qualibot-custom')

import httpx, mlflow
from databricks.sdk import WorkspaceClient
from mlflow.genai.scorers import Correctness, scorer

from server.services.streaming import stream_chat                                      # app code, unchanged
from server.routers.chat import _apply_citation_markers, _number_sources, _with_today_date  # app code, unchanged
from server.services.doc_catalog import augment_sources, canon_ref                       # app code, unchanged

w = WorkspaceClient()
HOST = w.config.host.rstrip('/')
def token() -> str:
    return w.config.authenticate()['Authorization'].split(' ', 1)[1]

EXPERIMENT = '/Users/mehdi.lamrani@databricks.com/qualibot-vsi-vs-ka'
KA_ID = '4d15cb32-1edb-4f86-aa7c-e6c2e91a9002'          # qualibot_ALL_v2
KA_ENDPOINT = 'ka-4d15cb32-endpoint'
INDEX = 'dev_landingzone.qualibot.chunks_index_v1'       # the KA's own knowledge source
LLM = 'databricks-claude-sonnet-4-6'
K = 10                                                   # passages per query (KA passed 10 to generation)

QUESTIONS = [                                            # the 5 questions prefilled in the project
    'What documents reference the NDT/NDI qualification requirements?',
    'Which procedures must be updated when a supplier changes their process?',
    'List the key quality standards applicable to composite part manufacturing.',
    'What is the approval process for deviations from engineering specifications?',
    'comment dois-je faire pour réparer une carte relais sur un testeur électrique LATE4000 ?',
]

KA_INSTRUCTIONS = w.knowledge_assistants.get_knowledge_assistant(name=f'knowledge-assistants/{KA_ID}').instructions
print('KA instructions:', len(KA_INSTRUCTIONS), 'chars')

def run_async(coro):
    """Run a coroutine from sync code, even inside the notebook's running event loop."""
    out = {}
    t = threading.Thread(target=lambda: out.setdefault('v', asyncio.run(coro)))
    t.start(); t.join()
    return out['v']

def post_process(clean: str, sources: list, citations: list):
    """Exactly what chat_ws does after the stream (chat.py)."""
    final = _apply_citation_markers(clean, citations)
    sources = augment_sources(clean, sources)
    _number_sources(sources, citations)
    return final, sources

def refs_of(sources: list) -> list:
    return sorted({canon_ref(s['title']) for s in sources if s.get('title')})

# COMMAND ----------

# DBTITLE 1,KA reference — the app's own path
async def _ka(question: str):
    text, sources, citations = [], [], []
    async for chunk in stream_chat(HOST, token(), KA_ENDPOINT, _with_today_date([{'role': 'user', 'content': question}])):
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
    return post_process(''.join(text), sources, citations)

@mlflow.trace(name='ka_reference')
def ka_predict(question: str) -> dict:
    final, sources = run_async(_ka(question))
    return {'response': final, 'refs': refs_of(sources)}

# COMMAND ----------

# DBTITLE 1,VSI v0 — Vector Search + LLM, no KA
REWRITE_PROMPT = """Rewrite the user's question as one standalone search query in French, for a search
engine over a mostly French document base (Latécoère quality documents). Keep document codes,
acronyms and technical terms as they are. Return only the query."""

CITATION_RULE = """
# How to cite (mandatory)
You are given numbered documents, each with one or more passages. Cite inline: put the document
number in square brackets at the end of the sentence or list item it supports, before the line
break, e.g. "... EN4179. [3]". Never put markers on a line of their own, after a heading, or under
a table. Use only the document numbers given. Do not write URLs."""

def llm(messages: list, max_tokens: int) -> str:
    r = httpx.post(f'{HOST}/serving-endpoints/{LLM}/invocations', timeout=180,
                   headers={'Authorization': f'Bearer {token()}'},
                   json={'messages': messages, 'max_tokens': max_tokens, 'temperature': 0.0})
    r.raise_for_status()
    return r.json()['choices'][0]['message']['content']

@mlflow.trace(span_type='RETRIEVER')
def search(query: str) -> list:
    r = httpx.post(f'{HOST}/api/2.0/vector-search/indexes/{INDEX}/query', timeout=30,
                   headers={'Authorization': f'Bearer {token()}'},
                   json={'query_text': query, 'num_results': K, 'query_type': 'HYBRID',
                         'columns': ['chunk_id', 'REF', 'url', 'chunk_text']})
    r.raise_for_status()
    cols = [c['name'] for c in r.json()['manifest']['columns']]
    return [dict(zip(cols, row)) for row in r.json()['result']['data_array']]

@mlflow.trace(name='vsi_v0')
def vsi_predict(question: str) -> dict:
    fr_query = llm([{'role': 'system', 'content': REWRITE_PROMPT}, {'role': 'user', 'content': question}], 120).strip()
    # Merge both result lists by best rank, dedup by chunk_id
    ranked = {}
    for hits in (search(question), search(fr_query)):
        for rank, row in enumerate(hits):
            if row['chunk_id'] not in ranked or rank < ranked[row['chunk_id']][0]:
                ranked[row['chunk_id']] = (rank, row)
    rows = [row for _, row in sorted(ranked.values(), key=lambda x: x[0])]
    # Number documents (not passages): the unit the UI numbers
    docs = {}
    for row in rows:
        docs.setdefault(row['REF'], {'url': row['url'], 'passages': []})['passages'].append(row['chunk_text'])
    doc_list = list(docs.items())
    context = '\n\n'.join(f'[{i}] Document {ref}\n' + '\n\n'.join(d['passages']) for i, (ref, d) in enumerate(doc_list, 1))
    messages = _with_today_date([{'role': 'user', 'content': question}])
    messages[-1]['content'] = f'Documents:\n\n{context}\n\n---\n\n{messages[-1]["content"]}'
    raw = llm([{'role': 'system', 'content': KA_INSTRUCTIONS + '\n' + CITATION_RULE}] + messages, 2000)
    # [n] markers -> clean text + the app's internal sources/citations format
    sources, citations, n_of, clean, pos = [], [], {}, '', 0
    for m in re.finditer(r'\[(\d+)\]', raw):
        clean += raw[pos:m.start()]; pos = m.end()
        i = int(m.group(1))
        if not 1 <= i <= len(doc_list):
            continue
        ref, d = doc_list[i - 1]
        if ref not in n_of:
            sources.append({'title': ref, 'url': d['url'], 'doc_uri': d['url']})
            n_of[ref] = len(sources)
        citations.append({'n': n_of[ref], 'pos': len(clean)})
    clean += raw[pos:]
    final, sources = post_process(clean, sources, citations)
    return {'response': final, 'refs': refs_of(sources), 'search_query_fr': fr_query}

# COMMAND ----------

# DBTITLE 1,Scorers
@scorer
def ka_doc_recall(outputs, expectations) -> float:
    """Share of the documents the KA referenced that VSI also referenced (canonical REFs)."""
    ka = set(expectations.get('ka_refs') or [])
    return round(len(ka & set(outputs.get('refs') or [])) / len(ka), 3) if ka else 1.0

@scorer
def num_documents(outputs) -> int:
    return len(outputs.get('refs') or [])

# COMMAND ----------

# DBTITLE 1,Run 1 — KA reference
mlflow.set_experiment(EXPERIMENT)
ka_answers = {}
def ka_predict_and_keep(question: str) -> dict:
    out = ka_predict(question)
    ka_answers[question] = out
    return out

ka_run = mlflow.genai.evaluate(data=[{'inputs': {'question': q}} for q in QUESTIONS],
                               predict_fn=ka_predict_and_keep, scorers=[num_documents])
print('KA run:', ka_run.run_id)

# COMMAND ----------

# DBTITLE 1,Run 2 — VSI v0, scored against the KA
data = [{'inputs': {'question': q},
         'expectations': {'expected_response': ka_answers[q]['response'], 'ka_refs': ka_answers[q]['refs']}}
        for q in QUESTIONS]
vsi_answers = {}
def vsi_predict_and_keep(question: str) -> dict:
    out = vsi_predict(question)
    vsi_answers[question] = out
    return out

vsi_run = mlflow.genai.evaluate(data=data, predict_fn=vsi_predict_and_keep,
                                scorers=[Correctness(), ka_doc_recall, num_documents])
print('VSI run:', vsi_run.run_id, vsi_run.metrics)

# COMMAND ----------

# DBTITLE 1,Summary
rows = []
traces = mlflow.search_traces(run_id=vsi_run.run_id, return_type='list')
correctness = {}
for t in traces:
    q = json.loads(t.data.request).get('question') if t.data.request else None
    for a in (t.info.assessments or []):
        if a.name == 'correctness':
            correctness[q] = {'value': str(a.value), 'rationale': (a.rationale or '')[:400]}
for q in QUESTIONS:
    ka_refs, vsi_refs = ka_answers[q]['refs'], vsi_answers[q]['refs']
    rows.append({'question': q, 'ka_refs': ka_refs, 'vsi_refs': vsi_refs,
                 'recall': round(len(set(ka_refs) & set(vsi_refs)) / len(ka_refs), 3) if ka_refs else None,
                 'search_query_fr': vsi_answers[q]['search_query_fr'],
                 'correctness': correctness.get(q)})
summary = {'experiment': EXPERIMENT, 'ka_run': ka_run.run_id, 'vsi_run': vsi_run.run_id,
           'vsi_metrics': vsi_run.metrics, 'rows': rows}
print(json.dumps(summary, ensure_ascii=False, indent=1))
dbutils.notebook.exit(json.dumps(summary, ensure_ascii=False))
