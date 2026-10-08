# Databricks notebook source
# MAGIC %md
# MAGIC # Qualibot — re-chunk the corpus into a TEST index (DEV only)
# MAGIC
# MAGIC Rebuilds the passages of every document of `chunks_v1` with the 2026-10 chunker
# MAGIC (`utils/parsing_pipeline/chunking.py`, audit `docs/chat_vsi_audit_2026-10.md` § 5), from
# MAGIC the text already parsed in `_pipeline_checkpoint_v1` — no GPU, no re-parse, no new image
# MAGIC description. Writes `chunks_<variant>` and its Vector Search index
# MAGIC `chunks_index_<variant>`, then `retrieval_eval` measures it (widget `index_variants`).
# MAGIC
# MAGIC What a variant contains:
# MAGIC - text passages: real section headings, no passage over two level-1/2 sections, the section
# MAGIC   line once, overlap between passages, token AND character ceilings, tables of contents /
# MAGIC   front matter / text repeated in many documents marked in `chunk_content_type`;
# MAGIC - `[Source: REF | Title | Type | Division | Category | Date]` prefix (widget `embed_prefix`);
# MAGIC - columns `titre`, `type_document`, `indice`, `langue`, `body_sha256`;
# MAGIC - image passages from the stored descriptions, with their section and caption, placed after
# MAGIC   the passage they sit in (`anchor_chunk_index`), long transcriptions split;
# MAGIC - optional, OFF by default (they call an LLM, cost in the audit § 5.3):
# MAGIC   `doc_cards` = one summary passage per document (E1), `chunk_context` = 1–2 sentences
# MAGIC   placing each text passage in its document (E2).
# MAGIC
# MAGIC Never touches `chunks_v1` / `chunks_index_v1` (the app and the DEV KA keep using them).

# COMMAND ----------

dbutils.widgets.text('app_code_path', '/Workspace/Shared/.bundle/qualibot/dev/files')
dbutils.widgets.text('variant', 'v2a')                          # -> chunks_v2a / chunks_index_v2a
dbutils.widgets.text('catalog_schema', 'dev_landingzone.qualibot')
dbutils.widgets.text('source_suffix', '_v1')                    # _pipeline_checkpoint_v1, processed_files_v1, ...
dbutils.widgets.text('min_tokens', '250')
dbutils.widgets.text('target_tokens', '500')
dbutils.widgets.text('max_tokens', '1000')
dbutils.widgets.text('max_chars', '4000')
dbutils.widgets.text('overlap_ratio', '0.12')
dbutils.widgets.text('boilerplate_min_docs', '20')
dbutils.widgets.dropdown('embed_prefix', 'true', ['true', 'false'])
dbutils.widgets.dropdown('doc_cards', 'false', ['false', 'true'])          # E1, LLM
dbutils.widgets.dropdown('chunk_context', 'false', ['false', 'true'])      # E2, LLM
dbutils.widgets.text('enrich_max_docs', '0')                    # E1/E2 on the first N documents only (0 = all)
dbutils.widgets.text('llm_endpoint', 'databricks-gpt-5-6-luna')
dbutils.widgets.text('vector_search_endpoint', 'qualibot')
dbutils.widgets.text('embedding_model', 'databricks-qwen3-embedding-0-6b')
dbutils.widgets.dropdown('create_index', 'true', ['true', 'false'])

APP = dbutils.widgets.get('app_code_path').strip().rstrip('/')
VARIANT = dbutils.widgets.get('variant').strip()
CS = dbutils.widgets.get('catalog_schema').strip()
SFX = dbutils.widgets.get('source_suffix').strip()
assert VARIANT and VARIANT != SFX.lstrip('_'), 'pick a new variant name: the source tables must never be overwritten'
CFG = dict(min_tokens=int(dbutils.widgets.get('min_tokens')), target_tokens=int(dbutils.widgets.get('target_tokens')),
           max_tokens=int(dbutils.widgets.get('max_tokens')), max_chars=int(dbutils.widgets.get('max_chars')),
           overlap_ratio=float(dbutils.widgets.get('overlap_ratio')))
