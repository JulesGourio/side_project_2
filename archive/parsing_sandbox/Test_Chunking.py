# Databricks notebook source
# DBTITLE 1,Header
# MAGIC %md
# MAGIC # 05 — Test / comparaison des stratégies de chunking
# MAGIC
# MAGIC Banc d'essai **lecture seule** pour comparer, sur quelques IDDOCs, plusieurs
# MAGIC stratégies de découpage et mesurer leur impact retrieval via l'embedding Qwen.
# MAGIC
# MAGIC Stratégies comparées :
# MAGIC 1. **fallback + préfixe** (= production actuelle) — regex markdown, `[Source:…]` embeddé
# MAGIC 2. **fallback sans préfixe** — corps seul
# MAGIC 3. **sémantique (HybridChunker)** — chunking Docling structuré (titres/tables)
# MAGIC
# MAGIC Métriques : nb chunks, tokens moyens, **similarité inter-chunks** (plus bas = chunks
# MAGIC plus distinguables au retrieval), et une **question « sur une source »**.
# MAGIC
# MAGIC Réglages testés depuis `config.py` : `USE_TIKTOKEN`, `CHUNK_OVERLAP_RATIO`. N'écrit aucune table.

# COMMAND ----------

# MAGIC %pip install -q "numpy<2" docling docling-core langchain-text-splitters tiktoken striprtf odfpy xlrd openpyxl lxml olefile openai
# MAGIC %load_ext autoreload
# MAGIC %autoreload 2

# COMMAND ----------

# DBTITLE 1,Imports, config, sélection des fichiers de test
import os, sys, re, json, importlib
import numpy as np

_ctx = dbutils.notebook.entry_point.getDbutils().notebook().getContext()
REPO_DIR = "/Workspace" + os.path.dirname(_ctx.notebookPath().get())
sys.path.insert(0, REPO_DIR)

from pyspark.sql import functions as F
import utils, image_utils, selection
from utils import configure, parse_with_docling, chunk_document, count_tokens
from config import *

# Dev : tables Intraqual suffixées _stack
_src = INTRAQUAL_SOURCE_CATALOG_SCHEMA
selection.GD_DOC_CAT_TABLE     = f"{_src}.gd_doc_cat_stack"
selection.GD_CAT_TABLE         = f"{_src}.gd_cat_stack"
selection.GD_TYPDOC_TABLE      = f"{_src}.gd_typdoc_stack"
selection.GD_UTILISATEUR_TABLE = f"{_src}.gd_utilisateur_stack"

# ============================================================================
# INTERRUPTEUR — préfixe de provenance dans le texte embeddé (chunk_text)
#   True  : "[Source: ref | Title | Division | Category]" inclus  (= prod actuelle)
#   False : corps seul ; une question "sur une source" se scope par FILTRE
#           métadonnée (REF/division) — vérifié : le filtre fonctionne sur l'index.
# Surcharge le défaut de config.py POUR CE NOTEBOOK. Bascule et relance pour comparer.
# (La cellule "question sur une source" en bas montre toujours les deux modes.)
# ============================================================================
EMBED_SOURCE_PREFIX = True
print("EMBED_SOURCE_PREFIX =", EMBED_SOURCE_PREFIX)

# ⚠️ Choisir des IDDOCs AS (métadonnées présentes en dev). Mix texte/tables idéal.
TEST_IDDOCS = [11147, 10638, 2073]

ANTIWORD_BIN  = f"{REPO_DIR}/{ANTIWORD_BIN_RELATIVE}"
configure(
    OFFLINE_MODELS_DIR=OFFLINE_MODELS_DIR, VOLUME_BASE_PATH=VOLUME_BASE_PATH,
    ANTIWORD_BIN=ANTIWORD_BIN, ANTIWORD_SHARE_DIR=f"{REPO_DIR}/{ANTIWORD_SHARE_RELATIVE}",
    USE_GPU=USE_GPU, DO_OCR=DO_OCR, GPU_OCR_FALLBACK=GPU_OCR_FALLBACK,
    TABLE_STRUCTURE_MODE=TABLE_STRUCTURE_MODE, GENERATE_PICTURE_IMAGES=False,
    USE_TIKTOKEN=USE_TIKTOKEN, CHARS_PER_TOKEN=CHARS_PER_TOKEN,
    MIN_CHUNK_TOKENS=MIN_CHUNK_TOKENS, TARGET_CHUNK_TOKENS=TARGET_CHUNK_TOKENS,
    MAX_CHUNK_TOKENS=MAX_CHUNK_TOKENS, CHUNK_OVERLAP_RATIO=CHUNK_OVERLAP_RATIO,
)

df_bm, _, _ = selection.load_business_metadata(spark)
df_meta, df_content = selection.scan_volume_files(spark, VOLUME_ROOT_PATH)
df_sel = selection.select_best_files(selection.rank_candidates(df_meta, df_bm), df_bm, parse_filter=TEST_IDDOCS)
df_files = selection.attach_content(df_sel, df_content, "test-chunking")
files = df_files.select("IDDOC","ref","titre","division","niveau_plus_1","source_file_extension","content").collect()
print(f"{len(files)} fichier(s) pour {TEST_IDDOCS}")

