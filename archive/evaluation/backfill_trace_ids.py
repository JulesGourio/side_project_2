# Databricks notebook source
# DBTITLE 1,Header
# MAGIC %md
# MAGIC # Backfill real trace_id for pre-2026-09-03 ChatBot turns
# MAGIC
# MAGIC One-off (run manually, not scheduled). `chat_messages.trace_id` on turns
# MAGIC answered before the `streaming.py` fix (see score_production_qa.py Technical
# MAGIC Debt #9) holds a fake fallback id, not a real MLflow `tr-<hex>` one — but the
# MAGIC KA's own trace was logged regardless. This finds it after the fact: one
# MAGIC `search_traces` per division over the full needed date range (free, no LLM
# MAGIC calls), then matches each message to the nearest preceding trace,
# MAGIC disambiguated by comparing the trace's own captured user-input text against
# MAGIC the message's actual last user question.
# MAGIC
# MAGIC **Two-pass** (added 2026-09-08): pass 1 searches the 3 current per-division
# MAGIC native experiments (`DIVISION_EXPERIMENT_IDS`). Anything still unresolved goes
# MAGIC to pass 2, which looks up each remaining message's own `endpoint_name` and
# MAGIC searches THAT endpoint's dedicated MLflow experiment instead — retired KA
# MAGIC endpoints (from before the current 3 were provisioned) log to their own
# MAGIC experiment (`ka-<id>-dev-experiment`, found via `search-experiments`), invisible
# MAGIC to a division-only search. Without pass 2, 115/1119 turns looked permanently
# MAGIC unrecoverable — they weren't, retention wasn't the issue, the search just
# MAGIC didn't know where to look. Resolved 1119/1119 once both passes ran.
# MAGIC
# MAGIC `chat_messages` is dropped and recreated on every Lakebase sync (Technical
# MAGIC Debt #3), so the result can't be written back into its `trace_id` column —
# MAGIC it goes to a standalone `{catalog_schema}.chat_message_trace_backfill`
# MAGIC lookup table instead, which `fetch_real_trace_hits()` will consult.

# COMMAND ----------

# DBTITLE 1,Setup
# MAGIC %pip install --upgrade mlflow[databricks] --quiet
# MAGIC dbutils.library.restartPython()

# COMMAND ----------

# DBTITLE 1,Parameters
# Runs against qualibot-uat: the native per-division MLflow experiments only
# exist in this workspace, and uat_landingzone has no imported Delta copy of
# chat_messages (only the staging volume JSON) — same constraint as
# score_production_qa.py's source_type=volume_json path.
dbutils.widgets.text("catalog_schema", "uat_landingzone.qualibot")
dbutils.widgets.text("staging_volume_path", "/Volumes/uat_landingzone/qualibot/staging/lakebase_export")
CATALOG_SCHEMA = dbutils.widgets.get("catalog_schema").strip()
STAGING_VOLUME_PATH = dbutils.widgets.get("staging_volume_path").strip()
OUTPUT_TABLE = f"{CATALOG_SCHEMA}.chat_message_trace_backfill"

DIVISION_EXPERIMENT_IDS = {
    "ALL": "4171178917767011",
    "IS": "2748374992560665",
    "AS": "2748374992560664",
}
# A KA call must start before the assistant message is persisted (created_at is
# stamped after generation completes) — bound how far back a candidate trace's
# start can be from created_at (generous: covers even a slow multi-tool-call turn).
MAX_GENERATION_SECONDS = 300

import mlflow
print(f"MLflow {mlflow.__version__}")

# COMMAND ----------

# DBTITLE 1,Eligible messages + the question each answers
# Same eligibility filter as score_production_qa.py's df_pairs, restricted to
# rows whose trace_id is NOT the real tr-<hex> format (pre-fix or failed capture).
spark.read.json(f"{STAGING_VOLUME_PATH}/chat_messages.json").createOrReplaceTempView("_chat_messages_src")

df_targets = spark.sql("""
    WITH msgs AS (
        SELECT id, created_at, session_id, division, role, content, trace_id, endpoint_name
        FROM _chat_messages_src
        WHERE status = 'ok' AND deleted = false
    ),
    prior_user AS (
        SELECT
            id, created_at, session_id, division, trace_id, endpoint_name,
            LAST(CASE WHEN role = 'user' THEN content END) OVER (
                PARTITION BY session_id ORDER BY created_at
                ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING
            ) AS question
        FROM msgs
    )
    SELECT id AS message_id, created_at, division, endpoint_name, question
    FROM prior_user
    WHERE trace_id NOT RLIKE '^tr-[0-9a-f]{32}$'
""").toPandas()
print(f"{len(df_targets)} message(s) needing a real trace_id.")
display(df_targets.groupby("division").size())

# COMMAND ----------

# DBTITLE 1,Matching helper (shared by both passes)
import pandas as pd
from datetime import datetime, timezone

df_targets["created_at"] = pd.to_datetime(df_targets["created_at"])
df_targets["created_ms"] = df_targets["created_at"].astype("int64") // 10**6


