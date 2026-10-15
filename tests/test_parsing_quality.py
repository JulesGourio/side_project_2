"""Quality checks of the parsing pipeline (utils/parsing_pipeline/quality.py): evaluation in pure Python, then the
same counters computed by Spark on a local session (skipped when pyspark or a JVM is missing)."""
import logging
import os
import shutil
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "utils", "parsing_pipeline"))

import quality
from quality import AMBER, RED, QualityCheckError

ROWS = [
    {"chunk_id": "1-000001", "IDDOC": 1, "division": "AS", "ref": "QR-1"},
    {"chunk_id": "1-000002", "IDDOC": 1, "division": None, "ref": "QR-1"},
    {"chunk_id": "2-000001", "IDDOC": 2, "division": "IS", "ref": None},
]


def _by_label(results):
    return {r.check.label: r for r in results}


def test_a_clean_table_passes_every_check():
    checks = [quality.non_empty(), quality.not_null("chunk_id"), quality.unique("chunk_id"),
              quality.unique("IDDOC", "chunk_id")]
    results = quality.evaluate(checks, quality.stats_from_rows(ROWS, checks))
    assert all(r.passed for r in results)


def test_not_null_counts_the_nulls_and_honours_mostly():
    checks = [quality.not_null("division"), quality.not_null("division", mostly=0.6),
              quality.not_null("division", mostly=0.7)]
    stats = quality.stats_from_rows(ROWS, checks)
    strict, lenient, tight = quality.evaluate(checks, stats)
    assert not strict.passed and "1 null of 3 rows" in strict.detail
    assert lenient.passed          # 2 of 3 rows populated (66.67 %) >= 60 %
    assert not tight.passed        # 66.67 % < 70 %


def test_unique_single_and_compound_ignore_null_keys():
    rows = [{"a": 1, "b": "x"}, {"a": 1, "b": "x"}, {"a": 1, "b": "y"}, {"a": None, "b": "x"}, {"a": None, "b": "x"}]
    checks = [quality.unique("a"), quality.unique("a", "b"), quality.unique("b")]
    single, compound, only_b = quality.evaluate(checks, quality.stats_from_rows(rows, checks))
    assert not single.passed and "2 duplicate" in single.detail   # 3 rows hold a=1, the null keys are not compared
    assert not compound.passed and "1 duplicate" in compound.detail
    assert not only_b.passed


def test_non_empty_fails_on_no_row():
    checks = [quality.non_empty()]
    (result,) = quality.evaluate(checks, quality.stats_from_rows([], checks))
    assert not result.passed


def test_not_null_and_unique_on_an_empty_table_pass_vacuously():
    checks = [quality.not_null("x"), quality.not_null("x", mostly=0.5), quality.unique("x")]
    assert all(r.passed for r in quality.evaluate(checks, quality.stats_from_rows([], checks)))


def test_a_red_failure_raises_after_everything_is_logged(caplog):
    checks = [quality.not_null("ref"), quality.unique("chunk_id"), quality.not_null("division", severity=AMBER)]
    with caplog.at_level(logging.INFO, logger="DATABRICKS LOGS"):
        with pytest.raises(QualityCheckError) as exc:
            quality.check_rows(ROWS, "chunks", checks)
    assert "not_null(ref)" in str(exc.value) and "division" not in str(exc.value)
    messages = [r.getMessage() for r in caplog.records]
    assert any(m.startswith("[QUALITY RED] chunks: not_null(ref)") for m in messages)
    assert any(m.startswith("[QUALITY AMBER] chunks: not_null(division)") for m in messages)
    assert any(m.startswith("[QUALITY OK] chunks: unique(chunk_id)") for m in messages)


def test_an_amber_failure_only_warns(caplog):
    with caplog.at_level(logging.INFO, logger="DATABRICKS LOGS"):
        results = quality.check_rows(ROWS, "chunks", [quality.not_null("division", severity=AMBER)])
    assert not results[0].passed
    assert caplog.records[0].levelno == logging.WARNING


def test_required_stats_asks_each_counter_once():
    checks = [quality.not_null("a"), quality.not_null("a", mostly=0.9), quality.unique("a"), quality.unique("a", "b")]
    assert sorted(quality.required_stats(checks)) == [
        "all_non_null:a", "all_non_null:a,b", "distinct:a", "distinct:a,b", "non_null:a", "rows"]


def test_an_unknown_kind_is_refused():
    with pytest.raises(ValueError):
        quality.required_stats([quality.Check("is_blue", ("a",))])


# --- Spark: the same counters from one aggregation ---
@pytest.fixture(scope="module")
def spark():
    pytest.importorskip("pyspark")
    if shutil.which("java") is None:
        pytest.skip("no JVM")
    from pyspark.sql import SparkSession

    session = (SparkSession.builder.master("local[1]").appName("test_parsing_quality")
               .config("spark.ui.enabled", "false").config("spark.sql.shuffle.partitions", "1")
               .config("spark.driver.bindAddress", "127.0.0.1").getOrCreate())
    yield session
    session.stop()


def test_check_dataframe_raises_on_a_red_duplicate(spark):
    df = spark.createDataFrame(ROWS + [ROWS[0]], "chunk_id string, IDDOC long, division string, ref string")
    with pytest.raises(Exception, match="unique|RED"):
        quality.check_dataframe(df, "chunks", [quality.unique("chunk_id")])


def test_try_cast_returns_null_instead_of_raising_on_a_bad_value(spark):
    from pyspark.sql import functions as F

    df = spark.createDataFrame([("12",), ("abc",)], "v string")
    out = df.select(F.col("v").try_cast("bigint").alias("n"), F.col("v").try_cast("date").alias("d")).collect()
    assert [r.n for r in out] == [12, None] and [r.d for r in out] == [None, None]
