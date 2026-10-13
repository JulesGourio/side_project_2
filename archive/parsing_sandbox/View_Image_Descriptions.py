# Databricks notebook source
# /// script
# [tool.databricks.environment]
# environment_version = "5"
# ///
# DBTITLE 1,Header
# MAGIC %md
# MAGIC # 03 — Test Pipeline : Parse → LLM → Affichage
# MAGIC
# MAGIC Pipeline de test autonome et **complètement isolé** de la production.
# MAGIC Exécute le cycle complet sur un petit sous-ensemble d’IDDOCs choisis.
# MAGIC
# MAGIC **Étapes :**
# MAGIC 1. Parse les fichiers via Docling (texte + extraction d’images)
# MAGIC 2. Construit `processed_files_test`, `chunks_all_test`, `image_metadata_test`
# MAGIC 3. Décrit les images avec le Vision LLM
# MAGIC 4. Affiche les chunks générés en carte HTML
# MAGIC 5. Affiche les images avec leurs descriptions en galerie HTML
# MAGIC
# MAGIC **Isolation :** toutes les tables écrites ont le suffixe `_test`.
# MAGIC Le volume images utilise un sous-dossier `_test_pipeline` séparé.
# MAGIC Aucune table ni volume de production n’est touché.
# MAGIC
# MAGIC **À faire avant de lancer :** remplir `TEST_IDDOCS` dans la cellule suivante.
# MAGIC
# MAGIC (Le rebuild de `image_metadata` de PROD depuis le checkpoint — qui vivait ici — a
# MAGIC été déplacé dans `Rebuild_Image_Metadata.py`, pour que ce notebook tienne
# MAGIC réellement sa promesse d'isolation.)

# COMMAND ----------

# DBTITLE 1,Installation des dépendances
# MAGIC %pip install -q "numpy<2" docling docling-core langchain-text-splitters tiktoken striprtf odfpy xlrd openpyxl lxml olefile openai tenacity nest_asyncio
# MAGIC %load_ext autoreload
# MAGIC %autoreload 2

# COMMAND ----------

# DBTITLE 1,Imports, config et paramètres de test
import os, sys, uuid, time, asyncio, base64, shutil, importlib
from datetime import datetime

# Derive repo dir from notebook path
_ctx = dbutils.notebook.entry_point.getDbutils().notebook().getContext()
REPO_DIR = "/Workspace" + os.path.dirname(_ctx.notebookPath().get())
sys.path.insert(0, REPO_DIR)

from pyspark.sql import functions as F, Row
import utils, image_utils, selection
from utils import configure, broadcast_config, write_worker_config
from config import *

# ---------------------------------------------------------------------------
# Dev env : les tables Intraqual sont suffixées _stack (Lakeflow Connect).
# On patche les constantes de module après import pour pointer vers les bons noms.
# ---------------------------------------------------------------------------
_src = INTRAQUAL_SOURCE_CATALOG_SCHEMA
selection.GD_DOC_CAT_TABLE      = f"{_src}.gd_doc_cat_stack"
selection.GD_CAT_TABLE          = f"{_src}.gd_cat_stack"
selection.GD_TYPDOC_TABLE       = f"{_src}.gd_typdoc_stack"
selection.GD_UTILISATEUR_TABLE  = f"{_src}.gd_utilisateur_stack"
print(f"[PATCH] Tables Intraqual mappées vers suffixe _stack dans {_src}")

# ---------------------------------------------------------------------------
# ⚠️  MODIFIER ICI : IDDOCs à tester
# Choisir des IDDOCs connus pour avoir des images (vérifier dans image_metadata)
# ---------------------------------------------------------------------------
# 5 IDDOCs retenus (mix AS/IS, 5–9 images chacun — 36 appels LLM au total)
# AS: 2073  (Q0396GO,    5 img) — Instrumentation extensométrique
# AS: 11147 (MI-13773,   7 img) — Plan de développement du matériel
# AS: 10638 (MM-1039,    9 img) — Méthodes justification composite
# IS: 20652 (INAQ595_FR, 6 img) — Accès tables élévatrices
# IS: 25243 (LATQSE_FR,  9 img) — Manuel qualité sécurité environnement
TEST_IDDOCS = [23312]

