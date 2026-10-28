# Databricks notebook source
# MAGIC %md
# MAGIC # DEV — SF-1027 study: why a French question misses a code the code alone finds (read only)
# MAGIC Rights: your own (SELECT on `dev_proj.qualibot.chunks`, `chat_turns`, `chat_retrieved_chunks`, query on the indexes and on the embedding endpoint). Nothing is written, the app is not touched.
# MAGIC Cells: A what the app really did -> B the target chunk -> C how the keyword side tokenizes -> D exact ranks -> E which words bury the code -> F who takes its place -> G reranker pool -> H options measured on 25 codes -> I side effects on questions without code.

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

logging.getLogger('httpx').setLevel(logging.WARNING)   # the previous notebook drowned its tables in request logs

INDEX = 'dev_proj.qualibot.chunks_index'                 # the index of the DEV app (CHAT_INDEX)
CHUNKS = 'dev_proj.qualibot.chunks'
TURNS = 'dev_proj.qualibot.chat_turns'                   # Delta copy of the Lakebase log (may lag behind the app)
RETRIEVED = 'dev_proj.qualibot.chat_retrieved_chunks'
EMBEDDING_ENDPOINT = 'databricks-qwen3-embedding-0-6b'
CODE = 'SF1027'
CODE_REGEX = r'(?i)SF[\s\-‐‑‒–—_]?1027'
# Used when the turn log has no SF-1027 turn: the question as Jules typed it and a plausible translation.
FALLBACK_QUESTION = 'Quels sont les deocuments citant le SF1027 ?'
FALLBACK_EN = 'What are the documents citing SF1027?'
# The DEV app on an opening French message (CHAT_TRANSLATE_ONLY=on): question + its English translation, 16 reranked
# passages each, 16 raw passages of the question, merged by best rank, every merged passage sent to the model.
APP_K = 16
COLUMNS = ['chunk_id', 'IDDOC', 'REF', 'division', 'semantic_headers', 'chunk_text']
RERANKER = {'model': 'databricks_reranker', 'parameters': {'columns_to_rerank': ['REF', 'semantic_headers', 'chunk_text']}}
N_CODES = 25            # codes of the benchmark (cell H), SF1027 included
SEED = 42

w = WorkspaceClient()
HOST = w.config.host.rstrip('/')
_cache = {}


def vs(payload: dict) -> tuple:
    """One Vector Search query on INDEX, cached on its body.

    :param payload: request body
    :return: (rows as dicts with their score, total_hit_count or None); raises with the server message when refused
    """
    key = json.dumps(payload, sort_keys=True)
    if key not in _cache:
        r = httpx.post(f'{HOST}/api/2.0/vector-search/indexes/{INDEX}/query',
                       headers={**w.config.authenticate(), 'Content-Type': 'application/json'}, json=payload, timeout=90)
        if r.status_code != 200:
            raise RuntimeError(f'{r.status_code} {r.text[:300]}')
        d = r.json()
        cols = [c['name'] for c in d['manifest']['columns']]
        _cache[key] = ([dict(zip(cols, row)) for row in d.get('result', {}).get('data_array') or []],
                       d['manifest'].get('total_hit_count'))
    return _cache[key]


def body(text: str, kind: str, k: int, vector: list | None = None) -> dict:
    """Request body.

    :param text: query text
    :param kind: ANN, FULL_TEXT, HYBRID or RERANK (= HYBRID + reranker, what the chat sends)
    :param k: num_results
    :param vector: query vector sent next to the text (hybrid with our own embedding), None for none
    :return: the body
    """
    p = {'query_text': text, 'columns': COLUMNS, 'num_results': k, 'query_type': 'HYBRID' if kind == 'RERANK' else kind}
    if kind == 'RERANK':
        p['reranker'] = RERANKER
    if vector is not None:
        p['query_vector'] = vector
    return p


def rank(rows: list, targets: set) -> int | None:
    """1-based rank of the first target, None when absent."""
    return next((i for i, r in enumerate(rows, 1) if r['chunk_id'] in targets), None)


def show(r: int | None, k: int) -> str:
    return str(r) if r else f'>{k}'