def match_against_experiment(sub: pd.DataFrame, exp_id: str, label: str) -> tuple:
    """Searches one MLflow experiment over sub's full date range, then matches
    each row to its nearest preceding trace (a KA call must start before the
    answer is persisted), disambiguating multiple candidates by whether the
    trace's own captured input text contains the message's actual question.
    Returns (results: list[dict], diag: dict)."""
    if sub.empty:
        return [], {}
    lo = int(sub["created_ms"].min() - MAX_GENERATION_SECONDS * 1000)
    hi = int(sub["created_ms"].max())
    try:
        traces = mlflow.search_traces(
            experiment_ids=[exp_id],
            filter_string=f"timestamp_ms >= {lo} AND timestamp_ms <= {hi}",
            max_results=5000,
            return_type="pandas",
        )
    except Exception as e:
        print(f"[{label}] search_traces FAILED: {e!r}")
        return [{"message_id": r["message_id"], "trace_id": None, "match_method": "no_candidate", "candidates": 0}
                for _, r in sub.iterrows()], {"n_targets": len(sub), "error": repr(e)[:200]}
    if traces.empty:
        return [{"message_id": r["message_id"], "trace_id": None, "match_method": "no_candidate", "candidates": 0}
                for _, r in sub.iterrows()], {"n_targets": len(sub), "n_traces": 0, "lo": lo, "hi": hi}

    traces = traces[["trace_id", "request_time", "request"]].copy()
    traces["request_preview"] = traces["request"].astype(str).str.slice(0, 2000)
    traces["request_time"] = traces["request_time"].astype("int64")  # already epoch ms as a string
    traces = traces.sort_values("request_time").reset_index(drop=True)
    diag = {
        "n_targets": len(sub), "n_traces": len(traces), "lo": lo, "hi": hi,
        "trace_rt_min": int(traces["request_time"].min()), "trace_rt_max": int(traces["request_time"].max()),
    }

    results = []
    trace_times = traces["request_time"].to_numpy()
    for _, row in sub.iterrows():
        window = traces[(trace_times <= row["created_ms"]) &
                         (trace_times >= row["created_ms"] - MAX_GENERATION_SECONDS * 1000)]
        if window.empty:
            results.append({"message_id": row["message_id"], "trace_id": None,
                             "match_method": "no_candidate", "candidates": 0})
            continue
        if len(window) == 1:
            results.append({"message_id": row["message_id"], "trace_id": window.iloc[0]["trace_id"],
                             "match_method": "unique_in_window", "candidates": 1})
            continue
        q = str(row["question"] or "")[:80].replace("'", "")
        text_hits = window[window["request_preview"].str.contains(q, regex=False, na=False)] if q else window.iloc[0:0]
        if len(text_hits) == 1:
            results.append({"message_id": row["message_id"], "trace_id": text_hits.iloc[0]["trace_id"],
                             "match_method": "text_match", "candidates": len(window)})
        else:
            nearest = window.iloc[(row["created_ms"] - window["request_time"]).abs().argsort().iloc[0]]
            results.append({"message_id": row["message_id"], "trace_id": nearest["trace_id"],
                             "match_method": "nearest_ambiguous", "candidates": len(window)})
    return results, diag

# COMMAND ----------

# DBTITLE 1,Pass 1 — the 3 current per-division native experiments
results = []
diag = {"pass1": {}, "pass2": {}}
for division, exp_id in DIVISION_EXPERIMENT_IDS.items():
    r, d = match_against_experiment(df_targets[df_targets["division"] == division], exp_id, division)
    results.extend(r)
    if d:
        diag["pass1"][division] = d

df_results = pd.DataFrame(results)
print("Pass 1:", df_results["match_method"].value_counts().to_dict())

# COMMAND ----------

# DBTITLE 1,Pass 2 — retired endpoints, each in its own dedicated experiment
# A message whose endpoint predates the 3 current per-division endpoints logs to
# that endpoint's own experiment (name pattern "<endpoint_name>-dev-experiment"),
# which pass 1 never sees. Only worth searching for rows pass 1 didn't resolve.
unresolved_ids = set(df_results.loc[df_results["trace_id"].isna(), "message_id"])
df_unresolved = df_targets[df_targets["message_id"].isin(unresolved_ids)]

if df_unresolved.empty:
    print("Nothing left for pass 2 — pass 1 resolved everything.")
else:
    all_experiments = mlflow.search_experiments(max_results=5000)
    endpoint_to_exp = {}
    for endpoint in df_unresolved["endpoint_name"].dropna().unique():
        match = next((e for e in all_experiments if e.name.endswith(f"/{endpoint}-dev-experiment")), None)
        if match:
            endpoint_to_exp[endpoint] = match.experiment_id
    print(f"Pass 2: {len(df_unresolved)} still-unresolved message(s) across "
          f"{df_unresolved['endpoint_name'].nunique()} endpoint(s), "
          f"{len(endpoint_to_exp)} with a discoverable dedicated experiment.")

    pass2_results = []
    for endpoint, exp_id in endpoint_to_exp.items():
        sub = df_unresolved[df_unresolved["endpoint_name"] == endpoint]
        r, d = match_against_experiment(sub, exp_id, endpoint)
        pass2_results.extend(r)
        diag["pass2"][endpoint] = d

    if pass2_results:
        df_pass2 = pd.DataFrame(pass2_results).set_index("message_id")
        df_results = df_results.set_index("message_id")
        df_results.update(df_pass2)
        df_results = df_results.reset_index()
        print("Pass 2:", pd.DataFrame(pass2_results)["match_method"].value_counts().to_dict())

print("Combined:", df_results["match_method"].value_counts().to_dict())

# COMMAND ----------

# DBTITLE 1,Persist the mapping
df_results["matched_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
spark.createDataFrame(df_results).write.mode("overwrite").option("mergeSchema", "true").saveAsTable(OUTPUT_TABLE)
print(f"Wrote {len(df_results)} row(s) to {OUTPUT_TABLE}.")
print(f"Resolved (non-null trace_id): {df_results['trace_id'].notna().sum()} / {len(df_results)}")

import json as _json
dbutils.notebook.exit(_json.dumps({
    "n_total": len(df_results),
    "n_resolved": int(df_results["trace_id"].notna().sum()),
    "by_method": df_results["match_method"].value_counts().to_dict(),
    "diag": diag,
}))
