"""Phases of the `4_describe_images` task (see `4_Describe_Images_LLM.py`).

Describes the PENDING images with the vision LLM, turns the descriptions into image passages of the chunk tables,
and promotes the EMPTY_TEXT documents whose images are now described. Driver side only: the pandas function that
places images runs on the executors but is defined inside `build_image_chunks`, so it is shipped by value.
"""
import asyncio
import gc
import math
import time
from datetime import datetime

import nest_asyncio
from databricks.sdk.runtime import spark
from delta.tables import DeltaTable
from pyspark.sql import Row
from pyspark.sql import functions as F

from image_utils import describe_all_images, safe_requests_per_minute
from utils import intraqual_ref_url, logger, token_count_udf
from config import (
    CATALOG_SCHEMA,
    CHARS_PER_TOKEN,
    DOC_DATE_CUTOFF,
    EMBED_SOURCE_PREFIX,
    LLM_AVG_INPUT_TOKENS,
    LLM_AVG_OUTPUT_TOKENS,
    LLM_CHECKPOINT_CHUNK_SIZE,
    LLM_ITPM_BUDGET,
    LLM_MAX_CONCURRENT,
    LLM_MAX_RETRIES,
    LLM_MAX_TOKENS,
    LLM_MODEL_ENDPOINT,
    LLM_OTPM_BUDGET,
    LLM_QPH_BUDGET,
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

IMAGE_TABLE = TARGET_IMAGE_METADATA_TABLE
_UPDATES_TABLE = f"{CATALOG_SCHEMA}._image_updates_temp{TABLE_SUFFIX}"

_PLACED_SCHEMA = "IDDOC long, image_id int, part_no int, anchor_chunk_index int, section string, body string"


def reset_images_to_pending():
    """Full mode: every image goes back to PENDING and is described again."""
    spark.sql(f"""
        UPDATE {IMAGE_TABLE}
        SET status = 'PENDING', description = NULL,
            input_tokens = NULL, output_tokens = NULL, described_at = NULL
        WHERE volume_path IS NOT NULL
    """)
    logger.info(f"[FULL] Reset all images to PENDING in {IMAGE_TABLE}")


def pending_images():
    """Images to describe: PENDING, plus ERROR ones (a failed LLM call, e.g. a blank answer) which are retried."""
    return spark.table(IMAGE_TABLE).filter(F.col("status").isin("PENDING", "ERROR") & F.col("volume_path").isNotNull())


def _merge_chunk_results(results):
    """Persist one checkpoint chunk of descriptions into the image table with a MERGE."""
    update_rows = [
        Row(
            IDDOC=int(r["IDDOC"]),
            image_id=int(r["image_id"]),
            description=r["description"],
            input_tokens=int(r.get("input_tokens") or 0),
            output_tokens=int(r.get("output_tokens") or 0),
            status=r["status"],
            described_at=datetime.now(),
        )
        for r in results
    ]
    # MERGE fails on a duplicated (IDDOC, image_id) in its source batch.
    df_updates = spark.createDataFrame(update_rows).dropDuplicates(["IDDOC", "image_id"])
    df_updates.write.format("delta").mode("overwrite").option("overwriteSchema", "true").saveAsTable(_UPDATES_TABLE)
    spark.sql(f"""
        MERGE INTO {IMAGE_TABLE} AS t
        USING {_UPDATES_TABLE} AS s
        ON t.IDDOC = s.IDDOC AND t.image_id = s.image_id
        WHEN MATCHED THEN UPDATE SET
            t.description = s.description,
            t.input_tokens = s.input_tokens,
            t.output_tokens = s.output_tokens,
            t.status = s.status,
            t.described_at = s.described_at
    """)


def describe_images(rows, ws_host, ws_token):
    """Call the vision LLM on `rows` in checkpoint chunks, merging each chunk's results as soon as it is done.

    A mid-run failure keeps the chunks already persisted. `nest_asyncio` lets `asyncio.run` work inside a notebook.
    """
    if not rows:
        logger.info("Nothing to describe - no PENDING or ERROR images found in image_metadata.")
        return

    nest_asyncio.apply()
    # Throughput bounded by the endpoint quota; the output tokens per minute are the bottleneck.
    rpm = safe_requests_per_minute(
        LLM_ITPM_BUDGET, LLM_OTPM_BUDGET, LLM_QPH_BUDGET, LLM_AVG_INPUT_TOKENS, LLM_AVG_OUTPUT_TOKENS,
    )
    logger.info(f"Rate limiter : {rpm:.0f} req/min  (ITPM<={LLM_ITPM_BUDGET:,}, OTPM<={LLM_OTPM_BUDGET:,}, "
                f"QPH<={LLM_QPH_BUDGET:,})")

    n_chunks = math.ceil(len(rows) / LLM_CHECKPOINT_CHUNK_SIZE)
    logger.info(f"{len(rows)} images -> {n_chunks} chunk(s) of up to {LLM_CHECKPOINT_CHUNK_SIZE}, persisted after each chunk")

    total_done = total_err = total_tin = total_tout = 0
    for chunk_idx in range(n_chunks):
        chunk = rows[chunk_idx * LLM_CHECKPOINT_CHUNK_SIZE:(chunk_idx + 1) * LLM_CHECKPOINT_CHUNK_SIZE]
        started = time.time()
        try:
            described = asyncio.run(describe_all_images(
                rows=chunk, ws_host=ws_host, ws_token=ws_token, model=LLM_MODEL_ENDPOINT,
                max_tokens=LLM_MAX_TOKENS, temperature=LLM_TEMPERATURE, max_retries=LLM_MAX_RETRIES,
                max_concurrent=LLM_MAX_CONCURRENT, requests_per_minute=rpm,
            ))
        except Exception as exc:
            logger.error(f"[FATAL] chunk {chunk_idx + 1}/{n_chunks} describe_all_images failed: {str(exc)[:300]}")
            logger.info(f"        {total_done} ok / {total_err} err from prior chunks are already persisted.")
            raise

        done = sum(1 for r in described if r["status"] == "DONE")
        err = sum(1 for r in described if r["status"] == "ERROR")
        tin = sum(r.get("input_tokens", 0) for r in described)
        tout = sum(r.get("output_tokens", 0) for r in described)
        _merge_chunk_results(described)

        # Single-node clusters run out of memory when the chunks pile up.
        del described
        gc.collect()

        total_done += done
        total_err += err
        total_tin += tin
        total_tout += tout
        logger.info(f"[chunk {chunk_idx + 1}/{n_chunks}] {len(chunk)} images in {time.time() - started:.1f}s | "
                    f"ok={done} err={err} | tokens {tin:,} in / {tout:,} out | cumulative: ok={total_done} err={total_err}")

    spark.sql(f"DROP TABLE IF EXISTS {_UPDATES_TABLE}")
    _log_description_summary(len(rows), total_done, total_err, total_tin, total_tout)


def _log_description_summary(n_rows, total_done, total_err, total_tin, total_tout):
    df_images = spark.table(IMAGE_TABLE)
    remaining_pending = df_images.filter(F.col("status") == "PENDING").count()
    remaining_error = df_images.filter(F.col("status") == "ERROR").count()
    logger.info(f"Description summary: {total_done} ok / {total_err} err over {n_rows} images")
    logger.info(f"  Tokens: {total_tin:,} in / {total_tout:,} out")
    logger.info(f"  Remaining: {remaining_pending} PENDING, {remaining_error} ERROR (will retry next run)")
    if remaining_error > 0:
        n_docs = df_images.filter(F.col("status") == "ERROR").select("IDDOC").distinct().count()
        logger.warning(f"  {remaining_error} images in ERROR across {n_docs} documents")
    if remaining_pending:
        logger.info("  -> Re-run for the rest.")
    elif remaining_error == 0:
        logger.info("  -> All images described successfully.")
    else:
        logger.info("  -> No PENDING left, but ERROR images remain - they will be retried next run.")


def described_images_without_chunk(rebuild):
    """Described images that have no passage yet (or all of them when `rebuild`).

    Scoped by an anti-join on `chunk_id` against the chunk tables, not by "described this run": a run that crashed
    after the descriptions were merged but before the passages were written would otherwise leave those images
    without a passage forever (DONE is never selected again). This makes the phase idempotent and self-healing, at
    the price of scanning all DONE images each run, which is cheap at the current volume.
    """
    df_described = (
        spark.table(IMAGE_TABLE)
        .filter(
            (F.col("status") == "DONE")
            & F.col("description").isNotNull()
            & (F.length(F.trim(F.col("description"))) >= F.lit(MIN_INDEXABLE_DESC_CHARS))
            & (~F.upper(F.trim(F.col("description"))).startswith("SKIP"))
        )
        .withColumn("chunk_id", F.concat_ws("-", F.col("IDDOC").cast("string"), F.lit("IMG"),
                                            F.lpad(F.col("image_id").cast("string"), 3, "0")))
    )
    if rebuild:
        return df_described
    # Image passages of pre-cutoff documents live in chunks_archive, not chunks.
    for table in (TARGET_CHUNK_TABLE, TARGET_CHUNK_TABLE_ARCHIVE):
        if spark.catalog.tableExists(table):
            df_existing = spark.table(table).filter(F.col("chunk_content_type") == "image").select("chunk_id")
            df_described = df_described.join(F.broadcast(df_existing), on="chunk_id", how="left_anti")
    return df_described


def log_nothing_to_inject():
    df_images = spark.table(IMAGE_TABLE)
    n_done = df_images.filter((F.col("status") == "DONE") & F.col("description").isNotNull()).count()
    n_error = df_images.filter(F.col("status") == "ERROR").count()
    logger.info("No new image chunks to inject.")
    logger.info(f"  {n_done} DONE images -> all already have chunks in {TARGET_CHUNK_TABLE}")
    if n_error > 0:
        logger.info(f"  {n_error} ERROR images -> not injected (will retry description next run)")


def build_image_chunks(df_described):
    """Image passages: position in the document, section, caption and a source prefix, long transcriptions split.

    No LLM call: everything comes from the stored descriptions and the text passages of the same document.
    """
    import pandas as pd

    df_text_all = spark.table(TARGET_CHUNK_TABLE).select(
        "IDDOC", "chunk_index", "chunk_content_type", "chunk_text", "semantic_headers")
    if spark.catalog.tableExists(TARGET_CHUNK_TABLE_ARCHIVE):
        df_text_all = df_text_all.unionByName(spark.table(TARGET_CHUNK_TABLE_ARCHIVE).select(
            "IDDOC", "chunk_index", "chunk_content_type", "chunk_text", "semantic_headers"))
    df_text_chunks = df_text_all.filter(F.col("chunk_content_type") != "image")
    df_max_idx = df_text_chunks.groupBy("IDDOC").agg(F.max("chunk_index").alias("max_text_index"))

    # Document metadata is not on image_metadata: it comes from processed_files, for the source prefix.
    df_pf = spark.table(TARGET_PROCESSED_FILES_TABLE)
    df_doc_meta = (
        df_pf.select("IDDOC", "doc_date",
                     *[F.col(c) if c in df_pf.columns else F.lit(None).cast("string").alias(c)
                       for c in ("type_document", "indice", "langue")])
        .withColumn("indice", F.col("indice").cast("string"))
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

    image_id = F.concat_ws("-", F.col("IDDOC").cast("string"), F.lit("IMG"), F.lpad(F.col("image_id").cast("string"), 3, "0"))
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
                F.lit(" | Image: page "), F.coalesce(F.col("page_no").cast("string"), F.lit("?")),
                F.lit(", "), F.col("label"), F.lit("]\n\n"), F.col("body"),
            ) if EMBED_SOURCE_PREFIX else F.col("body")),
        })
        .withColumns({
            "chunk_token_count": token_count_udf(F.col("chunk_text")),
            # Part 1 keeps the historical id (the anti-join in described_images_without_chunk relies on it);
            # the next parts of a long transcription get -2, -3...
            "chunk_id": F.when(F.col("part_no") == 1, image_id)
                         .otherwise(F.concat_ws("-", image_id, F.col("part_no").cast("string"))),
            "chunk_content_type": F.lit("image"),
            "semantic_headers": F.to_json(F.struct(
                F.col("label").alias("image_label"),
                F.col("page_no").cast("string").alias("page"),
                F.col("volume_path").alias("volume_path"),
                F.col("area_ratio").cast("string").alias("area_ratio"),
                F.col("captions").cast("string").alias("captions"),
                F.col("section").alias("section"),
            )),
            "chunk_sha256": F.sha2(F.col("chunk_text"), 256),
            "body_sha256": F.sha2(F.col("body"), 256),
            "indice": F.col("indice").cast("string"),
        })
        .withColumn("REF", F.coalesce(F.col("ref"), F.col("source_file_name")))
        .select(
            "IDDOC", "REF", "division", "chunk_id", "chunk_index", "chunk_text", "chunk_token_count",
            "chunk_content_type", "semantic_headers", "chunk_sha256", "url", "doc_date", "titre", "type_document",
            "indice", "langue", "body_sha256", "anchor_chunk_index",
        )
    )


