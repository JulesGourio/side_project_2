# Databricks notebook source
# Rechunk test PROD: writes prod_proj.qualibot.chunks_test_tailles and its index chunks_test_tailles_index, next to
# chunks / chunks_index, which are only read. Text passages are cut again from the parsing checkpoint with the sizes
# below; image passages are copied as they are. Run on serverless, one cell, with your own account.
import json
import logging
import re
import sys
import time
import types

import pandas as pd
import pyspark.sql.functions as F
from databricks.sdk import WorkspaceClient
from databricks.sdk.errors import NotFound
from databricks.sdk.service.vectorsearch import (
    DeltaSyncVectorIndexSpecRequest, EmbeddingSourceColumn, PipelineType, VectorIndexType,
)
from pyspark.sql import Window

logger = logging.getLogger("rechunk_test_prod")
logger.setLevel(logging.INFO)
if not logger.handlers:
    _handler = logging.StreamHandler(sys.stdout)
    _handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    logger.addHandler(_handler)
logger.propagate = False

APP_FILES = "/Workspace/.bundle/qualibot/qualibot-prod/files"
SOURCE_CHUNKS = "prod_proj.qualibot.chunks"
CHECKPOINT = "prod_proj.qualibot.parsing_checkpoint"
TARGET_TABLE = "prod_proj.qualibot.chunks_test_tailles"
TARGET_INDEX = "prod_proj.qualibot.chunks_test_tailles_index"
ENDPOINT = "qualibot"
EMBEDDING_MODEL = "databricks-qwen3-embedding-0-6b"
# PROD today: 150 / 300 / 450 tokens, 1600 characters. DEV (better scores): 250 / 500 / 1000 tokens, 4000 characters.
SIZES = {"min_tokens": 200, "target_tokens": 400, "max_tokens": 800, "max_chars": 3200,
         "overlap_ratio": 0.12, "chars_per_token": 3.5}
MAX_CHUNKS_SPREADSHEET = 100
BOILERPLATE_MIN_DOCS = 20
SOURCE_PREFIX_RE = r"^(\[Source: [^\n]*\]\n\n)"

if TARGET_TABLE == SOURCE_CHUNKS or TARGET_INDEX.endswith(".chunks_index"):
    raise RuntimeError("The test table and index must differ from the ones the app uses")


def read_source(name: str) -> str:
    """Source code of one pipeline module of the deployed PROD bundle.

    :param name: module name, e.g. ``chunking``
    :return: the Python source
    """
    with open(f"{APP_FILES}/utils/parsing_pipeline/{name}.py", encoding="utf-8") as handle:
        return handle.read()


SOURCES = {name: read_source(name) for name in ("chunking", "code_normalization")}


def load_modules(sources: dict) -> dict:
    """Build the pipeline modules from their source, once per Python worker.

    :param sources: module name -> source code
    :return: module name -> module
    """
    modules = {}
    for name, source in sources.items():
        module = sys.modules.get(name)
        if module is None or not getattr(module, "_rechunk_test", False):
            module = types.ModuleType(name)
            module._rechunk_test = True
            sys.modules[name] = module
            exec(compile(source, f"{name}.py", "exec"), module.__dict__)
        modules[name] = module
    return modules


