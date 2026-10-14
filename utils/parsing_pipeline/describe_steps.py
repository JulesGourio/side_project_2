"""Phases of the `4_describe_images` task (see `create_proj_image_metadata.py`).

Describes the PENDING images with the vision LLM, turns the descriptions into image passages of the chunk tables,
and promotes the documents waiting for their images (EMPTY_TEXT, IMAGES_PENDING) once all of them are described.
Driver side only: the pandas function that places images runs on the executors but is defined inside
`build_image_chunks`, so it is shipped by value.
"""
import asyncio
import gc
import math
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from typing import List

import nest_asyncio
from databricks.sdk.runtime import spark
from pyspark.sql import functions as F

import doc_status
from leap_utils.common.logger import get_logger
from llm_describe import EndpointBreaker, describe_all_images, one_per_image, safe_requests_per_minute
from tables import add_missing_columns, merge_into, table_exists, write_table
from utils import intraqual_ref_url, token_count_udf
from config import (
    CATALOG_SCHEMA,
    CHARS_PER_TOKEN,
    DOC_DATE_CUTOFF,
    EMBED_SOURCE_PREFIX,
    LLM_AVG_INPUT_TOKENS,
    LLM_AVG_OUTPUT_TOKENS,
    IMAGE_MAX_ATTEMPTS,
    LLM_BREAKER_FAILURES,
    LLM_CHECKPOINT_CHUNK_SIZE,
    LLM_ITPM_BUDGET,
    LLM_MAX_CONCURRENT,
    LLM_MAX_RETRIES,
    LLM_MAX_TOKENS,
    LLM_OTPM_BUDGET,
    LLM_QPH_BUDGET,
    LLM_ROUND_WAITS_S,
    LLM_ROUNDS,
    LLM_TEMPERATURE,
    MAX_CHUNK_CHARS,
    MAX_CHUNK_TOKENS,
    MIN_INDEXABLE_DESC_CHARS,
    TARGET_CHUNK_TABLE,
    TARGET_CHUNK_TABLE_ARCHIVE,
    TARGET_IMAGE_METADATA_TABLE,
    TARGET_PROCESSED_FILES_TABLE,
    TABLE_SUFFIX,
)

logger = get_logger(__name__)

MOVED_BY_IMAGES = doc_status.MOVED_BY_IMAGES
IMAGE_TABLE = TARGET_IMAGE_METADATA_TABLE
_UPDATES_TABLE = f"{CATALOG_SCHEMA}._image_updates_temp{TABLE_SUFFIX}"
_PROCESSED_UPDATES_TABLE = f"{CATALOG_SCHEMA}._processed_updates_temp{TABLE_SUFFIX}"
_UPDATED_IMAGE_COLUMNS = ("description", "input_tokens", "output_tokens", "status", "described_at", "attempts",
                          "last_error_kind")

_PLACED_SCHEMA = "IDDOC long, image_id int, part_no int, anchor_chunk_index int, section string, body string"


def reset_images_to_pending():
    """Full mode: every image goes back to PENDING, with no attempt yet, and is described again."""
    spark.sql(f"""
        UPDATE {IMAGE_TABLE}
        SET status = 'PENDING', description = NULL,
            input_tokens = NULL, output_tokens = NULL, described_at = NULL,
            attempts = 0, last_error_kind = NULL
        WHERE volume_path IS NOT NULL
    """)
    logger.info(f"[FULL] Reset all images to PENDING in {IMAGE_TABLE}")


def pending_images():
    """Images to describe: the retried statuses (PENDING, and ERROR rows left by earlier runs). FAILED_LIMIT is not one."""
    return spark.table(IMAGE_TABLE).filter(F.col("status").isin(*doc_status.IMAGE_RETRIED_STATUSES) & F.col("volume_path").isNotNull())


_UPDATES_SCHEMA = ("IDDOC long, image_id int, description string, input_tokens int, output_tokens int, "
                   "status string, described_at timestamp, attempts int, last_error_kind string")


