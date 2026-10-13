# Databricks notebook source
# /// script
# [tool.databricks.environment]
# environment_version = "5"
# ///
# DBTITLE 1,Debug — Retry ERROR Images
# MAGIC %md
# MAGIC # Debug — Retry ERROR Images
# MAGIC
# MAGIC **Purpose:** Diagnostic notebook to understand why 324 images are stuck in `ERROR`
# MAGIC (all `REQUEST_LIMIT_EXCEEDED` on `databricks-gpt-5-6-luna`).
# MAGIC
# MAGIC Strategy:
# MAGIC 1. Load the ERROR images from `image_metadata_v1`
# MAGIC 2. Test a **small sample** (5 images) with **concurrency=1** — no rate pressure
# MAGIC 3. If that works → the issue is rate limiting during the batch, not the images themselves
# MAGIC 4. Then retry ALL remaining ERROR images with reduced concurrency
# MAGIC 5. Persist results back to `image_metadata_v1`

# COMMAND ----------

# DBTITLE 1,Setup — imports & config
import os, sys, time, asyncio, base64
from datetime import datetime

REPO_DIR = "/Workspace/Shared/.bundle/qualibot/qualibot-uat/files/utils/parsing_pipeline"
sys.path.insert(0, REPO_DIR)

# Env vars required by config.py (normally set by the job cluster)
os.environ.setdefault("PARSING_CATALOG_SCHEMA", "uat_landingzone.qualibot")
os.environ.setdefault("PARSING_INTRAQUAL_BRONZE", "prod_bronze.intraqual")
os.environ.setdefault("PARSING_INTRAQUAL_SOURCE", "prod_landingzone.intraqual")
os.environ.setdefault("PARSING_TABLE_SUFFIX", "_v1")
os.environ.setdefault("PARSING_USE_GPU", "false")
os.environ.setdefault("PARSING_VOLUME_BASE_PATH", "/Volumes/uat_landingzone/qualibot/images")
os.environ.setdefault("PARSING_VOLUME_ROOT_PATH", "/Volumes/prod_landingzone/intraqual/intraqual_documents")
os.environ.setdefault("PARSING_OFFLINE_MODELS", "/Volumes/uat_landingzone/qualibot/docling_models")
os.environ.setdefault("PARSING_RUN_MODE", "incremental")

from pyspark.sql import functions as F, Row

# No addPyFile needed — driver-side only, modules are importable from sys.path
from config import *
from image_utils import describe_all_images, safe_requests_per_minute

SOURCE_IMAGE_TABLE = TARGET_IMAGE_METADATA_TABLE

try:
    WS_TOKEN = dbutils.secrets.get(scope="qualibot", key="serving_token")
except Exception:
    WS_TOKEN = dbutils.notebook.entry_point.getDbutils().notebook().getContext().apiToken().get()
WS_HOST = spark.conf.get("spark.databricks.workspaceUrl")

print(f"Model: {LLM_MODEL_ENDPOINT}")
print(f"Config: concurrency={LLM_MAX_CONCURRENT}, retries={LLM_MAX_RETRIES}")
print(f"Budgets: ITPM={LLM_ITPM_BUDGET:,} | OTPM={LLM_OTPM_BUDGET:,} | QPH={LLM_QPH_BUDGET:,}")
print(f"safe_rpm = {safe_requests_per_minute(LLM_ITPM_BUDGET, LLM_OTPM_BUDGET, LLM_QPH_BUDGET, LLM_AVG_INPUT_TOKENS, LLM_AVG_OUTPUT_TOKENS):.0f} req/min")

# COMMAND ----------

# DBTITLE 1,Step 1 — Load ERROR images
# MAGIC %md
# MAGIC ## 1 — Load ERROR images and diagnose

# COMMAND ----------

# DBTITLE 1,Load ERROR images breakdown
df_errors = spark.table(SOURCE_IMAGE_TABLE).filter(F.col("status") == "ERROR")

# Breakdown by error type
df_errors_collected = df_errors.select(
    "iddoc", "image_id", "volume_path", "description",
    "image_width", "image_height", "described_at", "label"
).collect()

