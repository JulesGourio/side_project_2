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
    leap_data_asset = types.ModuleType("leap_utils.data_asset")
    table_utils = types.ModuleType("leap_utils.data_asset.table_utils")
    table_utils.save_table = lambda table, df, mode, **options: calls.append((table, mode, options))
    leap_data_asset.table_utils = table_utils
    monkeypatch.setitem(sys.modules, "leap_utils.data_asset", leap_data_asset)
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


def test_add_missing_columns_saves_an_empty_frame_with_only_the_missing_columns(monkeypatch):
    from tables import add_missing_columns

    saved = []
    leap_data_asset = types.ModuleType("leap_utils.data_asset")
    table_utils = types.ModuleType("leap_utils.data_asset.table_utils")
    table_utils.save_table = lambda table, df, mode, **options: saved.append((table, df, mode, options))
    leap_data_asset.table_utils = table_utils
    monkeypatch.setitem(sys.modules, "leap_utils.data_asset", leap_data_asset)
    monkeypatch.setitem(sys.modules, "leap_utils.data_asset.table_utils", table_utils)

    class Frame:
        columns = ["IDDOC", "image_id", "attempts", "last_error_kind"]
        sparkSession = types.SimpleNamespace(table=lambda t: types.SimpleNamespace(columns=["IDDOC", "image_id"]))

        def select(self, *cols):
            self.selected = cols
            return self

        def limit(self, n):
            self.limited = n
            return self

    df = Frame()
    add_missing_columns(df, "cat.sch.image_metadata")
    table, written, mode, options = saved[0]
    assert (table, mode) == ("cat.sch.image_metadata", "append")
    assert written.selected == ("attempts", "last_error_kind") and written.limited == 0
    assert options == {"overwrite_schema": False, "merge_schema": True}

    class Complete:
        columns = ["IDDOC"]
        sparkSession = types.SimpleNamespace(table=lambda t: types.SimpleNamespace(columns=["IDDOC", "image_id"]))

    saved.clear()
    add_missing_columns(Complete(), "cat.sch.t")
    assert saved == []   # the table has every column: nothing is saved