def _merge_chunk_results(results):
    """Persist one checkpoint chunk of descriptions into the image table with a MERGE.

    The schema is explicit: a chunk where every image stayed PENDING has no description to infer the type from.
    """
    now = datetime.now()
    update_rows = [
        (
            int(r["IDDOC"]),
            int(r["image_id"]),
            r["description"],
            int(r["input_tokens"]),
            int(r["output_tokens"]),
            r["status"],
            now if r["status"] in ("DONE", "SKIPPED") else None,
            int(r["attempts"]),
            r["last_error_kind"],
        )
        for r in results
    ]
    # A duplicated (IDDOC, image_id) in the source would fail the merge.
    df_updates = spark.createDataFrame(update_rows, _UPDATES_SCHEMA).dropDuplicates(["IDDOC", "image_id"])
    add_missing_columns(df_updates, IMAGE_TABLE)
    # merge_into takes whole rows: the image rows of this chunk, with the new values on the updated columns.
    target = spark.table(IMAGE_TABLE)
    new_values = df_updates.select("IDDOC", "image_id", *[F.col(c).alias(f"_new_{c}") for c in _UPDATED_IMAGE_COLUMNS])
    write_table(
        target.join(new_values, on=["IDDOC", "image_id"], how="inner").select(*[
            F.col(f"_new_{c}").alias(c) if c in _UPDATED_IMAGE_COLUMNS else F.col(c) for c in target.columns
        ]),
        _UPDATES_TABLE, "overwrite", overwrite_schema=True,
    )
    merge_into(spark.table(_UPDATES_TABLE), IMAGE_TABLE, ["IDDOC", "image_id"], do_when_notmatched=False)


@dataclass
class _RunTally:
    """Counts and tokens of described images: one chunk, or the whole run."""
    images: int = 0
    done: int = 0
    skipped: int = 0
    pending: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    failed_input_tokens: int = 0
    failed_output_tokens: int = 0
    pending_by_kind: Counter = field(default_factory=Counter)
    limited: List[tuple] = field(default_factory=list)   # (IDDOC, image_id, last_error_kind, attempts) of FAILED_LIMIT

    def add(self, described):
        for r in described:
            self.images += 1
            if r["status"] == "FAILED_LIMIT":
                self.limited.append((r["IDDOC"], r["image_id"], r["last_error_kind"] or "unknown", r["attempts"]))
            self.input_tokens += int(r["input_tokens"])
            self.output_tokens += int(r["output_tokens"])
            self.failed_input_tokens += int(r["failed_input_tokens"])
            self.failed_output_tokens += int(r["failed_output_tokens"])
            if r["status"] == "DONE":
                self.done += 1
            elif r["status"] == "SKIPPED":
                self.skipped += 1
            elif r["status"] == "PENDING":
                self.pending += 1
                self.pending_by_kind[r["last_error_kind"] or "unknown"] += 1
        return self