rate_limit = [r for r in df_errors_collected if r["description"] and "REQUEST_LIMIT_EXCEEDED" in r["description"]]
empty_desc = [r for r in df_errors_collected if not r["description"] or r["description"].strip() == ""]
other = [r for r in df_errors_collected if r not in rate_limit and r not in empty_desc]

print(f"Total ERROR images: {len(df_errors_collected)}")
print(f"  Rate limit (429): {len(rate_limit)}")
print(f"  Empty description: {len(empty_desc)}")
print(f"  Other: {len(other)}")
print(f"  Distinct IDDOCs: {len(set(r['iddoc'] for r in df_errors_collected))}")
print(f"  Last described_at: {max(r['described_at'] for r in df_errors_collected if r['described_at'])}")

# Check if the volume files still exist
import os as _os
missing_files = [r for r in df_errors_collected if not _os.path.exists(r["volume_path"])]
print(f"\nMissing volume files: {len(missing_files)} / {len(df_errors_collected)}")
if missing_files:
    print("  ⚠️  Some image files are missing from the volume — these can't be retried!")
    for r in missing_files[:5]:
        print(f"    {r['volume_path']}")

# COMMAND ----------

# DBTITLE 1,Step 2 — Sample test
# MAGIC %md
# MAGIC ## 2 — Test a small sample (5 images, concurrency=1, no rate pressure)
# MAGIC
# MAGIC If this works → rate limiting during the batch was the issue, not the images.

# COMMAND ----------

# DBTITLE 1,Test 5 images sequentially
import nest_asyncio
nest_asyncio.apply()

# Pick 5 ERROR images with existing files
sample_rows = [
    r.asDict() for r in
    df_errors.filter(F.col("volume_path").isNotNull()).limit(5).collect()
]
print(f"Testing {len(sample_rows)} images with concurrency=1, no rate limiter...")
for r in sample_rows:
    print(f"  IDDOC={r['IDDOC']} image_id={r['image_id']} ({r['image_width']}x{r['image_height']}) label={r.get('label','?')}")

t0 = time.time()
sample_results = asyncio.run(describe_all_images(
    rows=sample_rows,
    ws_host=WS_HOST,
    ws_token=WS_TOKEN,
    model=LLM_MODEL_ENDPOINT,
    max_tokens=LLM_MAX_TOKENS,
    temperature=LLM_TEMPERATURE,
    max_retries=3,
    max_concurrent=1,          # sequential — zero contention
    requests_per_minute=None,  # no rate limiter — raw endpoint test
))
elapsed = time.time() - t0

print(f"\n{'='*60}")
print(f"Sample results ({elapsed:.1f}s):")
for r in sample_results:
    status = r["status"]
    desc_preview = (r.get("description") or "")[:120]
    emoji = "✅" if status == "DONE" else "⚠️" if status == "SKIPPED" else "❌"
    print(f"  {emoji} IDDOC={r['IDDOC']} img={r['image_id']} | {status} | "
          f"in={r.get('input_tokens',0)} out={r.get('output_tokens',0)} | {desc_preview}")

done = sum(1 for r in sample_results if r["status"] == "DONE")
err = sum(1 for r in sample_results if r["status"] == "ERROR")
skip = sum(1 for r in sample_results if r["status"] == "SKIPPED")
print(f"\nVerdict: {done} DONE / {skip} SKIPPED / {err} ERROR")
if err == 0:
    print("→ The images are fine! The rate limit error was purely contention during the batch.")
else:
    print("→ Still failing — endpoint may have a persistent quota issue.")

# COMMAND ----------

# DBTITLE 1,Raw API call — inspect full response
# Manual raw API call on ONE image to inspect the full response
from openai import OpenAI

_test_row = sample_rows[0]
print(f"Testing IDDOC={_test_row['IDDOC']} image_id={_test_row['image_id']}")
print(f"  volume_path: {_test_row['volume_path']}")
print(f"  image: {_test_row['image_width']}x{_test_row['image_height']} label={_test_row.get('label','')}")