BOILERPLATE_MIN_DOCS = int(dbutils.widgets.get('boilerplate_min_docs'))
EMBED_PREFIX = dbutils.widgets.get('embed_prefix') == 'true'
DOC_CARDS = dbutils.widgets.get('doc_cards') == 'true'
CHUNK_CONTEXT = dbutils.widgets.get('chunk_context') == 'true'
ENRICH_MAX_DOCS = int(dbutils.widgets.get('enrich_max_docs') or 0)
LLM = dbutils.widgets.get('llm_endpoint').strip()
TARGET_TABLE = f'{CS}.chunks_{VARIANT}'
TARGET_INDEX = f'{CS}.chunks_index_{VARIANT}'
print(TARGET_TABLE, CFG, 'prefix' if EMBED_PREFIX else 'no prefix', 'doc_cards' if DOC_CARDS else '',
      'chunk_context' if CHUNK_CONTEXT else '')

# COMMAND ----------

# MAGIC %pip install -q tiktoken "databricks-sdk>=0.102"

# COMMAND ----------

# DBTITLE 1,The pipeline's own chunker
import hashlib, json, re, sys, time
from concurrent.futures import ThreadPoolExecutor

APP = dbutils.widgets.get('app_code_path').strip().rstrip('/')
sys.path.insert(0, f'{APP}/utils/parsing_pipeline')
import chunking  # noqa: E402

try:
    import tiktoken
    _ENC = tiktoken.get_encoding('cl100k_base')        # what the pipeline counts with (USE_TIKTOKEN)
    count_tokens = lambda t: len(_ENC.encode(t)) if t else 0
except Exception as exc:  # offline: same fallback as the pipeline
    print('tiktoken unavailable, chars/3.5:', exc)
    count_tokens = chunking.default_count_tokens


