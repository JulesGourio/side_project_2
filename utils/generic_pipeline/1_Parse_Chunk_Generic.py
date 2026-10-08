# Databricks notebook source
# /// script
# [tool.databricks.environment]
# environment_version = "5"
# ///
# MAGIC %md
# MAGIC # Generic Parse & Chunk — Step-by-Step Test
# MAGIC
# MAGIC Single-document test run of the generic pipeline on serverless.

# COMMAND ----------

# MAGIC %pip install -q "numpy<2" docling docling-core langchain-text-splitters tiktoken
# MAGIC %pip install -q --force-reinstall --no-deps opencv-python-headless==4.12.0.88

# COMMAND ----------


SOURCE_VOLUME_PATH = "/Volumes/uat_landingzone/qualibot/test/test_documents"

# Target catalog.schema for the chunks table
CATALOG_SCHEMA = "uat_landingzone.qualibot"

# Target table name (will be created if it doesn't exist)
TARGET_CHUNKS_TABLE = f"{CATALOG_SCHEMA}.chunks_test_generic"

# Path to offline Docling models (reuse from parsing_pipeline)
OFFLINE_MODELS_DIR = "/Volumes/uat_landingzone/qualibot/docling_models"

# Chunking parameters
MIN_CHUNK_TOKENS = 250
TARGET_CHUNK_TOKENS = 500
MAX_CHUNK_TOKENS = 1000
CHUNK_OVERLAP_RATIO = 0.12

# Run mode: "full" overwrites the table, "incremental" appends only new files
RUN_MODE = "full"  # "full" | "incremental"

# GPU auto-detection
import shutil, subprocess
def _detect_gpu():
    if shutil.which("nvidia-smi") is None:
        return False
    try:
        subprocess.check_output(["nvidia-smi"])
        return True
    except Exception:
        return False

USE_GPU = _detect_gpu()

# COMMAND ----------

import os
import hashlib
import logging
from pathlib import Path

from pyspark.sql import functions as F
from pyspark.sql import types as T

# Logging
logger = logging.getLogger("generic_pipeline")
logger.setLevel(logging.INFO)
if not logger.handlers:
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S"))
    logger.addHandler(_h)
    logger.propagate = False

# Suppress noisy loggers
for _noisy in ("py4j", "py4j.clientserver", "pyspark", "docling", "transformers"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)

logger.info(f"USE_GPU = {USE_GPU} | SOURCE_VOLUME_PATH = {SOURCE_VOLUME_PATH} | TARGET_CHUNKS_TABLE = {TARGET_CHUNKS_TABLE}")

# Offline env
os.environ["HF_HUB_OFFLINE"] = "1"
os.environ["TRANSFORMERS_OFFLINE"] = "1"
os.environ["USE_TF"] = "0"
os.environ["TRANSFORMERS_NO_TF"] = "1"
os.environ["CUDA_MODULE_LOADING"] = "LAZY"

# COMMAND ----------

from docling.document_converter import DocumentConverter, PdfFormatOption
from docling.datamodel.base_models import InputFormat
from docling.datamodel.pipeline_options import (
    PdfPipelineOptions, AcceleratorOptions, AcceleratorDevice,
)
from docling.chunking import HybridChunker
from docling_core.transforms.chunker.tokenizer.huggingface import HuggingFaceTokenizer

try:
    from docling.datamodel.pipeline_options import TableFormerMode
except ImportError:
    TableFormerMode = None

def build_converter():
    """Build a Docling DocumentConverter."""
    opts = PdfPipelineOptions()
    opts.artifacts_path = Path(OFFLINE_MODELS_DIR)
    opts.do_ocr = False
    opts.do_table_structure = True
    opts.generate_picture_images = False  # No image description in this pipeline
    opts.images_scale = 0.75
    if TableFormerMode is not None:
        try:
            opts.table_structure_options.mode = TableFormerMode.ACCURATE
        except Exception:
            pass
    if USE_GPU:
        opts.accelerator_options = AcceleratorOptions(num_threads=4, device=AcceleratorDevice.CUDA)
    else:
        opts.accelerator_options = AcceleratorOptions(num_threads=1, device=AcceleratorDevice.CPU)

    format_options = {}
    try:
        from docling.backend.pypdfium2_backend import PyPdfiumDocumentBackend
        format_options[InputFormat.PDF] = PdfFormatOption(pipeline_options=opts, backend=PyPdfiumDocumentBackend)
    except ImportError:
        format_options[InputFormat.PDF] = PdfFormatOption(pipeline_options=opts)

    return DocumentConverter(format_options=format_options)

