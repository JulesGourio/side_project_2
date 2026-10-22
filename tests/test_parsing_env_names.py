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
RETIRED = ("PARSING_INTRAQUAL_GOLD", "PARSING_INTRAQUAL_SUFFIX", "PIPELINE_WRITE_ENV", "REFERENCE_READ_ENV")


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
           "PARSING_VOLUME_ROOT_PATH": "/Volumes/c/s/root", "PARSING_VOLUME_BASE_PATH": "/Volumes/c/s/img",
           "PARSING_OFFLINE_MODELS": "/models", "PYTHONPATH": os.pathsep.join([PARSING, os.path.abspath(ROOT)])}
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