assert TEST_IDDOCS, (
    "❌ TEST_IDDOCS est vide.\n"
    "Remplir la liste avec des IDDOCs ayant des images connues."
)

# ---------------------------------------------------------------------------
# Tables de test — suffixe _test, même catalog/schema que la prod
# ---------------------------------------------------------------------------
TEST_CHECKPOINT      = f"{CATALOG_SCHEMA}._pipeline_checkpoint_test"
TEST_PROCESSED_FILES = f"{CATALOG_SCHEMA}.processed_files_test"
TEST_CHUNKS_ALL      = f"{CATALOG_SCHEMA}.chunks_all_test"
TEST_IMAGE_METADATA  = f"{CATALOG_SCHEMA}.image_metadata_test"

# Volume images de test (sous-dossier séparé dans le même volume)
TEST_VOLUME_BASE_PATH = VOLUME_BASE_PATH.rstrip("/") + "/_test_pipeline"

INGESTION_RUN_ID = str(uuid.uuid4())

print("Tables de test :")
for t in [TEST_CHECKPOINT, TEST_PROCESSED_FILES, TEST_CHUNKS_ALL, TEST_IMAGE_METADATA]:
    print(f"  {t}")
print(f"\nVolume images test : {TEST_VOLUME_BASE_PATH}")
print(f"IDDOCs testés      : {TEST_IDDOCS}")

# COMMAND ----------

# DBTITLE 1,Configuration Spark et workers
ANTIWORD_BIN       = f"{REPO_DIR}/{ANTIWORD_BIN_RELATIVE}"
ANTIWORD_SHARE_DIR = f"{REPO_DIR}/{ANTIWORD_SHARE_RELATIVE}"

configure(
    OFFLINE_MODELS_DIR=OFFLINE_MODELS_DIR, VOLUME_BASE_PATH=TEST_VOLUME_BASE_PATH,
    ANTIWORD_BIN=ANTIWORD_BIN, ANTIWORD_SHARE_DIR=ANTIWORD_SHARE_DIR,
    WORKER_CONFIG_JSON=os.path.join(TEST_VOLUME_BASE_PATH, "_parsing_config.json"),
    USE_GPU=USE_GPU, DO_OCR=DO_OCR, GPU_OCR_FALLBACK=GPU_OCR_FALLBACK,
    TABLE_STRUCTURE_MODE=TABLE_STRUCTURE_MODE, GENERATE_PICTURE_IMAGES=GENERATE_PICTURE_IMAGES,
    IMAGE_SCALE=IMAGE_SCALE, MIN_AREA_RATIO=MIN_AREA_RATIO, MAX_REPEAT=MAX_REPEAT,
    USE_TIKTOKEN=USE_TIKTOKEN, CHARS_PER_TOKEN=CHARS_PER_TOKEN,
    MIN_CHUNK_TOKENS=MIN_CHUNK_TOKENS, TARGET_CHUNK_TOKENS=TARGET_CHUNK_TOKENS,
    MAX_CHUNK_TOKENS=MAX_CHUNK_TOKENS,
    LLM_MODEL_ENDPOINT=LLM_MODEL_ENDPOINT, LLM_MAX_TOKENS=LLM_MAX_TOKENS,
    LLM_TEMPERATURE=LLM_TEMPERATURE, LLM_MAX_RETRIES=LLM_MAX_RETRIES,
    LLM_MAX_CONCURRENT=LLM_MAX_CONCURRENT,
)

try:
    _ = spark.sparkContext
    broadcast_config(spark)
    print("✅ Config broadcast aux workers")
except Exception:
    print("⚠️  Cluster partagé — config JSON seulement")

write_worker_config(spark)
print(f"Run ID : {INGESTION_RUN_ID}")