def build_chunker():
    """Build a HybridChunker with offline tokenizer."""
    tokenizer_dir = Path(OFFLINE_MODELS_DIR) / "sentence-transformers--all-MiniLM-L6-v2"
    tokenizer = HuggingFaceTokenizer.from_pretrained(model_name=tokenizer_dir, max_tokens=MAX_CHUNK_TOKENS)
    return HybridChunker(tokenizer=tokenizer, merge_peers=True)

logger.info("Docling imports OK")

# COMMAND ----------

# MAGIC %md
# MAGIC # Step 1 — List source files

# COMMAND ----------

# Supported extensions (same as parsing_pipeline)
SUPPORTED_EXTENSIONS = {
    "pdf", "docx", "docm", "pptx", "xlsx", "xlsm", "xlsb",
    "html", "htm", "xml", "md", "txt",
    "doc", "rtf", "odt", "ods", "xls",
}

# Read files as binary from the volume
df_raw = (
    spark.read.format("binaryFile")
    .option("recursiveFileLookup", "true")
    .option("pathGlobFilter", "*")
    .load(SOURCE_VOLUME_PATH)
)

# Extract file metadata
df_files = (
    df_raw
    .withColumn("file_name", F.element_at(F.split(F.col("path"), "/"), -1))
    .withColumn("extension", F.lower(F.element_at(F.split(F.col("file_name"), r"\."), -1)))
    .withColumn("file_size_bytes", F.col("length"))
    # Metadata from title: strip extension, replace underscores/hyphens with spaces
    .withColumn("doc_title",
        F.regexp_replace(
            F.element_at(F.split(F.col("file_name"), r"\."), 1),
            r"[_\-]+", " "
        )
    )
    .filter(F.col("extension").isin(list(SUPPORTED_EXTENSIONS)))
    .filter(F.col("file_size_bytes") > 0)
)

file_count = df_files.count()
logger.info(f"Found {file_count} supported file(s) in {SOURCE_VOLUME_PATH}")
display(df_files.select("file_name", "extension", "file_size_bytes", "doc_title").limit(20))

# COMMAND ----------

# MAGIC %md
# MAGIC # Step 2 — Parse & Chunk each file

# COMMAND ----------

import tempfile

# Docling format map
_FORMAT_MAP = {
    ".pdf": "PDF", ".docx": "DOCX", ".docm": "DOCX", ".pptx": "PPTX",
    ".xlsx": "XLSX", ".xlsm": "XLSX", ".xlsb": "XLSX", ".html": "HTML",
    ".htm": "HTML", ".xml": "HTML", ".md": "MD", ".txt": "MD",
}

def parse_and_chunk_file(file_path, file_bytes, extension, doc_title, converter, chunker):
    """Parse one file with Docling + chunk it. Returns list of chunk dicts."""
    ext = f".{extension}"
    fmt_name = _FORMAT_MAP.get(ext)
    if not fmt_name:
        return [], f"Unsupported extension: {ext}", ""

    input_format = getattr(InputFormat, fmt_name, None)
    if input_format is None:
        return [], f"Unknown Docling format: {fmt_name}", ""

    try:
        # Write to temp file for Docling
        with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmp:
            tmp.write(file_bytes)
            tmp_path = tmp.name

        # Parse
        result = converter.convert(tmp_path)
        doc = result.document
        md_text = doc.export_to_markdown()

        if not md_text or len(md_text.strip()) == 0:
            return [], None, "EMPTY_TEXT"

        # Chunk
        chunks = []
        try:
            chunk_iter = chunker.chunk(doc)
            for i, chunk in enumerate(chunk_iter):
                text = chunk.text if hasattr(chunk, 'text') else str(chunk)
                if not text or len(text.strip()) < 20:
                    continue

                # Estimate token count (~3.5 chars/token)
                token_count = max(1, int(len(text) / 3.5))

                # Extract semantic headers from chunk metadata
                headers = ""
                if hasattr(chunk, 'meta') and chunk.meta:
                    hdrs = getattr(chunk.meta, 'headings', None)
                    if hdrs:
                        headers = " > ".join(hdrs)

                chunks.append({
                    "chunk_index": i,
                    "chunk_text": text,
                    "chunk_token_count": token_count,
                    "chunk_content_type": "text",
                    "semantic_headers": headers,
                })
        except Exception as chunk_err:
            # Fallback: if HybridChunker fails, split by paragraphs
            logger.warning(f"HybridChunker failed for {file_path}: {chunk_err}. Falling back to paragraph split.")
            paragraphs = [p.strip() for p in md_text.split("\n\n") if len(p.strip()) > 20]
            for i, para in enumerate(paragraphs):
                chunks.append({
                    "chunk_index": i,
                    "chunk_text": para,
                    "chunk_token_count": max(1, int(len(para) / 3.5)),
                    "chunk_content_type": "text",
                    "semantic_headers": "",
                })

        return chunks, None, "SUCCESS"

    except Exception as e:
        return [], str(e)[:500], "ERROR"
    finally:
        try:
            os.unlink(tmp_path)
        except Exception:
            pass

