# Databricks notebook source
# MAGIC %md
# MAGIC # DEV — why is the SF-1027 chunk not retrieved? (read only)
# MAGIC Rights: your own (SELECT on `dev_proj.qualibot.chunks`, query on the two indexes). Nothing is written.
# MAGIC Checks, in this order: what the target chunks are (language, length, headers, text) -> are they in each index -> controls that must rank first (the chunk's own words, its REF) -> the code queries with and without the chat's division filter -> a verdict.

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

INDEXES = ['dev_proj.qualibot.chunks_index', 'dev_proj.qualibot.chunks_text_index']
CHUNKS = 'dev_proj.qualibot.chunks'
CODE_REGEX = r'(?i)SF[\s\-‐‑‒–—_]?1027'
QUERIES = {
    'code spelled SF-1027': 'SF-1027',
    'code glued SF1027': 'SF1027',
    'app query SF-1027 [SF1027]': 'SF-1027 [SF1027]',
    'FR question': 'Quels sont les documents citant le SF-1027 ?',
    'FR rewrite (what the app sent)': 'Quels sont les documents citant le SF1027 ?',
    'EN question': 'Which documents cite SF-1027?',
    'EN with context': 'SF-1027 training for managers',
}
FILTERS = {'no filter': {}, 'AS+GROUP': {'division': ['AS', 'GROUP']}, 'IS+GROUP': {'division': ['IS', 'GROUP']}}
METHODS = ['ANN', 'FULL_TEXT', 'HYBRID', 'HYBRID+RERANK']
TOP = 50
COLUMNS = ['chunk_id', 'IDDOC', 'REF', 'division', 'semantic_headers', 'chunk_text']

w = WorkspaceClient()
HOST = w.config.host.rstrip('/')


def query(payload: dict, index: str) -> list:
    """One Vector Search query.

    :param payload: request body
    :param index: full index name
    :return: result rows as dicts; raises with the server message when refused
    """
    r = httpx.post(f'{HOST}/api/2.0/vector-search/indexes/{index}/query',
                   headers={**w.config.authenticate(), 'Content-Type': 'application/json'}, json=payload, timeout=60)
    if r.status_code != 200:
        raise RuntimeError(f'{r.status_code} {r.text[:200]}')
    d = r.json()
    cols = [c['name'] for c in d['manifest']['columns']]
    return [dict(zip(cols, row)) for row in d['result']['data_array']]


def make_payload(method: str, text: str, flt: dict) -> dict:
    """Request body of a query as the chat builds it.

    :param method: ANN, FULL_TEXT, HYBRID or HYBRID+RERANK
    :param text: query text
    :param flt: Vector Search filter, empty for none
    :return: the request body
    """
    p = {'query_text': text, 'columns': COLUMNS, 'num_results': TOP,
         'query_type': 'HYBRID' if method.startswith('HYBRID') else method}
    if flt:
        p['filters_json'] = json.dumps(flt)
    if method == 'HYBRID+RERANK':
        p['reranker'] = {'model': 'databricks_reranker', 'parameters': {'columns_to_rerank': ['REF', 'semantic_headers', 'chunk_text']}}
    return p


def first_rank(index: str, method: str, text: str, flt: dict, targets: set) -> str:
    """Rank of the first target chunk in the results.

    :param index: full index name
    :param method: query type
    :param text: query text
    :param flt: filter
    :param targets: chunk ids looked for
    :return: the rank, ``> N`` when absent, ``refused`` when the index refuses the method
    """
    try:
        res = query(make_payload(method, text, flt), index)
    except Exception as exc:
        return 'refused' if 'FULL_TEXT' in str(exc) or '400' in str(exc) else f'error {str(exc)[:40]}'
    ranks = [i + 1 for i, r in enumerate(res) if r['chunk_id'] in targets]
    return str(ranks[0]) if ranks else f'> {len(res)}'

# COMMAND ----------

# 1. The target chunks: what they are.
df = spark.table(CHUNKS)
hit = df.filter(F.col('chunk_text').rlike(CODE_REGEX))
TARGET_ROWS = [r.asDict() for r in hit.select('chunk_id', 'IDDOC', 'REF', 'division', 'semantic_headers', 'chunk_text',
                                              'chunk_preprocessed').collect()]