# COMMAND ----------

# DBTITLE 1,Étape 1 — Sélection des fichiers
# Sélection des fichiers pour les TEST_IDDOCS
df_business_meta, df_doc_lookup, df_kb_lookup = selection.load_business_metadata(spark)
df_meta_raw, df_content = selection.scan_volume_files(spark, VOLUME_ROOT_PATH)
df_matched_full = selection.rank_candidates(df_meta_raw, df_business_meta)

df_selected = selection.select_best_files(
    df_matched_full, df_business_meta, parse_filter=TEST_IDDOCS
)
df_files = selection.attach_content(df_selected, df_content, INGESTION_RUN_ID)

num_test_files = df_files.count()
print(f"{num_test_files} fichier(s) sélectionné(s) pour {TEST_IDDOCS}")
assert num_test_files > 0, f"❌ Aucun fichier trouvé pour les IDDOCs {TEST_IDDOCS}"

display(df_files.select(
    "IDDOC", "source_file_name", "source_file_extension",
    "ref", "titre", "division", "doc_date"
))

# COMMAND ----------

# DBTITLE 1,Étape 2 — Parsing Docling → checkpoint test
# Nettoyage du volume de test (re-run propre)
os.makedirs(TEST_VOLUME_BASE_PATH, exist_ok=True)
for iddoc in TEST_IDDOCS:
    folder = os.path.join(TEST_VOLUME_BASE_PATH, str(iddoc))
    if os.path.isdir(folder):
        shutil.rmtree(folder)
        print(f"  🗑️  Dossier images nettoyé : {folder}")

# Parsing Docling
importlib.reload(image_utils)
from image_utils import parse_and_extract_images_udf

t0 = time.time()
df_parsed = (
    df_files
    .repartition(max(1, num_test_files))
    .withColumn("result", parse_and_extract_images_udf(
        F.col("content"), F.col("source_file_extension"),
        F.col("IDDOC").cast("string"),
        F.lit(TEST_VOLUME_BASE_PATH),
        F.lit(MIN_AREA_RATIO), F.lit(MAX_REPEAT), F.lit(False)
    ))
    .withColumns({
        "document_text":     F.col("result.text"),
        "parser_error":      F.col("result.parser_error"),
        "parser_strategy":   F.col("result.parser_strategy"),
        "parse_time_seconds": F.col("result.parse_time_seconds"),
        "images":            F.col("result.images"),
        "timings":           F.col("result.timings"),
    })
    .withColumn("image_count", F.size(F.col("images")))
    .drop("result", "content")
    .withColumn("parse_status",
        F.when(F.col("parser_error").isNotNull(), F.lit("ERROR"))
        .when(F.length(F.trim(F.col("document_text"))) == 0, F.lit("EMPTY_TEXT"))
        .otherwise(F.lit("SUCCESS")))
)

# Overwrite checkpoint de test (état toujours propre)
df_parsed.write.format("delta").mode("overwrite") \
    .option("overwriteSchema", "true").saveAsTable(TEST_CHECKPOINT)

elapsed = time.time() - t0
df_cp = spark.table(TEST_CHECKPOINT)
ok    = df_cp.filter(F.col("parse_status") == "SUCCESS").count()
print(f"\n✅ Parsing terminé en {elapsed:.1f}s — {ok}/{num_test_files} SUCCESS")
display(df_cp.select("IDDOC", "source_file_name", "parse_status",
                     "image_count", "parse_time_seconds", "parser_strategy"))

# COMMAND ----------

# DBTITLE 1,Étape 3 — processed_files_test + chunks_all_test
df_cp = spark.table(TEST_CHECKPOINT)
if "doc_date" not in df_cp.columns:
    df_cp = df_cp.withColumn("doc_date", F.lit(None).cast("date"))
_cutoff = F.lit(DOC_DATE_CUTOFF).cast("date")

