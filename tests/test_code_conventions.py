"""Guards for the conventions of CLAUDE.md that a reader would otherwise have to remember."""
import ast
import glob
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PYTHON_DIRS = ("server", "utils", "scripts")


def _python_files():
    for d in PYTHON_DIRS:
        yield from glob.glob(os.path.join(ROOT, d, "**", "*.py"), recursive=True)


def _parse(path):
    text = open(path, encoding="utf-8").read()
    # Databricks notebooks carry %magic lines that are not Python.
    return text, ast.parse("\n".join("pass" if l.startswith("%") else l for l in text.split("\n")))


def test_no_print_calls():
    offenders = []
    for path in _python_files():
        _, tree = _parse(path)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "print":
                offenders.append(f"{os.path.relpath(path, ROOT)}:{node.lineno}")
    assert not offenders, offenders


def test_no_dbtitle_markers():
    offenders = [
        os.path.relpath(p, ROOT) for p in _python_files() if re.search(r"^# DBTITLE", open(p, encoding="utf-8").read(), re.M)
    ]
    assert not offenders, offenders


def test_no_commented_out_imports_or_prints():
    pattern = re.compile(r"^\s*#\s*(import \w|from \w+ import |print\()")
    offenders = []
    for path in _python_files():
        for i, line in enumerate(open(path, encoding="utf-8").read().split("\n"), 1):
            if pattern.match(line) and "MAGIC" not in line:
                offenders.append(f"{os.path.relpath(path, ROOT)}:{i}")
    assert not offenders, offenders


def test_every_pipeline_notebook_explains_each_code_block():
    """LEAP layout: a markdown cell with the why comes before each code cell, all sections are present."""
    sep = "\n\n# COMMAND ----------\n\n"
    sections = ["Technical debt", "Configuration", "Inputs", "Data Preparation", "Data Transformations",
                "Quality Checks", "Outputs"]
    offenders = []
    for folder in ("parsing_pipeline", "generic_pipeline"):
        for path in sorted(glob.glob(os.path.join(ROOT, "utils", folder, "[0-9]_*.py"))):
            text = open(path, encoding="utf-8").read()
            cells = text.split(sep)
            name = os.path.relpath(path, ROOT)
            for i, cell in enumerate(cells[1:], 1):
                if not cell.lstrip().startswith("# MAGIC %md") and not cells[i - 1].lstrip().startswith("# MAGIC %md"):
                    offenders.append(f"{name}: code cell {i} follows another code cell")
            offenders += [f"{name}: missing section '{s}'" for s in sections
                          if not re.search(rf"^# MAGIC # {re.escape(s)}\s*$", text, re.M)]
    assert not offenders, offenders
