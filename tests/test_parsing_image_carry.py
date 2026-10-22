"""A rebuild keeps an image description only when it still describes the image now at that (IDDOC, image_id).

The ids are positions in the parse output: a re-parse that changes the number or order of images moves them, and the
old description would land on another image. Runs on a local Spark session (skipped without pyspark or a JVM).
"""
import datetime as dt
import os
import shutil
import sys
import types

import pytest

ROOT = os.path.join(os.path.dirname(__file__), "..")
PARSING = os.path.join(ROOT, "utils", "parsing_pipeline")

ENV = {"PIPELINE_WRITE_ENV": "dev", "PARSING_INTRAQUAL_GOLD": "gold.sch",
       "PARSING_VOLUME_ROOT_PATH": "/Volumes/c/s/root", "PARSING_VOLUME_BASE_PATH": "/Volumes/c/s/img",
       "PARSING_OFFLINE_MODELS": "/models"}


@pytest.fixture(scope="module")
def spark():
    pytest.importorskip("pyspark")
    if shutil.which("java") is None:
        pytest.skip("no JVM")
    from pyspark.sql import SparkSession

    session = (SparkSession.builder.master("local[1]").appName("test_parsing_image_carry")
               .config("spark.ui.enabled", "false").config("spark.sql.shuffle.partitions", "1")
               .config("spark.driver.bindAddress", "127.0.0.1").getOrCreate())
    yield session
    session.stop()


@pytest.fixture
def build_steps(spark, monkeypatch):
    for k, v in ENV.items():
        monkeypatch.setenv(k, os.environ.get(k, v))
    monkeypatch.setitem(sys.modules, "databricks.sdk.runtime",
                        types.SimpleNamespace(spark=spark, dbutils=None, display=None))
    monkeypatch.syspath_prepend(os.path.abspath(ROOT))
    monkeypatch.syspath_prepend(PARSING)
    for name in ("config", "parse_steps", "build_steps"):
        sys.modules.pop(name, None)
    import build_steps as module
    return module


T_OLD = dt.datetime(2026, 7, 1)
T_DESCRIBED = dt.datetime(2026, 8, 1)
T_REPARSED = dt.datetime(2026, 10, 16)

PREV_SCHEMA = ("IDDOC long, image_id int, status string, description string, input_tokens int, output_tokens int, "
               "described_at timestamp, attempts int, last_error_kind string, page_no int, image_width int, "
               "image_height int")
NEW_SCHEMA = ("IDDOC long, image_id int, page_no int, image_width int, image_height int, status string, "
              "description string, input_tokens int, output_tokens int, described_at timestamp, _parsed_at timestamp")


def _run(build_steps, spark, monkeypatch, prev_rows, new_rows):
    prev = spark.createDataFrame(prev_rows, PREV_SCHEMA)
    monkeypatch.setattr(build_steps, "table_exists", lambda table: True)
    monkeypatch.setattr(build_steps, "spark", types.SimpleNamespace(table=lambda table: prev))
    out = build_steps._carry_previous_image_rows(spark.createDataFrame(new_rows, NEW_SCHEMA))
    return {r.IDDOC: r for r in out.collect()}


def test_a_document_not_parsed_again_keeps_its_descriptions(build_steps, spark, monkeypatch):
    out = _run(build_steps, spark, monkeypatch,
               [(1, 0, "DONE", "flowchart", 10, 5, T_DESCRIBED, 1, None, 1, 800, 600)],
               [(1, 0, 1, 801, 600, "PENDING", None, None, None, None, T_OLD)])
    assert out[1].status == "DONE" and out[1].description == "flowchart" and out[1].attempts == 1


def test_a_reparsed_document_keeps_a_description_only_for_the_same_image(build_steps, spark, monkeypatch):
    out = _run(build_steps, spark, monkeypatch,
               [(1, 0, "DONE", "same image", 10, 5, T_DESCRIBED, 1, None, 2, 800, 600),
                (2, 0, "DONE", "another image", 10, 5, T_DESCRIBED, 3, None, 2, 800, 600)],
               [(1, 0, 2, 800, 600, "PENDING", None, None, None, None, T_REPARSED),
                (2, 0, 4, 1200, 300, "PENDING", None, None, None, None, T_REPARSED)])
    assert out[1].status == "DONE" and out[1].description == "same image"
    assert out[2].status == "PENDING" and out[2].description is None and out[2].attempts == 0


