"""Write and cast conventions of the parsing pipeline (utils/parsing_pipeline): one write helper, no raw cast."""
import ast
import glob
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PARSING = os.path.join(ROOT, "utils", "parsing_pipeline")
sys.path.insert(0, PARSING)

from tables import write_table


class FakeWriter:
    def __init__(self):
        self.calls = []

    def format(self, name):
        self.calls.append(("format", name))
        return self

    def mode(self, name):
        self.calls.append(("mode", name))
        return self

    def option(self, key, value):
        self.calls.append(("option", key, value))
        return self

    def saveAsTable(self, table):
        self.calls.append(("saveAsTable", table))


class FakeFrame:
    def __init__(self):
        self.write = FakeWriter()


def _written(**kwargs):
    df = FakeFrame()
    write_table(df, "cat.sch.t", **kwargs)
    return df.write.calls


def test_append_with_merge_schema():
    assert _written(mode="append", merge_schema=True) == [
        ("format", "delta"), ("mode", "append"), ("option", "mergeSchema", "true"), ("saveAsTable", "cat.sch.t")]


def test_overwrite_with_overwrite_schema():
    assert _written(mode="overwrite", overwrite_schema=True) == [
        ("format", "delta"), ("mode", "overwrite"), ("option", "overwriteSchema", "true"), ("saveAsTable", "cat.sch.t")]


def test_default_is_a_plain_append_and_errorifexists_is_passed_through():
    assert _written() == [("format", "delta"), ("mode", "append"), ("saveAsTable", "cat.sch.t")]
    assert ("mode", "errorifexists") in _written(mode="errorifexists")


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