logger.info("Parse & chunk function defined.")

# COMMAND ----------

converter = build_converter()
chunker = build_chunker()
logger.info("Converter & chunker built.")

files_collected = df_files.select("path", "content", "extension", "doc_title", "file_name", "modificationTime").collect()
logger.info(f"Processing {len(files_collected)} file(s)...")

all_chunks = []
errors = []

for row in files_collected:
    file_path = row["path"]
    file_bytes = bytes(row["content"])
    extension = row["extension"]
    doc_title = row["doc_title"]
    file_name = row["file_name"]

    # Extract REF-like identifier from filename (first token before underscore)
    stem = Path(file_name).stem
    ref = stem.split("_")[0] if "_" in stem else stem.split(" ")[0]

    chunks, error, status = parse_and_chunk_file(file_path, file_bytes, extension, doc_title, converter, chunker)

    if status == "ERROR":
        errors.append({"file_name": file_name, "error": error})
        logger.warning(f"  ERROR: {file_name} — {error[:100]}")
    elif status == "EMPTY_TEXT":
        logger.info(f"  EMPTY: {file_name}")
    else:
        logger.info(f"  OK: {file_name} — {len(chunks)} chunk(s)")

    # Source prefix matching Intraqual format (simplified: no division/category/date)
    source_prefix = f"[Source: {ref} | Title: {doc_title}]"

    for chunk in chunks:
        raw_id = f"{file_name}::{chunk['chunk_index']}"
        chunk_id = hashlib.sha256(raw_id.encode()).hexdigest()[:16]

        # Prepend source prefix to chunk_text (same pattern as Intraqual chunks)
        full_text = f"{source_prefix}\n\n{chunk['chunk_text']}"
        chunk_sha = hashlib.sha256(full_text.encode()).hexdigest()

        all_chunks.append({
            "IDDOC": 0,
            "REF": ref,
            "division": "GENERIC",
            "chunk_id": chunk_id,
            "chunk_index": chunk["chunk_index"],
            "chunk_text": full_text,
            "chunk_token_count": max(1, int(len(full_text) / 3.5)),
            "chunk_content_type": chunk["chunk_content_type"],
            "semantic_headers": chunk["semantic_headers"],
            "chunk_sha256": chunk_sha,
            "url": file_path,
            "doc_date": None,
        })

logger.info(f"Parsing complete: {len(all_chunks)} total chunks from {len(files_collected)} files.")
if errors:
    logger.warning(f"{len(errors)} file(s) in error:")
    for e in errors:
        logger.warning(f"  - {e['file_name']}: {e['error'][:100]}")

# COMMAND ----------

# MAGIC %md
# MAGIC # Step 3 — Sample 10% REFs from chunks + union with parsed chunks

# COMMAND ----------

if not all_chunks:
    logger.warning("No chunks to write — nothing to do.")
    dbutils.notebook.exit("no_chunks")

# Schema matching chunks exactly
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
generic_count = df_generic_chunks.count()
logger.info(f"Generic parsed chunks: {generic_count}")

# Sample 10% of distinct REFs from the full Intraqual chunks
df_chunks_ref = spark.table("uat_landingzone.qualibot.chunks")
df_sampled_refs = df_chunks_ref.select("REF").distinct().sample(fraction=0.1, seed=42)
sampled_ref_count = df_sampled_refs.count()
logger.info(f"Sampled {sampled_ref_count} REFs from chunks (~10% of 4901)")

df_sampled_intraqual = df_chunks_ref.join(df_sampled_refs, "REF", "inner")
intraqual_count = df_sampled_intraqual.count()
logger.info(f"Intraqual chunks from sampled REFs: {intraqual_count}")

# Union: sampled Intraqual + generic parsed chunks
df_combined = df_sampled_intraqual.unionByName(df_generic_chunks)
total_count = df_combined.count()
logger.info(f"Combined: {total_count} chunks ({intraqual_count} Intraqual + {generic_count} generic)")

df_combined.write.format("delta").mode("overwrite").option("overwriteSchema", "true").saveAsTable(TARGET_CHUNKS_TABLE)
logger.info(f"Table {TARGET_CHUNKS_TABLE} written with {total_count} chunks.")

# Summary by division
display(
    spark.table(TARGET_CHUNKS_TABLE)
    .groupBy("division")
    .agg(
        F.count("*").alias("chunk_count"),
        F.countDistinct("REF").alias("ref_count"),
    )
    .orderBy("division")
)

# COMMAND ----------

