"""Throwaway test (not part of the repo): answer a Chat question WITHOUT the KA.

Vector Search HYBRID on the division index -> claude-sonnet-4-6 with the live KA
instructions -> [n] markers -> the app's own post-processing (chat.py, doc_catalog.py).
"""
import json, os, re, subprocess, sys, time
import httpx

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))   # repo root
from server.routers.chat import _apply_citation_markers, _number_sources, _with_today_date  # app code, unchanged
from server.services.doc_catalog import augment_sources                                      # app code, unchanged

HOST = 'https://dbc-c623749d-731b.cloud.databricks.com'
TOKEN = json.loads(subprocess.run(['databricks', 'auth', 'token', '-p', 'latecoere', '-o', 'json'],
                                  capture_output=True, text=True).stdout)['access_token']
H = {'Authorization': f'Bearer {TOKEN}', 'Content-Type': 'application/json'}

QUESTION = 'What documents reference the NDT/NDI qualification requirements?'
DIVISION = 'ALL'
INDEX = {'ALL': 'dev_landingzone.qualibot.chunks_index_v1',
         'AS': 'dev_landingzone.qualibot.chunks_as_index_v1',
         'IS': 'dev_landingzone.qualibot.chunks_is_index_v1'}[DIVISION]
NUM_PASSAGES = 10          # what the KA passed to generation in the captured trace
LLM = 'databricks-claude-sonnet-4-6'
INSTRUCTIONS = json.loads(subprocess.run(                                              # live KA ALL instructions
    ['databricks', 'knowledge-assistants', 'get-knowledge-assistant',
     'knowledge-assistants/4d15cb32-1edb-4f86-aa7c-e6c2e91a9002', '-p', 'latecoere', '-o', 'json'],
    capture_output=True, text=True, check=True).stdout)['instructions']

CITATION_RULE = """
# How to cite (mandatory)
You are given numbered passages. Cite inline: put the passage number in square brackets at the
end of the sentence or list item it supports, before the line break, e.g. "... EN4179. [3]" or
"... [2][5]". Never put markers on a line of their own, after a heading, or under a table.
Use only the passage numbers given. Do not invent passages. Do not write URLs."""

SEARCH_QUERY_PROMPT = """Rewrite the user's last question as one standalone search query in French,
for a search engine over a mostly French document base (Latécoère quality documents).
Keep document codes, acronyms and technical terms (e.g. NDT, NDI, CND, EN4179) as they are.
Return only the query, nothing else."""

# 0. Search query in French — the one measured gap (KA instruction "Search language")
t0 = time.time()
r = httpx.post(f'{HOST}/serving-endpoints/{LLM}/invocations', headers=H, timeout=60, json={
    'messages': [{'role': 'system', 'content': SEARCH_QUERY_PROMPT}, {'role': 'user', 'content': QUESTION}],
    'max_tokens': 120, 'temperature': 0.0})
r.raise_for_status()
SEARCH_QUERY = r.json()['choices'][0]['message']['content'].strip()
t_rw = time.time() - t0

# 1. Retrieval — Vector Search HYBRID (same call shape as vector_search._fetch_chunks)
t0 = time.time()
r = httpx.post(f'{HOST}/api/2.0/vector-search/indexes/{INDEX}/query', headers=H, timeout=30, json={
    'query_text': SEARCH_QUERY, 'num_results': NUM_PASSAGES, 'query_type': 'HYBRID',
    'columns': ['chunk_id', 'REF', 'division', 'doc_date', 'url', 'chunk_text']})
r.raise_for_status()
cols = [c['name'] for c in r.json()['manifest']['columns']]
rows = [dict(zip(cols, x)) for x in r.json()['result']['data_array']]
t_vs = time.time() - t0

# 2. Prompt — KA instructions + numbered passages + the question as the app sends it
passages = '\n\n'.join(f'[{i}] {row["chunk_text"]}' for i, row in enumerate(rows, 1))
messages = _with_today_date([{'role': 'user', 'content': QUESTION}])
messages[-1]['content'] = f'Passages:\n\n{passages}\n\n---\n\n{messages[-1]["content"]}'

# 3. Generation — streamed, to measure time to first token
t0 = time.time(); ttft = None; raw = ''
with httpx.stream('POST', f'{HOST}/serving-endpoints/{LLM}/invocations', headers=H, timeout=180, json={
        'messages': [{'role': 'system', 'content': INSTRUCTIONS + '\n' + CITATION_RULE}] + messages,
        'max_tokens': 2000, 'temperature': 0.0, 'stream': True}) as resp:
    resp.raise_for_status()
    for line in resp.iter_lines():
        if not line.startswith('data: ') or line[6:].strip() == '[DONE]':
            continue
        chunk = json.loads(line[6:])
        for ch in chunk.get('choices', []):
            delta = (ch.get('delta') or {}).get('content') or ''
            if delta and ttft is None:
                ttft = time.time() - t0
            raw += delta
t_llm = time.time() - t0

# 4. [n] markers -> clean text + sources/citations in the app's internal format
sources, citations, key_to_n, clean = [], [], {}, ''
pos = 0
for m in re.finditer(r'\[(\d+)\]', raw):
    clean += raw[pos:m.start()]
    pos = m.end()
    i = int(m.group(1))
    if not 1 <= i <= len(rows):
        continue
    row = rows[i - 1]
    if row['url'] not in key_to_n:
        sources.append({'title': row['REF'], 'url': row['url'], 'doc_uri': row['url']})
        key_to_n[row['url']] = len(sources)
    citations.append({'n': key_to_n[row['url']], 'pos': len(clean)})
clean += raw[pos:]

# 5. App post-processing, unchanged (chat.py / doc_catalog.py)
final = _apply_citation_markers(clean, citations)
sources = augment_sources(clean, sources)
_number_sources(sources, citations)

print(f'Search query (FR): {SEARCH_QUERY}')
print(f'Index: {INDEX} | passages: {len(rows)} | rewrite {t_rw:.1f}s | VS {t_vs:.1f}s | '
      f'LLM TTFT {(ttft or 0):.1f}s, total {t_llm:.1f}s | first token after {t_rw + t_vs + (ttft or 0):.1f}s')
print('Retrieved REFs:', [row['REF'] for row in rows])
print('\n' + '=' * 70 + '\n')
print(final.replace('⟦', '[').replace('⟧', ']'))
print('\nSources :')
for s in sorted(sources, key=lambda s: (s.get('n') is None, s.get('n') or 0)):
    print(f"  {s['n'] if s.get('n') else '-'}  {s['title']}")
