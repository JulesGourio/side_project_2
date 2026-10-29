# Databricks notebook source
# MAGIC %md
# MAGIC # Codes and acronyms the baseline index misses (DEV, read only on the indexes)
# MAGIC Finds reference codes and acronyms whose documents the **baseline** index (`chunks_baseline_index`, no code
# MAGIC normalization) does not return, to write new golden-set questions from them.
# MAGIC
# MAGIC | | |
# MAGIC | --- | --- |
# MAGIC | Reads | `dev_proj.qualibot.chunks`, `dev_proj.qualibot.f1_inventaire_codes` (acronyms, optional), the two indexes |
# MAGIC | Writes | `dev_proj.qualibot.eval_missed_terms` (overwritten) |
# MAGIC | Rights | your own: SELECT on the tables, query on the indexes, CREATE TABLE on `dev_proj.qualibot` |
# MAGIC
# MAGIC Method, for each term drawn (codes and acronyms found in 1 to `MAX_DOCS` documents):
# MAGIC 1. expected documents = the documents whose passages contain the term (language variants merged, as the golden set does);
# MAGIC 2. two questions (FR, EN) with the term written as in the documents, sent as is (no normalization) to the baseline
# MAGIC    index, HYBRID + reranker, as the chat does;
# MAGIC 3. missed = no expected document among the first `K` distinct documents.
# MAGIC The same questions, normalised as the app does, are also sent to `chunks_index` for information (`COMPARE_NEW`).

# COMMAND ----------

# MAGIC %pip install -q httpx "databricks-sdk>=0.102"
# MAGIC dbutils.library.restartPython()

# COMMAND ----------

import json
import logging
import random
import re
from concurrent.futures import ThreadPoolExecutor

import httpx
from databricks.sdk import WorkspaceClient
from pyspark.sql import functions as F

logging.getLogger('httpx').setLevel(logging.WARNING)
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
logger = logging.getLogger('missed_terms')

BASELINE_INDEX = 'dev_proj.qualibot.chunks_baseline_index'
NEW_INDEX = 'dev_proj.qualibot.chunks_index'
CHUNKS = 'dev_proj.qualibot.chunks'
INVENTORY = 'dev_proj.qualibot.f1_inventaire_codes'
OUTPUT = 'dev_proj.qualibot.eval_missed_terms'
COMPARE_NEW = True
N_CODES = 150            # codes drawn
N_ACRONYMS = 60          # acronyms drawn
MAX_DOCS = 3             # a term cited by more documents is too vague to give a golden answer
K = 10                   # distinct documents looked at (the golden set measures Recall@K at document level)
DEPTH = 50               # passages asked per query
SEED = 7
QUESTIONS = {
    'code': ('Dans quel document trouve-t-on le {t} ?', 'Which document mentions {t}?'),
    'acronym': ('Que signifie {t} ?', 'What does {t} stand for?'),
}
LANGUAGE_SUFFIX = re.compile(r'(?:[\s_\-.](?:FR|EN|GB|MX|BG|CZ|BR|ES|ESP))+$', re.IGNORECASE)
RERANKER = {'model': 'databricks_reranker', 'parameters': {'columns_to_rerank': ['REF', 'semantic_headers', 'chunk_text']}}

w = WorkspaceClient()
HOST = w.config.host.rstrip('/')


def doc_key(ref: str) -> str:
    """Document key of a REF, language variants merged (``PRLAT538_FR`` -> ``PRLAT538``).

    :param ref: REF in any spelling
    :return: upper case, without language suffix, letters and digits only
    """
    return re.sub(r'[^A-Z0-9]', '', LANGUAGE_SUFFIX.sub('', (ref or '').strip()).upper())


def search(index: str, text: str) -> list:
    """HYBRID + reranker query, as the chat sends it.

    :param index: full index name
    :param text: query text
    :return: the REFs of the passages, best first; raises with the server message when refused
    """
    payload = {'query_text': text, 'columns': ['chunk_id', 'REF'], 'num_results': DEPTH, 'query_type': 'HYBRID',
               'reranker': RERANKER}
    r = httpx.post(f'{HOST}/api/2.0/vector-search/indexes/{index}/query',
                   headers={**w.config.authenticate(), 'Content-Type': 'application/json'}, json=payload, timeout=90)
    if r.status_code != 200:
        raise RuntimeError(f'{r.status_code} {r.text[:200]}')
    d = r.json()
    cols = [c['name'] for c in d['manifest']['columns']]
    return [dict(zip(cols, row))['REF'] for row in d.get('result', {}).get('data_array') or []]


