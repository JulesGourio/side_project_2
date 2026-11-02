# Databricks notebook source
# Rechunk of PROD, run in DEV: reads prod_proj.qualibot.chunks and parsing_checkpoint, cuts the text passages again with
# the sizes below (image passages copied as they are), writes dev_proj.qualibot.chunks_rechunk_prod and compares it with
# the PROD chunks. Nothing is written in PROD. One cell, on a classic cluster of the DEV workspace, your own account.
import json
import logging
import re
import sys
import types

import pandas as pd
import pyspark.sql.functions as F
from pyspark.sql import Window

logger = logging.getLogger("rechunk_prod_in_dev")
logger.setLevel(logging.INFO)
if not logger.handlers:
    _handler = logging.StreamHandler(sys.stdout)
    _handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    logger.addHandler(_handler)
logger.propagate = False

APP_FILES = "/Workspace/.bundle/qualibot/dev/files"
SOURCE_CHUNKS = "prod_proj.qualibot.chunks"
CHECKPOINT = "prod_proj.qualibot.parsing_checkpoint"
TARGET_TABLE = "dev_proj.qualibot.chunks_rechunk_prod"
# The reranker reads the first 2,000 characters of REF + semantic_headers + chunk_text, and chunk_text starts with the
# [Source: ...] line (about 200 characters): the body stays under 1,650. The higher minimum and target fill passages
# up to that ceiling instead of leaving many small ones (past_rerank in the log: share of passages over 2,000).
SIZES = {"min_tokens": 250, "target_tokens": 300, "max_tokens": 470, "max_chars": 1650,
         "overlap_ratio": 0.12, "chars_per_token": 3.5}
RERANK_CHARS = 2000
MAX_CHUNKS_SPREADSHEET = 100
# Only the first MAX_CHUNKS_SPREADSHEET passages of a spreadsheet are kept: its text is cut before chunking, so a huge
# workbook does not exhaust the memory of a Python worker.
SPREADSHEET_MAX_CHARS = MAX_CHUNKS_SPREADSHEET * 1650 * 3
BOILERPLATE_MIN_DOCS = 20
SOURCE_PREFIX_RE = r"^(\[Source: [^\n]*\]\n\n)"

def read_source(name: str) -> str:
    """Source code of one pipeline module of the deployed DEV bundle.

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

    :param batches: pandas frames with ``doc_key``, ``REF``, ``prefix``, ``document_text``, ``extension``
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

    # One document at a time in and out: a whole batch of document texts and their passages does not fit a worker.
    for pdf in batches:
        for doc in pdf.itertuples(index=False):
            text = clean_text(doc.document_text)
            if str(doc.extension or "").lower() in ("xlsx", "xls", "xlsm"):
                text = text[:SPREADSHEET_MAX_CHARS]
            if not text:
                continue
            rows = []
            for chunk in chunking.chunk_markdown(text, count_tokens=count_tokens, **SIZES):
                full = (doc.prefix or "") + chunk["chunk_text"]
                rows.append([doc.doc_key, int(chunk["chunk_index"]), full, codes.normalize_codes(full),
                             " ".join(codes.cited_codes(full, doc.REF or "")), int(chunk["chunk_token_count"]),
                             chunk["chunk_content_type"], json.dumps(chunk.get("metadata") or {}, ensure_ascii=False)])
            yield pd.DataFrame(rows, columns=OUT_COLUMNS)


spark.conf.set("spark.sql.execution.arrow.maxRecordsPerBatch", "4")

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

work = docs.join(texts, "doc_key").repartition(2048, "doc_key")
extension = F.col(ext[0]) if ext else F.lit(None).cast("string")
passages = (work.select("doc_key", "REF", "prefix", "document_text", extension.alias("extension"))
            .mapInPandas(rechunk, OUT_SCHEMA))

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

logger.info("Writing %s with the sizes %s", TARGET_TABLE, SIZES)
result.write.format("delta").mode("overwrite").option("overwriteSchema", "true").saveAsTable(TARGET_TABLE)


def stats(table: str) -> pd.DataFrame:
    """Passages, median length and share past the reranker window, per type.

    :param table: chunk table
    :return: one row per type and a TOTAL row
    """
    return spark.sql(f"""
        SELECT coalesce(chunk_content_type, 'TOTAL') AS type, count(*) AS chunks, count(DISTINCT IDDOC) AS docs,
               percentile(length(chunk_text), 0.5) AS median_chars,
               round(avg(CASE WHEN length(coalesce(REF, '')) + length(coalesce(semantic_headers, ''))
                                   + length(chunk_text) > {RERANK_CHARS} THEN 1.0 ELSE 0.0 END), 3) AS past_rerank
        FROM {table} GROUP BY ROLLUP(chunk_content_type) ORDER BY type""").toPandas()


comparison = stats(SOURCE_CHUNKS).merge(stats(TARGET_TABLE), on="type", how="outer", suffixes=("_prod", "_new"))
logger.info("Passages with the sizes %s, PROD chunks against the new cut:\n%s", SIZES, comparison.to_string(index=False))