def describe_images(rows, ws_host, ws_token, endpoints):
    """Call the vision LLM on `rows` in checkpoint chunks, merging each chunk's results as soon as it is done.

    `endpoints` is the chain tried for each image. An image no endpoint described stays PENDING for the next run.
    One circuit breaker serves the whole run. A mid-run failure keeps the chunks already persisted.
    `nest_asyncio` lets `asyncio.run` work inside a notebook.
    """
    rows = one_per_image(rows)
    if not rows:
        logger.info("Nothing to describe - no PENDING or ERROR images found in image_metadata.")
        return
    # Without an endpoint every image would stay PENDING with no error: a missing setting must fail the run instead.
    if not endpoints:
        raise ValueError("No LLM endpoint: set PARSING_LLM_ENDPOINT (primary) or llm_fallback_endpoints.")

    nest_asyncio.apply()
    # Throughput bounded by the endpoint quota; the output tokens per minute are the bottleneck.
    rpm = safe_requests_per_minute(
        LLM_ITPM_BUDGET, LLM_OTPM_BUDGET, LLM_QPH_BUDGET, LLM_AVG_INPUT_TOKENS, LLM_AVG_OUTPUT_TOKENS,
    )
    logger.info(f"Rate limiter : {rpm:.0f} req/min  (ITPM<={LLM_ITPM_BUDGET:,}, OTPM<={LLM_OTPM_BUDGET:,}, "
                f"QPH<={LLM_QPH_BUDGET:,})")
    logger.info(f"Endpoints  : {' -> '.join(endpoints)}  ({LLM_ROUNDS} passes, waits {list(LLM_ROUND_WAITS_S)} s, "
                f"an endpoint skipped after {LLM_BREAKER_FAILURES} failed images in a row, "
                f"an image set aside as FAILED_LIMIT after {IMAGE_MAX_ATTEMPTS} attempts)")

    breaker = EndpointBreaker(LLM_BREAKER_FAILURES)
    n_chunks = math.ceil(len(rows) / LLM_CHECKPOINT_CHUNK_SIZE)
    logger.info(f"{len(rows)} images -> {n_chunks} chunk(s) of up to {LLM_CHECKPOINT_CHUNK_SIZE}, persisted after each chunk")

    total = _RunTally()
    for chunk_idx in range(n_chunks):
        chunk = rows[chunk_idx * LLM_CHECKPOINT_CHUNK_SIZE:(chunk_idx + 1) * LLM_CHECKPOINT_CHUNK_SIZE]
        started = time.time()
        try:
            described = asyncio.run(describe_all_images(
                rows=chunk, ws_host=ws_host, ws_token=ws_token, endpoints=endpoints,
                max_tokens=LLM_MAX_TOKENS, temperature=LLM_TEMPERATURE, max_retries=LLM_MAX_RETRIES,
                max_concurrent=LLM_MAX_CONCURRENT, requests_per_minute=rpm, breaker=breaker,
                rounds=LLM_ROUNDS, round_waits_s=LLM_ROUND_WAITS_S, max_attempts=IMAGE_MAX_ATTEMPTS,
            ))
        except Exception as exc:
            logger.error(f"[FATAL] chunk {chunk_idx + 1}/{n_chunks} describe_all_images failed: {str(exc)[:300]}")
            logger.info(f"        {total.done} described / {total.pending} left pending from prior chunks are already persisted.")
            raise

        _merge_chunk_results(described)
        chunk_tally = _RunTally().add(described)
        total.add(described)
        # Released per chunk: the single-node cluster has little memory.
        del described
        gc.collect()

        logger.info(f"[chunk {chunk_idx + 1}/{n_chunks}] {chunk_tally.images} images in {time.time() - started:.1f}s | "
                    f"done={chunk_tally.done} skipped={chunk_tally.skipped} pending={chunk_tally.pending} | "
                    f"tokens {chunk_tally.input_tokens:,} in / {chunk_tally.output_tokens:,} out")
        if breaker.open_endpoints:
            logger.warning(f"  Endpoints skipped for the rest of the run: {', '.join(breaker.open_endpoints)}")

    spark.sql(f"DROP TABLE IF EXISTS {_UPDATES_TABLE}")
    _log_description_summary(total)


_LIMIT_LINES = 50


def _log_description_summary(total):
    """Log the run: outcomes, failures by kind, the images set aside this run, tokens (failed attempts too), what remains."""
    df_images = spark.table(IMAGE_TABLE)
    remaining = df_images.filter(F.col("status").isin(*doc_status.IMAGE_RETRIED_STATUSES)).count()
    limit_total = df_images.filter(F.col("status") == "FAILED_LIMIT").count()
    logger.info(f"Description summary: {total.done} described, {total.skipped} skipped, {total.pending} left pending, "
                f"{len(total.limited)} set aside as FAILED_LIMIT over {total.images} images")
    if total.pending_by_kind:
        kinds = ", ".join(f"{kind} {n}" for kind, n in total.pending_by_kind.most_common())
        logger.info(f"  Left pending this run, by last error kind: {kinds}")
    for iddoc, image_id, kind, attempts in total.limited[:_LIMIT_LINES]:
        logger.warning(f"  FAILED_LIMIT: IDDOC {iddoc} image {image_id}, last error {kind}, {attempts} attempts")
    if len(total.limited) > _LIMIT_LINES:
        logger.warning(f"  ... {len(total.limited) - _LIMIT_LINES} more FAILED_LIMIT images this run")
    logger.info(f"  Tokens: {total.input_tokens:,} in / {total.output_tokens:,} out, of which failed attempts "
                f"{total.failed_input_tokens:,} in / {total.failed_output_tokens:,} out")
    logger.info(f"  Remaining to describe: {remaining} (PENDING, described again by the next run); "
                f"FAILED_LIMIT in the table: {limit_total} (not retried, see image_metadata.last_error_kind)")
    if remaining == 0:
        logger.info("  -> No image left to describe.")