def doc_rank(refs: list, expected: set) -> int | None:
    """Rank of the first expected document among the distinct documents returned.

    :param refs: REFs of the passages, best first
    :param expected: expected document keys
    :return: 1-based document rank, None when absent from the results
    """
    docs = list(dict.fromkeys(doc_key(r) for r in refs))
    return next((i for i, d in enumerate(docs, 1) if d in expected), None)

# COMMAND ----------

# 1. Candidate terms and their documents.
chunks = spark.table(CHUNKS).select('chunk_id', 'REF', 'chunk_text', 'chunk_preprocessed', 'cited_codes')

# Codes: canonical forms the pipeline wrote (brackets of chunk_preprocessed, cited_codes), with a letter and a digit.
codes = (chunks.select('REF', F.explode(F.array_union(
            F.regexp_extract_all('chunk_preprocessed', F.lit(r'\[([A-Z0-9]{3,25})\]'), 1),
            F.split(F.coalesce('cited_codes', F.lit('')), ' '))).alias('term'))
         .filter(F.col('term').rlike('[A-Z]') & F.col('term').rlike('[0-9]')))
code_docs = codes.groupBy('term').agg(F.collect_set('REF').alias('refs'))

# Acronyms: the inventory when it exists, else upper-case words of 2 to 6 letters.
try:
    acr = (spark.table(INVENTORY).filter((F.col('env') == 'dev') & (F.col('section') == 'B_corpus') & (F.col('categorie') == 'sigle'))
           .select(F.col('cle').alias('term')).distinct())
    logger.info('acronyms from %s', INVENTORY)
except Exception:  # noqa: BLE001 — the inventory is optional
    acr = None
    logger.info('inventory not readable: acronyms taken from the text')
words = chunks.select('REF', F.explode(F.regexp_extract_all('chunk_text', F.lit(r'\b([A-Z]{2,6})\b'), 1)).alias('term'))
if acr is not None:
    words = words.join(acr, 'term')
acr_docs = words.groupBy('term').agg(F.collect_set('REF').alias('refs'))


def draw(df, n: int) -> list:
    """Terms found in 1 to MAX_DOCS documents (language variants merged), drawn at random.

    :param df: term, refs
    :param n: terms to draw
    :return: [(term, sorted document keys)]
    """
    rows = [(r.term, sorted({doc_key(x) for x in r.refs})) for r in df.filter(F.size('refs') <= 4 * MAX_DOCS).collect()]
    rows = [r for r in rows if 1 <= len(r[1]) <= MAX_DOCS]
    random.seed(SEED)
    return random.sample(rows, min(n, len(rows)))


TERMS = [('code', t, d) for t, d in draw(code_docs, N_CODES)] + [('acronym', t, d) for t, d in draw(acr_docs, N_ACRONYMS)]
logger.info('%d codes and %d acronyms drawn', sum(k == 'code' for k, _, _ in TERMS), sum(k == 'acronym' for k, _, _ in TERMS))

# How each code is written in the documents (most frequent spelling before its bracket).
spell_rows = (chunks.filter(F.col('chunk_preprocessed').isNotNull())
              .select(F.explode(F.regexp_extract_all('chunk_preprocessed', F.lit(r'(\S+) \[([A-Z0-9]{3,25})\]'), 0)).alias('m'))
              .groupBy('m').count().collect())
SPELLING = {}
for r in sorted(spell_rows, key=lambda r: -r['count']):
    raw, canon = r.m.rsplit(' [', 1)
    canon, raw = canon.rstrip(']'), raw.strip('()[],;:.*|')
    if re.sub(r'[^0-9A-Za-z]', '', raw).upper() == canon:
        SPELLING.setdefault(canon, raw)

# COMMAND ----------