# Read and encode image
with open(_test_row["volume_path"], "rb") as f:
    _img_bytes = f.read()
    _img_b64 = base64.b64encode(_img_bytes).decode()
print(f"  file size: {len(_img_bytes):,} bytes | base64: {len(_img_b64):,} chars")

# Build same prompt as the pipeline
from image_utils import build_llm_prompt
_prompt = build_llm_prompt(
    _test_row.get("context_text", ""),
    division=_test_row.get("division", ""),
    category=" > ".join([p for p in (_test_row.get("niveau_plus_1", ""), _test_row.get("niveau_plus_2", "")) if p]),
    label=_test_row.get("label", ""),
)

client = OpenAI(api_key=WS_TOKEN, base_url=f"https://{WS_HOST}/serving-endpoints")
resp = client.chat.completions.create(
    model=LLM_MODEL_ENDPOINT,
    messages=[{"role": "user", "content": [
        {"type": "text", "text": _prompt},
        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{_img_b64}"}},
    ]}],
    max_tokens=LLM_MAX_TOKENS,
    temperature=LLM_TEMPERATURE,
)

print(f"\n{'='*60}")
print(f"finish_reason: {resp.choices[0].finish_reason}")
print(f"content type : {type(resp.choices[0].message.content)}")
print(f"content repr : {repr((resp.choices[0].message.content or '')[:500])}")
print(f"content len  : {len(resp.choices[0].message.content or '')}")
print(f"usage        : in={resp.usage.prompt_tokens} out={resp.usage.completion_tokens} total={resp.usage.total_tokens}")

_content = resp.choices[0].message.content
if _content:
    print(f"\n--- First 500 chars of response ---")
    print(_content[:500])
else:
    print("\n❌ content is None/empty!")
    print(f"Full choice object: {resp.choices[0]}")

# COMMAND ----------

# DBTITLE 1,Test with higher max_tokens
# Retry same image with much higher max_tokens
for _mt in [4096, 8192, 16384]:
    print(f"\n--- max_tokens={_mt} ---")
    t0 = time.time()
    resp2 = client.chat.completions.create(
        model=LLM_MODEL_ENDPOINT,
        messages=[{"role": "user", "content": [
            {"type": "text", "text": _prompt},
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{_img_b64}"}},
        ]}],
        max_tokens=_mt,
        temperature=LLM_TEMPERATURE,
    )
    elapsed = time.time() - t0
    _c = resp2.choices[0].message.content or ""
    print(f"  finish_reason={resp2.choices[0].finish_reason} | in={resp2.usage.prompt_tokens} out={resp2.usage.completion_tokens} | content_len={len(_c)} | {elapsed:.1f}s")
    if _c:
        print(f"  ✅ Content preview: {_c[:200]}")
        break
    else:
        print(f"  ❌ Still empty content — reasoning tokens consumed all {_mt}")

# COMMAND ----------

# DBTITLE 1,Step 3 — Retry all
# MAGIC %md
# MAGIC ## 3 — Retry ALL ERROR images (reduced concurrency)
# MAGIC
# MAGIC **Only run this cell if Step 2 passed.** Uses `concurrency=3` and rate limiter
# MAGIC to stay well under the endpoint limits.

# COMMAND ----------

# DBTITLE 1,Retry all 171 ERROR images (concurrency=2, max_tokens=16000, rpm=30)
# Reload config.py and image_utils.py to pick up latest changes
import importlib, config, image_utils
importlib.reload(config); importlib.reload(image_utils)
from config import *
from image_utils import describe_all_images

# -- Configurable knobs for this retry --
# 169 rate-limited + 2 reasoning-exhausted → go very gentle
RETRY_MAX_TOKENS = 16000           # 16K — handles even the heaviest reasoning images
RETRY_CONCURRENCY = 2              # 2 instead of 10 — minimal contention
RETRY_RPM = 30                     # 30 req/min — 10x under the 333 safe_rpm
RETRY_MAX_RETRIES = 5
print(f"Retry config: max_tokens={RETRY_MAX_TOKENS} | concurrency={RETRY_CONCURRENCY} | rpm={RETRY_RPM} | retries={RETRY_MAX_RETRIES}")

