# Databricks notebook source
# MAGIC %md
# MAGIC # DEV — does `SF-1027` come back from Vector Search? (read only)
# MAGIC
# MAGIC Rights: your own are enough (SELECT on `dev_proj.qualibot.chunks`, query on `chunks_index`). No job runner, nothing written.

# COMMAND ----------

# MAGIC %pip install -q httpx "databricks-sdk>=0.102"
# MAGIC dbutils.library.restartPython()

# COMMAND ----------

import json
import re
from concurrent.futures import ThreadPoolExecutor

import httpx
from databricks.sdk import WorkspaceClient
from pyspark.sql import functions as F

INDEXES = ['dev_proj.qualibot.chunks_index', 'dev_proj.qualibot.chunks_text_index']   # ignored when absent
CHUNKS = 'dev_proj.qualibot.chunks'
CODE_REGEX = r'(?i)SF[\s\-‐‑‒–—_]?1027'     # every spelling of the code
VARIANTS = ['SF-1027 [SF1027]',      # what the app sends after its query normalization
            'SF-1027', 'SF 1027', 'SF1027', 'sf-1027', "c'est quoi SF-1027 ?", 'Dans quel document se trouve SF-1027 ?']
METHODS = ['ANN', 'HYBRID', 'HYBRID+RERANK', 'FULL_TEXT']              # HYBRID+RERANK = what the chat sends
TOP = 50
COLUMNS = ['chunk_id', 'IDDOC', 'REF', 'division', 'semantic_headers', 'chunk_text']

w = WorkspaceClient()
HOST = w.config.host.rstrip('/')


def headers() -> dict:
    return {**w.config.authenticate(), 'Content-Type': 'application/json'}


def query(payload: dict, index: str) -> list:
    """One Vector Search query; returns rows as dicts, raises with the server message on refusal."""
    r = httpx.post(f'{HOST}/api/2.0/vector-search/indexes/{index}/query', headers=headers(), json=payload, timeout=60)
    if r.status_code != 200:
        raise RuntimeError(f'{r.status_code} {r.text[:200]}')
    d = r.json()
    cols = [c['name'] for c in d['manifest']['columns']]
    return [dict(zip(cols, row)) for row in d['result']['data_array']]

# COMMAND ----------

# 1. Target chunks: every chunk of the table that cites the code, whatever the spelling.
# (Column.rlike: a SQL string literal would eat the backslashes of the regex.)
hit = spark.table(CHUNKS).filter(F.col('chunk_text').rlike(CODE_REGEX))
rows = [r.asDict() for r in hit.select('chunk_id', 'IDDOC', 'REF').collect()]
TARGETS = {r['chunk_id']: r for r in rows}
print(f'{len(TARGETS)} target chunks in the table:')
for r in rows:
    print('  ', r['chunk_id'], r['REF'], r['IDDOC'])
assert TARGETS, 'No chunk cites the code: nothing to find (parsing problem, not search).'

# Does the table carry the preprocessed text, and what do the indexes embed?
cols = spark.table(CHUNKS).columns
print('chunk_preprocessed column in chunks:', 'chunk_preprocessed' in cols)
if 'chunk_preprocessed' in cols:
    for r in hit.select('chunk_id', 'chunk_preprocessed').limit(3).collect():
        m = re.search(r'.{0,40}SF.{0,3}1027.{0,60}', r['chunk_preprocessed'] or '', re.S)
        print('  ', r['chunk_id'], repr(m.group(0)) if m else 'canonical form ABSENT:', (r['chunk_preprocessed'] or '')[:80] if not m else '')
    n_null = spark.table(CHUNKS).filter(F.col('chunk_preprocessed').isNull()).count()
    print('chunks with null chunk_preprocessed:', n_null)
