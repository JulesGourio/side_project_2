"""File selection and LLM helpers of the parsing pipeline (utils/parsing_pipeline): pure helpers, then Spark on a local
session (skipped when pyspark or a JVM is missing)."""
import asyncio
import os
import shutil
import sys

import pytest

PARSING = os.path.join(os.path.dirname(__file__), "..", "utils", "parsing_pipeline")
sys.path.insert(0, PARSING)
for key, value in {"PARSING_CATALOG_SCHEMA": "cat.sch", "PARSING_INTRAQUAL_BRONZE": "bronze.sch",
                   "PARSING_VOLUME_ROOT_PATH": "/Volumes/c/s/root", "PARSING_VOLUME_BASE_PATH": "/Volumes/c/s/img",
                   "PARSING_OFFLINE_MODELS": "/models"}.items():
    os.environ.setdefault(key, value)

pyspark = pytest.importorskip("pyspark")

import llm_describe
import selection


def test_iddoc_of_a_root_folder_name():
    assert selection.extract_iddoc_from_name("D_123/") == 123
    assert selection.extract_iddoc_from_name("Dm_45") == 45
    assert selection.extract_iddoc_from_name("d_7") == 7
    assert selection.extract_iddoc_from_name("readme.txt") is None
    assert selection.extract_iddoc_from_name(None) is None


def test_the_quota_axis_with_the_lowest_rate_sets_the_requests_per_minute():
    # input 1,000,000 / 1,000 = 1,000 rpm; output 100,000 / 300 = 333 rpm; requests 324,000 / 60 = 5,400 rpm
    assert llm_describe.safe_requests_per_minute(1_000_000, 100_000, 324_000, 1000, 300) == pytest.approx(333.33, abs=0.01)
    assert llm_describe.safe_requests_per_minute(1, 1, 1, 1_000_000, 1_000_000) == 1.0


def test_the_prompt_survives_braces_in_the_context_and_picks_the_ocr_template():
    prompt = llm_describe.build_llm_prompt("see {table} and } here", division="AS", category="Proc > Qualité")
    assert "{table}" in prompt and "Division: AS" in prompt and "Proc > Qualité" in prompt
    assert "(none)" in llm_describe.build_llm_prompt("")
    assert "transcription engine" in llm_describe.build_llm_prompt("x", label="scanned_page")
    assert "extraction engine" in llm_describe.build_llm_prompt("x", label="picture")


def test_a_skip_answer_is_recognised():
    assert llm_describe.is_skip_response("  skip: decorative logo")
    assert not llm_describe.is_skip_response("# [TABLE] Skipped steps of the process")
    assert not llm_describe.is_skip_response("")


def test_the_rate_limiter_spends_its_burst_token_then_waits():
    async def scenario():
        limiter = llm_describe._AsyncRateLimiter(rate_per_minute=6000)   # 100 tokens per second
        loop = asyncio.get_running_loop()
        started = loop.time()
        for _ in range(3):
            await limiter.acquire()
        return loop.time() - started

    elapsed = asyncio.run(scenario())
    assert 0.015 <= elapsed < 1.0     # two refills of 10 ms after the burst token


def test_a_failed_description_is_returned_not_raised():
    class Client:
        class chat:
            class completions:
                @staticmethod
                async def create(**kwargs):
                    raise RuntimeError("429 REQUEST_LIMIT_EXCEEDED")

    pytest.importorskip("tenacity")

    async def scenario():
        limiter = llm_describe._AsyncRateLimiter(rate_per_minute=600000)
        row = {"IDDOC": 1, "image_id": 0, "volume_path": __file__, "label": "picture"}
        return await llm_describe.describe_one_image(Client, row, asyncio.Semaphore(1), "m", 100, 1.0, 1, limiter)

    out = asyncio.run(scenario())
    assert out["status"] == "ERROR" and out["description"].startswith("[RATE_LIMITED after 1 retries")
    assert out["IDDOC"] == 1 and out["input_tokens"] == 0


# --- Spark ---
@pytest.fixture(scope="module")
def spark():
    if shutil.which("java") is None:
        pytest.skip("no JVM")
    from pyspark.sql import SparkSession

    return (SparkSession.builder.master("local[1]").appName("test_parsing_selection")
            .config("spark.ui.enabled", "false").config("spark.sql.shuffle.partitions", "1")
            .config("spark.driver.bindAddress", "127.0.0.1").getOrCreate())


def _files(spark, names):
    """Candidate files as the scan returns them: (folder, name, extension, IDDOC, mtime in seconds)."""
    from pyspark.sql import functions as F

    rows = [(f"/Volumes/c/s/root/{folder}/{name}", 100, name, ext, f"/Volumes/c/s/root/{folder}", iddoc, mtime)
            for folder, name, ext, iddoc, mtime in names]
    df = spark.createDataFrame(
        rows, "source_path string, source_file_size_bytes long, source_file_name string, source_file_extension string, "
              "source_folder_path string, IDDOC long, _mtime int")
    return df.withColumn("source_modification_time", F.col("_mtime").cast("timestamp")).drop("_mtime")


def test_iddoc_column_prefers_the_folder_then_the_file_then_a_bare_number(spark):
    df = spark.createDataFrame(
        [("/r/D_12/x.pdf", "x.pdf"), ("/r/z/Dm_34.docx", "Dm_34.docx"), ("/r/z/56.pdf", "56.pdf"), ("/r/z/n.pdf", "n.pdf")],
        "source_path string, source_file_name string")
    got = [r.iddoc for r in df.select(selection.iddoc_column().alias("iddoc")).collect()]
    assert got == [12, 34, 56, None]


def test_best_file_is_d_before_dm_then_format_then_newest_and_unsupported_formats_are_skipped(spark):
    meta = _files(spark, [
        ("D_1", "Dm_1.docx", "docx", 1, 5),     # Dm_ loses against any D_ file
        ("D_1", "D_1.pdf", "pdf", 1, 1),        # pdf (priority 2)
        ("D_1", "D_1.docx", "docx", 1, 2),      # docx (priority 1) wins
        ("D_2", "D_2.msg", "msg", 2, 9),        # unsupported, rank 1 overall
        ("D_2", "D_2.xlsx", "xlsx", 2, 1),      # best SUPPORTED file
        ("D_3", "D_3.pdf", "pdf", 3, 1),        # no reference in the manifest: orphan, dropped
    ])
    manifest = spark.createDataFrame([(1, "REF-1", "T1"), (2, "REF-2", "T2")], "IDDOC long, ref string, titre string")

    ranked = selection.rank_candidates(meta)
    assert {r.source_file_name: r.priority_rank for r in ranked.collect() if r.IDDOC == 1} == {
        "D_1.docx": 1, "D_1.pdf": 2, "Dm_1.docx": 3}
    chosen = {r.IDDOC: r.source_file_name for r in selection.select_best_files(ranked, manifest).collect()}
    assert chosen == {1: "D_1.docx", 2: "D_2.xlsx"}


def test_the_selected_files_always_carry_the_enrichment_columns(spark):
    from config import ENRICHMENT_COLS

    meta = _files(spark, [("D_1", "D_1.pdf", "pdf", 1, 1)])
    manifest = spark.createDataFrame([(1, "REF-1")], "IDDOC long, ref string")
    selected = selection.select_best_files(selection.rank_candidates(meta), manifest)
    assert set(ENRICHMENT_COLS) <= set(selected.columns)
    assert dict(selected.dtypes)["doc_date"] == "date"
