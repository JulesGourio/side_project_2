"""The parsing job reads its environment through the names of the reference layout: pipeline_write_env and
reference_read_env, one variable per environment, and the Intraqual source derives from reference_read_env."""
import os
import subprocess
import sys

import yaml

ROOT = os.path.join(os.path.dirname(__file__), "..")
PARSING = os.path.join(ROOT, "utils", "parsing_pipeline")
JOB = os.path.join(ROOT, "resources", "parsing_pipeline.job.yml")


def _clusters():
    with open(JOB, encoding="utf-8") as handle:
        return yaml.safe_load(handle)["resources"]["jobs"]["parsing_pipeline"]["job_clusters"]


def test_every_cluster_receives_the_write_and_reference_environments():
    for cluster in _clusters():
        env_vars = cluster["new_cluster"]["spark_env_vars"]
        assert "PIPELINE_WRITE_ENV" in env_vars, cluster["job_cluster_key"]
        assert "REFERENCE_READ_ENV" in env_vars, cluster["job_cluster_key"]


def test_no_cluster_still_sets_the_retired_intraqual_variables():
    text = open(JOB, encoding="utf-8").read()
    assert "PARSING_INTRAQUAL_GOLD" not in text
    assert "PARSING_INTRAQUAL_SUFFIX" not in text


def test_intraqual_source_is_read_in_the_reference_environment():
    env = {**os.environ, "PIPELINE_WRITE_ENV": "dev", "REFERENCE_READ_ENV": "prod",
           "PARSING_VOLUME_ROOT_PATH": "/Volumes/c/s/root", "PARSING_VOLUME_BASE_PATH": "/Volumes/c/s/img",
           "PARSING_OFFLINE_MODELS": "/models", "PYTHONPATH": os.pathsep.join([PARSING, os.path.abspath(ROOT)])}
    out = subprocess.run([sys.executable, "-c",
                          "import config; print(config.INTRAQUAL_GOLD_CATALOG_SCHEMA); "
                          "print(config.INTRAQUAL_REVISIONS_TABLE)"],
                         env=env, capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr[-2000:]
    schema, revisions = out.stdout.split()
    assert schema == "prod_gold.quality_system"
    assert revisions == "prod_gold.quality_system.document_revisions_intraqual"
