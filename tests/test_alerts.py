"""Guards for resources/alerts.yml: the alerts deploy to every target and watch that target's own schema."""

import os
import re

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ALERTS_PATH = os.path.join(ROOT, "resources", "alerts.yml")
BUNDLE_PATH = os.path.join(ROOT, "databricks.yml")


def _load(path):
    with open(path, encoding="utf-8-sig") as f:
        return yaml.safe_load(f)


def _alerts():
    return _load(ALERTS_PATH)["resources"]["alerts"]


# Reads system.lakeflow (the job history of the workspace), not the project schema.
_NOT_PROJECT_SCHEMA = {"job_failures"}


def test_every_alert_reads_the_schema_of_its_target():
    for key, alert in _alerts().items():
        query = alert["query_text"]
        assert not re.search(r"\b(dev|uat|prod)_proj\b", query), f"{key} hardcodes an environment"
        if key not in _NOT_PROJECT_SCHEMA:
            assert "${var.pipeline_write_env}_proj.qualibot." in query, key


def test_every_alert_runs_on_the_warehouse_of_its_target():
    for key, alert in _alerts().items():
        assert alert["warehouse_id"] == "${var.alerts_warehouse_id}", key


def test_the_source_column_is_a_column_of_the_query():
    for key, alert in _alerts().items():
        column = alert["evaluation"]["source"]["name"]
        assert re.search(rf"\bAS\s+{column}\b", alert["query_text"]), f"{key}: {column} is not selected"


def test_every_target_sets_its_alert_warehouse_and_the_variables_exist():
    bundle = _load(BUNDLE_PATH)
    for name in ("alerts_warehouse_id", "alerts_pause_status"):
        assert name in bundle["variables"], name
    for target, values in bundle["targets"].items():
        assert values.get("variables", {}).get("alerts_warehouse_id"), f"{target} has no alerts_warehouse_id"