def described_images_without_chunk(rebuild):
    """Described images that have no passage yet (or all of them when `rebuild`).

    Scoped by an anti-join on `chunk_id` against the chunk tables, not by "described this run": a run that crashed
    after the descriptions were merged but before the passages were written would otherwise leave those images
    without a passage forever (DONE is never selected again). This makes the phase idempotent and self-healing, at
    the price of scanning all DONE images each run.
    """
    df_described = (
        spark.table(IMAGE_TABLE)
        .filter(
            (F.col("status") == "DONE")
            & F.col("description").isNotNull()
            & (F.length(F.trim(F.col("description"))) >= F.lit(MIN_INDEXABLE_DESC_CHARS))
            & (~F.upper(F.trim(F.col("description"))).startswith("SKIP"))
        )
        .withColumn("chunk_id", F.concat_ws("-", F.col("IDDOC").try_cast("string"), F.lit("IMG"),
                                            F.lpad(F.col("image_id").try_cast("string"), 3, "0")))
    )
    if rebuild:
        return df_described
    # Image passages of pre-cutoff documents live in chunks_archive, not chunks.
    for table in (TARGET_CHUNK_TABLE, TARGET_CHUNK_TABLE_ARCHIVE):
        if table_exists(table):
            df_existing = spark.table(table).filter(F.col("chunk_content_type") == "image").select("chunk_id")
            df_described = df_described.join(F.broadcast(df_existing), on="chunk_id", how="left_anti")
    return df_described


def log_nothing_to_inject():
    """Explain why no image passage is written: every DONE image already has one."""
    df_images = spark.table(IMAGE_TABLE)
    n_done = df_images.filter((F.col("status") == "DONE") & F.col("description").isNotNull()).count()
    n_pending = df_images.filter(F.col("status").isin(*doc_status.IMAGE_RETRIED_STATUSES)).count()
    logger.info("No new image chunks to inject.")
    logger.info(f"  {n_done} DONE images -> all already have chunks in {TARGET_CHUNK_TABLE}")
    if n_pending > 0:
        logger.info(f"  {n_pending} images not described -> not injected (described again next run)")