def test_a_new_image_starts_pending_with_no_attempt(build_steps, spark, monkeypatch):
    out = _run(build_steps, spark, monkeypatch, [],
               [(5, 0, 1, 800, 600, "PENDING", None, None, None, None, T_REPARSED)])
    assert out[5].status == "PENDING" and out[5].attempts == 0


def test_documents_whose_checkpoint_lists_no_image_keep_their_rows(build_steps, spark, monkeypatch):
    prev = spark.createDataFrame(
        [(7, 0, "DONE", "kept as is", 10, 5, T_DESCRIBED, 1, None, 1, 800, 600),
         (8, 0, "DONE", "same image, re-parsed", 10, 5, T_DESCRIBED, 1, None, 1, 800, 600)], PREV_SCHEMA)
    monkeypatch.setattr(build_steps, "table_exists", lambda table: True)
    monkeypatch.setattr(build_steps, "spark", types.SimpleNamespace(table=lambda table: prev))
    checkpoint = spark.createDataFrame(
        [(7, 3, []), (8, 1, [(0,)])], "IDDOC long, image_count int, images array<struct<image_id:int>>")
    new_rows = build_steps._carry_previous_image_rows(spark.createDataFrame(
        [(8, 0, 1, 800, 600, "PENDING", None, None, None, None, T_REPARSED)], NEW_SCHEMA))
    out = build_steps._with_rows_of_unlisted_documents(new_rows, checkpoint).collect()
    assert sorted((r.IDDOC, r.description) for r in out) == [(7, "kept as is"), (8, "same image, re-parsed")]


def test_a_document_in_scope_takes_its_category_fields_as_they_are_now(spark, build_steps, monkeypatch):
    """A document without category in the reference no longer keeps the division it had before: it is shared (ALL)."""
    spark.createDataFrame([(1, "REF-1", "Titre"), (2, "REF-2", "Titre")],
                          "IDDOC long, ref string, titre string").write.mode("overwrite").saveAsTable("fresh_manifest")
    spark.createDataFrame([(1, "AS", "AS - Cat")], "IDDOC long, division string, niveau_plus_1 string"
                          ).write.mode("overwrite").saveAsTable("fresh_reference")
    monkeypatch.setattr(build_steps, "PARSE_MANIFEST_TABLE", "fresh_manifest")
    monkeypatch.setattr(build_steps, "DIVISION_REFERENCE_TABLE", "fresh_reference")
    monkeypatch.setattr(build_steps, "HIERARCHY_COLS", ["division", "niveau_plus_1"])
    monkeypatch.setattr(build_steps, "BUSINESS_METADATA_COLS", ["ref", "titre", "division", "niveau_plus_1"])
    monkeypatch.setattr(build_steps, "MANIFEST_METADATA_COLS", ["ref", "titre"])

    checkpoint = spark.createDataFrame(
        [(1, "OLD", "old", "x", "y"), (2, "IS", "IS - Old", "x", "y"), (3, "IS", "IS - Old", "x", "y")],
        "IDDOC long, division string, niveau_plus_1 string, ref string, titre string")
    out = {r.IDDOC: r for r in build_steps.with_fresh_business_metadata(checkpoint).collect()}
    assert (out[1].division, out[1].niveau_plus_1) == ("AS", "AS - Cat")
    assert (out[2].division, out[2].niveau_plus_1) == ("ALL", None)       # in scope, no category
    assert (out[3].division, out[3].niveau_plus_1) == ("IS", "IS - Old")  # out of scope: its own values
    spark.sql("DROP TABLE IF EXISTS fresh_manifest")
    spark.sql("DROP TABLE IF EXISTS fresh_reference")
