"""The parsing modules the tests use import leap_utils without its config: leap_utils.config starts a Spark session at
import time, so it is only imported inside the functions that save or merge a table (tables.py)."""
import os
import subprocess
import sys

ROOT = os.path.join(os.path.dirname(__file__), "..")
PARSING = os.path.join(ROOT, "utils", "parsing_pipeline")

CHECK = """
import sys
import tables, llm_describe, chunking, config
loaded = [m for m in sys.modules if m == 'leap_utils.config' or m == 'leap_utils.data_asset.table_utils'
          or m.startswith('pyspark')]
print(','.join(loaded))
"""


def test_importing_the_driver_helpers_does_not_start_spark():
    defaults = {"PIPELINE_WRITE_ENV": "dev", "REFERENCE_READ_ENV": "ref",
                "PARSING_VOLUME_ROOT_PATH": "/Volumes/c/s/root", "PARSING_VOLUME_BASE_PATH": "/Volumes/c/s/img",
                "PARSING_OFFLINE_MODELS": "/models"}
    env = {**os.environ, **{k: v for k, v in defaults.items() if k not in os.environ},
           "PYTHONPATH": os.pathsep.join([PARSING, os.path.abspath(ROOT)])}
    out = subprocess.run([sys.executable, "-c", CHECK], env=env, capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr[-2000:]
    assert out.stdout.strip() == "", f"loaded at import time: {out.stdout.strip()}"


def test_every_import_between_parsing_modules_resolves():
    """An import of a name a parsing module no longer defines, or of the repository's `utils` package, only fails on the cluster."""
    import ast
    import glob
    import re

    trees = {}
    for path in sorted(glob.glob(os.path.join(PARSING, "*.py"))):
        source = "\n".join("" if re.match(r"\s*(%|!)", line) or line.startswith("# MAGIC") else line
                           for line in open(path, encoding="utf-8").read().split("\n"))
        trees[os.path.basename(path)[:-3]] = ast.parse(source)

    defined = {}
    for module, tree in trees.items():
        names = set()
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                names.add(node.name)
            elif isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                names.update(n.id for t in targets for n in ast.walk(t) if isinstance(n, ast.Name))
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                names.update((a.asname or a.name).split(".")[0] for a in node.names)
        defined[module] = names

    problems = []
    for module, tree in trees.items():
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom) or node.level:
                continue
            if node.module == "utils":
                problems.append(f"{module}.py:{node.lineno} imports from the utils package")
            elif node.module in trees:
                problems += [f"{module}.py:{node.lineno} {node.module}.{a.name} is not defined"
                             for a in node.names if a.name != "*" and a.name not in defined[node.module]]
    assert not problems, problems