LIVE = []
for ix in INDEXES:
    r = httpx.get(f'{HOST}/api/2.0/vector-search/indexes/{ix}', headers=headers(), timeout=30)
    if r.status_code != 200:
        print(ix, '-> absent')
        continue
    d = r.json()
    emb = [c['name'] for c in d.get('delta_sync_index_spec', {}).get('embedding_source_columns', [])]
    print(ix, '-> embeds', emb, '| status', d.get('status', {}).get('detailed_state'), '| rows', d.get('status', {}).get('indexed_row_count'))
    LIVE.append(ix)

# COMMAND ----------

# 2. Are the targets in the index? Read each one back by filter on chunk_id (any method works with a filter).
indexed = {}
for cid in TARGETS:
    try:
        got = query({'query_text': 'SF-1027', 'columns': COLUMNS, 'num_results': 1, 'query_type': 'ANN',
                     'filters_json': json.dumps({'chunk_id': [cid]})}, INDEXES[0])
        indexed[cid] = bool(got)
    except Exception as exc:
        indexed[cid] = f'error: {exc}'
print('In the index:', sum(v is True for v in indexed.values()), '/', len(TARGETS))
for cid, v in indexed.items():
    if v is not True:
        print('  NOT in the index:', cid, v)

# COMMAND ----------

# 3. Every variant x every method: rank of the targets among the first TOP results.
def payload(method: str, text: str) -> dict:
    p = {'query_text': text, 'columns': COLUMNS, 'num_results': TOP,
         'query_type': 'HYBRID' if method.startswith('HYBRID') else method}
    if method == 'HYBRID+RERANK':
        p['num_results'] = 12                  # the chat reranks 12
        p['reranker'] = {'model': 'databricks_reranker', 'parameters': {'columns_to_rerank': ['REF', 'semantic_headers', 'chunk_text']}}
    return p


def run(job):
    ix, method, text = job
    try:
        res = query(payload(method, text), ix)
    except Exception as exc:
        return ix, method, text, None, str(exc)
    return ix, method, text, res, None


jobs = [(ix, m, v) for ix in LIVE for m in METHODS for v in VARIANTS]
with ThreadPoolExecutor(max_workers=4) as ex:
    results = list(ex.map(run, jobs))

summary = []
for ix, method, text, res, err in results:
    name = ix.split('.')[-1]
    if err:
        summary.append((name, method, text, 'refused', err[:80]))
        continue
    ranks = [i + 1 for i, r in enumerate(res) if r['chunk_id'] in TARGETS]
    summary.append((name, method, text, ranks[0] if ranks else f'> {len(res)}', f'{len(ranks)}/{len(TARGETS)} targets'))

print(f"{'index':<20}{'method':<14}{'query':<42}{'first rank':<12}found")
for s in summary:
    print(f'{s[0]:<20}{s[1]:<14}{s[2]:<42}{str(s[3]):<12}{s[4]}')

# COMMAND ----------

# 4. What comes back instead (HYBRID+RERANK, the chat's own query on the plain code): REF and first 90 characters.
for ix, method, text, res, err in results:
    if method == 'HYBRID+RERANK' and text in ('SF-1027', "c'est quoi SF-1027 ?") and res:
        print(f'--- {ix.split(".")[-1]} | {method} | {text}')
        for i, r in enumerate(res, 1):
            flag = 'TARGET' if r['chunk_id'] in TARGETS else '      '
            print(f"{i:>2} {flag} {r['REF']:<16}{re.sub(chr(10), ' ', r['chunk_text'])[:90]}")

# COMMAND ----------

# 5. Verdict.
def first(index, method, text):
    for r in summary:
        if (r[0], r[1], r[2]) == (index, method, text):
            return r[3]

print('Targets in the first index :', all(v is True for v in indexed.values()))
for ix in LIVE:
    n = ix.split('.')[-1]
    print(f'{n}: FULL_TEXT SF-1027 / SF1027 =', first(n, 'FULL_TEXT', 'SF-1027'), '/', first(n, 'FULL_TEXT', 'SF1027'),
          '| HYBRID normalized =', first(n, 'HYBRID', 'SF-1027 [SF1027]'),
          '| chat (RERANK) question =', first(n, 'HYBRID+RERANK', "c'est quoi SF-1027 ?"))