def clean_text(text: str) -> str:
    """Document text as the pipeline chunks it: placeholders removed, line endings and blank runs folded.

    :param text: document text of the checkpoint
    :return: the cleaned text, empty when there is none
    """
    text = re.sub(r"\s*<!-- image -->\s*", " ", text or "")
    text = re.sub(r"\s*<!-- formula-not-decoded -->\s*", " ", text)
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "")
    text = re.sub(r"[ \t]+", " ", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


OUT_COLUMNS = ["doc_key", "chunk_index", "chunk_text", "chunk_preprocessed", "cited_codes", "chunk_token_count",
               "chunk_content_type", "semantic_headers"]
OUT_SCHEMA = ("doc_key string, chunk_index int, chunk_text string, chunk_preprocessed string, cited_codes string, "
              "chunk_token_count int, chunk_content_type string, semantic_headers string")


def rechunk(batches):
    """Cut every document of the batches into passages with ``SIZES``.

    :param batches: pandas frames with ``doc_key``, ``REF``, ``prefix``, ``document_text``
    :return: iterator of pandas frames with ``OUT_COLUMNS``
    """
    modules = load_modules(SOURCES)
    chunking, codes = modules["chunking"], modules["code_normalization"]

    def count_tokens(text: str) -> int:
        """Token estimate from the characters.

        :param text: passage text
        :return: estimated tokens
        """
        return max(1, int(len(text) / SIZES["chars_per_token"])) if text else 0

    for pdf in batches:
        rows = []
        for doc in pdf.itertuples(index=False):
            text = clean_text(doc.document_text)
            if not text:
                continue
            for chunk in chunking.chunk_markdown(text, count_tokens=count_tokens, **SIZES):
                full = (doc.prefix or "") + chunk["chunk_text"]
                rows.append([doc.doc_key, int(chunk["chunk_index"]), full, codes.normalize_codes(full),
                             " ".join(codes.cited_codes(full, doc.REF or "")), int(chunk["chunk_token_count"]),
                             chunk["chunk_content_type"], json.dumps(chunk.get("metadata") or {}, ensure_ascii=False)])
        yield pd.DataFrame(rows, columns=OUT_COLUMNS)


try:
    spark.conf.set("spark.sql.execution.arrow.maxRecordsPerBatch", "16")
except Exception:
    logger.info("Arrow batch size left at its default")

source = spark.table(SOURCE_CHUNKS)
meta_columns = [c for c in ["REF", "division", "url", "doc_date", "titre", "type_document", "indice", "langue"]
                if c in source.columns]
first_text = Window.partitionBy("IDDOC").orderBy("chunk_index")
docs = (source.where(F.col("chunk_content_type") != "image")
        .withColumn("_rn", F.row_number().over(first_text)).where("_rn = 1")
        .withColumn("prefix", F.regexp_extract("chunk_text", SOURCE_PREFIX_RE, 1))
        .select(F.col("IDDOC").cast("string").alias("doc_key"), "IDDOC", *meta_columns, "prefix"))

checkpoint = spark.table(CHECKPOINT)
ext = ["source_file_extension"] if "source_file_extension" in checkpoint.columns else []
by_time = Window.partitionBy("IDDOC").orderBy(F.desc("ingestion_timestamp"))
best = Window.partitionBy("IDDOC").orderBy(
    F.when(F.col("document_sha256").eqNullSafe(F.col("_latest_sha")), 0).otherwise(1),
    F.when(F.col("parse_status") == "SUCCESS", 0).otherwise(1),
    F.desc("ingestion_timestamp"),
)
texts = (checkpoint.select("IDDOC", "parse_status", "ingestion_timestamp", "document_sha256", "document_text", *ext)
         .withColumn("_latest_sha", F.first("document_sha256").over(by_time))
         .withColumn("_rn", F.row_number().over(best))
         .where("_rn = 1 AND parse_status = 'SUCCESS'")
         .select(F.col("IDDOC").cast("string").alias("doc_key"), "document_text", *ext))

work = docs.join(texts, "doc_key").repartition(256, "doc_key")
passages = work.select("doc_key", "REF", "prefix", "document_text").mapInPandas(rechunk, OUT_SCHEMA)

body_key = F.sha2(F.trim(F.regexp_replace(
    F.lower(F.regexp_replace(F.col("chunk_text"), r"^\[[^\]]*\]\s*", "")), r"\s+", " ")), 256)
new = (passages.join(work.drop("prefix", "document_text"), "doc_key")
       .withColumn("chunk_id", F.concat_ws("-", F.col("IDDOC").cast("string"),
                                           F.lpad((F.col("chunk_index") + 1).cast("string"), 6, "0")))
       .withColumn("chunk_sha256", F.sha2("chunk_text", 256))
       .withColumn("body_sha256", body_key))
new = (new.withColumn("_rn", F.row_number().over(Window.partitionBy("IDDOC", "chunk_sha256").orderBy("chunk_index")))
       .where("_rn = 1").drop("_rn"))
if ext:
    new = (new.withColumn("_rn", F.row_number().over(Window.partitionBy("IDDOC").orderBy("chunk_index")))
           .where(~F.lower(F.col("source_file_extension")).isin("xlsx", "xls", "xlsm")
                  | (F.col("_rn") <= MAX_CHUNKS_SPREADSHEET))
           .drop("_rn"))
common = (new.groupBy("body_sha256").agg(F.countDistinct("IDDOC").alias("_docs"))
          .where(F.col("_docs") >= BOILERPLATE_MIN_DOCS).select("body_sha256", F.lit(True).alias("_common")))
new = (new.join(F.broadcast(common), "body_sha256", "left")
       .withColumn("chunk_content_type",
                   F.when(F.col("_common") & F.col("chunk_content_type").isin("text", "table", "mixed"),
                          F.lit("boilerplate")).otherwise(F.col("chunk_content_type"))))
new = new.select(*[F.col(f.name).cast(f.dataType).alias(f.name) if f.name in new.columns
                   else F.lit(None).cast(f.dataType).alias(f.name) for f in source.schema.fields])
result = new.unionByName(source.where(F.col("chunk_content_type") == "image"))

logger.info("Writing %s (sizes %s)", TARGET_TABLE, SIZES)
try:
    (result.write.format("delta").mode("overwrite").option("overwriteSchema", "true")
     .option("delta.enableChangeDataFeed", "true").saveAsTable(TARGET_TABLE))
except Exception as exc:
    raise RuntimeError(f"Cannot write {TARGET_TABLE} with this account (CREATE TABLE on prod_proj.qualibot needed): "
                       f"{str(exc)[:300]}") from exc
spark.sql(f"ALTER TABLE {TARGET_TABLE} SET TBLPROPERTIES (delta.enableChangeDataFeed = true)")


def stats(table: str) -> pd.DataFrame:
    """Passages and median length per type.

    :param table: chunk table
    :return: one row per type and a TOTAL row
    """
    return spark.sql(f"""
        SELECT coalesce(chunk_content_type, 'TOTAL') AS type, count(*) AS chunks, count(DISTINCT IDDOC) AS docs,
               percentile(length(chunk_text), 0.5) AS median_chars
        FROM {table} GROUP BY ROLLUP(chunk_content_type) ORDER BY type""").toPandas()


comparison = stats(SOURCE_CHUNKS).merge(stats(TARGET_TABLE), on="type", how="outer", suffixes=("_prod", "_test"))
logger.info("Passages, PROD chunks against the test table:\n%s", comparison.to_string(index=False))
target_rows = spark.table(TARGET_TABLE).count()

w = WorkspaceClient()
try:
    w.vector_search_indexes.get_index(index_name=TARGET_INDEX)
    logger.info("%s exists: sync", TARGET_INDEX)
    w.vector_search_indexes.sync_index(index_name=TARGET_INDEX)
except NotFound:
    logger.info("%s absent: creation on endpoint %s", TARGET_INDEX, ENDPOINT)
    w.vector_search_indexes.create_index(
        name=TARGET_INDEX,
        endpoint_name=ENDPOINT,
        primary_key="chunk_id",
        index_type=VectorIndexType.DELTA_SYNC,
        delta_sync_index_spec=DeltaSyncVectorIndexSpecRequest(
            source_table=TARGET_TABLE,
            pipeline_type=PipelineType.TRIGGERED,
            embedding_source_columns=[EmbeddingSourceColumn(name="chunk_preprocessed",
                                                            embedding_model_endpoint_name=EMBEDDING_MODEL)],
        ),
    )

deadline = time.time() + 3 * 3600
while True:
    status = w.vector_search_indexes.get_index(index_name=TARGET_INDEX).status
    indexed = getattr(status, "indexed_row_count", None) or 0
    logger.info("%s: ready=%s, %d / %d rows", TARGET_INDEX, status.ready, indexed, target_rows)
    if status.ready and indexed >= target_rows:
        logger.info("Index ready: run the evaluation with indexes = deploye,tailles_test=%s", TARGET_INDEX)
        break
    if time.time() > deadline:
        logger.warning("Index still syncing after 3 h: the notebook stops, the sync goes on")
        break
    time.sleep(60)