# --- processed_files_test ---
df_processed = (
    df_cp
    .withColumns({
        "document_char_count":  F.length(F.col("document_text")),
        "document_token_count": F.when(
            F.col("parse_status") == "SUCCESS",
            F.greatest(F.lit(1), F.floor(F.length(F.col("document_text")) / F.lit(CHARS_PER_TOKEN)).cast("int"))
        ).otherwise(F.lit(0)),
    })
    .withColumnRenamed("parser_error", "error_trace")
    .select(
        "IDDOC", "document_sha256", "source_path", "source_file_name", "source_file_extension",
        "source_folder_path", "source_file_size_bytes", "source_modification_time",
        "ref", "titre", "type_document", "categorie", "langue", "auteur", "document_prefixes",
        "division", "niveau_plus_1", "niveau_plus_2", "niveau_plus_3",
        "niveau_plus_4", "niveau_plus_5", "niveau_plus_6",
        "doc_date", "ingestion_run_id", "ingestion_timestamp",
        "parser_strategy", "parse_status", "error_trace", "parse_time_seconds",
        "document_char_count", "document_token_count", "image_count",
    )
    .withColumn("chunking_strategy",
        F.when(F.col("parse_status") == "SUCCESS", F.lit("table_aware_hybrid")).otherwise(F.lit(None)))
    .withColumn("filtered_by_date",
        F.when(
            (F.col("parse_status") == "SUCCESS") & F.col("doc_date").isNotNull() & (F.col("doc_date") < _cutoff),
            F.lit(True)
        ).otherwise(F.lit(False)))
    .withColumn("include_in_rag",
        F.when(F.col("filtered_by_date") == True, F.lit(False)).otherwise(F.lit(True)))
    .withColumn("chunks_truncated", F.lit(False))
)
df_processed.write.format("delta").mode("overwrite") \
    .option("overwriteSchema", "true").saveAsTable(TEST_PROCESSED_FILES)
print(f"✅ {TEST_PROCESSED_FILES} ({df_processed.count()} lignes)")

# --- chunks_all_test ---
df_for_chunking = df_cp.filter(
    (F.col("parse_status") == "SUCCESS")
    & (F.col("doc_date").isNull() | (F.col("doc_date") >= _cutoff))
)
df_for_chunking = (
    df_for_chunking
    .withColumn("document_text",
        F.regexp_replace(F.col("document_text"), r"\s*<!-- image -->\s*", " "))
    .withColumn("document_text",
        F.regexp_replace(F.col("document_text"), r"\s*<!-- formula-not-decoded -->\s*", " "))
)
df_chunks = (
    df_for_chunking
    .withColumn("chunks", utils.build_chunks_udf(F.col("document_text")))
    .withColumn("c", F.explode(F.col("chunks")))
    .withColumn("_text_full", utils.source_prefixed_text(
        F.col("c.chunk_text"), F.col("ref"), F.col("titre"),
        F.col("division"), F.col("niveau_plus_1"), doc_date_col=F.col("doc_date"),
        include_prefix=EMBED_SOURCE_PREFIX,
    ))
    .select(
        "IDDOC",
        F.coalesce(F.col("ref"), F.col("source_file_name")).alias("REF"),
        F.col("division"),
        F.concat_ws("-", F.col("IDDOC").cast("string"),
                    F.lpad((F.col("c.chunk_index") + F.lit(1)).cast("string"), 6, "0")).alias("chunk_id"),
        F.col("c.chunk_index").alias("_ci"),
        F.col("_text_full").alias("chunk_text"),
        F.to_json(F.col("c.metadata")).alias("semantic_headers"),
        F.lit(None).cast("string").alias("url"),
    )
    .drop("_ci")
)
df_chunks.write.format("delta").mode("overwrite") \
    .option("overwriteSchema", "true").saveAsTable(TEST_CHUNKS_ALL)
print(f"✅ {TEST_CHUNKS_ALL} — {spark.table(TEST_CHUNKS_ALL).count()} chunks")

# COMMAND ----------