def app_query(text: str) -> str:
    """normalize_query of the app for the codes of this notebook: ``SF-1027`` -> ``SF-1027 [SF1027]``."""
    return re.sub(r'(?i)\b(SF[\s\-‐‑‒–—_]1027)\b(?! \[)', r'\1 [SF1027]', text)

# COMMAND ----------

# A. What the app really did for the SF-1027 turns: queries sent, and was the target chunk retrieved / sent to the model?
TARGETS = {r.chunk_id for r in spark.table(CHUNKS).filter(F.col('chunk_text').rlike(CODE_REGEX)).select('chunk_id').collect()}
print('target chunks:', sorted(TARGETS))
APP_QUESTION, APP_EN = FALLBACK_QUESTION, FALLBACK_EN
try:
    turns = (spark.table(TURNS).filter(F.col('question').rlike(r'(?i)1027')).orderBy(F.col('created_at').desc()).limit(8).collect())
    print(f"turn log up to {spark.table(TURNS).agg(F.max('created_at')).first()[0]} ; {len(turns)} turn(s) on 1027")
    retrieved = spark.table(RETRIEVED)
    for t in turns:
        d = t.asDict()
        print(f"\n--- {d['created_at']} | division {d.get('division')} | mode {d.get('rewrite_mode')} | index {d.get('index_name')}")
        print('question :', d.get('question'))
        print('fr_query :', d.get('fr_query'))
        print('en_query :', d.get('en_query'))
        print('passages retrieved / sent:', d.get('passages_retrieved'), '/', d.get('passages_sent'), '| cited:', d.get('cited_refs'))
        hits = retrieved.filter((F.col('turn_id') == d['id']) & F.col('chunk_id').isin(list(TARGETS))).collect()
        for h in hits:
            print(f"  TARGET {h.chunk_id}: position {h.position}, kept {h.kept}, drop {h.drop_reason}, prompt_rank {h.prompt_rank}, cited {h.cited}, hits {h.hits}")
        if not hits:
            print('  target NOT retrieved by this turn')
    if turns and turns[0]['en_query']:
        APP_QUESTION, APP_EN = turns[0]['question'], turns[0]['en_query']
except Exception as exc:   # noqa: BLE001 — the study goes on with the fallback queries
    print('turn log not readable:', str(exc)[:200])
print(f'\nqueries studied: {APP_QUESTION!r} / {APP_EN!r}')

# COMMAND ----------

# B. The target chunk: where the code sits. The reranker reads only the first 2000 characters of REF + headers + text.
for r in spark.table(CHUNKS).filter(F.col('chunk_id').isin(list(TARGETS))).collect():
    text = r.chunk_text or ''
    m = re.search(CODE_REGEX, text)
    seen_by_reranker = len(r.REF or '') + len(r.semantic_headers or '') + (m.start() if m else 10**6) < 2000
    words = re.findall(r"[a-zà-ÿ]+", text.lower())
    fr = sum(x in {'le', 'la', 'les', 'des', 'du', 'est', 'et', 'pour', 'dans'} for x in words)
    en = sum(x in {'the', 'of', 'and', 'is', 'for', 'in', 'to', 'shall', 'be'} for x in words)
    print(f"{r.chunk_id}: {len(text)} chars, code at char {m.start() if m else None}, seen by reranker: {seen_by_reranker}, "
          f"occurrences {len(re.findall(CODE_REGEX, text))}, FR/EN function words {fr}/{en}")
    print('   ...', text[max(0, (m.start() if m else 0) - 150):(m.start() if m else 0) + 150].replace('\n', ' '), '...')

# COMMAND ----------

# C. Keyword side: how many chunks each token matches (total_hit_count). Reading:
#    'the' ~0 and 'les' high -> English analyzer: French function words count as keywords.
#    'cite' = 'cited' = 'citing' -> stemming. 'SF-1027' ~ 'SF' + '1027' -> the dash splits the code.
PROBES = ['SF1027', 'sf1027', '[SF1027]', 'SF-1027', 'SF 1027', 'SF', '1027', 'les', 'le', 'sont', 'quels', 'citant',
          'documents', 'deocuments', 'the', 'which', 'are', 'what', 'cite', 'cited', 'citing', 'document']
