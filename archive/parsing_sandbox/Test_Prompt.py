# Databricks notebook source
# DBTITLE 1,Header
# MAGIC %md
# MAGIC # 04 — Test / validation du prompt de description d'images
# MAGIC
# MAGIC Banc d'essai **reproductible** pour juger la qualité du prompt vision SANS re-parser.
# MAGIC Il rejoue le **vrai chemin de production** (`build_llm_prompt` + `describe_all_images`
# MAGIC depuis `image_utils.py`, prompt depuis `config.py`) sur un échantillon d'images
# MAGIC déjà présentes dans `image_metadata`, puis affiche :
# MAGIC
# MAGIC - métriques : catégories, recouvrement description↔contexte (copie de texte),
# MAGIC   fuite de template, SKIP, taux indexé après post-filtre ;
# MAGIC - **tokens et coût estimé par image** ;
# MAGIC - galerie image + description.
# MAGIC
# MAGIC **N'écrit aucune table** (lecture seule). Pour tester une variante de prompt :
# MAGIC modifier `IMAGE_DESCRIPTION_PROMPT` dans `config.py` et relancer.

# COMMAND ----------

# DBTITLE 1,Dépendances
# MAGIC %pip install -q openai tenacity nest_asyncio
# MAGIC %load_ext autoreload
# MAGIC %autoreload 2

# COMMAND ----------

# DBTITLE 1,Imports, config et paramètres
import os, sys, re, time, base64, asyncio
from collections import Counter

_ctx = dbutils.notebook.entry_point.getDbutils().notebook().getContext()
REPO_DIR = "/Workspace" + os.path.dirname(_ctx.notebookPath().get())
sys.path.insert(0, REPO_DIR)

from pyspark.sql import functions as F
import image_utils
from image_utils import describe_all_images
from config import (
    TARGET_IMAGE_METADATA_TABLE, LLM_MODEL_ENDPOINT, LLM_MAX_TOKENS, LLM_TEMPERATURE,
    LLM_MAX_RETRIES, LLM_MAX_CONCURRENT, MIN_INDEXABLE_DESC_CHARS,
)

# ---------------------------------------------------------------------------
# Paramètres du test
# ---------------------------------------------------------------------------
SOURCE_IMAGE_TABLE = TARGET_IMAGE_METADATA_TABLE
N_SAMPLE   = 50          # nombre d'images à tester
SEED       = 7           # graine d'échantillonnage (changer pour un autre tirage)
TEST_IDDOCS = []         # [] = tout le corpus ; sinon restreindre à ces IDDOCs
ONLY_STATUS = "PENDING"  # "PENDING" / "DONE" / None (= tous statuts décrits/à décrire)

# Tarifs publics OpenAI gpt-5-nano (à ajuster — la facturation Databricks peut différer,
# cf. system.billing.usage). $ / token.
PRICE_IN_PER_TOKEN  = 0.05 / 1_000_000
PRICE_OUT_PER_TOKEN = 0.40 / 1_000_000

print(f"Modèle={LLM_MODEL_ENDPOINT} | table={SOURCE_IMAGE_TABLE} | N={N_SAMPLE} seed={SEED}")

# COMMAND ----------

# DBTITLE 1,Échantillonnage des images
df = spark.table(SOURCE_IMAGE_TABLE).filter(F.col("volume_path").isNotNull())
if ONLY_STATUS:
    df = df.filter(F.col("status") == ONLY_STATUS)
if TEST_IDDOCS:
    df = df.filter(F.col("IDDOC").isin([int(x) for x in TEST_IDDOCS]))

df_sample = df.orderBy(F.rand(SEED)).limit(N_SAMPLE)
rows = [r.asDict() for r in df_sample.collect()]
print(f"{len(rows)} image(s) échantillonnée(s).")
assert rows, "Aucune image trouvée — vérifier ONLY_STATUS / TEST_IDDOCS."

# COMMAND ----------

# DBTITLE 1,Description LLM (vrai chemin de prod, lecture seule)
import nest_asyncio
nest_asyncio.apply()

try:
    WS_TOKEN = dbutils.secrets.get(scope="qualibot", key="serving_token")
except Exception:
    WS_TOKEN = _ctx.apiToken().get()
WS_HOST = spark.conf.get("spark.databricks.workspaceUrl")