# 2. Ask each question to the baseline index (and to the new one).


def run(term_row: tuple) -> dict:
    """Both questions of a term on the baseline index, and on the new one when COMPARE_NEW.

    :param term_row: (kind, term, expected document keys)
    :return: one result row
    """
    kind, term, expected = term_row
    written = SPELLING.get(term, term) if kind == 'code' else term
    fr, en = (q.format(t=written) for q in QUESTIONS[kind])
    out = {'kind': kind, 'term': term, 'written': written, 'expected_refs': expected, 'question_fr': fr, 'question_en': en}
    for lang, q in (('fr', fr), ('en', en)):
        try:
            out[f'baseline_{lang}'] = doc_rank(search(BASELINE_INDEX, q), set(expected))
        except Exception as exc:  # noqa: BLE001
            out[f'baseline_{lang}'] = -1
            out['error'] = str(exc)[:200]
        if COMPARE_NEW:
            normalised = q.replace(written, f'{written} [{term}]') if kind == 'code' and written != term else q
            try:
                out[f'new_{lang}'] = doc_rank(search(NEW_INDEX, normalised), set(expected))
            except Exception as exc:  # noqa: BLE001
                out[f'new_{lang}'] = -1
                out['error'] = str(exc)[:200]
    return out


with ThreadPoolExecutor(max_workers=6) as ex:
    RESULTS = list(ex.map(run, TERMS))
errors = [r for r in RESULTS if r.get('error')]
if errors:
    logger.warning('%d term(s) with a refused query, first: %s', len(errors), errors[0]['error'])


def missed(rank: int | None) -> bool:
    return rank is None or rank > K


for r in RESULTS:
    r['missed_baseline'] = missed(r['baseline_fr']) and missed(r['baseline_en'])
    r['missed_baseline_any'] = missed(r['baseline_fr']) or missed(r['baseline_en'])

# COMMAND ----------

# 3. Write the table and show the terms missed by the baseline.
schema = ('kind string, term string, written string, expected_refs array<string>, question_fr string, question_en string, '
          'baseline_fr int, baseline_en int, new_fr int, new_en int, missed_baseline boolean, missed_baseline_any boolean, error string')
rows = [tuple(r.get(k) for k in ('kind', 'term', 'written', 'expected_refs', 'question_fr', 'question_en', 'baseline_fr', 'baseline_en',
                                  'new_fr', 'new_en', 'missed_baseline', 'missed_baseline_any', 'error')) for r in RESULTS]
spark.createDataFrame(rows, schema).withColumn('measured_at', F.current_timestamp()) \
    .write.mode('overwrite').option('overwriteSchema', 'true').saveAsTable(OUTPUT)
logger.info('%d terms written to %s', len(rows), OUTPUT)

for kind in QUESTIONS:
    rs = [r for r in RESULTS if r['kind'] == kind and not r.get('error')]
    print(f"{kind}: {len(rs)} terms | missed by the baseline in FR and EN: {sum(r['missed_baseline'] for r in rs)}"
          f" | in FR or EN: {sum(r['missed_baseline_any'] for r in rs)}"
          + (f" | of those, found by the new index (FR or EN): "
             f"{sum(r['missed_baseline_any'] and not (missed(r['new_fr']) and missed(r['new_en'])) for r in rs)}" if COMPARE_NEW else ''))


def show(rank: int | None) -> str:
    return '-' if rank is None else str(rank)


print(f"\nrank of the first expected document (- = not in the results), missed = beyond {K}\n")
print(f"{'kind':<8}{'term':<18}{'written':<20}{'base FR':>8}{'base EN':>8}{'new FR':>7}{'new EN':>7}  expected REFs")
for r in sorted((r for r in RESULTS if r['missed_baseline_any'] and not r.get('error')), key=lambda r: (r['kind'], not r['missed_baseline'], r['term'])):
    print(f"{r['kind']:<8}{r['term'][:17]:<18}{r['written'][:19]:<20}{show(r['baseline_fr']):>8}{show(r['baseline_en']):>8}"
          f"{show(r.get('new_fr')):>7}{show(r.get('new_en')):>7}  {', '.join(r['expected_refs'])}")