def build_image_chunks(df_described):
    """Image passages: position in the document, section, caption and a source prefix, long transcriptions split.

    No LLM call: everything comes from the stored descriptions and the text passages of the same document.
    """
    import pandas as pd

    df_text_all = spark.table(TARGET_CHUNK_TABLE).select(
        "IDDOC", "chunk_index", "chunk_content_type", "chunk_text", "semantic_headers")
    if table_exists(TARGET_CHUNK_TABLE_ARCHIVE):
        df_text_all = df_text_all.unionByName(spark.table(TARGET_CHUNK_TABLE_ARCHIVE).select(
            "IDDOC", "chunk_index", "chunk_content_type", "chunk_text", "semantic_headers"))
    df_text_chunks = df_text_all.filter(F.col("chunk_content_type") != "image")
    df_max_idx = df_text_chunks.groupBy("IDDOC").agg(F.max("chunk_index").alias("max_text_index"))

    # Document metadata is not on image_metadata: it comes from parsed_documents, for the source prefix.
    df_pf = spark.table(TARGET_PROCESSED_FILES_TABLE)
    df_doc_meta = (
        df_pf.select("IDDOC", "doc_date",
                     *[F.col(c) if c in df_pf.columns else F.lit(None).cast("string").alias(c)
                       for c in ("type_document", "indice", "langue")])
        .withColumn("indice", F.col("indice").try_cast("string"))
        .dropDuplicates(["IDDOC"])
    )

    # Defined here so it is pickled by value: the executors cannot import this module.
    def place_images(images: "pd.DataFrame", texts: "pd.DataFrame") -> "pd.DataFrame":
        import json
        import chunking
        chunks = []
        for t in texts.itertuples():
            try:
                headers = json.loads(t.semantic_headers or "{}")
            except ValueError:
                headers = {}
            chunks.append((int(t.chunk_index), t.chunk_text or "",
                           {k: v for k, v in headers.items() if k.startswith("Header")}))
        rows = []
        for im in images.itertuples():
            anchor = chunking.image_anchor(im.context_text or "", chunks)
            headers = anchor[1] if anchor else {}
            captions = list(im.captions) if im.captions is not None else []
            parts = chunking.split_long_description(im.description or "", max_chars=MAX_CHUNK_CHARS,
                                                    max_tokens=MAX_CHUNK_TOKENS)
            for n, part in enumerate(parts, 1):
                rows.append({"IDDOC": int(im.IDDOC), "image_id": int(im.image_id), "part_no": n,
                             "anchor_chunk_index": anchor[0] if anchor else None,
                             "section": " > ".join(headers[k] for k in sorted(headers)) or None,
                             "body": chunking.image_passage_body(part, captions, headers)})
        return pd.DataFrame(rows, columns=["IDDOC", "image_id", "part_no", "anchor_chunk_index", "section", "body"])

    df_images = df_described.select("IDDOC", "image_id", "description", "captions", "context_text")
    df_texts = (
        df_text_chunks.join(df_images.select("IDDOC").distinct(), on="IDDOC", how="inner")
        .select("IDDOC", "chunk_index", "chunk_text", "semantic_headers")
    )
    df_placed = df_images.groupBy("IDDOC").cogroup(df_texts.groupBy("IDDOC")).applyInPandas(place_images, _PLACED_SCHEMA)

    image_id = F.concat_ws("-", F.col("IDDOC").try_cast("string"), F.lit("IMG"),
                           F.lpad(F.col("image_id").try_cast("string"), 3, "0"))
    return (
        df_described.drop("description", "chunk_id")
        .join(df_placed, on=["IDDOC", "image_id"], how="inner")
        .join(df_max_idx, on="IDDOC", how="left")
        .join(F.broadcast(df_doc_meta), on="IDDOC", how="left")
        .withColumn("url", intraqual_ref_url(F.col("ref")))
        .withColumns({
            "max_text_index": F.coalesce(F.col("max_text_index"), F.lit(-1)),
            "chunk_index": F.coalesce(F.col("max_text_index"), F.lit(-1)) + F.lit(1) + F.col("image_id"),
            # Same prefix as utils.source_prefixed_text(), plus an "Image: page N, label" field.
            "chunk_text": (F.concat(
                F.lit("[Source: "), F.coalesce(F.col("ref"), F.lit("")),
                F.lit(" | Title: "), F.coalesce(F.col("titre"), F.lit("")),
                F.lit(" | Type: "), F.coalesce(F.col("type_document"), F.lit("")),
                F.lit(" | Division: "), F.coalesce(F.col("division"), F.lit("")),
                F.lit(" | Category: "), F.coalesce(F.col("niveau_plus_1"), F.lit("")),
                F.when(F.col("niveau_plus_2").isNotNull(),
                       F.concat(F.lit(" > "), F.col("niveau_plus_2"))).otherwise(F.lit("")),
                F.lit(" | Date de diffusion: "), F.coalesce(F.date_format(F.col("doc_date"), "yyyy-MM-dd"), F.lit("inconnue")),
                F.lit(" | Image: page "), F.coalesce(F.col("page_no").try_cast("string"), F.lit("?")),
                F.lit(", "), F.col("label"), F.lit("]\n\n"), F.col("body"),
            ) if EMBED_SOURCE_PREFIX else F.col("body")),
        })
        .withColumns({
            "chunk_token_count": token_count_udf(F.col("chunk_text")),
            # Part 1 keeps the plain image id (the anti-join in described_images_without_chunk relies on it);
            # the next parts of a long transcription get -2, -3...
            "chunk_id": F.when(F.col("part_no") == 1, image_id)
                         .otherwise(F.concat_ws("-", image_id, F.col("part_no").try_cast("string"))),
            "chunk_content_type": F.lit("image"),
            "semantic_headers": F.to_json(F.struct(
                F.col("label").alias("image_label"),
                F.col("page_no").try_cast("string").alias("page"),
                F.col("volume_path").alias("volume_path"),
                F.col("area_ratio").try_cast("string").alias("area_ratio"),
                F.col("captions").try_cast("string").alias("captions"),
                F.col("section").alias("section"),
            )),
            "chunk_sha256": F.sha2(F.col("chunk_text"), 256),
            "body_sha256": F.sha2(F.col("body"), 256),
            "indice": F.col("indice").try_cast("string"),
        })
        .withColumn("REF", F.coalesce(F.col("ref"), F.col("source_file_name")))
        .select(
            "IDDOC", "REF", "division", "chunk_id", "chunk_index", "chunk_text", "chunk_token_count",
            "chunk_content_type", "semantic_headers", "chunk_sha256", "url", "doc_date", "titre", "type_document",
            "indice", "langue", "body_sha256", "anchor_chunk_index",
        )
    )


