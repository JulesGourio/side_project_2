"""Write and cast conventions of the parsing pipeline (utils/parsing_pipeline): one write helper, no raw cast."""
import ast
import glob
import os
import sys
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PARSING = os.path.join(ROOT, "utils", "parsing_pipeline")
sys.path.insert(0, PARSING)

from tables import write_table


def _saved(monkeypatch, **kwargs):
    """What write_table asks leap_utils to save: (table, mode, options). leap_utils itself needs a Spark session."""
    calls = []
    data_asset_pkg = types.ModuleType("leap_utils.data_asset")
    table_utils = types.ModuleType("leap_utils.data_asset.table_utils")
    table_utils.save_table = lambda table, df, mode, **options: calls.append((table, mode, options))
    data_asset_pkg.table_utils = table_utils
    monkeypatch.setitem(sys.modules, "leap_utils.data_asset", data_asset_pkg)
    monkeypatch.setitem(sys.modules, "leap_utils.data_asset.table_utils", table_utils)
    write_table(object(), "cat.sch.t", **kwargs)
    assert len(calls) == 1
    return calls[0]


def test_append_with_merge_schema_goes_through_save_table(monkeypatch):
    assert _saved(monkeypatch, mode="append", merge_schema=True) == (
        "cat.sch.t", "append", {"overwrite_schema": False, "merge_schema": True})


def test_overwrite_with_overwrite_schema_goes_through_save_table(monkeypatch):
    assert _saved(monkeypatch, mode="overwrite", overwrite_schema=True) == (
        "cat.sch.t", "overwrite", {"overwrite_schema": True, "merge_schema": False})


def test_default_is_a_plain_append_and_errorifexists_is_passed_through(monkeypatch):
    assert _saved(monkeypatch) == ("cat.sch.t", "append", {"overwrite_schema": False, "merge_schema": False})
    assert _saved(monkeypatch, mode="errorifexists")[1] == "errorifexists"


def _trees():
    for path in sorted(glob.glob(os.path.join(PARSING, "*.py"))):
        text = open(path, encoding="utf-8").read()
        yield os.path.basename(path), ast.parse("\n".join("pass" if l.startswith("%") else l for l in text.split("\n")))


def test_only_the_write_helper_calls_save_as_table():
    offenders = [f"{name}:{n.lineno}" for name, tree in _trees() for n in ast.walk(tree)
                 if isinstance(n, ast.Attribute) and n.attr == "saveAsTable" and name != "tables.py"]
    assert not offenders, offenders


def test_a_cast_only_types_a_literal_and_dates_are_not_parsed_with_to_date():
    """Data columns go through try_cast (NULL on a bad value, not a failed job); lit(...) / array() typing may cast."""
    offenders = []
    for name, tree in _trees():
        for n in ast.walk(tree):
            if not (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)):
                continue
            if n.func.attr == "to_date":
                offenders.append(f"{name}:{n.lineno} to_date")
            if n.func.attr == "cast":
                receiver = n.func.value
                typed_literal = (isinstance(receiver, ast.Call) and isinstance(receiver.func, ast.Attribute)
                                 and receiver.func.attr in ("lit", "array"))
                if not typed_literal:
                    offenders.append(f"{name}:{n.lineno} cast")
    assert not offenders, offenders


def test_add_missing_columns_alters_the_table_instead_of_writing_an_empty_frame():
    """An empty write lacking a NOT NULL column (the primary key) is refused by Delta: the columns are added by DDL."""
    from tables import add_missing_columns

    statements = []

    def frame(names_and_types):
        fields = [types.SimpleNamespace(name=n, dataType=types.SimpleNamespace(simpleString=lambda t=t: t))
                  for n, t in names_and_types]
        session = types.SimpleNamespace(table=lambda t: types.SimpleNamespace(columns=["chunk_id", "IDDOC"]),
                                        sql=statements.append)
        return types.SimpleNamespace(schema=types.SimpleNamespace(fields=fields), sparkSession=session)

    add_missing_columns(frame([("chunk_id", "string"), ("image_id", "int"), ("captions", "array<string>")]),
                        "cat.sch.chunks")
    assert statements == ["ALTER TABLE cat.sch.chunks ADD COLUMNS (`image_id` int, `captions` array<string>)"]

    statements.clear()
    add_missing_columns(frame([("chunk_id", "string")]), "cat.sch.chunks")
    assert statements == []   # the table has every column: nothing is altered


def test_an_update_only_merge_passes_leap_utils_and_never_inserts(monkeypatch):
    """leap_utils refuses a MERGE whose only clause is a plain WHEN MATCHED: the source is cut to the existing keys."""
    import shutil
    import pytest

    pytest.importorskip("pyspark")
    if shutil.which("java") is None:
        pytest.skip("no JVM")
    from pyspark.sql import SparkSession
    from tables import merge_into

    spark = (SparkSession.builder.master("local[1]").appName("test_update_only_merge")
             .config("spark.ui.enabled", "false").config("spark.driver.bindAddress", "127.0.0.1").getOrCreate())
    spark.createDataFrame([(1, 0, "old"), (1, 1, "old")], "IDDOC long, image_id int, description string") \
        .createOrReplaceTempView("image_metadata")
    source = spark.createDataFrame([(1, 0, "new"), (9, 0, "not in the table")],
                                   "IDDOC long, image_id int, description string")

    calls = []

    def fake_merge_into(df, table, keys, *, do_when_matched, matched_clause, do_when_notmatched,
                        do_when_notmatchedbysource):
        # The check of leap_utils.common.merge_into.merge_into_spark.
        if not (matched_clause or do_when_notmatched or do_when_notmatchedbysource):
            raise ValueError("MERGE INTO must have at least one MERGE clause")
        calls.append((sorted((r.IDDOC, r.image_id, r.description) for r in df.collect()), do_when_notmatched))

    data_asset_pkg = types.ModuleType("leap_utils.data_asset")
    table_utils = types.ModuleType("leap_utils.data_asset.table_utils")
    table_utils.merge_into = fake_merge_into
    data_asset_pkg.table_utils = table_utils
    monkeypatch.setitem(sys.modules, "leap_utils.data_asset", data_asset_pkg)
    monkeypatch.setitem(sys.modules, "leap_utils.data_asset.table_utils", table_utils)

    merge_into(source, "image_metadata", ["IDDOC", "image_id"], do_when_notmatched=False)
    assert calls == [([(1, 0, "new")], True)]