def normalize(text):
    # utils.normalize_text + the CLEAN_* placeholders removal of 3_Parse_Pipeline._build_chunks
    text = re.sub(r'\s*<!-- (image|formula-not-decoded) -->\s*', ' ', text or '')
    text = text.replace('\r\n', '\n').replace('\r', '\n').replace('\x00', '')
    text = re.sub(r'[ \t]+', ' ', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()

# COMMAND ----------

# DBTITLE 1,Documents: the ones of chunks_v1, their parsed text and metadata
from pyspark.sql import functions as F, Window

ckpt = (spark.table(f'{CS}._pipeline_checkpoint{SFX}').filter("parse_status = 'SUCCESS'")
        .withColumn('_rn', F.row_number().over(Window.partitionBy('IDDOC').orderBy(F.desc('ingestion_timestamp'))))
        .filter('_rn = 1').select('IDDOC', 'document_text', 'source_file_extension'))
in_index = spark.table(f'{CS}.chunks{SFX}').filter("chunk_content_type <> 'image'").select('IDDOC').distinct()
meta = (spark.table(f'{CS}.processed_files{SFX}')
        .select('IDDOC', 'ref', 'titre', 'type_document', F.col('indice').cast('string').alias('indice'),
                'division', 'niveau_plus_1', 'niveau_plus_2', 'doc_date').dropDuplicates(['IDDOC']))
docs = ckpt.join(in_index, 'IDDOC').join(meta, 'IDDOC', 'left').cache()
DOC_IDS = sorted(r['IDDOC'] for r in docs.select('IDDOC').collect())
print(len(DOC_IDS), 'documents')


def doc_batches(size=300):
    """Documents fetched in batches with collect(): a serverless (Spark Connect) result read row
    by row is dropped after a few minutes of client-side work (INVALID_HANDLE.OPERATION_ABANDONED)."""
    for i in range(0, len(DOC_IDS), size):
        yield from docs.filter(F.col('IDDOC').isin(DOC_IDS[i:i + size])).collect()

# COMMAND ----------

# DBTITLE 1,Text passages (driver, one document at a time)
SPREADSHEETS = ('xlsx', 'xls', 'xlsm')
rows, doc_info = [], {}
for d in doc_batches():
    text = normalize(d['document_text'])
    if not text:
        continue
    lang = chunking.detect_language(text, d['ref'] or '')
    prefix = chunking.source_prefix(d['ref'], d['titre'], d['type_document'], d['division'],
                                    d['niveau_plus_1'], d['doc_date']) if EMBED_PREFIX else ''
    chunks = chunking.chunk_markdown(text, count_tokens=count_tokens, **CFG)
    seen = set()
    kept = []
    for c in chunks:                                     # DEDUPLICATE_CHUNKS (intra-document)
        if c['chunk_text'] in seen:
            continue
        seen.add(c['chunk_text'])
        kept.append(c)
    if (d['source_file_extension'] or '').lower() in SPREADSHEETS:
        kept = kept[:100]                                # MAX_CHUNKS_SPREADSHEET
    doc_info[d['IDDOC']] = {'ref': d['ref'], 'lang': lang, 'text': text,
                            'chunks': [(c['chunk_index'], c['chunk_text'], c['metadata']) for c in kept]}
    for c in kept:
        body = c['chunk_text']
        rows.append({
            'IDDOC': d['IDDOC'], 'REF': d['ref'], 'division': d['division'],
            'chunk_id': f"{d['IDDOC']}-{c['chunk_index'] + 1:06d}", 'chunk_index': c['chunk_index'],
            'body': body, 'prefix': prefix, 'chunk_context': None,
            'chunk_content_type': c['chunk_content_type'],
            'semantic_headers': json.dumps(c['metadata'], ensure_ascii=False),
            'url': f"https://intraqual.lat.corp/intraqual_prod/identification.aspx?ref={d['ref']}",
            'doc_date': d['doc_date'], 'titre': d['titre'], 'type_document': d['type_document'],
            'indice': d['indice'], 'langue': lang,
            'body_sha256': hashlib.sha256(chunking.body_fingerprint(body).encode()).hexdigest(),
            'anchor_chunk_index': None,
        })
print(len(rows), 'text passages from', len(doc_info), 'documents')

# Same body in BOILERPLATE_MIN_DOCS+ documents -> "boilerplate" (3_Parse_Pipeline._dedupe_and_limit_chunks)
docs_per_body = {}
for r in rows:
    docs_per_body.setdefault(r['body_sha256'], set()).add(r['IDDOC'])
n_boiler = 0
for r in rows:
    if r['chunk_content_type'] in ('text', 'table', 'mixed') and len(docs_per_body[r['body_sha256']]) >= BOILERPLATE_MIN_DOCS:
        r['chunk_content_type'] = 'boilerplate'
        n_boiler += 1
print({t: sum(1 for r in rows if r['chunk_content_type'] == t) for t in ('toc', 'front_matter', 'boilerplate')})

# COMMAND ----------

# DBTITLE 1,Image passages from the stored descriptions (no LLM call)
max_idx = {}
for r in rows:
    max_idx[r['IDDOC']] = max(max_idx.get(r['IDDOC'], -1), r['chunk_index'])
images = (spark.table(f'{CS}.image_metadata{SFX}')
          .filter("status = 'DONE' AND description IS NOT NULL AND length(trim(description)) >= 40 "
                  "AND NOT upper(trim(description)) LIKE 'SKIP%'")
          .select('IDDOC', 'image_id', 'page_no', 'label', 'captions', 'context_text', 'description'))
n_img = 0
for im in images.collect():
    info = doc_info.get(im['IDDOC'])
    if not info or not info['chunks']:
        continue
    anchor = chunking.image_anchor(im['context_text'] or '', info['chunks'])
    headers = anchor[1] if anchor else {}
    parts = chunking.split_long_description(im['description'], max_chars=CFG['max_chars'],
                                            max_tokens=CFG['max_tokens'], count_tokens=count_tokens)
    for n, part in enumerate(parts, 1):
        body = chunking.image_passage_body(part, list(im['captions'] or []), headers)
        img_id = f"{im['IDDOC']}-IMG-{im['image_id']:03d}" + ('' if n == 1 else f'-{n}')
        rows.append({
            'IDDOC': im['IDDOC'], 'REF': info['ref'], 'division': None,
            'chunk_id': img_id, 'chunk_index': max_idx[im['IDDOC']] + 1 + im['image_id'],
            'body': body, 'prefix': None, 'chunk_context': None, 'chunk_content_type': 'image',
            'semantic_headers': json.dumps({'image_label': im['label'], 'page': str(im['page_no']),
                                            'section': ' > '.join(headers[k] for k in sorted(headers)) or None},
                                           ensure_ascii=False),
            'url': None, 'doc_date': None, 'titre': None, 'type_document': None, 'indice': None,
            'langue': info['lang'], 'body_sha256': hashlib.sha256(body.encode()).hexdigest(),
            'anchor_chunk_index': anchor[0] if anchor else None,
            '_image': (im['page_no'], im['label']),
        })
        n_img += 1
print(n_img, 'image passages')

# Image rows take the document's metadata and prefix (with "Image: page N, label", like 4_Describe).
first_row = {}
for r in rows:
    if r['chunk_content_type'] != 'image' and r['IDDOC'] not in first_row:
        first_row[r['IDDOC']] = r
for r in rows:
    if r['chunk_content_type'] == 'image':
        src = first_row[r['IDDOC']]
        for k in ('division', 'url', 'doc_date', 'titre', 'type_document', 'indice'):
            r[k] = src[k]
        page, label = r.pop('_image')
        if EMBED_PREFIX:
            r['prefix'] = src['prefix'].rstrip('\n').rstrip(']') + f' | Image: page {page}, {label}]\n\n'
        else:
            r['prefix'] = ''

# COMMAND ----------

# DBTITLE 1,Optional LLM enrichments (E1 document cards, E2 passage context) — OFF by default
from databricks.sdk import WorkspaceClient
import requests

w = WorkspaceClient()
HOST = w.config.host.rstrip('/')


def llm(prompt, max_tokens=2000):
    headers = {**w.config.authenticate(), 'Content-Type': 'application/json'}
    for attempt in range(3):
        try:
            r = requests.post(f'{HOST}/serving-endpoints/{LLM}/invocations', headers=headers, timeout=120,
                              json={'messages': [{'role': 'user', 'content': prompt}], 'max_tokens': max_tokens})
            r.raise_for_status()
            content = r.json()['choices'][0]['message']['content']
            if isinstance(content, list):
                content = ''.join(b.get('text', '') for b in content if isinstance(b, dict))
            return (content or '').strip()
        except Exception as exc:
            if attempt == 2:
                print('LLM failed:', str(exc)[:200])
                return ''
            time.sleep(5 * (attempt + 1))


CARD_PROMPT = """Tu prépares la fiche d'identification d'un document qualité Latécoère pour un moteur de recherche.
À partir du début du document ci-dessous, écris en français, en 120 à 200 mots, sans inventer :
- Objet : à quoi sert le document (1 phrase) ;
- Domaine d'application : sites, divisions, produits, activités concernés ;
- Sujets traités : les thèmes principaux, avec leurs termes techniques et acronymes (développés si le document le fait) ;
- Rôles cités ; documents de référence cités (REF, normes).
Réponds seulement par la fiche.

Document {ref} — {titre} :
{text}"""

CONTEXT_PROMPT = """Voici un document qualité Latécoère, puis un passage de ce document.
Écris une ou deux phrases courtes, en français, qui situent ce passage dans le document (de quel processus,
section ou cas il parle) pour améliorer sa recherche. Réponds seulement par ces phrases.

<document ref="{ref}" titre="{titre}">
{text}
</document>

<passage>
{passage}
</passage>"""

enrich_docs = list(doc_info)[:ENRICH_MAX_DOCS] if ENRICH_MAX_DOCS else list(doc_info)

if DOC_CARDS:
    def card(iddoc):
        info, src = doc_info[iddoc], first_row.get(iddoc)
        if not src:
            return None
        out = llm(CARD_PROMPT.format(ref=info['ref'], titre=src['titre'] or '', text=info['text'][:24000]))
        if not out:
            return None
        return {**{k: src[k] for k in ('IDDOC', 'REF', 'division', 'url', 'doc_date', 'titre', 'type_document',
                                       'indice', 'langue', 'prefix')},
                'chunk_id': f'{iddoc}-CARD', 'chunk_index': -2, 'body': 'Fiche du document\n\n' + out,
                'chunk_context': None, 'chunk_content_type': 'doc_card', 'semantic_headers': '{}',
                'body_sha256': hashlib.sha256(out.encode()).hexdigest(), 'anchor_chunk_index': None}
    with ThreadPoolExecutor(8) as pool:
        cards = [c for c in pool.map(card, enrich_docs) if c]
    rows.extend(cards)
    print(len(cards), 'document cards')

if CHUNK_CONTEXT:
    wanted = set(enrich_docs)
    targets = [r for r in rows if r['IDDOC'] in wanted and r['chunk_content_type'] in ('text', 'table', 'mixed')]

    def context(r):
        info = doc_info[r['IDDOC']]
        r['chunk_context'] = llm(CONTEXT_PROMPT.format(ref=info['ref'], titre=r['titre'] or '',
                                                       text=info['text'][:20000], passage=r['body'][:4000]),
                                 max_tokens=1500) or None
    with ThreadPoolExecutor(8) as pool:
        list(pool.map(context, targets))
    print(sum(1 for r in targets if r['chunk_context']), 'passages with a context line')

# COMMAND ----------

# DBTITLE 1,Write chunks_<variant>
import pandas as pd

for r in rows:
    ctx = f"Contexte : {r['chunk_context']}\n\n" if r.get('chunk_context') else ''
    r['chunk_text'] = (r['prefix'] or '') + ctx + r['body']
    r['chunk_token_count'] = count_tokens(r['chunk_text'])
    r['chunk_sha256'] = hashlib.sha256(r['chunk_text'].encode()).hexdigest()

cols = ['IDDOC', 'REF', 'division', 'chunk_id', 'chunk_index', 'chunk_text', 'chunk_token_count',
        'chunk_content_type', 'semantic_headers', 'chunk_sha256', 'url', 'doc_date', 'titre', 'type_document',
        'indice', 'langue', 'body_sha256', 'anchor_chunk_index', 'chunk_context']
pdf = pd.DataFrame([{k: r.get(k) for k in cols} for r in rows])
pdf['anchor_chunk_index'] = pdf['anchor_chunk_index'].astype('Int64')
schema = ('IDDOC long, REF string, division string, chunk_id string, chunk_index int, chunk_text string, '
          'chunk_token_count int, chunk_content_type string, semantic_headers string, chunk_sha256 string, '
          'url string, doc_date date, titre string, type_document string, indice string, langue string, '
          'body_sha256 string, anchor_chunk_index int, chunk_context string')
df = spark.createDataFrame(pdf.astype(object).where(pdf.notna(), None), schema=schema).dropDuplicates(['chunk_id'])
df.write.mode('overwrite').option('overwriteSchema', 'true').saveAsTable(TARGET_TABLE)
spark.sql(f"ALTER TABLE {TARGET_TABLE} SET TBLPROPERTIES (delta.enableChangeDataFeed = true)")
display(spark.sql(f"""
SELECT chunk_content_type, count(*) AS passages, count(DISTINCT IDDOC) AS documents,
       percentile(length(chunk_text), 0.5) AS p50_chars, percentile(length(chunk_text), 0.9) AS p90_chars,
       max(length(chunk_text)) AS max_chars,
       round(avg(CASE WHEN length(chunk_text) > 2000 THEN 1 ELSE 0 END) * 100, 1) AS pct_over_2000_chars
FROM {TARGET_TABLE} GROUP BY ALL ORDER BY passages DESC"""))

# COMMAND ----------

# DBTITLE 1,Vector Search index chunks_index_<variant> (created, or synced if it exists)
from databricks.sdk.errors import NotFound
from databricks.sdk.service.vectorsearch import (DeltaSyncVectorIndexSpecRequest, EmbeddingSourceColumn,
                                                 PipelineType, VectorIndexType)

if dbutils.widgets.get('create_index') == 'true':
    try:
        w.vector_search_indexes.get_index(index_name=TARGET_INDEX)
        w.vector_search_indexes.sync_index(index_name=TARGET_INDEX)
        print('sync started:', TARGET_INDEX)
    except NotFound:
        w.vector_search_indexes.create_index(
            name=TARGET_INDEX, endpoint_name=dbutils.widgets.get('vector_search_endpoint').strip(),
            primary_key='chunk_id', index_type=VectorIndexType.DELTA_SYNC,
            delta_sync_index_spec=DeltaSyncVectorIndexSpecRequest(
                source_table=TARGET_TABLE, pipeline_type=PipelineType.TRIGGERED,
                embedding_source_columns=[EmbeddingSourceColumn(
                    name='chunk_text', embedding_model_endpoint_name=dbutils.widgets.get('embedding_model').strip())]))
        print('created:', TARGET_INDEX, '— first sync embeds every passage, allow 30–60 min')
    print('When the index is ONLINE: retrieval_eval with index_variants =', VARIANT)