print(f"{'token':<14}{'hits':>8}{'target rank':>13}")
for p in PROBES:
    try:
        rows, total = vs(body(p, 'FULL_TEXT', 1000))
        print(f"{p:<14}{str(total if total is not None else len(rows)):>8}{show(rank(rows, TARGETS), 1000):>13}")
    except Exception as exc:  # noqa: BLE001
        print(f'{p:<14} refused: {str(exc)[:80]}')

# COMMAND ----------

# D. Exact ranks of the target, deep: FULL_TEXT up to 1000, HYBRID up to 200 (its cap), with the scores around it.
QUERIES = {'code alone': 'SF1027', 'app question (normalised)': app_query(APP_QUESTION), 'app EN query': app_query(APP_EN),
           'FR question without typo': app_query('Quels sont les documents citant le SF-1027 ?'),
           'EN reference': 'Which documents cite SF-1027 [SF1027]?'}
print(f"{'query':<28}{'FULL_TEXT':>10}{'score tgt':>11}{'score #1':>10}{'score #16':>10}{'HYBRID':>8}{'RERANK k16':>11}")
for name, q in QUERIES.items():
    ft, _ = vs(body(q, 'FULL_TEXT', 1000))
    rk = rank(ft, TARGETS)
    sc = lambda i: f"{ft[i]['score']:.2f}" if len(ft) > i and isinstance(ft[i].get('score'), (int, float)) else '-'
    hy, _ = vs(body(q, 'HYBRID', 200))
    rr, _ = vs(body(q, 'RERANK', APP_K))
    print(f"{name:<28}{show(rk, 1000):>10}{sc(rk - 1) if rk else '-':>11}{sc(0):>10}{sc(15):>10}"
          f"{show(rank(hy, TARGETS), 200):>8}{show(rank(rr, TARGETS), APP_K):>11}")

# COMMAND ----------

# E. Which words bury the code (FULL_TEXT rank of the target, up to 1000).
words = [x for x in re.findall(r'[\w\-]+', APP_QUESTION) if not re.fullmatch(CODE_REGEX, x)]
print('code + one word:')
for x in words:
    print(f'  SF1027 {x:<14}{show(rank(vs(body(f"SF1027 {x}", "FULL_TEXT", 1000))[0], TARGETS), 1000):>6}')
print('question without one word:')
for x in words:
    q = app_query(re.sub(rf'(?<!\w){re.escape(x)}(?!\w)', ' ', APP_QUESTION, count=1))
    print(f'  minus {x:<14}{show(rank(vs(body(q, "FULL_TEXT", 1000))[0], TARGETS), 1000):>6}   {q}')

# COMMAND ----------

# F. Who takes the target's place for the app question: the 10 first keyword hits, with the query words they contain.
qwords = {x.lower() for x in re.findall(r'\w+', app_query(APP_QUESTION))}
ft, _ = vs(body(app_query(APP_QUESTION), 'FULL_TEXT', 10))
for i, r in enumerate(ft, 1):
    body_words = set(re.findall(r'\w+', (r['chunk_text'] or '').lower()))
    print(f"{i:>2} {r.get('score') or 0:.2f} {r['REF']:<18} words of the query inside: {sorted(qwords & body_words)}")

# COMMAND ----------

# G. Reranker pool: does num_results change what the reranker sees? (docs: it does not)
for name in ('app EN query', 'EN reference'):
    q = QUERIES[name]
    print(name, '| HYBRID', show(rank(vs(body(q, 'HYBRID', 200))[0], TARGETS), 200),
          '| RERANK k=12/16/50:', [show(rank(vs(body(q, 'RERANK', k))[0], TARGETS), k) for k in (12, 16, 50)])

# COMMAND ----------