# MAGIC %md
# MAGIC # Step 4 — Prepare table for Vector Search & create index

# COMMAND ----------

# Enable CDF (required for Delta Sync index)
spark.sql(f"ALTER TABLE {TARGET_CHUNKS_TABLE} SET TBLPROPERTIES ('delta.enableChangeDataFeed' = 'true')")
logger.info(f"CDF enabled on {TARGET_CHUNKS_TABLE}")

# chunk_id is nullable after unionByName — set NOT NULL before adding PK
spark.sql(f"ALTER TABLE {TARGET_CHUNKS_TABLE} ALTER COLUMN chunk_id SET NOT NULL")
logger.info("chunk_id set to NOT NULL")

try:
    spark.sql(f"ALTER TABLE {TARGET_CHUNKS_TABLE} ADD CONSTRAINT pk_chunk_id PRIMARY KEY (chunk_id)")
    logger.info("Primary key constraint added on chunk_id.")
except Exception as e:
    if "CONSTRAINT_ALREADY_EXISTS" in str(e) or "already exists" in str(e).lower():
        logger.info("Primary key constraint already exists — skipping.")
    else:
        raise

# COMMAND ----------

from databricks.sdk import WorkspaceClient
from databricks.sdk.errors import NotFound
from databricks.sdk.service.vectorsearch import (
    DeltaSyncVectorIndexSpecRequest,
    EmbeddingSourceColumn,
    PipelineType,
    VectorIndexType,
)
import time

INDEX_NAME = f"{CATALOG_SCHEMA}.chunks_test_generic_index"
ENDPOINT_NAME = "qualibot"
EMBEDDING_MODEL = "databricks-qwen3-embedding-0-6b"

w = WorkspaceClient()

# Check if index already exists
created_now = False
try:
    idx = w.vector_search_indexes.get_index(index_name=INDEX_NAME)
    logger.info(f"Index {INDEX_NAME} already exists — ready={idx.status.ready}, rows={idx.status.indexed_row_count}")
except NotFound:
    created_now = True
    logger.info(f"Index {INDEX_NAME} not found — creating...")
    w.vector_search_indexes.create_index(
        name=INDEX_NAME,
        endpoint_name=ENDPOINT_NAME,
        primary_key="chunk_id",
        index_type=VectorIndexType.DELTA_SYNC,
        delta_sync_index_spec=DeltaSyncVectorIndexSpecRequest(
            source_table=TARGET_CHUNKS_TABLE,
            pipeline_type=PipelineType.TRIGGERED,
            embedding_source_columns=[
                EmbeddingSourceColumn(name="chunk_text", embedding_model_endpoint_name=EMBEDDING_MODEL)
            ],
        ),
    )
    logger.info(f"Index {INDEX_NAME} creation started. Waiting up to 15 min...")

    deadline = time.time() + 15 * 60
    while time.time() < deadline:
        idx = w.vector_search_indexes.get_index(index_name=INDEX_NAME)
        ready = idx.status.ready if idx.status else False
        rows = idx.status.indexed_row_count if idx.status else 0
        state = (getattr(idx.status, "detailed_state", None) or "unknown")
        if ready:
            logger.info(f"Index {INDEX_NAME} is READY — rows={rows}")
            break
        logger.info(f"  Not ready yet... state={state}, rows={rows}")
        time.sleep(30)
    else:
        logger.warning(f"Index not ready after 15 min — check status manually.")

    # Trigger initial sync if ready but no rows yet
    if ready and (rows or 0) == 0:
        logger.info("Triggering initial sync...")
        w.vector_search_indexes.sync_index(index_name=INDEX_NAME)
        logger.info("Sync triggered.")

# Wait for the index to be ready (handles both creation and existing INITIALIZING)
logger.info("Waiting for index to be ready...")
deadline = time.time() + 15 * 60
while time.time() < deadline:
    idx = w.vector_search_indexes.get_index(index_name=INDEX_NAME)
    ready = idx.status.ready if idx.status else False
    rows = idx.status.indexed_row_count if idx.status else 0
    state = (getattr(idx.status, "detailed_state", None) or "unknown")
    if ready:
        logger.info(f"Index {INDEX_NAME} is READY — rows={rows}")
        break
    logger.info(f"  Not ready yet... state={state}, rows={rows}")
    time.sleep(30)
else:
    logger.warning(f"Index not ready after 15 min — check status manually.")

# Sync if the index is ready and was not freshly created (fresh ones auto-sync)
if ready and not created_now:
    try:
        w.vector_search_indexes.sync_index(index_name=INDEX_NAME)
        logger.info("Sync triggered.")
    except Exception as e:
        logger.warning(f"Sync trigger failed (may already be syncing): {e}")

logger.info("Done.")