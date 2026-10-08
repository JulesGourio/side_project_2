# Databricks notebook source
# /// script
# [tool.databricks.environment]
# environment_version = "5"
# ///

# MAGIC %md
# MAGIC # GENERIC PARSE AND CHUNK
# MAGIC
# MAGIC **Description:**
# MAGIC Parses the documents of a volume with Docling, chunks them, adds 10 % of the Intraqual REFs of the chunk table so the
# MAGIC test index has realistic neighbours, and creates a Vector Search index on the result. Test run on serverless.
# MAGIC
# MAGIC **Highlighted complexities:**
# MAGIC Chunks carry `division = 'GENERIC'` and `IDDOC = 0`; their `chunk_text` gets the same `[Source: ... | Title: ...]` prefix as the Intraqual chunks.
# MAGIC
# MAGIC **Intended Pipeline**
# MAGIC - None, run by hand (not wired to a job)
# MAGIC
# MAGIC **Inputs Data**
# MAGIC - `{source_volume_path}` (documents)
# MAGIC - `{catalog_schema}.chunks` (sampled for the Intraqual chunks)
# MAGIC
# MAGIC **Output Tables (Pipeline)**
# MAGIC - `{catalog_schema}.chunks_test_generic` and the index `{catalog_schema}.chunks_test_generic_index`

# COMMAND ----------

# MAGIC %md
# MAGIC # Technical debt
# MAGIC - The sample of REFs is a fixed 10 % with seed 42.
# MAGIC - `chunk_token_count` is estimated at 3.5 characters per token, not counted with a tokenizer.

# COMMAND ----------

# MAGIC %md
# MAGIC # Configuration
# MAGIC ## Config Standard Package Imports

# COMMAND ----------

# MAGIC %pip install -q "numpy<2" docling docling-core langchain-text-splitters tiktoken
# MAGIC %pip install -q --force-reinstall --no-deps opencv-python-headless==4.12.0.88

# COMMAND ----------

import hashlib
import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path

from pyspark.sql import functions as F
from pyspark.sql import types as T

_ctx = dbutils.notebook.entry_point.getDbutils().notebook().getContext()
sys.path.insert(0, "/Workspace" + os.path.dirname(_ctx.notebookPath().get()))

# COMMAND ----------

# MAGIC %md
# MAGIC ## Config Widgets

# COMMAND ----------

dbutils.widgets.text("source_volume_path", "/Volumes/uat_landingzone/qualibot/test/test_documents")
dbutils.widgets.text("catalog_schema", "uat_landingzone.qualibot")
dbutils.widgets.text("offline_models_dir", "/Volumes/uat_landingzone/qualibot/docling_models")
dbutils.widgets.text("max_chunk_tokens", "1000")
dbutils.widgets.text("vector_search_endpoint", "qualibot")
dbutils.widgets.text("embedding_model", "databricks-qwen3-embedding-0-6b")

SOURCE_VOLUME_PATH = dbutils.widgets.get("source_volume_path")
CATALOG_SCHEMA = dbutils.widgets.get("catalog_schema")
OFFLINE_MODELS_DIR = dbutils.widgets.get("offline_models_dir")
MAX_CHUNK_TOKENS = int(dbutils.widgets.get("max_chunk_tokens"))
VECTOR_SEARCH_ENDPOINT = dbutils.widgets.get("vector_search_endpoint")
EMBEDDING_MODEL = dbutils.widgets.get("embedding_model")

INTRAQUAL_CHUNKS_TABLE = f"{CATALOG_SCHEMA}.chunks"
TARGET_CHUNKS_TABLE = f"{CATALOG_SCHEMA}.chunks_test_generic"
INDEX_NAME = f"{TARGET_CHUNKS_TABLE}_index"
INTRAQUAL_SAMPLE_FRACTION = 0.1

# COMMAND ----------

# MAGIC %md
# MAGIC ## Config logger and offline environment

# COMMAND ----------

logger = logging.getLogger("generic_pipeline")
logger.setLevel(logging.INFO)
if not logger.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S"))
    logger.addHandler(_handler)
    logger.propagate = False