def _merge_chunks(df_chunks, table):
    """Merge on chunk_id rather than delete-all-for-IDDOC then reinsert, so untouched image chunks are not rewritten.

    The columns the passages add reach the table first.
    """
    if not table_exists(table):
        write_table(df_chunks, table, "append", merge_schema=True)
        return
    add_missing_columns(df_chunks, table)
    merge_into(df_chunks, table, ["chunk_id"], do_when_notmatchedbysource=False)


def _update_processed_files(df_values, columns):
    """Set `columns` on the parsed_documents rows of the IDDOCs of `df_values` (IDDOC + columns); the others stay.

    The rows are saved first: the merge source must not read the table it merges into.
    """
    target = spark.table(TARGET_PROCESSED_FILES_TABLE)
    new_values = df_values.select("IDDOC", *[F.col(c).alias(f"_new_{c}") for c in columns])
    write_table(
        target.join(new_values, on="IDDOC", how="inner").select(*[
            F.col(f"_new_{c}").alias(c) if c in columns else F.col(c) for c in target.columns
        ]),
        _PROCESSED_UPDATES_TABLE, "overwrite", overwrite_schema=True,
    )
    merge_into(spark.table(_PROCESSED_UPDATES_TABLE), TARGET_PROCESSED_FILES_TABLE, ["IDDOC"],
               do_when_notmatched=False)
    spark.sql(f"DROP TABLE IF EXISTS {_PROCESSED_UPDATES_TABLE}")


def write_image_chunks(df_image_chunks):
    """Pre-cutoff documents go to chunks_archive, the others to chunks (Change Data Feed on, as Vector Search needs)."""
    # Columns added upstream reach the chunk tables on write (schema auto-merge).
    spark.conf.set("spark.databricks.delta.schema.autoMerge.enabled", "true")

    # Cached because the left_anti join that built df_described would return 0 rows once the first MERGE ran.
    df_cached = df_image_chunks.cache()
    is_recent = F.col("doc_date").isNull() | (F.col("doc_date") >= F.lit(DOC_DATE_CUTOFF).cast("date"))

    df_archive = df_cached.filter(~is_recent)
    n_archive = df_archive.count()
    _merge_chunks(df_archive, TARGET_CHUNK_TABLE_ARCHIVE)
    logger.info(f"Merged {n_archive} image chunks into {TARGET_CHUNK_TABLE_ARCHIVE} (pre-{DOC_DATE_CUTOFF})")

    df_recent = df_cached.filter(is_recent)
    n_recent = df_recent.count()
    _merge_chunks(df_recent, TARGET_CHUNK_TABLE)
    logger.info(f"Merged {n_recent} image chunks into {TARGET_CHUNK_TABLE}")
    df_cached.unpersist()

    spark.sql(f"ALTER TABLE {TARGET_CHUNK_TABLE} SET TBLPROPERTIES (delta.enableChangeDataFeed = true)")
    logger.info(f"CDF enabled on {TARGET_CHUNK_TABLE}")