all_error_rows = [
    r.asDict() for r in
    df_errors.filter(F.col("volume_path").isNotNull()).collect()
]
print(f"Retrying {len(all_error_rows)} ERROR images | concurrency={RETRY_CONCURRENCY} | rpm={RETRY_RPM} | retries={RETRY_MAX_RETRIES}")

import math
CHUNK_SIZE = 200  # persist every 200 images

_TEMP_TABLE = f"{CATALOG_SCHEMA}._debug_retry_temp"
total_done = total_err = total_skip = 0

n_chunks = math.ceil(len(all_error_rows) / CHUNK_SIZE)
for chunk_idx in range(n_chunks):
    chunk = all_error_rows[chunk_idx * CHUNK_SIZE:(chunk_idx + 1) * CHUNK_SIZE]
    t0 = time.time()
    results = asyncio.run(describe_all_images(
        rows=chunk,
        ws_host=WS_HOST,
        ws_token=WS_TOKEN,
        model=LLM_MODEL_ENDPOINT,
        max_tokens=RETRY_MAX_TOKENS,
        temperature=LLM_TEMPERATURE,
        max_retries=RETRY_MAX_RETRIES,
        max_concurrent=RETRY_CONCURRENCY,
        requests_per_minute=RETRY_RPM,
    ))
    elapsed = time.time() - t0

    done = sum(1 for r in results if r["status"] == "DONE")
    err = sum(1 for r in results if r["status"] == "ERROR")
    skip = sum(1 for r in results if r["status"] in ("SKIPPED", "SKIPPED_DECORATIVE"))
    total_done += done; total_err += err; total_skip += skip

    # Persist results
    update_rows = [
        Row(
            IDDOC=int(r["IDDOC"]), image_id=int(r["image_id"]),
            description=r["description"],
            input_tokens=int(r.get("input_tokens") or 0),
            output_tokens=int(r.get("output_tokens") or 0),
            status=r["status"], described_at=datetime.now(),
        ) for r in results
    ]
    df_updates = spark.createDataFrame(update_rows).dropDuplicates(["IDDOC", "image_id"])
    df_updates.write.format("delta").mode("overwrite").option("overwriteSchema", "true").saveAsTable(_TEMP_TABLE)
    spark.sql(f"""
        MERGE INTO {SOURCE_IMAGE_TABLE} AS t USING {_TEMP_TABLE} AS s
        ON t.IDDOC = s.IDDOC AND t.image_id = s.image_id
        WHEN MATCHED THEN UPDATE SET
            t.description = s.description, t.input_tokens = s.input_tokens,
            t.output_tokens = s.output_tokens, t.status = s.status, t.described_at = s.described_at
    """)

    print(f"[chunk {chunk_idx+1}/{n_chunks}] {len(chunk)} images in {elapsed:.1f}s | "
          f"✅ {done} DONE / ⚠️ {skip} SKIP / ❌ {err} ERROR | cumul: {total_done}/{total_err}/{total_skip}")

spark.sql(f"DROP TABLE IF EXISTS {_TEMP_TABLE}")

print(f"\n{'='*60}")
print(f"FINAL: {total_done} DONE / {total_skip} SKIPPED / {total_err} ERROR out of {len(all_error_rows)}")
if total_err == 0:
    print("🎉 All images resolved!")
else:
    print(f"⚠️  {total_err} images still in ERROR — check endpoint quotas or image content.")

# COMMAND ----------

# DBTITLE 1,Step 4 — Final status
# MAGIC %md
# MAGIC ## 4 — Final status check

# COMMAND ----------

# DBTITLE 1,Final status breakdown
display(
    spark.table(SOURCE_IMAGE_TABLE).groupBy("status").agg(
        F.count("*").alias("count"),
        F.sum("input_tokens").alias("total_input_tokens"),
        F.sum("output_tokens").alias("total_output_tokens"),
    ).orderBy(F.desc("count"))
)

# COMMAND ----------