# DBTITLE 1,Étape 4 — image_metadata_test
df_cp = spark.table(TEST_CHECKPOINT)
if "doc_date" not in df_cp.columns:
    df_cp = df_cp.withColumn("doc_date", F.lit(None).cast("date"))

_img_cutoff = F.lit(DOC_DATE_CUTOFF).cast("date")

df_img = (
    df_cp.filter(
        (F.col("image_count") > 0)
        & (F.col("doc_date").isNull() | (F.col("doc_date") >= _img_cutoff))
    )
    .select(
        "IDDOC", "ref", "titre", "division", "niveau_plus_1", "niveau_plus_2",
        "source_file_name", F.posexplode("images").alias("img_pos", "img")
    )
    .select(
        "IDDOC", "source_file_name", "ref", "titre", "division", "niveau_plus_1", "niveau_plus_2",
        F.col("img.image_id").alias("image_id"),
        F.col("img.page_no").alias("page_no"),
        F.col("img.label").alias("label"),
        F.col("img.area_ratio").alias("area_ratio"),
        F.col("img.captions").alias("captions"),
        F.col("img.context_text").alias("context_text"),
        F.col("img.volume_path").alias("volume_path"),
        F.col("img.image_width").alias("image_width"),
        F.col("img.image_height").alias("image_height"),
        image_utils.image_status_col(
            F.col("img.volume_path"), F.col("img.image_width"), F.col("img.image_height")
        ).alias("status"),
        F.lit(None).cast("string").alias("description"),
        F.lit(None).cast("integer").alias("input_tokens"),
        F.lit(None).cast("integer").alias("output_tokens"),
        F.lit(None).cast("timestamp").alias("described_at"),
        F.lit(INGESTION_RUN_ID).alias("ingestion_run_id"),
        F.current_timestamp().alias("ingestion_timestamp"),
    )
)
df_img.write.format("delta").mode("overwrite") \
    .option("overwriteSchema", "true").saveAsTable(TEST_IMAGE_METADATA)
n_img = spark.table(TEST_IMAGE_METADATA).count()
print(f"✅ {TEST_IMAGE_METADATA} — {n_img} images")
display(spark.table(TEST_IMAGE_METADATA).groupBy("status", "label").count().orderBy("label"))

# COMMAND ----------

# DBTITLE 1,Étape 5 — Description LLM des images
import nest_asyncio
nest_asyncio.apply()
from image_utils import describe_all_images, safe_requests_per_minute

# Auth token
try:
    WS_TOKEN = dbutils.secrets.get(scope="qualibot", key="serving_token")
except Exception:
    WS_TOKEN = _ctx.apiToken().get()
WS_HOST = spark.conf.get("spark.databricks.workspaceUrl")

df_pending = spark.table(TEST_IMAGE_METADATA).filter(
    (F.col("status") == "PENDING") & F.col("volume_path").isNotNull()
)
rows_to_process = [r.asDict() for r in df_pending.collect()]
print(f"Images à décrire : {len(rows_to_process)}")

if not rows_to_process:
    print("Aucune image PENDING — passer directement à l'affichage.")
