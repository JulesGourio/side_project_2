"""The Docling batches of `3_parse` cover every pending file exactly once, even when the pending files are recomputed
after a batch was checkpointed (local Spark session, skipped without pyspark or a JVM)."""
import os
import shutil
import sys
import types

import pytest

ROOT = os.path.join(os.path.dirname(__file__), "..")
PARSING = os.path.join(ROOT, "utils", "parsing_pipeline")
ENV = {"PIPELINE_WRITE_ENV": "dev", "REFERENCE_READ_ENV": "prod", "PARSING_IMAGES_VOLUME": "img"}


@pytest.fixture(scope="module")
def spark():
    pytest.importorskip("pyspark")
    if shutil.which("java") is None:
        pytest.skip("no JVM")
    from pyspark.sql import SparkSession

    session = (SparkSession.builder.master("local[2]").appName("test_parsing_batches")
               .config("spark.ui.enabled", "false").config("spark.sql.shuffle.partitions", "4")
               .config("spark.driver.bindAddress", "127.0.0.1").getOrCreate())
    yield session
    shutil.rmtree("spark-warehouse", ignore_errors=True)
    session.stop()


@pytest.fixture
def parse_steps(spark, monkeypatch):
    for key, value in ENV.items():
        monkeypatch.setenv(key, os.environ.get(key, value))
    monkeypatch.setitem(sys.modules, "databricks.sdk.runtime",
                        types.SimpleNamespace(spark=spark, dbutils=None, display=None))
    monkeypatch.syspath_prepend(os.path.abspath(ROOT))
    monkeypatch.syspath_prepend(PARSING)
    for name in ("config", "parse_steps"):
        sys.modules.pop(name, None)
    import parse_steps as module
    return module


def test_every_pending_file_is_parsed_once_when_the_pending_files_are_recomputed(spark, parse_steps, monkeypatch):
    spark.sql("DROP TABLE IF EXISTS batch_done")
    spark.sql("CREATE TABLE batch_done (IDDOC BIGINT) USING parquet")
    files = spark.createDataFrame([(i, f"/v/D_{i}/f.docx") for i in range(1, 251)], "IDDOC long, source_path string")
    # The pending files are the ones the checkpoint does not hold yet: they change as the batches are written.
    pending = files.join(spark.table("batch_done"), on="IDDOC", how="left_anti")

    def write_batch(df, table, mode="append", **kwargs):
        df.select("IDDOC").write.mode("append").saveAsTable("batch_done")
        spark.catalog.clearCache()

    monkeypatch.setattr(parse_steps, "write_table", write_batch)
    monkeypatch.setattr(parse_steps, "CHECKPOINT_BATCH_SIZE", 100)
    parse_steps.run_docling_batches(pending, 250, lambda df, n: df)

    done = spark.table("batch_done")
    assert done.count() == 250 and done.distinct().count() == 250
    spark.sql("DROP TABLE IF EXISTS batch_done")
