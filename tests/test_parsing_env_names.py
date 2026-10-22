"""The parsing job takes its environments and the quality bypass from widgets, passed by the job as base parameters.
Each notebook copies them to the environment that config reads when it is imported."""
import glob
import os
import re
import subprocess
import sys

import yaml

ROOT = os.path.join(os.path.dirname(__file__), "..")
PARSING = os.path.join(ROOT, "utils", "parsing_pipeline")
JOB = os.path.join(ROOT, "resources", "parsing_pipeline.job.yml")
ENVIRONMENT_WIDGETS = ("pipeline_write_env", "pipeline_read_env", "reference_read_env", "bypass_quality_checks",
                       "debug", "log_level")
RETIRED = (
    "PARSING_INTRAQUAL_GOLD", "PARSING_INTRAQUAL_SUFFIX", "PARSING_VOLUME_ROOT_PATH", "PARSING_VOLUME_BASE_PATH",
    "PARSING_OFFLINE_MODELS", "PARSING_GPU_MIN_PDFS", "PARSING_ARCHIVE_NOTICES_IN_RAG", "PARSING_USE_GPU",
    "PARSING_MAX_PARSE_FILES", "PARSING_PARSE_FILTER", "PARSING_RUN_MODE", "PARSING_REDESCRIBE_ALL_IMAGES",
    "PARSING_MAX_SOURCE_STALENESS_HOURS", "PARSING_DOC_DATE_CUTOFF", "PARSING_DOC_SCOPE_FILTER",
    "PARSING_LLM_ENDPOINT", "PARSING_LLM_FALLBACK_ENDPOINTS", "PARSING_DIVISION_REFERENCE_TABLE",
    "PARSING_PARSE_MANIFEST_TABLE", "PARSING_INTRAQUAL_REVISIONS_TABLE",
)
# Variables a cluster may still set: the images volume name and the archive cap.
KEPT_ON_CLUSTERS = {"PARSING_IMAGES_VOLUME", "PARSING_ARCHIVE_MAX_DOCS"}


def _job():
    with open(JOB, encoding="utf-8") as handle:
        return yaml.safe_load(handle)["resources"]["jobs"]["parsing_pipeline"]


def _notebooks():
    return sorted(glob.glob(os.path.join(PARSING, "create_proj_*.py"))
                  + [os.path.join(PARSING, "update_proj_vector_indexes.py")])


def test_every_notebook_task_passes_the_three_environments():
    for task in _job()["tasks"]:
        notebook = task.get("notebook_task")
        if notebook:
            params = notebook.get("base_parameters", {})
            for key in ("pipeline_write_env", "pipeline_read_env", "reference_read_env"):
                assert key in params, f"{task['task_key']} does not pass {key}"


def test_clusters_do_not_carry_the_environments_any_more():
    for cluster in _job()["job_clusters"]:
        env_vars = cluster["new_cluster"].get("spark_env_vars", {})
        for name in RETIRED:
            assert name not in env_vars, f"{cluster['job_cluster_key']} still sets {name}"
        for name in env_vars:
            if name.startswith("PARSING_"):
                assert name in KEPT_ON_CLUSTERS, f"{cluster['job_cluster_key']} sets {name}"


def test_run_time_choices_are_job_parameters_passed_to_every_notebook_task():
    job = _job()
    declared = {p["name"] for p in job["parameters"]}
    assert declared == {"run_mode", "parse_filter", "max_parse_files", "max_source_staleness_hours",
                        "redescribe_all_images"}
    for task in job["tasks"]:
        notebook = task.get("notebook_task")
        if notebook:
            for name in declared:
                assert notebook["base_parameters"][name] == "{{job.parameters." + name + "}}", task["task_key"]


def test_the_retired_intraqual_and_cluster_variables_are_gone():
    text = open(JOB, encoding="utf-8").read()
    for name in RETIRED:
        assert name not in text, name


def test_every_notebook_declares_the_environment_widgets():
    for path in _notebooks():
        text = open(path, encoding="utf-8").read()
        for widget in ENVIRONMENT_WIDGETS:
            assert re.search(rf'dbutils\.widgets\.(text|dropdown)\("{widget}"', text), (os.path.basename(path), widget)
        config_import = re.search(r"^(from|import) config\b", text, re.M)
        if config_import:
            assert text.index("dbutils.widgets.removeAll()") < config_import.start(), os.path.basename(path)


def test_the_intraqual_source_is_read_in_the_reference_environment():
    env = {**os.environ, "PIPELINE_WRITE_ENV": "dev", "REFERENCE_READ_ENV": "prod",
           "PARSING_IMAGES_VOLUME": "img", "PYTHONPATH": os.pathsep.join([PARSING, os.path.abspath(ROOT)])}
    out = subprocess.run([sys.executable, "-c",
                          "import config; print(config.INTRAQUAL_GOLD_CATALOG_SCHEMA); "
                          "print(config.INTRAQUAL_REVISIONS_TABLE); print(config.PIPELINE_READ_ENV)"],
                         env=env, capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr[-2000:]
    schema, revisions, read_env = out.stdout.split()
    assert schema == "prod_gold.quality_system"
    assert revisions == "prod_gold.quality_system.document_revisions_intraqual"
    assert read_env == "dev", "an empty pipeline_read_env falls back to pipeline_write_env"


def test_bypass_turns_red_checks_into_amber(monkeypatch):
    sys.path.insert(0, PARSING)
    import tables

    checks = [tables.not_null("IDDOC"), tables.unique("IDDOC", severity=tables.AMBER)]
    monkeypatch.delenv("BYPASS_QUALITY_CHECKS", raising=False)
    assert [c.severity for c in tables._with_bypass(checks)] == [tables.RED, tables.AMBER]
    monkeypatch.setenv("BYPASS_QUALITY_CHECKS", "true")
    assert [c.severity for c in tables._with_bypass(checks)] == [tables.AMBER, tables.AMBER]


def test_paths_come_from_the_environment_and_the_fixed_source():
    env = {**os.environ, "PIPELINE_WRITE_ENV": "uat", "REFERENCE_READ_ENV": "prod", "PARSING_IMAGES_VOLUME": "images",
           "PYTHONPATH": os.pathsep.join([PARSING, os.path.abspath(ROOT)])}
    out = subprocess.run([sys.executable, "-c",
                          "import config as c; print(c.VOLUME_BASE_PATH); print(c.OFFLINE_MODELS_DIR); "
                          "print(c.VOLUME_ROOT_PATH); print(c.DIVISION_REFERENCE_TABLE); print(c.DOC_DATE_CUTOFF)"],
                         env=env, capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr[-2000:]
    assert out.stdout.split() == [
        "/Volumes/uat_proj/qualibot/images", "/Volumes/uat_proj/qualibot/docling_models",
        "/Volumes/prod_landingzone/intraqual/intraqual_documents", "uat_proj.qualibot.category_reference",
        "2016-01-01"]