# H. Options measured on N_CODES rare codes, 3 question shapes each, without touching the app.
#    A  = the DEV app today (question + EN translation reranked k16, question raw k16, merged by best rank)
#    B  = A with French/English grammatical words removed from every query (changes every question: see cell I)
#    C  = A + one extra reranked query made of the codes only (k8), merged by best rank (not forced first)
#    D  = A + one extra FULL_TEXT query of the codes only (k8), merged by best rank
#    E  = A with the canonical code repeated 3 times at the end of each query
#    F  = A where each query sends our own embedding of the full question + the codes only as keyword text
STOP = set('''le la les l un une des du de d au aux a à et ou est sont quel quels quelle quelles que qu qui quoi dans en sur
pour par avec ce ces cet cette se on t il elle y the a an of to in on for by with is are was what which who whom
where when how do does did about from at this that these those be'''.split())
TEMPLATES = [('Quels sont les documents citant le {c} ?', 'What are the documents citing {c}?'),
             ('Dans quel document trouve-t-on le {c} ?', 'In which document is {c} found?'),
             ('Que dit-on du {c} ?', 'What is said about {c}?')]

pre = spark.table(CHUNKS).select('chunk_id', 'chunk_preprocessed')
codes = (pre.select('chunk_id', F.explode(F.regexp_extract_all('chunk_preprocessed', F.lit(r'\[([A-Z0-9]{4,20})\]'), 1)).alias('code'))
         .groupBy('code').agg(F.countDistinct('chunk_id').alias('n')).filter('n BETWEEN 1 AND 5').collect())
random.seed(SEED)
pool = sorted(r.code for r in codes if r.code != CODE and re.search(r'[A-Z]', r.code) and re.search(r'\d', r.code))
BENCH = [CODE] + random.sample(pool, min(N_CODES - 1, len(pool)))
CASES = {}
for c in BENCH:
    hit = pre.filter(F.col('chunk_preprocessed').contains(f'[{c}]') | F.col('chunk_preprocessed').rlike(rf'\b{c}\b')).collect()
    spelled = c
    for h in hit:
        m = re.search(rf'(\S+) \[{c}\]', h.chunk_preprocessed or '')
        cand = m and m.group(1).strip('()[],;:.')
        if cand and re.sub(r'[^0-9A-Za-z]', '', cand).upper() == c:
            spelled = cand
            break
    CASES[c] = (spelled, {h.chunk_id for h in hit})
print(len(BENCH), 'codes:', ', '.join(f'{c} ({s}, {len(t)} chunks)' for c, (s, t) in CASES.items()))

_vec = {}


def embed(text: str) -> list:
    """Embedding of a text by the index's own model."""
    if text not in _vec:
        r = httpx.post(f'{HOST}/serving-endpoints/{EMBEDDING_ENDPOINT}/invocations', json={'input': [text]},
                       headers={**w.config.authenticate(), 'Content-Type': 'application/json'}, timeout=60)
        r.raise_for_status()
        _vec[text] = r.json()['data'][0]['embedding']
    return _vec[text]


def merge(lists: list) -> list:
    """_merge_by_rank of the app: one row per chunk, ordered by its best rank in any list."""
    best = {}
    for result in lists:
        for i, row in enumerate(result):
            if row['chunk_id'] not in best or i < best[row['chunk_id']][0]:
                best[row['chunk_id']] = (i, row)
    return [row for _, row in sorted(best.values(), key=lambda x: x[0])]


def strip_stop(q: str) -> str:
    return ' '.join(x for x in re.findall(r"\[?[\w\-]+\]?", q) if x.lower() not in STOP) or q


def run_app(fr: str, en: str, codes: list, option: str) -> list:
    """Passages the app would send for an opening French question, under an option.

    :param fr: French question, normalised
    :param en: its translation, normalised
    :param codes: canonical codes of the question
    :param option: A to F
    :return: the merged passages, best rank first
    """
    if option == 'B':
        fr, en = strip_stop(fr), strip_stop(en)
    if option == 'E':
        fr, en = (f"{q} {' '.join(codes * 3)}" for q in (fr, en))
    vec = (lambda q: embed(q)) if option == 'F' else (lambda q: None)
    kw = (lambda q: ' '.join(codes)) if option == 'F' else (lambda q: q)
    reranked = [vs(body(kw(q), 'RERANK', APP_K, vec(q)))[0] for q in (fr, en)]
    if option == 'C':
        reranked.append(vs(body(' '.join(codes), 'RERANK', 8))[0])
    if option == 'D':
        reranked.append(vs(body(' '.join(codes), 'FULL_TEXT', 8))[0])
    raw = [vs(body(kw(fr), 'HYBRID', APP_K, vec(fr)))[0]]
    return merge([merge(reranked), merge(raw)])