else:
    t0 = time.time()
    # Débit sûr borné par les 3 axes de quota (goulot = OUTPUT/OTPM).
    _rpm = safe_requests_per_minute(
        LLM_ITPM_BUDGET, LLM_OTPM_BUDGET, LLM_QPH_BUDGET,
        LLM_AVG_INPUT_TOKENS, LLM_AVG_OUTPUT_TOKENS,
    )
    print(f"Rate limiter : {_rpm:.0f} req/min  (ITPM≤{LLM_ITPM_BUDGET:,}, OTPM≤{LLM_OTPM_BUDGET:,}, QPH≤{LLM_QPH_BUDGET:,})")

    described_images = asyncio.run(describe_all_images(
        rows=rows_to_process,
        ws_host=WS_HOST, ws_token=WS_TOKEN,
        model=LLM_MODEL_ENDPOINT,
        max_tokens=LLM_MAX_TOKENS, temperature=LLM_TEMPERATURE,
        max_retries=LLM_MAX_RETRIES, max_concurrent=LLM_MAX_CONCURRENT,
        requests_per_minute=_rpm,
    ))
    done = sum(1 for r in described_images if r["status"] == "DONE")
    err  = sum(1 for r in described_images if r["status"] == "ERROR")
    tin  = sum(r.get("input_tokens", 0) for r in described_images)
    tout = sum(r.get("output_tokens", 0) for r in described_images)
    print(f"LLM terminé en {time.time()-t0:.1f}s | ok={done} err={err} | {tin:,} in / {tout:,} out tokens")

    # Merge back dans la table de test
    update_rows = [
        Row(
            IDDOC=int(r["IDDOC"]), image_id=int(r["image_id"]),
            description=r["description"],
            input_tokens=int(r.get("input_tokens") or 0),
            output_tokens=int(r.get("output_tokens") or 0),
            status=r["status"], described_at=datetime.now(),
        )
        for r in described_images
    ]
    df_updates = spark.createDataFrame(update_rows)
    _TEMP = f"{CATALOG_SCHEMA}._test_img_updates_temp"
    df_updates.write.format("delta").mode("overwrite") \
        .option("overwriteSchema", "true").saveAsTable(_TEMP)

    spark.sql(f"""
        MERGE INTO {TEST_IMAGE_METADATA} AS t
        USING {_TEMP} AS s ON t.IDDOC = s.IDDOC AND t.image_id = s.image_id
        WHEN MATCHED THEN UPDATE SET
            t.description   = s.description,
            t.input_tokens  = s.input_tokens,
            t.output_tokens = s.output_tokens,
            t.status        = s.status,
            t.described_at  = s.described_at
    """)
    spark.sql(f"DROP TABLE IF EXISTS {_TEMP}")
    print(f"✅ Descriptions mergées dans {TEST_IMAGE_METADATA}")

# COMMAND ----------

# DBTITLE 1,Étape 6 — Affichage des chunks
# Affichage des chunks en cartes HTML
chunk_rows = spark.table(TEST_CHUNKS_ALL).orderBy("IDDOC", "chunk_id").collect()

html = ["""
<style>
.chunks-view { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; max-width: 1100px; }
.chunks-title { font-size: 15px; font-weight: 700; color: #333; margin: 4px 0 14px; }
.chunk-card {
    border: 1px solid #e0e0e0; border-radius: 8px; margin: 10px 0; padding: 14px;
    background: #fff; box-shadow: 0 1px 3px rgba(0,0,0,.05);
}
.chunk-header {
    font-size: 11px; color: #888; margin-bottom: 8px; display: flex; gap: 8px; flex-wrap: wrap;
}
.chunk-header .tag { background: #f5f5f5; border-radius: 4px; padding: 2px 7px; }
.chunk-body {
    font-size: 13px; color: #1a1a1a; line-height: 1.65; white-space: pre-wrap;
    background: #fafafa; border: 1px solid #ebebeb; border-radius: 5px;
    padding: 10px; max-height: 360px; overflow-y: auto;
}
</style>
<div class="chunks-view">
"""]
html.append(f'<div class="chunks-title">📄 {len(chunk_rows)} chunks générés</div>')

for i, r in enumerate(chunk_rows):
    text_esc = (
        (r["chunk_text"] or "")
        .replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    )
    html.append(f"""
    <div class="chunk-card">
        <div class="chunk-header">
            <span class="tag">#{i+1}</span>
            <span class="tag">IDDOC {r['IDDOC']}</span>
            <span class="tag">📄 {r['REF'] or '—'}</span>
            <span class="tag">🏢 {r['division'] or '—'}</span>
            <span class="tag">🔑 {r['chunk_id']}</span>
        </div>
        <div class="chunk-body">{text_esc}</div>
    </div>
    """)
html.append("</div>")
displayHTML("".join(html))

# COMMAND ----------

# DBTITLE 1,Étape 7 — Galerie images + descriptions LLM
def _b64(path):
    try:
        with open(path, "rb") as f:
            return base64.b64encode(f.read()).decode("utf-8")
    except Exception:
        return None