# COMMAND ----------

# DBTITLE 1,Embedding helper (Qwen)
from openai import OpenAI
try:
    WS_TOKEN = dbutils.secrets.get(scope="qualibot", key="serving_token")
except Exception:
    WS_TOKEN = _ctx.apiToken().get()
WS_HOST = spark.conf.get("spark.databricks.workspaceUrl")
_emb = OpenAI(api_key=WS_TOKEN, base_url=f"https://{WS_HOST}/serving-endpoints")

def embed(texts):
    out = []
    for i in range(0, len(texts), 32):
        out += [d.embedding for d in _emb.embeddings.create(
            model="databricks-qwen3-embedding-0-6b", input=texts[i:i+32]).data]
    a = np.array(out, float); a /= (np.linalg.norm(a, axis=1, keepdims=True) + 1e-9)
    return a

def intra_sim(vecs):
    if len(vecs) < 2: return float("nan")
    s = vecs @ vecs.T; n = len(vecs)
    return float((s.sum() - np.trace(s)) / (n * (n - 1)))

def add_prefix(ch_text, f, force=None):
    """Ajoute le préfixe de provenance selon EMBED_SOURCE_PREFIX (ou force=True/False)."""
    on = EMBED_SOURCE_PREFIX if force is None else force
    if not on:
        return ch_text
    return (f"[Source: {f['ref'] or ''} | Title: {f['titre'] or ''} | "
            f"Division: {f['division'] or ''} | Category: {f['niveau_plus_1'] or ''}]\n\n{ch_text}")

# COMMAND ----------

# DBTITLE 1,Parse + chunking 3 façons + métriques
rows = []
for f in files:
    ext = f["source_file_extension"]
    res = parse_with_docling(bytes(f["content"]), str(ext))
    doc = res.pop("_docling_doc", None)
    text = res.get("text") or ""
    if not text.strip():
        print(f"IDDOC {f['IDDOC']}: texte vide, skip"); continue

    _pfx = "avec prefixe" if EMBED_SOURCE_PREFIX else "sans prefixe"
    strategies = {
        f"fallback ({_pfx})":           [add_prefix(c["chunk_text"], f) for c in chunk_document(text, docling_doc=None)],
        f"semantique HybridChunker ({_pfx})": [add_prefix(c["chunk_text"], f) for c in (chunk_document(text, docling_doc=doc) if doc else [])],
    }
    for name, chunks in strategies.items():
        chunks = [c for c in chunks if c.strip()]
        if not chunks: continue
        vecs = embed(chunks)
        rows.append({
            "IDDOC": f["IDDOC"], "strategie": name, "n_chunks": len(chunks),
            "tok_moy": round(sum(count_tokens(c) for c in chunks)/len(chunks)),
            "sim_intra": round(intra_sim(vecs), 3),
        })

import pandas as pd
dfres = pd.DataFrame(rows)
print(dfres.to_string(index=False))
print("\nsim_intra : similarité moyenne entre chunks d'un même doc — PLUS BAS = MIEUX (chunks distinguables).")

# COMMAND ----------

# DBTITLE 1,Question « sur une source » : prefixe vs filtrage métadonnée
# Démontre que le préfixe aide les requêtes titrées MAIS qu'un filtre métadonnée fait mieux.
f = files[0]
res = parse_with_docling(bytes(f["content"]), str(f["source_file_extension"]))
text = res.get("text") or ""
bodies = [c["chunk_text"] for c in chunk_document(text, docling_doc=None) if c["chunk_text"].strip()]
withp  = [add_prefix(b, f, force=True) for b in bodies]   # force=True : toujours montrer le contraste
# distracteurs : chunks d'autres docs déjà indexés
dist = [r[0] for r in spark.sql(
    f"SELECT chunk_text FROM {TARGET_CHUNK_TABLE} WHERE chunk_id NOT LIKE '%-IMG-%' "
    f"AND IDDOC != {f['IDDOC']} ORDER BY rand(1) LIMIT 100").collect()]
query = f"Que dit le document « {f['titre']} » ({f['ref']}) ?"
print("Requête:", query)
eq = embed([query])[0]
for name, corpus in [("AVEC prefixe", withp + dist), ("SANS prefixe (corps)", bodies + dist)]:
    n_tgt = len(withp) if "AVEC" in name else len(bodies)
    sims = embed(corpus) @ eq
    top10_tgt = int(np.sum(np.argsort(-sims)[:10] < n_tgt))
    print(f"  {name:22} | chunks du bon doc dans top-10 : {top10_tgt}/10")
print("→ Conclusion : pour cibler une source, préférer un FILTRE métadonnée (ref/division) côté retrieval,")
print("  plutôt que de polluer l'embedding de tous les chunks avec le préfixe.")
