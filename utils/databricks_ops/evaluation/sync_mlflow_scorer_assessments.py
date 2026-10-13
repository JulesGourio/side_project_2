# Databricks notebook source
# DBTITLE 1,Header
# MAGIC %md
# MAGIC # Sync MLflow native scorer assessments into a queryable Delta table
# MAGIC
# MAGIC The continuous monitoring registered on the KA's 3 native per-division
# MAGIC experiments (`mlflow_genai_eval_qualibot_uat.py`'s `ENABLE_CONTINUOUS_MONITORING`
# MAGIC section, enabled 2026-09-08) writes its verdicts as MLflow assessments
# MAGIC attached directly to each trace — visible on the MLflow agents page, but
# MAGIC **not** SQL-queryable: these experiments use classic `MLFLOW_EXPERIMENT`
# MAGIC (DBFS-backed) trace storage, not Unity-Catalog-native trace tables, so there
# MAGIC is no `mlflow_experiment_trace_unified`-style view for Lakeview to hit
# MAGIC directly. This notebook reads assessments via the MLflow SDK
# MAGIC (`mlflow.search_traces(..., return_type="pandas")`'s `assessments` column,
# MAGIC confirmed to carry them even without UC trace storage) and materializes them
# MAGIC into `{catalog_schema}.ka_mlflow_scorer_assessments`, incrementally, the same
# MAGIC way `score_production_qa.py` materializes Luna verdicts into
# MAGIC `chat_quality_scores` — so the "ChatBot - Quality" dashboard tab can show
# MAGIC both side by side.
# MAGIC
# MAGIC **Assessment shape** (verified live 2026-09-08, not assumed): each item in
# MAGIC `assessments` is `{assessment_name, trace_id, create_time, feedback: {value},
# MAGIC rationale, ...}` — `assessment_name` is exactly the registered scorer name
# MAGIC (`qualibot_<division>_<scorer>`), so the division and scorer both parse out
# MAGIC of it directly, no separate lookup needed.

# COMMAND ----------

# DBTITLE 1,Setup
# MAGIC %pip install --upgrade mlflow[databricks] --quiet
# MAGIC dbutils.library.restartPython()

# COMMAND ----------

# DBTITLE 1,Parameters
dbutils.widgets.text("catalog_schema", "uat_landingzone.qualibot")
CATALOG_SCHEMA = dbutils.widgets.get("catalog_schema").strip()
OUTPUT_TABLE = f"{CATALOG_SCHEMA}.ka_mlflow_scorer_assessments"

DIVISION_EXPERIMENT_IDS = {
    "ALL": "4171178917767011",
    "IS": "2748374992560665",
    "AS": "2748374992560664",
}
# First run has nothing to anchor to — look back this far. Later runs use the
# max already-synced request_time per experiment instead.
DEFAULT_LOOKBACK_DAYS = 3

import ast
import json
from datetime import datetime, timedelta, timezone

import mlflow
print(f"MLflow {mlflow.__version__}")

# COMMAND ----------

# DBTITLE 1,Parse one trace's assessments column into flat rows
def _parse_assessments(raw) -> list:
    """The assessments column comes back as a real list of dicts on some paths
    and as a Python-repr'd string on others (seen both from the same API in
    manual testing) — handle whichever shows up rather than assuming one."""
    if raw is None:
        return []
    if isinstance(raw, list):
        return raw
    if isinstance(raw, str):
        s = raw.strip()
        if not s or s == "[]":
            return []
        try:
            return json.loads(s)
        except (json.JSONDecodeError, TypeError):
            pass
        try:
            return ast.literal_eval(s)
        except (ValueError, SyntaxError):
            return []
    return []


def rows_from_trace(trace_id: str, request_time_ms: int, assessments_raw) -> list:
    rows = []
    for a in _parse_assessments(assessments_raw):
        name = a.get("assessment_name") or ""
        # "qualibot_<division>_<scorer>" — division is always one of the 3
        # lowercase codes, scorer is everything after it (may itself contain
        # underscores, e.g. "relevance_to_query").
        parts = name.split("_", 2)
        if len(parts) < 3 or parts[0] != "qualibot":
            continue  # not one of ours (shouldn't happen on these experiments, but don't fail the row)
        division, scorer_name = parts[1].upper(), parts[2]
        feedback = a.get("feedback") or {}
        rows.append({
            "trace_id": trace_id,
            "division": division,
            "scorer_name": scorer_name,
            "request_time": request_time_ms,
            "value": str(feedback.get("value")) if feedback.get("value") is not None else None,
            "rationale": a.get("rationale"),
            "assessment_created_at": a.get("create_time"),
        })
    return rows

# COMMAND ----------

# DBTITLE 1,Incremental read per division experiment
import pandas as pd

table_exists = spark.catalog.tableExists(OUTPUT_TABLE)
watermarks = {}
if table_exists:
    df_wm = spark.sql(f"SELECT division, max(request_time) AS max_rt FROM {OUTPUT_TABLE} GROUP BY division").toPandas()
    watermarks = dict(zip(df_wm["division"], df_wm["max_rt"]))
else:
    print(f"{OUTPUT_TABLE} does not exist yet — first run, using the default lookback.")

default_lo = int((datetime.now(timezone.utc) - timedelta(days=DEFAULT_LOOKBACK_DAYS)).timestamp() * 1000)
now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)

all_rows = []
for division, exp_id in DIVISION_EXPERIMENT_IDS.items():
    lo = int(watermarks[division]) + 1 if division in watermarks else default_lo
    traces = mlflow.search_traces(
        experiment_ids=[exp_id],
        filter_string=f"timestamp_ms >= {lo} AND timestamp_ms <= {now_ms}",
        max_results=5000,
        return_type="pandas",
    )
    n_with = 0
    for _, row in traces.iterrows():
        rows = rows_from_trace(row["trace_id"], int(row["request_time"]), row.get("assessments"))
        if rows:
            n_with += 1
            all_rows.extend(rows)
    print(f"[{division}] {len(traces)} trace(s) in window, {n_with} with assessments -> "
          f"{sum(1 for r in all_rows if r['division'] == division)} row(s).")

df_final = pd.DataFrame(all_rows)
print(f"\n{len(df_final)} assessment row(s) total across {df_final['scorer_name'].nunique() if not df_final.empty else 0} scorer(s).")

# COMMAND ----------

# DBTITLE 1,Persist
if df_final.empty:
    print("Nothing new to sync.")
else:
    df_final["synced_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    spark.createDataFrame(df_final).write.mode("append").option("mergeSchema", "true").saveAsTable(OUTPUT_TABLE)
    print(f"Appended {len(df_final)} row(s) to {OUTPUT_TABLE}.")

dbutils.notebook.exit(json.dumps({"n_synced": len(all_rows)}))
