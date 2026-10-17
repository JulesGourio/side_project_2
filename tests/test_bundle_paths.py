"""Static guards for the bundle and the parsing modules: nothing here needs Databricks."""
import ast
import glob
import os
import re

import pytest

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


PARSE_NOTEBOOKS = ["utils/parsing_pipeline/create_proj_parsing_checkpoint.py",
                   "utils/parsing_pipeline/create_proj_parsing_checkpoint_gpu.py",
                   "utils/parsing_pipeline/create_proj_chunks.py"]


@pytest.mark.parametrize("notebook", PARSE_NOTEBOOKS)
def test_parse_notebooks_only_read_config_names_that_exist(notebook):
    config_names = _module_names("utils/parsing_pipeline/config.py")
    used = set(re.findall(r"\bcfg\.(\w+)", _read(notebook)))
    assert used
    assert not used - config_names


@pytest.mark.parametrize("notebook", PARSE_NOTEBOOKS)
def test_parse_notebooks_only_call_parse_steps_functions_that_exist(notebook):
    defined = _module_names("utils/parsing_pipeline/parse_steps.py")
    code = [l for l in _read(notebook).splitlines() if not l.startswith("# MAGIC")]
    called = set(re.findall(r"\bparse_steps\.(\w+)", "\n".join(code)))
    assert called
    assert not called - defined - {"INGESTION_RUN_ID", "JOB_RUN_ID"}


def test_every_parsing_module_imports_only_names_config_defines():
    config_names = _module_names("utils/parsing_pipeline/config.py")
    missing = []
    for path in glob.glob(os.path.join(PARSING, "*.py")):
        if os.path.basename(path) == "config.py":
            continue
        text = open(path, encoding="utf-8").read()
        tree = ast.parse("\n".join("pass" if l.startswith("%") else l for l in text.split("\n")))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "config":
                missing += [f"{os.path.basename(path)}: {a.name}" for a in node.names
                            if a.name != "*" and a.name not in config_names]
    assert not missing, missing


def test_every_job_name_follows_the_launch_level_domain_convention():
    """`<D|W|M|F|Z>_<level>_<Domain>_<Purpose>` ; targets add a suffix."""
    import yaml

    pattern = re.compile(r"^[DWMFZ]_\d+_[A-Z][A-Za-z]*(_[A-Z][A-Za-z]*)*(_\$\{bundle\.target\})?$")
    offenders = []
    for yml in ["databricks.yml", *sorted(glob.glob("resources/*.yml", root_dir=ROOT))]:
        doc = yaml.safe_load(_read(yml).lstrip("﻿"))
        for key, job in ((doc.get("resources") or {}).get("jobs") or {}).items():
            if not pattern.match(job["name"]):
                offenders.append(f"{yml} {key}: {job['name']}")
        for target, body in (doc.get("targets") or {}).items():
            for key, job in ((body.get("resources") or {}).get("jobs") or {}).items():
                if "name" in job and not pattern.match(job["name"]):
                    offenders.append(f"{yml} {target}.{key}: {job['name']}")
    assert not offenders, offenders


def test_the_gpu_cluster_starts_only_through_the_pdf_condition():
    import yaml

    job = yaml.safe_load(_read("resources/parsing_pipeline.job.yml"))["resources"]["jobs"]["parsing_pipeline"]
    tasks = {t["task_key"]: t for t in job["tasks"]}
    gpu_tasks = [k for k, t in tasks.items() if t.get("job_cluster_key") == "parse_gpu_cluster"]
    assert gpu_tasks == ["3b_parse_pdf"]
    assert tasks["3b_parse_pdf"]["depends_on"] == [{"task_key": "3b_needs_gpu", "outcome": "true"}]

    condition = tasks["3b_needs_gpu"]["condition_task"]
    key = re.fullmatch(r"\{\{tasks\.3_parse\.values\.(\w+)\}\}", condition["left"]).group(1)
    assert f'taskValues.set(key="{key}"' in _read("utils/parsing_pipeline/create_proj_parsing_checkpoint.py")

    # A skipped GPU task must not skip the chunk build.
    assert tasks["3c_chunks"]["run_if"] == "NONE_FAILED"
    assert {d["task_key"] for d in tasks["3c_chunks"]["depends_on"]} == {"3b_needs_gpu", "3b_parse_pdf"}
    assert tasks["4_describe_images"]["depends_on"] == [{"task_key": "3c_chunks"}]