def _mime(path):
    ext = (path or "").lower().rsplit(".", 1)[-1]
    return "image/png" if ext == "png" else "image/jpeg"

df_done = (
    spark.table(TEST_IMAGE_METADATA)
    .filter((F.col("status") == "DONE") & F.col("description").isNotNull())
    .orderBy("IDDOC", "image_id")
)
rows = df_done.collect()

_counts = {r["status"]: r["count"] for r in
           spark.table(TEST_IMAGE_METADATA).groupBy("status").count().collect()}
print(
    f"Images DONE={len(rows)} | PENDING={_counts.get('PENDING', 0)} "
    f"| SKIPPED(LLM)={_counts.get('SKIPPED', 0)} "
    f"| SKIPPED_DECORATIVE={_counts.get('SKIPPED_DECORATIVE', 0)} "
    f"| EXTRACTION_FAILED={_counts.get('EXTRACTION_FAILED', 0)}"
)

html = ["""
<style>
.img-gallery { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; max-width: 1100px; }
.gallery-title { font-size: 15px; font-weight: 700; color: #333; margin: 4px 0 14px; }
.img-card {
    border: 1px solid #e0e0e0; border-radius: 10px; margin: 16px 0; padding: 16px;
    display: flex; gap: 24px; background: #fafafa;
}
.img-card img {
    max-width: 420px; max-height: 340px; object-fit: contain;
    border: 1px solid #e0e0e0; border-radius: 6px; background: white; flex-shrink: 0;
}
.img-card .no-img {
    width: 220px; height: 160px; background: #f0f0f0; border-radius: 6px;
    display: flex; align-items: center; justify-content: center; color: #999; flex-shrink: 0;
}
.img-card .meta { flex: 1; min-width: 0; }
.img-card .meta h3 { margin: 0 0 10px; font-size: 14px; font-weight: 600; color: #1a1a1a; }
.img-card .desc {
    font-size: 13px; color: #333; line-height: 1.6; white-space: pre-wrap;
    background: white; border: 1px solid #e8e8e8; border-radius: 6px; padding: 10px;
    max-height: 340px; overflow-y: auto;
}
.img-card .info { font-size: 11px; color: #888; margin-top: 10px; display: flex; flex-wrap: wrap; gap: 8px; }
.img-card .info .tag { background: #f0f0f0; border-radius: 4px; padding: 2px 8px; }
</style>
<div class="img-gallery">
"""]

if not rows:
    html.append("<p style='color:#999'>Aucune image décrite — lancer l'étape LLM d'abord.</p>")
else:
    html.append(f'<div class="gallery-title">🖼️ {len(rows)} image(s) décrite(s)</div>')
    for r in rows:
        b64 = _b64(r["volume_path"])
        img_el = (
            f'<img src="data:{_mime(r["volume_path"])};base64,{b64}" alt="img {r["image_id"]}" />'
            if b64 else '<div class="no-img">🖼️ Image non trouvée</div>'
        )
        desc = (
            (r["description"] or "—")
            .replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        )
        html.append(f"""
        <div class="img-card">
            {img_el}
            <div class="meta">
                <h3>IDDOC {r['IDDOC']} — Image #{r['image_id']} &nbsp;·&nbsp; page {r['page_no']}</h3>
                <div class="desc">{desc}</div>
                <div class="info">
                    <span class="tag">📄 {r['ref'] or r['source_file_name'] or 'N/A'}</span>
                    <span class="tag">🏢 {r['division'] or '—'}</span>
                    <span class="tag">🏷️ {r['label'] or '—'}</span>
                    <span class="tag">📐 {r['image_width']}×{r['image_height']}</span>
                    <span class="tag">🔤 {r['input_tokens'] or 0} in / {r['output_tokens'] or 0} out</span>
                </div>
            </div>
        </div>
        """)

html.append("</div>")
displayHTML("".join(html))