def promote_documents_awaiting_images():
    """Documents without text that wait for their images move on once no image of them is left to describe.

    The statuses are EMPTY_TEXT (no image left to describe, decided in the same job run) and IMAGES_PENDING (parked by
    3_parse). The outcome is SUCCESS, with the text assembled from the descriptions, or SKIPPED_EMPTY_IMAGES when no
    image was usable. A document with an image still PENDING (or ERROR) stays where it is: the next run describes it.
    A FAILED_LIMIT image is final for its document: the document is promoted without it, the image stays reported.
    """
    df_waiting = (
        spark.table(TARGET_PROCESSED_FILES_TABLE).filter(F.col("parse_status").isin(*MOVED_BY_IMAGES)).select("IDDOC")
    )
    n_waiting = df_waiting.count()
    if n_waiting == 0:
        logger.info("No document waiting for its images.")
        return

    df_their_images = spark.table(IMAGE_TABLE).join(F.broadcast(df_waiting), on="IDDOC", how="inner")
    df_not_ready = df_their_images.filter(F.col("status").isin(*doc_status.IMAGE_RETRIED_STATUSES)).select("IDDOC").distinct()
    df_ready = df_waiting.join(F.broadcast(df_not_ready), on="IDDOC", how="left_anti")
    if df_ready.count() == 0:
        logger.info(f"{n_waiting} document(s) waiting for images, none fully described yet.")
        return

    df_assembled = (
        df_their_images.join(F.broadcast(df_ready), on="IDDOC", how="inner")
        .filter((F.col("status") == "DONE") & F.col("description").isNotNull())
        .groupBy("IDDOC")
        .agg(F.concat_ws("\n\n", F.transform(
            F.sort_array(F.collect_list(F.struct(
                F.coalesce(F.col("page_no"), F.lit(0)).alias("page_no"),
                F.col("description").alias("d"),
            ))),
            lambda x: x["d"],
        )).alias("document_text"))
        .withColumns({
            "document_char_count": F.length("document_text"),
            "document_token_count": F.greatest(
                F.lit(1), F.floor(F.length("document_text") / F.lit(CHARS_PER_TOKEN)).try_cast("int")),
        })
    )
    n_promoted = df_assembled.count()
    if n_promoted > 0:
        _update_processed_files(
            df_assembled.select("IDDOC", "document_char_count", "document_token_count").withColumns({
                "parse_status": F.lit("SUCCESS"),
                "parser_strategy": F.lit("llm_ocr:pdf"),
                "chunking_strategy": F.lit("image_llm_ocr"),
                "images_needing_description": F.lit(0),
            }),
            ["document_char_count", "document_token_count", "parse_status", "parser_strategy", "chunking_strategy",
             "images_needing_description"],
        )
        logger.info(f"Promoted {n_promoted} document(s) to SUCCESS (all images now described).")

    # Ready but every image was SKIP or decorative: terminal as well, so the document is not looked at again.
    df_ready_empty = df_ready.join(df_assembled.select("IDDOC"), on="IDDOC", how="left_anti")
    n_skipped = df_ready_empty.count()
    if n_skipped > 0:
        _update_processed_files(
            df_ready_empty.select("IDDOC").withColumns({
                "parse_status": F.lit("SKIPPED_EMPTY_IMAGES"),
                "parser_strategy": F.lit("llm_ocr:pdf"),
                "images_needing_description": F.lit(0),
            }),
            ["parse_status", "parser_strategy", "images_needing_description"],
        )
        logger.info(f"{n_skipped} document(s) had no usable image -> SKIPPED_EMPTY_IMAGES.")