t0 = time.time()
described = asyncio.run(describe_all_images(
    rows=rows, ws_host=WS_HOST, ws_token=WS_TOKEN,
    model=LLM_MODEL_ENDPOINT, max_tokens=LLM_MAX_TOKENS, temperature=LLM_TEMPERATURE,
    max_retries=LLM_MAX_RETRIES, max_concurrent=LLM_MAX_CONCURRENT,
))
print(f"{len(described)} descriptions en {time.time()-t0:.0f}s")

# COMMAND ----------

# DBTITLE 1,Métriques de qualité + coût
_WORD = re.compile(r"[a-zàâçéèêëîïôûùüœ0-9]{4,}", re.I)

def _overlap(desc, ctx):
    dw = set(_WORD.findall((desc or "").lower()))
    cw = set(_WORD.findall((ctx or "").lower()))
    return len(dw & cw) / len(dw) if dw else 0.0

def _category(desc):
    t = (desc or "").strip()
    if t.upper().startswith("SKIP"):
        return "SKIP"
    m = re.match(r"^#\s*\[?([A-Z_]+)", t)
    return m.group(1) if m else "(none)"

def _leak(desc):
    return bool(re.search(r"<[a-z].*?>|placeholder|angle bracket", desc or "", re.I))

def _indexable(desc, status):
    return (status == "DONE"
            and desc is not None
            and not desc.strip().upper().startswith("SKIP")
            and len(desc.strip()) >= MIN_INDEXABLE_DESC_CHARS)

n = len(described)
cats = Counter(_category(r["description"]) for r in described)
leaks = sum(_leak(r["description"]) for r in described)
skips = sum(1 for r in described if r["status"] == "SKIPPED" or _category(r["description"]) == "SKIP")
nonefmt = cats.get("(none)", 0)
overlaps = [_overlap(r["description"], r.get("context_text", "")) for r in described]
indexed = [r for r in described if _indexable(r["description"], r["status"])]
lens = [len(r["description"] or "") for r in indexed]
tin = [r.get("input_tokens", 0) or 0 for r in described]
tout = [r.get("output_tokens", 0) or 0 for r in described]
avg = lambda xs: round(sum(xs) / len(xs), 3) if xs else 0

ai, ao = avg(tin), avg(tout)
cost_per = ai * PRICE_IN_PER_TOKEN + ao * PRICE_OUT_PER_TOKEN

print(f"=== {n} images — modèle {LLM_MODEL_ENDPOINT} ===")
print(f"catégories          : {dict(cats)}")
print(f"format cassé (none) : {nonefmt}")
print(f"fuite de template   : {leaks}")
print(f"SKIP (déchet écarté): {skips}")
print(f"indexées (post-filtre): {len(indexed)}/{n} ({round(100*len(indexed)/n)}%)")
print(f"recouvrement ctx moy: {avg(overlaps)}  (plus bas = moins de copie de contexte)")
print(f"longueur moy indexées: {round(sum(lens)/max(1,len(lens)))} caractères")
print(f"\ntokens/image        : {round(ai)} in / {round(ao)} out")
print(f"coût/image décrite  : ${cost_per:.6f}  (1000 → ${cost_per*1000:.3f})")
print(f"⚠️ tarif OpenAI public — la facturation Databricks réelle peut différer (system.billing.usage).")

# COMMAND ----------

# DBTITLE 1,Galerie image + description
def _b64(path):
    try:
        with open(path, "rb") as f:
            return base64.b64encode(f.read()).decode("utf-8")
    except Exception:
        return None

def _mime(path):
    return "image/png" if (path or "").lower().endswith(".png") else "image/jpeg"

def _esc(s):
    return (s or "—").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

# Tri : d'abord les indexées, puis SKIP/none — pour repérer vite les cas douteux.
order = {"ok": 0, "skip": 1}
described_sorted = sorted(
    described,
    key=lambda r: order.get("ok" if _indexable(r["description"], r["status"]) else "skip", 2)
)

