"""Static guards for the bundle and the parsing modules: nothing here needs Databricks."""
import ast
import glob
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PARSING = os.path.join(ROOT, "utils", "parsing_pipeline")


def _read(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as f:
        return f.read()


def _module_names(path):
    tree = ast.parse(_read(path))
    names = set()
    for node in tree.body:
        if isinstance(node, ast.Assign):
            names.update(t.id for t in node.targets if isinstance(t, ast.Name))
        elif isinstance(node, (ast.FunctionDef, ast.ClassDef)):
            names.add(node.name)
    return names


def test_every_notebook_path_in_the_bundle_exists():
    missing = []
    for yml in ["databricks.yml", *sorted(glob.glob("resources/*.yml", root_dir=ROOT))]:
        base = os.path.dirname(os.path.join(ROOT, yml))
        for rel in re.findall(r"(?:notebook_path|python_file):\s*(\.{1,2}/\S+)", _read(yml)):
            if not os.path.exists(os.path.normpath(os.path.join(base, rel))):
                missing.append(f"{yml}: {rel}")
    assert not missing, missing


def test_parse_steps_only_imports_names_config_defines():
    config_names = _module_names("utils/parsing_pipeline/config.py")
    tree = ast.parse(_read("utils/parsing_pipeline/parse_steps.py"))
    imported = [a.name for n in tree.body if isinstance(n, ast.ImportFrom) and n.module == "config" for a in n.names]
    assert imported
    assert not [n for n in imported if n not in config_names]


def test_parse_notebook_only_reads_config_names_that_exist():
    config_names = _module_names("utils/parsing_pipeline/config.py")
    used = set(re.findall(r"\bcfg\.(\w+)", _read("utils/parsing_pipeline/3_Parse_Pipeline.py")))
    assert used
    assert not used - config_names


def test_parse_notebook_only_calls_parse_steps_functions_that_exist():
    defined = _module_names("utils/parsing_pipeline/parse_steps.py")
    code = [l for l in _read("utils/parsing_pipeline/3_Parse_Pipeline.py").splitlines() if not l.startswith("# MAGIC")]
    called = set(re.findall(r"\bparse_steps\.(\w+)", "\n".join(code)))
    assert not called - defined - {"INGESTION_RUN_ID", "JOB_RUN_ID"}