TARGETS = {r['chunk_id'] for r in TARGET_ROWS}
assert TARGETS, 'No chunk cites the code: parsing problem, not search.'
for r in TARGET_ROWS:
    text = r['chunk_text'] or ''
    print(f"--- {r['chunk_id']} | REF {r['REF']} | division {r['division']} | {len(text)} characters, {len(text.split())} words")
    print('headers :', (r['semantic_headers'] or '')[:150])
    print('text    :', text[:400].replace('\n', ' '))
    print('prepro  :', re.sub(r'\s+', ' ', (r['chunk_preprocessed'] or ''))[:400])
    same_doc = df.filter(F.col('IDDOC') == r['IDDOC']).count()
    print('chunks of the same document:', same_doc)

# COMMAND ----------

# 2. Which indexes exist, what they embed, and whether each target is inside.
LIVE = []
for ix in INDEXES:
    r = httpx.get(f'{HOST}/api/2.0/vector-search/indexes/{ix}', headers=w.config.authenticate(), timeout=30)
    if r.status_code != 200:
        print(ix, '-> absent')
        continue
    d = r.json()
    emb = [c['name'] for c in d.get('delta_sync_index_spec', {}).get('embedding_source_columns', [])]
    inside = 0
    for cid in TARGETS:
        got = query({'query_text': 'x', 'columns': COLUMNS, 'num_results': 1, 'query_type': 'ANN',
                     'filters_json': json.dumps({'chunk_id': [cid]})}, ix)
        inside += bool(got)
    print(f"{ix} -> embeds {emb} | rows {d.get('status', {}).get('indexed_row_count')} | targets inside: {inside}/{len(TARGETS)}")
    LIVE.append(ix)

# COMMAND ----------

# 3. Controls that MUST rank first if the index works: the chunk's own opening words, and its REF.
controls = {}
for r in TARGET_ROWS:
    controls[f"own words ({r['chunk_id']})"] = re.sub(r'\s+', ' ', r['chunk_text'] or '')[:200]
    controls[f"REF {r['REF']}"] = r['REF']
controls = dict(list(controls.items())[:4])
print(f"{'index':<20}{'method':<15}{'control':<42}first rank")
for ix in LIVE:
    for method in ('HYBRID', 'HYBRID+RERANK'):
        for name, text in controls.items():
            print(f"{ix.split('.')[-1]:<20}{method:<15}{name[:40]:<42}{first_rank(ix, method, text, {}, TARGETS)}")

# COMMAND ----------

# 4. The code queries: every index x method x query x division filter.
jobs = [(ix, m, qn, qt, fn, ff) for ix in LIVE for m in METHODS for qn, qt in QUERIES.items() for fn, ff in FILTERS.items()]


def run(job: tuple) -> tuple:
    """Run one cell of the grid.

    :param job: (index, method, query name, query text, filter name, filter)
    :return: the job with its first rank appended
    """
    ix, m, qn, qt, fn, ff = job
    return (*job[:5], first_rank(ix, m, qt, ff, TARGETS))


with ThreadPoolExecutor(max_workers=4) as ex:
    grid = list(ex.map(run, jobs))

print(f"{'index':<20}{'method':<15}{'query':<34}{'filter':<11}first rank")
for ix, m, qn, qt, fn, rank in grid:
    print(f"{ix.split('.')[-1]:<20}{m:<15}{qn[:32]:<34}{fn:<11}{rank}")

# COMMAND ----------

# 5. Verdict.
def found(index_short: str, method: str, flt: str) -> list:
    """Query names whose first rank is within the top 12 for a combination.

    :param index_short: index name without catalog and schema
    :param method: query type
    :param flt: filter name
    :return: the query names that retrieve a target in the first 12
    """
    return [qn for ix, m, qn, qt, fn, rank in grid
            if ix.endswith(index_short) and m == method and fn == flt and rank.isdigit() and int(rank) <= 12]


for ix in LIVE:
    short = ix.split('.')[-1]
    for method in METHODS:
        print(f"{short:<20}{method:<15}top 12 without filter: {found(short, method, 'no filter') or 'none'}")
print()
print('Reading:')
print('- Own words / REF rank first but every code query fails: the chunk is indexed; the code word weighs too little (short chunk, or the code is a stop-word-like token). Next: dedicated code query.')
print('- Own words do NOT rank first: the chunk is not (properly) searchable: index out of sync or wrong column embedded.')
print('- AS+GROUP / IS+GROUP fail where "no filter" succeeds: the division filter hides the chunk (check its division above).')
print('- FULL_TEXT finds it and HYBRID does not: the keyword side works, the fusion buries it. FULL_TEXT refused: the index is not keyword-capable.')
print('- English queries find it, French ones do not: language gap between the question and an English chunk.')