def one_case(job: tuple) -> tuple:
    code, t, option = job
    spelled, targets = CASES[code]
    fr = TEMPLATES[t][0].format(c=spelled)
    en = TEMPLATES[t][1].format(c=spelled)
    norm = (lambda q: q.replace(spelled, f'{spelled} [{code}]')) if spelled != code else (lambda q: q)
    try:
        rows = run_app(norm(fr), norm(en), [code], option)
        return code, t, option, rank(rows, targets), len(rows)
    except Exception as exc:  # noqa: BLE001
        return code, t, option, 'refused: ' + str(exc)[:80], 0


OPTIONS = ['A', 'B', 'C', 'D', 'E', 'F']
CALLS = {'A': 3, 'B': 3, 'C': 4, 'D': 4, 'E': 3, 'F': '3 + embedding'}
jobs = [(c, t, o) for c in BENCH for t in range(len(TEMPLATES)) for o in OPTIONS]
with ThreadPoolExecutor(max_workers=4) as ex:
    results = list(ex.map(one_case, jobs))

print(f"\n{'option':<8}{'found':>8}{'in top 5':>10}{'median pos.':>13}{'passages':>10}{'VS calls':>16}  refused")
for o in OPTIONS:
    res = [r for r in results if r[2] == o]
    ok = [r for r in res if isinstance(r[3], int)]
    pos = sorted(r[3] for r in ok)
    refused = [r for r in res if isinstance(r[3], str)]
    print(f"{o:<8}{len(ok):>4}/{len(res) - len(refused):<4}{sum(p <= 5 for p in pos):>9}{(pos[len(pos) // 2] if pos else '-'):>13}"
          f"{sum(r[4] for r in res) / max(1, len(res) - len(refused)):>10.1f}{str(CALLS[o]):>16}  {len(refused)}{' ' + refused[0][3] if refused else ''}")
print('\nby question shape (found / cases):')
for t, (fr, _) in enumerate(TEMPLATES):
    print(f"  {fr:<42}", '  '.join(f"{o}:{sum(isinstance(r[3], int) for r in results if r[1] == t and r[2] == o)}" for o in OPTIONS))
print('\nSF1027:', {o: [r[3] or 'absent' for r in results if r[0] == CODE and r[2] == o] for o in OPTIONS})
missed_a = sorted({r[0] for r in results if r[2] == 'A' and r[3] is None})
print('codes missed by the app today (A, at least one shape):', missed_a)

# COMMAND ----------

# I. Side effects on questions WITHOUT a code. C, D, E, F only act when the question names a code: nothing changes here.
#    B changes every query: share of the passages of A still sent under B (1.00 = same passages).
PLAIN = [('Quelle est la procédure de First Article Inspection ?', 'What is the First Article Inspection procedure?'),
         ('Comment traiter une non-conformité fournisseur ?', 'How to handle a supplier non-conformity?'),
         ('Qui valide le plan de surveillance ?', 'Who approves the control plan?'),
         ("Quelle est la durée d'archivage des dossiers FAI ?", 'How long are FAI files archived?'),
         ('Comment est gérée la calibration des outillages ?', 'How is tooling calibration managed?'),
         ("Quelles sont les règles 5S en atelier ?", 'What are the 5S rules in the workshop?')]
for fr, en in PLAIN:
    a = {r['chunk_id'] for r in run_app(fr, en, [], 'A')}
    b = {r['chunk_id'] for r in run_app(fr, en, [], 'B')}
    print(f'{len(a & b) / max(1, len(a)):.2f}  {fr}')

# COMMAND ----------

# J. Same benchmark, the code written in the question the way users type it (cell H used the documents' spelling).
#    The app adds ' [CODE]' after a code written another way than its canonical form; a glued code gets nothing.
def spellings(code: str, spelled: str) -> dict:
    """The ways a user writes a code.

    :param code: canonical form (SF1027)
    :param spelled: spelling of the documents (SF-1027)
    :return: {name: text}
    """
    m = re.match(r'([A-Z]+)(\d.*)', code)
    out = {'documents': spelled, 'glued': code, 'glued lower': code.lower()}
    if m:
        out['space'] = f'{m.group(1)} {m.group(2)}'
    return out