for _noisy in ("py4j", "py4j.clientserver", "pyspark", "docling", "transformers"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)

os.environ.update({
    "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1", "USE_TF": "0",
    "TRANSFORMERS_NO_TF": "1", "CUDA_MODULE_LOADING": "LAZY",
})

USE_GPU = shutil.which("nvidia-smi") is not None and subprocess.call(["nvidia-smi"], stdout=subprocess.DEVNULL) == 0
logger.info(f"USE_GPU={USE_GPU} | source={SOURCE_VOLUME_PATH} | target={TARGET_CHUNKS_TABLE}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Config Project Imports

# COMMAND ----------

import chunk_steps

# COMMAND ----------

# MAGIC %md
# MAGIC # Inputs
# MAGIC ## Import source documents
# MAGIC Every supported file of the volume, read as binary, with the title derived from the file name.

# COMMAND ----------

df_files = (
    spark.read.format("binaryFile")
    .option("recursiveFileLookup", "true")
    .option("pathGlobFilter", "*")
    .load(SOURCE_VOLUME_PATH)
    .withColumn("file_name", F.element_at(F.split(F.col("path"), "/"), -1))
    .withColumn("extension", F.lower(F.element_at(F.split(F.col("file_name"), r"\."), -1)))
    .withColumn("file_size_bytes", F.col("length"))
    .withColumn(
        "doc_title",
        F.regexp_replace(F.element_at(F.split(F.col("file_name"), r"\."), 1), r"[_\-]+", " "),
    )
    .filter(F.col("extension").isin(list(chunk_steps.SUPPORTED_EXTENSIONS)))
    .filter(F.col("file_size_bytes") > 0)
)

logger.info(f"Found {df_files.count()} supported file(s) in {SOURCE_VOLUME_PATH}")
display(df_files.select("file_name", "extension", "file_size_bytes", "doc_title").limit(20))

# COMMAND ----------

# MAGIC %md
# MAGIC # Data Transformations
# MAGIC ## Tr. 1 - Parse and chunk each file
# MAGIC The REF is the first token of the file name, and every chunk text is prefixed with the Intraqual `[Source: ...]` header.

# COMMAND ----------

converter = chunk_steps.build_converter(OFFLINE_MODELS_DIR, USE_GPU)
chunker = chunk_steps.build_chunker(OFFLINE_MODELS_DIR, MAX_CHUNK_TOKENS)

all_chunks = []
errors = []

for row in df_files.select("path", "content", "extension", "doc_title", "file_name").collect():
    file_name = row["file_name"]
    stem = Path(file_name).stem
    ref = stem.split("_")[0] if "_" in stem else stem.split(" ")[0]

    chunks, error, status = chunk_steps.parse_and_chunk_file(
        row["path"], bytes(row["content"]), row["extension"], converter, chunker
    )
    if status == "ERROR":
        errors.append({"file_name": file_name, "error": error})
        logger.warning(f"ERROR: {file_name} - {error[:100]}")
    elif status == "EMPTY_TEXT":
        logger.info(f"EMPTY: {file_name}")
    else:
        logger.info(f"OK: {file_name} - {len(chunks)} chunk(s)")

    source_prefix = f"[Source: {ref} | Title: {row['doc_title']}]"
    for chunk in chunks:
        full_text = f"{source_prefix}\n\n{chunk['chunk_text']}"
        all_chunks.append({
            "IDDOC": 0,
            "REF": ref,
            "division": "GENERIC",
            "chunk_id": hashlib.sha256(f"{file_name}::{chunk['chunk_index']}".encode()).hexdigest()[:16],
            "chunk_index": chunk["chunk_index"],
            "chunk_text": full_text,
            "chunk_token_count": max(1, int(len(full_text) / chunk_steps.CHARS_PER_TOKEN)),
            "chunk_content_type": chunk["chunk_content_type"],
            "semantic_headers": chunk["semantic_headers"],
            "chunk_sha256": hashlib.sha256(full_text.encode()).hexdigest(),
            "url": row["path"],
            "doc_date": None,
        })

logger.info(f"Parsing complete: {len(all_chunks)} chunks from {df_files.count()} files.")
for e in errors:
    logger.warning(f"In error: {e['file_name']}: {e['error'][:100]}")

if not all_chunks:
    logger.warning("No chunks to write - nothing to do.")
    dbutils.notebook.exit("no_chunks")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Tr. 2 - Add a sample of the Intraqual chunks
# MAGIC The schema is the one of the `chunks` table, so the two sets union by name.

# COMMAND ----------

chunk_schema = T.StructType([
    T.StructField("IDDOC", T.LongType(), True),
    T.StructField("REF", T.StringType(), False),
    T.StructField("division", T.StringType(), True),
    T.StructField("chunk_id", T.StringType(), False),
    T.StructField("chunk_index", T.IntegerType(), False),
    T.StructField("chunk_text", T.StringType(), False),
    T.StructField("chunk_token_count", T.IntegerType(), True),
    T.StructField("chunk_content_type", T.StringType(), True),
    T.StructField("semantic_headers", T.StringType(), True),
    T.StructField("chunk_sha256", T.StringType(), True),
    T.StructField("url", T.StringType(), True),
    T.StructField("doc_date", T.DateType(), True),
])

df_generic_chunks = spark.createDataFrame(all_chunks, schema=chunk_schema)

df_intraqual = spark.table(INTRAQUAL_CHUNKS_TABLE)
df_sampled_refs = df_intraqual.select("REF").distinct().sample(fraction=INTRAQUAL_SAMPLE_FRACTION, seed=42)
df_sampled_intraqual = df_intraqual.join(df_sampled_refs, "REF", "inner")

df_combined = df_sampled_intraqual.unionByName(df_generic_chunks)

# COMMAND ----------

# MAGIC %md
# MAGIC # Quality Checks
# MAGIC `chunk_id` is the primary key of the index source: it must be unique.

# COMMAND ----------

n_total = df_combined.count()
n_distinct_ids = df_combined.select("chunk_id").distinct().count()
if n_total != n_distinct_ids:
    raise ValueError(f"chunk_id is not unique: {n_total} rows, {n_distinct_ids} distinct ids")
logger.info(f"Combined: {n_total} chunks ({df_generic_chunks.count()} generic + the sampled Intraqual ones)")

# COMMAND ----------

# MAGIC %md
# MAGIC # Outputs
# MAGIC ## Write the table
# MAGIC Change Data Feed is required by the Delta Sync index; `chunk_id` is nullable after the union, so it is set NOT NULL before the primary key.

# COMMAND ----------

df_combined.write.format("delta").mode("overwrite").option("overwriteSchema", "true").saveAsTable(TARGET_CHUNKS_TABLE)
spark.sql(f"ALTER TABLE {TARGET_CHUNKS_TABLE} SET TBLPROPERTIES ('delta.enableChangeDataFeed' = 'true')")
spark.sql(f"ALTER TABLE {TARGET_CHUNKS_TABLE} ALTER COLUMN chunk_id SET NOT NULL")
try:
    spark.sql(f"ALTER TABLE {TARGET_CHUNKS_TABLE} ADD CONSTRAINT pk_chunk_id PRIMARY KEY (chunk_id)")
except Exception as e:
    if "CONSTRAINT_ALREADY_EXISTS" not in str(e) and "already exists" not in str(e).lower():
        raise
logger.info(f"Table {TARGET_CHUNKS_TABLE} written with {n_total} chunks.")

display(
    spark.table(TARGET_CHUNKS_TABLE)
    .groupBy("division")
    .agg(F.count("*").alias("chunk_count"), F.countDistinct("REF").alias("ref_count"))
    .orderBy("division")
)

# COMMAND ----------

# MAGIC %md
# MAGIC ## Create or sync the Vector Search index

# COMMAND ----------

from databricks.sdk import WorkspaceClient

chunk_steps.create_and_sync_index(
    WorkspaceClient(), INDEX_NAME, VECTOR_SEARCH_ENDPOINT, TARGET_CHUNKS_TABLE, EMBEDDING_MODEL
)