def _merge_chunks(df_chunks, table):
    """MERGE on chunk_id rather than delete-all-for-IDDOC then reinsert, so untouched image chunks are not rewritten."""
    if not spark.catalog.tableExists(table):
        df_chunks.write.format("delta").mode("append").option("mergeSchema", "true").saveAsTable(table)
        return
    (
        DeltaTable.forName(spark, table).alias("t")
        .merge(df_chunks.alias("s"), "t.chunk_id = s.chunk_id")
        .whenMatchedUpdateAll()
        .whenNotMatchedInsertAll()
        .execute()
    )


def write_image_chunks(df_image_chunks):
    """Pre-cutoff documents go to chunks_archive, the others to chunks (Change Data Feed on, as Vector Search needs)."""
    # Columns added since the first runs must be able to reach chunk tables written before them.
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


def promote_empty_text_documents():
    """EMPTY_TEXT documents whose images are all described become SUCCESS (or SKIPPED_EMPTY_IMAGES).

    Nothing else moves an EMPTY_TEXT document to a terminal status once its images are described.
    """
    df_empty_text = spark.table(TARGET_PROCESSED_FILES_TABLE).filter(F.col("parse_status") == "EMPTY_TEXT").select("IDDOC")
    n_empty_text = df_empty_text.count()
    if n_empty_text == 0:
        logger.info("No EMPTY_TEXT document pending promotion.")
        return

    df_their_images = spark.table(IMAGE_TABLE).join(F.broadcast(df_empty_text), on="IDDOC", how="inner")
    # Ready = no PENDING image left. ERROR, EXTRACTION_FAILED and SKIPPED_DECORATIVE are terminal here: an image that
    # failed must not block the document from being promoted with its successful descriptions (ERROR ones are
    # retried by the description phase anyway).
    df_not_ready = df_their_images.filter(F.col("status") == "PENDING").select("IDDOC").distinct()
    df_ready = df_empty_text.join(F.broadcast(df_not_ready), on="IDDOC", how="left_anti")
    if df_ready.count() == 0:
        logger.info(f"{n_empty_text} EMPTY_TEXT document(s), none fully described yet.")
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
                F.lit(1), F.floor(F.length("document_text") / F.lit(CHARS_PER_TOKEN)).cast("int")),
        })
    )
    n_promoted = df_assembled.count()
    if n_promoted > 0:
        (
            DeltaTable.forName(spark, TARGET_PROCESSED_FILES_TABLE).alias("t")
            .merge(df_assembled.alias("s"), "t.IDDOC = s.IDDOC")
            .whenMatchedUpdate(set={
                "document_char_count": "s.document_char_count",
                "document_token_count": "s.document_token_count",
                "parse_status": "'SUCCESS'",
                "parser_strategy": "'llm_ocr:pdf'",
                "chunking_strategy": "'image_llm_ocr'",
                "images_needing_description": "0",
            })
            .execute()
        )
        logger.info(f"Promoted {n_promoted} EMPTY_TEXT document(s) to SUCCESS (all images now described).")

    # Ready but every image was SKIP: terminal as well, so the document stops being rescanned every day.
    df_ready_empty = df_ready.join(df_assembled.select("IDDOC"), on="IDDOC", how="left_anti")
    n_skipped = df_ready_empty.count()
    if n_skipped > 0:
        (
            DeltaTable.forName(spark, TARGET_PROCESSED_FILES_TABLE).alias("t")
            .merge(df_ready_empty.alias("s"), "t.IDDOC = s.IDDOC")
            .whenMatchedUpdate(set={
                "parse_status": "'SKIPPED_EMPTY_IMAGES'",
                "parser_strategy": "'llm_ocr:pdf'",
                "images_needing_description": "0",
            })
            .execute()
        )
        logger.info(f"{n_skipped} EMPTY_TEXT document(s) had every image judged non-informative -> SKIPPED_EMPTY_IMAGES.")