html = ["""
<style>
.g { font-family:-apple-system,'Segoe UI',Roboto,sans-serif; max-width:1100px; }
.c { border:1px solid #e0e0e0; border-radius:10px; margin:14px 0; padding:14px; display:flex; gap:20px; background:#fafafa; }
.c img { max-width:380px; max-height:300px; object-fit:contain; border:1px solid #ddd; border-radius:6px; background:#fff; flex-shrink:0; }
.c .no { width:200px; height:140px; background:#f0f0f0; border-radius:6px; display:flex; align-items:center; justify-content:center; color:#999; }
.c .m { flex:1; min-width:0; }
.c .d { font-size:13px; color:#222; line-height:1.55; white-space:pre-wrap; background:#fff; border:1px solid #e8e8e8; border-radius:6px; padding:10px; max-height:300px; overflow-y:auto; }
.c .i { font-size:11px; color:#888; margin-top:8px; display:flex; flex-wrap:wrap; gap:6px; }
.c .i .t { background:#eef; border-radius:4px; padding:2px 7px; }
.skip { opacity:.6; }
.skip .d { background:#fff7f0; }
</style>
<div class="g">
"""]

for r in described_sorted:
    desc = r["description"] or ""
    is_idx = _indexable(desc, r["status"])
    b64 = _b64(r["volume_path"])
    img = (f'<img src="data:{_mime(r["volume_path"])};base64,{b64}"/>'
           if b64 else '<div class="no">🖼️ introuvable</div>')
    cls = "c" if is_idx else "c skip"
    badge = "✅ indexée" if is_idx else f"⏭️ {r['status']}"
    html.append(f"""
    <div class="{cls}">
        {img}
        <div class="m">
            <h3 style="margin:0 0 8px;font-size:14px;">IDDOC {r.get('IDDOC')} · img {r.get('image_id')} · {badge}</h3>
            <div class="d">{_esc(desc)}</div>
            <div class="i">
                <span class="t">📐 {r.get('image_width')}×{r.get('image_height')}</span>
                <span class="t">🏷️ {_esc(r.get('label'))}</span>
                <span class="t">🔤 {r.get('input_tokens') or 0} in / {r.get('output_tokens') or 0} out</span>
                <span class="t">cat: {_category(desc)}</span>
            </div>
        </div>
    </div>
    """)
html.append("</div>")
displayHTML("".join(html))

# COMMAND ----------

# DBTITLE 1,Résumé machine-lisible (pour un run déclenché via l'API Jobs)
# ---------------------------------------------------------------------------
# dbutils.notebook.exit() est le seul moyen de récupérer un résultat via
# `jobs get-run-output` pour un run non-interactif (les print() de ce
# notebook ne sont pas capturés par l'API pour une tâche notebook classique).
#
# Inclut aussi le contexte (context_text) reçu par chaque image et sa
# longueur — pour vérifier concrètement que le texte autour de l'image est
# bien transmis au prompt (et pas juste supposé l'être), et quelques
# exemples complets (contexte + description) pour un contrôle qualité
# manuel, pas seulement des agrégats.
# ---------------------------------------------------------------------------
import json as _json

ctx_lens = [len((r.get("context_text") or "").strip()) for r in described]
n_empty_ctx = sum(1 for c in ctx_lens if c < 20)
desc_lens_indexed = [len((r["description"] or "").strip()) for r in indexed]

_examples = []
for r in described[:15]:
    _examples.append({
        "IDDOC": r.get("IDDOC"), "image_id": r.get("image_id"), "label": r.get("label"),
        "status": r.get("status"), "category": _category(r["description"]),
        "context_chars": len((r.get("context_text") or "").strip()),
        "context_preview": (r.get("context_text") or "")[:300],
        "description_preview": (r.get("description") or "")[:500],
    })

_summary = {
    "n_sampled": n,
    "categories": dict(cats),
    "template_leaks": leaks,
    "skipped": skips,
    "broken_format": nonefmt,
    "n_indexed": len(indexed),
    "pct_indexed": round(100 * len(indexed) / n, 1) if n else 0,
    "avg_context_overlap": avg(overlaps),
    "avg_input_tokens": ai,
    "avg_output_tokens": ao,
    "cost_per_image_usd": round(cost_per, 6),
    "pct_images_with_empty_context": round(100 * n_empty_ctx / n, 1) if n else 0,
    "avg_context_chars": round(sum(ctx_lens) / len(ctx_lens), 1) if ctx_lens else 0,
    "avg_indexed_desc_chars": round(sum(desc_lens_indexed) / len(desc_lens_indexed), 1) if desc_lens_indexed else 0,
    "min_indexed_desc_chars": min(desc_lens_indexed) if desc_lens_indexed else 0,
    "examples": _examples,
}
print("SUMMARY_JSON:" + _json.dumps(_summary))
dbutils.notebook.exit(_json.dumps(_summary))