def spelled_case(job: tuple) -> tuple:
    """One benchmark case with a given spelling.

    :param job: (code, template index, option, spelling name)
    :return: (code, template, option, spelling, rank or None, passages)
    """
    code, t, option, sname = job
    spelled, targets = CASES[code]
    text = spellings(code, spelled).get(sname)
    if text is None:
        return code, t, option, sname, 'n/a', 0
    norm = (lambda q: q.replace(text, f'{text} [{code}]')) if text != code else (lambda q: q)
    try:
        rows = run_app(norm(TEMPLATES[t][0].format(c=text)), norm(TEMPLATES[t][1].format(c=text)), [code], option)
        return code, t, option, sname, rank(rows, targets), len(rows)
    except Exception as exc:  # noqa: BLE001
        return code, t, option, sname, 'refused', 0


SPELLINGS = ['documents', 'glued', 'glued lower', 'space']
OPTS_J = ['A', 'D', 'E', 'F']
jobs = [(c, t, o, s) for c in BENCH for t in range(len(TEMPLATES)) for o in OPTS_J for s in SPELLINGS]
with ThreadPoolExecutor(max_workers=4) as ex:
    res_j = list(ex.map(spelled_case, jobs))
print(f"{'spelling':<14}" + ''.join(f'{o:>12}' for o in OPTS_J) + '   (found / cases)')
for s in SPELLINGS:
    cells = []
    for o in OPTS_J:
        rs = [r for r in res_j if r[2] == o and r[3] == s and r[4] != 'n/a']
        cells.append(f"{sum(isinstance(r[4], int) for r in rs)}/{len(rs)}")
    print(f'{s:<14}' + ''.join(f'{c:>12}' for c in cells))
print('\nSF1027 by spelling, option A (3 shapes):', {s: [r[4] for r in res_j if r[0] == CODE and r[2] == 'A' and r[3] == s] for s in SPELLINGS})
print('codes missed by A, glued:', sorted({r[0] for r in res_j if r[2] == 'A' and r[3] == 'glued' and r[4] is None}))

# COMMAND ----------

# K. Other families of methods, on the same 300 questions (25 codes x 3 shapes x 4 spellings).
#    Ranking methods (the whole app pipeline with one engine only):
#      N1 = keywords only (FULL_TEXT) instead of HYBRID      N2 = meaning only (ANN) instead of HYBRID
#    Exact-match methods (A + one extra reranked search of the question, k8, restricted to the passages that hold the
#    code: the code decides WHICH passages, the question decides their order):
#      G1 = filter "cited_codes LIKE code" (codes separated by spaces)   G2 = filter "chunk_preprocessed LIKE code"
#      G3 = chunk ids read from chunks.cited_codes (a code -> passages lookup table, kept by the pipeline)
#      G4 = REFs of the documents citing the code (doc_references, what the app does today for "which documents cite X")
probe_rows, _ = vs({'query_text': 'SF1027', 'columns': ['chunk_id', 'cited_codes'], 'num_results': 1, 'query_type': 'FULL_TEXT'})
print('cited_codes in the index:', probe_rows[0].get('cited_codes') if probe_rows else 'no row')
FILTER_FORMS = {'G1': lambda c: {'cited_codes LIKE': c}, 'G2': lambda c: {'chunk_preprocessed LIKE': c}}
for g, f in FILTER_FORMS.items():
    try:
        rows = vs({**body('SF1027', 'HYBRID', 5), 'filters_json': json.dumps(f(CODE))})[0]
        print(g, 'accepted, rows:', [r['chunk_id'] for r in rows])
    except Exception as exc:  # noqa: BLE001
        print(g, 'refused:', str(exc)[:160])

ch = spark.table(CHUNKS)
LOOKUP = {}
for c in BENCH:
    rows = ch.filter(F.array_contains(F.split(F.coalesce('cited_codes', F.lit('')), ' '), c)).select('chunk_id', 'REF').collect()
    LOOKUP[c] = (sorted({r.chunk_id for r in rows}), sorted({r.REF for r in rows}))
print('lookup cited_codes -> chunks:', {c: len(v[0]) for c, v in LOOKUP.items()})
print('targets without the code in cited_codes (own-document codes are left out by design):',
      {c: len(CASES[c][1] - set(v[0])) for c, v in LOOKUP.items() if CASES[c][1] - set(v[0])})


def run_k(fr: str, en: str, code: str, option: str) -> tuple:
    """Passages of the app under a K option.

    :param fr: French question, normalised
    :param en: its translation, normalised
    :param code: canonical code of the question
    :param option: N1, N2, G1, G2, G3 or G4
    :return: (merged passages, Vector Search calls)
    """
    if option in ('N1', 'N2'):
        kind = 'FULL_TEXT' if option == 'N1' else 'ANN'
        lists = []
        for q in (fr, en):
            try:
                lists.append(vs({**body(q, kind, APP_K), 'reranker': RERANKER})[0])
            except Exception:  # noqa: BLE001 — reranker refused with this query type: plain results
                lists.append(vs(body(q, kind, APP_K))[0])
        return merge([merge(lists), vs(body(fr, kind, APP_K))[0]]), 3
    base = run_app(fr, en, [code], 'A')
    ids, refs = LOOKUP[code]
    flt = {'G1': lambda: FILTER_FORMS['G1'](code), 'G2': lambda: FILTER_FORMS['G2'](code),
           'G3': lambda: {'chunk_id': ids}, 'G4': lambda: {'REF': refs}}[option]()
    if option in ('G3', 'G4') and not ids:
        return base, 3
    extra = vs({**body(fr, 'RERANK', 8), 'filters_json': json.dumps(flt)})[0]
    return merge([extra, base]), 4


def k_case(job: tuple) -> tuple:
    """One K case.

    :param job: (code, template index, option, spelling name)
    :return: (code, template, option, spelling, rank or None or 'refused', passages)
    """
    code, t, option, sname = job
    spelled, targets = CASES[code]
    text = spellings(code, spelled).get(sname)
    if text is None:
        return code, t, option, sname, 'n/a', 0
    norm = (lambda q: q.replace(text, f'{text} [{code}]')) if text != code else (lambda q: q)
    try:
        rows, _ = run_k(norm(TEMPLATES[t][0].format(c=text)), norm(TEMPLATES[t][1].format(c=text)), code, option)
        return code, t, option, sname, rank(rows, targets), len(rows)
    except Exception as exc:  # noqa: BLE001
        return code, t, option, sname, 'refused: ' + str(exc)[:60], 0


OPTS_K = ['N1', 'N2', 'G1', 'G2', 'G3', 'G4']
jobs = [(c, t, o, s) for c in BENCH for t in range(len(TEMPLATES)) for o in OPTS_K for s in SPELLINGS]
with ThreadPoolExecutor(max_workers=4) as ex:
    res_k = list(ex.map(k_case, jobs))
print(f"\n{'spelling':<14}" + ''.join(f'{o:>10}' for o in ['A'] + OPTS_K) + '   (found / cases)')
for s in SPELLINGS + ['ALL']:
    cells = []
    for o in ['A'] + OPTS_K:
        src = res_j if o == 'A' else res_k
        rs = [r for r in src if r[2] == o and (s == 'ALL' or r[3] == s) and r[4] != 'n/a']
        ok = [r for r in rs if not isinstance(r[4], str)]
        cells.append(f"{sum(isinstance(r[4], int) for r in ok)}/{len(ok)}" if ok else 'refused')
    print(f'{s:<14}' + ''.join(f'{c:>10}' for c in cells))
print('\nmedian position of the target when found / passages sent:')
for o in OPTS_K:
    pos = sorted(r[4] for r in res_k if r[2] == o and isinstance(r[4], int))
    n = [r[5] for r in res_k if r[2] == o and not isinstance(r[4], str) and r[4] != 'n/a']
    print(f"  {o}: {pos[len(pos) // 2] if pos else '-'} / {sum(n) / max(1, len(n)):.1f}")
refusals = sorted({(r[2], r[4]) for r in res_k if isinstance(r[4], str) and r[4].startswith('refused')})
print('refusals:', refusals[:6])
