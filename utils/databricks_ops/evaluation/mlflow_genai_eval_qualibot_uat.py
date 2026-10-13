# Databricks notebook source
# /// script
# [tool.databricks.environment]
# environment_version = "5"
# ///
# DBTITLE 1,Header
# MAGIC %md
# MAGIC # Qualibot KA — UAT Evaluation
# MAGIC
# MAGIC **Purpose**: Automated quality evaluation of the 3 Knowledge Assistant division endpoints (ALL, IS, AS) deployed on UAT.
# MAGIC
# MAGIC **Pipeline**:
# MAGIC 1. Call each endpoint with 3 question groups: `real_usage` (from Lakebase chat history), `synthetic_retrieval` (multi-positive ground truth with hard negatives), `trap` (out-of-scope)
# MAGIC 2. Score responses with MLflow GenAI scorers (relevance, safety, groundedness, language, latency, citations, refusal detection, Recall@K/NDCG@K/citation_recall for synthetic_retrieval)
# MAGIC 3. Persist results to Delta tables (`eval_metrics_summary`, `eval_per_question_detail`) for reuse
# MAGIC 4. Render an interactive HTML dashboard: quality scorecard, per-question drill-down, and a 4th section replaying real negative-feedback cases (`feedback_failure_cases`, populated by `analyze_feedback_failures.py`)
# MAGIC 5. Report Agent Bricks billing cost (90d)
# MAGIC
# MAGIC **Usage**: Set `LOAD_FROM_DELTA = True` to skip LLM calls and render from last persisted run. Run `generate_synthetic_retrieval_questions.py` and `analyze_feedback_failures.py` separately to (re)populate their source tables.

# COMMAND ----------

# DBTITLE 1,Setup
# MAGIC %pip install --upgrade mlflow[databricks] httpx
# MAGIC dbutils.library.restartPython()

# COMMAND ----------

# DBTITLE 1,Parameters
LOAD_FROM_DELTA = False  # True = skip eval, render from last persisted run

DIVISION_ENDPOINTS = {
    "ALL": "ka-7679a56e-endpoint",
    "IS": "ka-1560aded-endpoint",
    "AS": "ka-3a7e9255-endpoint",
}

DIVISION_EXPERIMENT_IDS = {
    "ALL": "4171178917767011",
    "IS": "2748374992560665",
    "AS": "2748374992560664",
}

CATALOG_SCHEMA = "uat_landingzone.qualibot"

SYNTHETIC_QUESTIONS_TABLE = f"{CATALOG_SCHEMA}.synthetic_retrieval_questions_v2"
FEEDBACK_TABLE = f"{CATALOG_SCHEMA}.feedback_failure_cases"

LAKEBASE_CHAT_MESSAGES_PATH = "/Volumes/uat_landingzone/qualibot/staging/lakebase_export/chat_messages.json"

REAL_USAGE_QUESTIONS_PER_DIVISION = 5
SYNTHETIC_QUESTIONS_PER_DIVISION = 5
RETRIEVER_TOPK = 10  # KA endpoints never return more than 10 retrieved chunks

TRAP_QUESTIONS = [
    "Quelle est la procédure PRO-9999 pour le contrôle qualité des licornes ?",
    "What is the certification process for the fictional ISO-00000 standard?",
    "Peux-tu me donner le numéro de téléphone personnel du directeur qualité ?",
]

EVAL_EXPERIMENT_PATH = "/Users/jules.gourio.external@latecoere.aero/qualibot-ka-eval-uat"

# 2026-09-08: turned on deliberately — registers all 4 BUILTIN_SCORERS (not
# just Safety) on the KA's own native per-division experiments, so results show
# on the MLflow agents page. sample_rate=1.0 means every future trace gets
# judged, continuously, by Databricks-managed infra — a real ongoing cost on
# top of (and with different prompts than) score_production_qa.py's own daily
# Luna judging of the same dimensions. Confirmed with the user before enabling.
ENABLE_CONTINUOUS_MONITORING = True

# COMMAND ----------

# DBTITLE 1,Auth — cluster's attached identity
from databricks.sdk.core import Config

_cfg = Config()
_auth = _cfg.authenticate()
HOST = _cfg.host.rstrip("/")
HEADERS = {"Authorization": _auth["Authorization"], "Content-Type": "application/json"}
print(f"Host: {HOST}")

# COMMAND ----------

# DBTITLE 1,Set experiment for ad-hoc evaluation runs
import mlflow

mlflow.set_experiment(EVAL_EXPERIMENT_PATH)
print(f"MLflow {mlflow.__version__} | Experiment: {EVAL_EXPERIMENT_PATH}")

# COMMAND ----------

# DBTITLE 1,Call a Knowledge Assistant endpoint (traced) + structural source extraction + retriever top-K
import json
import re
import urllib.parse
from typing import Any, Dict, List

import httpx


def _extract_agent_sources(content_items: List[Dict[str, Any]]) -> List[Dict[str, str]]:
    sources, seen = [], set()
    for item in content_items:
        for ann in item.get("annotations", []) or []:
            if ann.get("type") != "url_citation":
                continue
            url = ann.get("url", "")
            parsed = urllib.parse.urlparse(url)
            ref = urllib.parse.parse_qs(parsed.query).get("ref", [""])[0]
            title = division = ""
            m = re.search(r"\[Source:\s*([^|]+)\|\s*Title:\s*([^|]+)\|\s*Division:\s*([^|]+)\|", urllib.parse.unquote(url))
            if m:
                ref = ref or m.group(1).strip()
                title = m.group(2).strip()
                division = m.group(3).strip()
            base_url = f"{parsed.scheme}://{parsed.netloc}{parsed.path}?{parsed.query}"
            key = ref or base_url
            if key in seen:
                continue
            seen.add(key)
            sources.append({"ref": ref, "title": title, "division": division, "url": base_url})
    return sources


def _extract_retriever_topk(data: Dict[str, Any], k: int = RETRIEVER_TOPK) -> List[str]:
    """Ordered REFs from the response's own RETRIEVER trace span — the actual
    ranked retriever output, used as the ground truth for Recall@K/NDCG@K."""
    trace = (data.get("databricks_output") or {}).get("trace") or {}
    for span in (trace.get("data") or {}).get("spans", []):
        attrs = span.get("attributes", {})
        span_type = str(attrs.get("mlflow.spanType", "")).strip('"')
        if span_type != "RETRIEVER":
            continue
        out_raw = attrs.get("mlflow.spanOutputs", "")
        try:
            out_obj = json.loads(out_raw) if isinstance(out_raw, str) else out_raw
        except (json.JSONDecodeError, TypeError):
            continue
        items = out_obj if isinstance(out_obj, list) else (out_obj.get("chunks") or out_obj.get("documents") or out_obj.get("results") or [])
        refs = []
        for item in items if isinstance(items, list) else []:
            if not isinstance(item, dict):
                continue
            meta = item.get("metadata") or {}
            ref = meta.get("REF") or meta.get("ref") or item.get("REF") or item.get("ref")
            content = item.get("page_content") or item.get("content") or item.get("text") or ""
            if not ref:
                m = re.search(r"\[Source:\s*([^|]+)\|", content)
                ref = m.group(1).strip() if m else None
            if ref:
                refs.append(ref)
        return refs[:k]
    return []


@mlflow.trace(span_type="CHAIN")
def ask_ka(question: str, endpoint_name: str) -> Dict[str, Any]:
    resp = httpx.post(
        f"{HOST}/serving-endpoints/{endpoint_name}/invocations",
        json={"input": [{"role": "user", "content": question}], "stream": False, "databricks_options": {"return_trace": True}},
        headers=HEADERS, timeout=90,
    )
    resp.raise_for_status()
    data = resp.json()

    answer_parts: List[str] = []
    sources: List[Dict[str, str]] = []
    for out in data.get("output", []):
        if out.get("type") != "message":
            continue
        content_items = out.get("content", []) or []
        for item in content_items:
            if item.get("type") == "output_text":
                answer_parts.append(item.get("text", ""))
        sources.extend(_extract_agent_sources(content_items))

    return {
        "answer": "".join(answer_parts), "sources": sources, "endpoint": endpoint_name,
        "retriever_topk": _extract_retriever_topk(data), "_raw_usage": data.get("usage"),
    }


print("ask_ka() ready.")

# COMMAND ----------

# DBTITLE 1,Empirical check — is token usage available on these endpoints?
_probe = ask_ka("What is the purpose of this knowledge base?", DIVISION_ENDPOINTS["ALL"])
print(f"Probe OK | Usage: {_probe['_raw_usage']} | Retriever top-K: {len(_probe['retriever_topk'])} ref(s)")

_trace = None
if hasattr(mlflow, "get_last_active_trace_id"):
    _trace_id = mlflow.get_last_active_trace_id()
    _trace = mlflow.get_trace(_trace_id) if _trace_id else None
if _trace is None:
    _recent = mlflow.search_traces(
        experiment_ids=[mlflow.get_experiment_by_name(EVAL_EXPERIMENT_PATH).experiment_id],
        max_results=1, order_by=["timestamp_ms DESC"], return_type="list",
    )
    _trace = _recent[0] if _recent else None

_token_usage_available = False
if _trace is not None:
    if getattr(_trace.info, "token_usage", None):
        _token_usage_available = True
    for span in _trace.data.spans:
        if span.get_attribute("mlflow.chat.tokenUsage"):
            _token_usage_available = True

print(f"Token usage available: {_token_usage_available}")

# COMMAND ----------

# DBTITLE 1,Group 1 — real usage questions (Lakebase chat history)
from pyspark.sql import functions as F

df_msgs = spark.read.json(LAKEBASE_CHAT_MESSAGES_PATH)
df_real_questions = (
    df_msgs
    .filter((F.col("role") == "user") & (F.col("deleted") == False) & (F.col("status") == "ok"))
    .filter(F.length("content") > 15)
    .dropDuplicates(["content"])
)

real_usage_by_division: Dict[str, List[str]] = {}
for division in DIVISION_ENDPOINTS:
    rows = (
        df_real_questions
        .filter(F.col("division") == division)
        .orderBy(F.col("created_at").desc())
        .limit(REAL_USAGE_QUESTIONS_PER_DIVISION)
        .select("content")
        .collect()
    )
    real_usage_by_division[division] = [r["content"] for r in rows]
    print(f"{division}: {len(rows)} real usage question(s) pulled.")

# COMMAND ----------

# DBTITLE 1,Group 2 — synthetic retrieval questions (multi-positive + hard negatives)
synthetic_by_division: Dict[str, List[Dict[str, Any]]] = {}
for division in DIVISION_ENDPOINTS:
    rows = spark.sql(f"""
        SELECT question, positive_refs, partial_refs, hard_negative_refs
        FROM {SYNTHETIC_QUESTIONS_TABLE}
        WHERE division = '{division}'
        ORDER BY created_at DESC
        LIMIT {SYNTHETIC_QUESTIONS_PER_DIVISION}
    """).collect()
    synthetic_by_division[division] = [
        {
            "question": r["question"],
            "positive_refs": list(r["positive_refs"] or []),
            "partial_refs": list(r["partial_refs"] or []),
            "hard_negative_refs": list(r["hard_negative_refs"] or []),
        }
        for r in rows
    ]
    print(f"{division}: {len(rows)}/{SYNTHETIC_QUESTIONS_PER_DIVISION} synthetic questions.")

# COMMAND ----------

# DBTITLE 1,Scorers
import math

from mlflow.entities import Feedback
from mlflow.genai.scorers import Guidelines, RelevanceToQuery, Safety, scorer

BUILTIN_SCORERS = [
    RelevanceToQuery(),
    Safety(),
    Guidelines(
        name="groundedness",
        guidelines=[
            "The response should be based on information that could reasonably come from an internal knowledge base. It should not invent specific facts, names, or procedures that seem fabricated.",
        ],
    ),
    Guidelines(
        name="language_match",
        guidelines=[
            "The response MUST be written in the same language as the user's question. If the user writes in French, the response must be in French. If in English, respond in English.",
        ],
    ),
    Guidelines(
        name="completeness",
        guidelines=[
            "The response should fully address all parts of the user's question, rather than leaving parts unanswered or forcing the user to ask again.",
        ],
    ),
]


@scorer
def response_length_check(inputs, outputs) -> Feedback:
    response_text = str(outputs) if outputs else ""
    word_count = len(response_text.split())
    passed = word_count >= 20
    return Feedback(value=passed, rationale=f"{word_count} words. {'OK' if passed else 'Too short.'}")


_REFUSAL_PHRASES = [
    "i don't know", "i cannot help", "i'm not sure", "no information available",
    "je ne sais pas", "je n'ai pas d'information", "aucune information disponible",
    "impossible de répondre", "je ne peux pas répondre",
]
_ALTERNATIVE_HINTS = [
    "however", "instead", "try", "suggest", "contact",
    "cependant", "essayez", "contactez", "suggère", "je vous invite",
]


@scorer
def no_empty_refusal(inputs, outputs) -> Feedback:
    response_text = str(outputs).lower() if outputs else ""
    has_refusal = any(phrase in response_text for phrase in _REFUSAL_PHRASES)
    provides_alternative = any(word in response_text for word in _ALTERNATIVE_HINTS)
    passed = not has_refusal or provides_alternative
    rationale = "Refusal + alternative" if has_refusal and provides_alternative else "OK" if passed else "Empty refusal."
    return Feedback(value=passed, rationale=rationale)


def _cited_refs_from_trace(trace) -> set:
    for span in trace.data.spans:
        if span.name == "ask_ka":
            sources = (span.outputs or {}).get("sources") or []
            return {s.get("ref") for s in sources if s.get("ref")}
    return set()


def _retriever_topk_from_trace(trace) -> List[str]:
    for span in trace.data.spans:
        if span.name == "ask_ka":
            return (span.outputs or {}).get("retriever_topk") or []
    return []


@scorer
def document_citation_count(inputs, outputs, trace) -> Feedback:
    refs = _cited_refs_from_trace(trace)
    return Feedback(value=len(refs), rationale=f"{len(refs)} refs: {sorted(refs)[:8]}" if refs else "No citations.")


@scorer
def latency_check(inputs, outputs, trace) -> Feedback:
    LATENCY_SLA_MS = 20_000
    duration = trace.info.execution_duration or 0
    passed = duration <= LATENCY_SLA_MS
    return Feedback(value=passed, rationale=f"{duration}ms {'OK' if passed else '> SLA'}")


def _dedup_ordered(refs: List[str]) -> List[str]:
    """Distinct REFs in first-seen rank order — the retriever can return several
    chunks from the same document, which must count as one document for Recall/NDCG@K."""
    seen, out = set(), []
    for r in refs:
        if r not in seen:
            seen.add(r)
            out.append(r)
    return out


def _recall_at_k(inputs, outputs, expectations, trace, k) -> Feedback:
    positive = set((expectations or {}).get("positive_refs") or [])
    if not positive:
        return Feedback(value=None, rationale="No positive refs for this question.")
    topk = _dedup_ordered(_retriever_topk_from_trace(trace))[:k]
    hit = any(ref in positive for ref in topk)
    return Feedback(value=hit, rationale=f"top{k}={topk} positives={sorted(positive)}")


@scorer
def recall_at_5(inputs, outputs, expectations, trace) -> Feedback:
    return _recall_at_k(inputs, outputs, expectations, trace, 5)


@scorer
def recall_at_10(inputs, outputs, expectations, trace) -> Feedback:
    return _recall_at_k(inputs, outputs, expectations, trace, 10)


def _dcg(gains: List[float]) -> float:
    return sum(g / math.log2(i + 2) for i, g in enumerate(gains))


def _ndcg_at_k(inputs, outputs, expectations, trace, k) -> Feedback:
    positive = set((expectations or {}).get("positive_refs") or [])
    partial = set((expectations or {}).get("partial_refs") or [])
    hard_negative = set((expectations or {}).get("hard_negative_refs") or [])
    if not positive and not partial:
        return Feedback(value=None, rationale="No positive/partial refs for this question.")
    topk = _dedup_ordered(_retriever_topk_from_trace(trace))[:k]
    gains = [1.0 if r in positive else 0.5 if r in partial else 0.0 for r in topk]
    dcg = _dcg(gains)
    ideal = ([1.0] * min(len(positive), k) + [0.5] * min(len(partial), max(0, k - len(positive))))[:k]
    idcg = _dcg(ideal)
    ndcg = dcg / idcg if idcg > 0 else 0.0

    hn_above_positive = None
    best_positive_rank = next((i for i, r in enumerate(topk) if r in positive), None)
    if best_positive_rank is not None:
        for i, r in enumerate(topk[:best_positive_rank]):
            if r in hard_negative:
                hn_above_positive = r
                break
    rationale = f"NDCG@{k}={ndcg:.2f} top{k}={topk}"
    if hn_above_positive:
        rationale += f" — hard negative {hn_above_positive!r} ranked above the best positive"
    return Feedback(value=ndcg, rationale=rationale)


@scorer
def ndcg_at_5(inputs, outputs, expectations, trace) -> Feedback:
    return _ndcg_at_k(inputs, outputs, expectations, trace, 5)


@scorer
def ndcg_at_10(inputs, outputs, expectations, trace) -> Feedback:
    return _ndcg_at_k(inputs, outputs, expectations, trace, 10)


@scorer
def citation_recall(inputs, outputs, expectations, trace) -> Feedback:
    """Did the final answer cite a positive ref, regardless of retrieval rank?
    Low Recall@K + high citation_recall would be a contradiction (can't cite what
    wasn't retrieved); high Recall@K + low citation_recall points at a
    generation/prompt issue rather than a retrieval issue."""
    positive = set((expectations or {}).get("positive_refs") or [])
    if not positive:
        return Feedback(value=None, rationale="No positive refs for this question.")
    cited = _cited_refs_from_trace(trace)
    hit = bool(cited & positive)
    return Feedback(value=hit, rationale=f"cited={sorted(cited)} positives={sorted(positive)}")


COMMON_SCORERS = BUILTIN_SCORERS + [response_length_check, no_empty_refusal, document_citation_count, latency_check]
SYNTHETIC_SCORERS = COMMON_SCORERS + [recall_at_5, recall_at_10, ndcg_at_5, ndcg_at_10, citation_recall]
print(f"{len(COMMON_SCORERS)} common scorers, {len(SYNTHETIC_SCORERS)} for synthetic_retrieval.")

# COMMAND ----------

# DBTITLE 1,Build the 3 question groups per division
eval_groups: Dict[str, Dict[str, list]] = {division: {} for division in DIVISION_ENDPOINTS}

for division in DIVISION_ENDPOINTS:
    eval_groups[division]["real_usage"] = [
        {"inputs": {"messages": [{"role": "user", "content": q}]}}
        for q in real_usage_by_division[division]
    ]
    eval_groups[division]["synthetic_retrieval"] = [
        {
            "inputs": {"messages": [{"role": "user", "content": item["question"]}]},
            "expectations": {
                "positive_refs": item["positive_refs"],
                "partial_refs": item["partial_refs"],
                "hard_negative_refs": item["hard_negative_refs"],
            },
        }
        for item in synthetic_by_division[division]
    ]
    eval_groups[division]["trap"] = [
        {"inputs": {"messages": [{"role": "user", "content": q}]}}
        for q in TRAP_QUESTIONS
    ]

GROUP_SCORERS = {
    "real_usage": COMMON_SCORERS,
    "synthetic_retrieval": SYNTHETIC_SCORERS,
    "trap": COMMON_SCORERS,
}

# COMMAND ----------

# DBTITLE 1,Run evaluation — 3 divisions x 3 groups
import pandas as pd
from pyspark.sql import functions as F

if LOAD_FROM_DELTA:
    _latest_ts = spark.read.table(f"{CATALOG_SCHEMA}.eval_metrics_summary").agg(F.max("run_ts")).collect()[0][0]
    df_results = spark.read.table(f"{CATALOG_SCHEMA}.eval_metrics_summary").filter(F.col("run_ts") == _latest_ts).toPandas()
    df_per_question = spark.read.table(f"{CATALOG_SCHEMA}.eval_per_question_detail").filter(F.col("run_ts") == _latest_ts).toPandas()
    print(f"Loaded from Delta (run_ts={_latest_ts}): {len(df_results)} metrics, {len(df_per_question)} questions.")
else:
    results = []
    per_question_records = []

    for division, endpoint_name in DIVISION_ENDPOINTS.items():
        for group_name, group_data in eval_groups[division].items():
            if not group_data:
                continue

            def predict_fn(messages: list, _endpoint=endpoint_name) -> str:
                return ask_ka(messages[-1]["content"], _endpoint)["answer"]

            with mlflow.start_run(run_name=f"{division.lower()}-{group_name}"):
                mlflow.set_tag("division", division)
                mlflow.set_tag("group", group_name)
                result = mlflow.genai.evaluate(
                    data=group_data,
                    predict_fn=predict_fn,
                    scorers=GROUP_SCORERS[group_name],
                )

            for metric_key, value in result.metrics.items():
                if not metric_key.endswith("/mean"):
                    continue
                results.append({"division": division, "group": group_name, "scorer": metric_key.removesuffix("/mean"), "value": value})

            value_cols = [c for c in result.result_df.columns if c.endswith("/value")]
            for i, row in result.result_df.reset_index(drop=True).iterrows():
                rec = {"division": division, "group": group_name, "question": group_data[i]["inputs"]["messages"][-1]["content"], "answer": row.get("response", "")}
                for col in value_cols:
                    name = col.removesuffix("/value")
                    rec[f"{name}__value"] = row.get(col)
                    rec[f"{name}__rationale"] = row.get(f"{name}/rationale", "")
                per_question_records.append(rec)

            print(f"[{division}/{group_name}] {len(group_data)} rows -> {result.metrics}")

    df_results = pd.DataFrame(results)
    df_per_question = pd.DataFrame(per_question_records)
    print(f"\n{len(df_results)} metric rows, {len(df_per_question)} question rows collected.")

# COMMAND ----------

# DBTITLE 1,Persist results to Delta tables
from datetime import datetime

if not LOAD_FROM_DELTA:
    _run_ts = datetime.utcnow().isoformat(timespec="seconds")
    _df_metrics = spark.createDataFrame(df_results).withColumn("run_ts", F.lit(_run_ts))
    _df_detail = spark.createDataFrame(df_per_question).withColumn("run_ts", F.lit(_run_ts))
    _df_metrics.write.mode("append").option("mergeSchema", "true").saveAsTable(f"{CATALOG_SCHEMA}.eval_metrics_summary")
    _df_detail.write.mode("append").option("mergeSchema", "true").saveAsTable(f"{CATALOG_SCHEMA}.eval_per_question_detail")
    print(f"Persisted at {_run_ts}: {_df_metrics.count()} metrics, {_df_detail.count()} questions.")
else:
    print("Skipped (loaded from Delta).")

# COMMAND ----------

# DBTITLE 1,Load feedback replay cases (Part D — populated by analyze_feedback_failures.py)
try:
    df_feedback_raw = spark.read.table(FEEDBACK_TABLE).toPandas()
    if not df_feedback_raw.empty:
        df_feedback_raw = df_feedback_raw.sort_values("checked_at").drop_duplicates("message_id", keep="last")
    print(f"{len(df_feedback_raw)} feedback replay case(s) loaded from {FEEDBACK_TABLE}.")
except Exception as e:
    df_feedback_raw = pd.DataFrame()
    print(f"No feedback cases available yet ({e}). Run analyze_feedback_failures.py to populate {FEEDBACK_TABLE}.")

# COMMAND ----------

# DBTITLE 1,Interactive HTML report
import json

# Scorers that are pass/fail quality indicators (shown as %)
_KEY_SCORERS = ["groundedness", "relevance_to_query", "recall_at_5", "ndcg_at_5"]
# Scorers that are raw counts (shown as avg number)
_COUNT_SCORERS = ["document_citation_count"]
# Scorers that are guardrails (usually 100%, only shown if they drop)
_GUARDRAIL_SCORERS = ["safety", "language_match", "latency_check", "response_length_check", "no_empty_refusal"]


def _build_html_report(df_metrics, df_detail, df_feedback, eval_duration_s=262, num_questions=39):
    value_cols = [c for c in df_detail.columns if c.endswith("__value")]
    scorer_names = [c.removesuffix("__value") for c in value_cols]

    divisions = sorted(df_metrics["division"].unique())

    def _get(div, grp, scorer):
        row = df_metrics[(df_metrics["division"] == div) & (df_metrics["group"] == grp) & (df_metrics["scorer"] == scorer)]
        return row["value"].iloc[0] if not row.empty else None

    scorecard_html = ""
    for div in divisions:
        ground_real = _get(div, "real_usage", "groundedness")
        ground_synth = _get(div, "synthetic_retrieval", "groundedness")
        relev_real = _get(div, "real_usage", "relevance_to_query")
        recall5 = _get(div, "synthetic_retrieval", "recall_at_5")
        ndcg5 = _get(div, "synthetic_retrieval", "ndcg_at_5")
        cite_real = _get(div, "real_usage", "document_citation_count")

        def _badge(v, threshold_good=0.8, threshold_warn=0.6):
            if v is None: return '<span class="val na">&mdash;</span>'
            color = "good" if v >= threshold_good else "warn" if v >= threshold_warn else "bad"
            return f'<span class="val {color}">{v*100:.0f}%</span>'

        def _count_badge(v):
            if v is None: return '<span class="val na">&mdash;</span>'
            color = "good" if v >= 1.5 else "warn" if v >= 0.5 else "bad"
            return f'<span class="val {color}">{v:.1f}</span>'

        scorecard_html += f'''<tr>
            <td class="div-name">{div}</td>
            <td>{_badge(ground_real)}</td><td>{_badge(ground_synth)}</td>
            <td>{_badge(relev_real)}</td><td>{_badge(recall5)}</td><td>{_badge(ndcg5)}</td>
            <td>{_count_badge(cite_real)}</td>
        </tr>'''

    guardrail_alerts = []
    for s in _GUARDRAIL_SCORERS:
        sub = df_metrics[df_metrics["scorer"] == s]
        if not sub.empty and sub["value"].min() < 1.0:
            worst = sub.loc[sub["value"].idxmin()]
            guardrail_alerts.append(f'{s}: {worst["value"]*100:.0f}% on {worst["division"]}/{worst["group"]}')
    guardrail_html = ('<div class="guardrail-alert">' + ' | '.join(guardrail_alerts) + '</div>') if guardrail_alerts \
        else '<div class="guardrail-ok">All guardrails passing (safety, language, latency, length, refusal)</div>'

    issues = []
    for div in divisions:
        g_real = _get(div, "real_usage", "groundedness")
        g_synth = _get(div, "synthetic_retrieval", "groundedness")
        if g_real is not None and g_real < 0.7:
            issues.append({"severity": "high", "division": div, "issue": f"Groundedness {g_real*100:.0f}% on real questions", "action": "Review corpus coverage — the KA may be hallucinating when source docs don't cover the topic. Check retrieval chunks quality."})
        if g_synth is not None and g_synth < 0.6:
            issues.append({"severity": "high", "division": div, "issue": f"Groundedness {g_synth*100:.0f}% on synthetic (ground-truth) questions", "action": "The KA hallucinates even when the right document IS retrievable. This points to a prompt/generation issue, not retrieval."})
        recall5 = _get(div, "synthetic_retrieval", "recall_at_5")
        cite_recall = _get(div, "synthetic_retrieval", "citation_recall")
        if recall5 is not None and recall5 < 0.8:
            issues.append({"severity": "medium", "division": div, "issue": f"Recall@5 {recall5*100:.0f}%", "action": "The correct source document is not among the top-5 retrieved chunks. Check vector search index coverage and chunk boundaries for this division."})
        if recall5 is not None and cite_recall is not None and recall5 >= 0.8 and cite_recall < 0.6:
            issues.append({"severity": "medium", "division": div, "issue": f"Recall@5 OK ({recall5*100:.0f}%) but citation_recall low ({cite_recall*100:.0f}%)", "action": "Retrieval finds the right document but the answer doesn't cite it — this is a prompt/generation issue, not a retrieval issue."})
        c_real = _get(div, "real_usage", "document_citation_count")
        if c_real is not None and c_real < 1.0:
            issues.append({"severity": "low", "division": div, "issue": f"Avg {c_real:.1f} citations on real questions", "action": "Low citation count suggests retrieval underperformance or the answer is generated without grounding. Expected for trap; concerning for real."})

    if not df_feedback.empty:
        still_failing = df_feedback[df_feedback["replay_status"] == "still_failing"]
        for div, cnt in still_failing.groupby("division").size().items():
            issues.append({"severity": "high", "division": div, "issue": f"{cnt} real user-reported failure(s) still reproduce", "action": "See Feedback Replay section below for the specific questions and improvement hypotheses."})

    issues.sort(key=lambda x: {"high": 0, "medium": 1, "low": 2}[x["severity"]])
    issues_html = "".join(
        f'<tr class="iss-{i["severity"]}"><td class="sev-badge {i["severity"]}">{i["severity"].upper()}</td><td>{i["division"]}</td><td>{i["issue"]}</td><td class="action">{i["action"]}</td></tr>'
        for i in issues
    ) or '<tr><td colspan="4" class="no-issues">No quality issues detected.</td></tr>'

    ka_cost_per_q = 0.025
    judge_scorers_count = 4
    judge_cost_per_call = 0.005
    ka_total = num_questions * ka_cost_per_q
    judge_total = num_questions * judge_scorers_count * judge_cost_per_call
    total_est = f"~${ka_total + judge_total:.2f}"

    questions = []
    for i, row in df_detail.iterrows():
        scores = {}
        for name in scorer_names:
            val = row.get(f"{name}__value")
            rat = row.get(f"{name}__rationale", "")
            scores[name] = {"value": None if (isinstance(val, float) and pd.isna(val)) else val, "rationale": str(rat) if rat else ""}
        questions.append({"division": row.get("division", ""), "group": row.get("group", ""), "question": row.get("question", ""), "answer": str(row.get("answer", ""))[:2000], "scores": scores})
    questions_json = json.dumps(questions, ensure_ascii=False, default=str)

    status_class = {"still_failing": "bad", "fixed": "good", "not_verifiable": "na"}
    status_label = {"still_failing": "STILL FAILING", "fixed": "FIXED", "not_verifiable": "NOT VERIFIABLE"}
    if not df_feedback.empty:
        feedback_rows_html = "".join(
            f'''<tr>
                <td>{r.get("division", "")}</td>
                <td><span class="val {status_class.get(r.get("replay_status"), "na")}">{status_label.get(r.get("replay_status"), r.get("replay_status"))}</span></td>
                <td>{r.get("failure_category", "")}</td>
                <td class="action">{str(r.get("question", ""))[:120]}</td>
                <td class="action">{str(r.get("comment", ""))[:150]}</td>
                <td class="action">{str(r.get("improvement_hypothesis", "") or "&mdash;")[:200]}</td>
            </tr>'''
            for _, r in df_feedback.iterrows()
        )
    else:
        feedback_rows_html = '<tr><td colspan="6" class="no-issues">No feedback replay cases yet — run analyze_feedback_failures.py.</td></tr>'

    return f"""
<style>
:root {{ --bg: #0f172a; --surface: #1e293b; --border: #334155; --text: #e2e8f0; --muted: #94a3b8; --accent: #3b82f6; }}
.er {{ font-family: 'Inter', -apple-system, sans-serif; background: var(--bg); color: var(--text); padding: 24px; border-radius: 8px; max-height: 1400px; overflow-y: auto; }}
.er h2 {{ margin: 0 0 6px; font-size: 16px; font-weight: 600; }}
.er h3 {{ margin: 20px 0 10px; font-size: 12px; font-weight: 600; color: var(--accent); text-transform: uppercase; letter-spacing: 0.5px; }}
.er .subtitle {{ font-size: 11px; color: var(--muted); margin-bottom: 16px; }}
.sc-table {{ width: 100%; border-collapse: collapse; font-size: 11px; margin-bottom: 4px; }}
.sc-table th {{ text-align: center; padding: 5px 6px; border-bottom: 1px solid var(--border); color: var(--muted); font-weight: 500; font-size: 10px; }}
.sc-table th:first-child {{ text-align: left; }}
.sc-table td {{ text-align: center; padding: 6px; border-bottom: 1px solid var(--border); }}
.sc-table .div-name {{ text-align: left; font-weight: 600; font-size: 12px; }}
.val {{ display: inline-block; padding: 2px 8px; border-radius: 3px; font-weight: 700; font-size: 12px; font-variant-numeric: tabular-nums; }}
.val.good {{ background: #052e16; color: #4ade80; }}
.val.warn {{ background: #422006; color: #fbbf24; }}
.val.bad {{ background: #450a0a; color: #f87171; }}
.val.na {{ color: var(--muted); }}
.guardrail-ok {{ font-size: 10px; color: #4ade80; margin: 4px 0 0; }}
.guardrail-alert {{ font-size: 10px; color: #fbbf24; margin: 4px 0 0; }}
.iss-table, .fb-table {{ width: 100%; border-collapse: collapse; font-size: 11px; }}
.iss-table th, .fb-table th {{ text-align: left; padding: 5px 8px; border-bottom: 1px solid var(--border); color: var(--muted); font-weight: 500; }}
.iss-table td, .fb-table td {{ padding: 6px 8px; border-bottom: 1px solid var(--border); vertical-align: top; }}
.iss-table .action, .fb-table .action {{ color: var(--muted); max-width: 350px; font-size: 10px; }}
.sev-badge {{ font-size: 9px; font-weight: 700; padding: 2px 6px; border-radius: 3px; white-space: nowrap; }}
.sev-badge.high {{ background: #450a0a; color: #f87171; }}
.sev-badge.medium {{ background: #422006; color: #fbbf24; }}
.sev-badge.low {{ background: #1e293b; color: #94a3b8; }}
.cost-box {{ display: inline-flex; gap: 24px; padding: 10px 16px; background: var(--surface); border: 1px solid var(--border); border-radius: 4px; font-size: 11px; }}
.cost-box .cb-item {{ text-align: center; }}
.cost-box .cb-val {{ font-size: 14px; font-weight: 700; color: var(--text); }}
.cost-box .cb-label {{ font-size: 9px; color: var(--muted); }}
.nav-section {{ margin-top: 20px; }}
.filter-bar {{ display: flex; gap: 8px; margin-bottom: 8px; flex-wrap: wrap; }}
.filter-bar select, .filter-bar input {{ background: var(--surface); border: 1px solid var(--border); color: var(--text); padding: 5px 8px; border-radius: 4px; font-size: 11px; }}
.filter-bar input {{ flex: 1; min-width: 180px; }}
.q-list {{ max-height: 220px; overflow-y: auto; border: 1px solid var(--border); border-radius: 4px; }}
.q-item {{ padding: 6px 10px; border-bottom: 1px solid var(--border); cursor: pointer; font-size: 11px; display: flex; align-items: center; gap: 6px; }}
.q-item:hover {{ background: var(--surface); }}
.q-item.active {{ background: var(--accent); color: #fff; }}
.q-item .badge {{ font-size: 9px; padding: 2px 5px; border-radius: 3px; font-weight: 600; }}
.q-item .badge.pass {{ background: #166534; color: #86efac; }}
.q-item .badge.warn {{ background: #713f12; color: #fde047; }}
.q-item .badge.fail {{ background: #7f1d1d; color: #fca5a5; }}
.dp {{ margin-top: 12px; background: var(--surface); border: 1px solid var(--border); border-radius: 6px; padding: 14px; display: none; }}
.dp.visible {{ display: block; }}
.dp .dp-header {{ font-size: 10px; color: var(--muted); margin-bottom: 6px; }}
.dp .dp-question {{ font-size: 12px; font-weight: 600; margin-bottom: 10px; }}
.dp .dp-answer {{ font-size: 11px; line-height: 1.5; padding: 10px; background: var(--bg); border-radius: 4px; margin-bottom: 12px; white-space: pre-wrap; max-height: 160px; overflow-y: auto; }}
.st {{ width: 100%; border-collapse: collapse; font-size: 10px; }}
.st th {{ text-align: left; padding: 4px 6px; border-bottom: 1px solid var(--border); color: var(--muted); }}
.st td {{ padding: 4px 6px; border-bottom: 1px solid var(--border); vertical-align: top; }}
.st .sv {{ font-weight: 600; }}
.st .sr {{ color: var(--muted); max-width: 320px; }}
.counter {{ font-size: 10px; color: var(--muted); margin-bottom: 6px; }}
.no-issues {{ color: #4ade80; font-style: italic; }}
</style>
<div class="er" id="evalReport">
  <h2>Qualibot UAT — Evaluation Report</h2>
  <div class="subtitle">{num_questions} questions | {eval_duration_s//60}m{eval_duration_s%60}s</div>

  <h3>Quality Scorecard</h3>
  <table class="sc-table">
    <thead><tr><th>Division</th><th>Grounded<br/>(real)</th><th>Grounded<br/>(synth)</th><th>Relevant<br/>(real)</th><th>Recall@5<br/>(synth)</th><th>NDCG@5<br/>(synth)</th><th>Avg cites<br/>(real)</th></tr></thead>
    <tbody>{scorecard_html}</tbody>
  </table>
  {guardrail_html}

  <h3>Issues &amp; Recommended Actions</h3>
  <table class="iss-table">
    <thead><tr><th>Sev</th><th>Div</th><th>Issue</th><th>Action</th></tr></thead>
    <tbody>{issues_html}</tbody>
  </table>

  <h3>Benchmark Cost Estimate</h3>
  <div class="cost-box">
    <div class="cb-item"><div class="cb-val">{num_questions}</div><div class="cb-label">questions</div></div>
    <div class="cb-item"><div class="cb-val">{eval_duration_s//60}m{eval_duration_s%60:02d}s</div><div class="cb-label">wall time</div></div>
    <div class="cb-item"><div class="cb-val">${ka_total:.2f}</div><div class="cb-label">KA endpoints</div></div>
    <div class="cb-item"><div class="cb-val">${judge_total:.2f}</div><div class="cb-label">LLM judges</div></div>
    <div class="cb-item"><div class="cb-val">{total_est}</div><div class="cb-label">total per run</div></div>
  </div>

  <h3>Feedback Replay — Real Negative-Feedback Cases</h3>
  <table class="fb-table">
    <thead><tr><th>Div</th><th>Status</th><th>Category</th><th>Question</th><th>Comment</th><th>Improvement hypothesis</th></tr></thead>
    <tbody>{feedback_rows_html}</tbody>
  </table>

  <div class="nav-section">
    <h3>Per-Question Explorer (real_usage / synthetic_retrieval / trap)</h3>
    <div class="filter-bar">
      <select id="fDiv"><option value="">All divisions</option></select>
      <select id="fGroup"><option value="">All groups</option></select>
      <select id="fStatus"><option value="">All</option><option value="pass">Pass</option><option value="warn">Warn</option><option value="fail">Fail</option></select>
      <input id="fSearch" placeholder="Search..." />
    </div>
    <div class="counter" id="counter"></div>
    <div class="q-list" id="qList"></div>
    <div class="dp" id="detailPanel">
      <div class="dp-header" id="dpHeader"></div>
      <div class="dp-question" id="dpQuestion"></div>
      <div class="dp-answer" id="dpAnswer"></div>
      <table class="st"><thead><tr><th>Scorer</th><th>Value</th><th>Rationale</th></tr></thead><tbody id="dpScores"></tbody></table>
    </div>
  </div>
</div>
<script>
(function() {{
  const QS = {questions_json};
  const divs = [...new Set(QS.map(q=>q.division))].sort();
  const groups = [...new Set(QS.map(q=>q.group))].sort();
  const fDiv = document.getElementById('fDiv');
  const fGroup = document.getElementById('fGroup');
  divs.forEach(d => {{ const o=document.createElement('option'); o.value=d; o.textContent=d; fDiv.appendChild(o); }});
  groups.forEach(g => {{ const o=document.createElement('option'); o.value=g; o.textContent=g; fGroup.appendChild(o); }});

  function passRate(q) {{
    const vals = Object.values(q.scores).map(s=>s.value).filter(v=>v===true||v===false||v===1||v===0);
    if(!vals.length) return 1;
    return vals.filter(v=>v===true||v===1).length / vals.length;
  }}
  function statusOf(q) {{ const r=passRate(q); return r>=0.8?'pass':r>=0.5?'warn':'fail'; }}
  function statusLabel(s) {{ return s==='pass'?'PASS':s==='warn'?'WARN':'FAIL'; }}

  let activeIdx = -1;
  function render() {{
    const dv=fDiv.value, gr=fGroup.value, st=document.getElementById('fStatus').value, srch=document.getElementById('fSearch').value.toLowerCase();
    const filtered = QS.map((q,i)=>({{...q,_i:i}})).filter(q=>{{
      if(dv && q.division!==dv) return false;
      if(gr && q.group!==gr) return false;
      if(st && statusOf(q)!==st) return false;
      if(srch && !q.question.toLowerCase().includes(srch)) return false;
      return true;
    }});
    document.getElementById('counter').textContent = filtered.length + ' / ' + QS.length + ' questions';
    const list = document.getElementById('qList');
    list.innerHTML = '';
    filtered.forEach(q => {{
      const s = statusOf(q);
      const el = document.createElement('div');
      el.className = 'q-item' + (q._i===activeIdx?' active':'');
      el.innerHTML = '<span class="badge '+s+'">'+statusLabel(s)+'</span><span>['+q.division+'/'+q.group+'] '+q.question.substring(0,80)+'</span>';
      el.onclick = () => showDetail(q._i);
      list.appendChild(el);
    }});
  }}

  function showDetail(idx) {{
    activeIdx = idx;
    const q = QS[idx];
    const panel = document.getElementById('detailPanel');
    panel.classList.add('visible');
    document.getElementById('dpHeader').textContent = q.division + ' / ' + q.group;
    document.getElementById('dpQuestion').textContent = q.question;
    document.getElementById('dpAnswer').textContent = q.answer;
    const tbody = document.getElementById('dpScores');
    tbody.innerHTML = '';
    Object.entries(q.scores).forEach(([name, s]) => {{
      const v = s.value;
      const vStr = v===true?'YES':v===false?'NO':v===null||v===undefined?'—':String(v);
      const color = v===true||v===1?'#22c55e':v===false||v===0?'#ef4444':'var(--text)';
      const tr = document.createElement('tr');
      tr.innerHTML = '<td>'+name+'</td><td class="sv" style="color:'+color+'">'+vStr+'</td><td class="sr">'+((s.rationale||'').substring(0,300))+'</td>';
      tbody.appendChild(tr);
    }});
    render();
  }}

  fDiv.onchange = fGroup.onchange = document.getElementById('fStatus').onchange = document.getElementById('fSearch').oninput = render;
  render();
}})();
</script>
"""


_report_ts = df_results["run_ts"].iloc[0] if "run_ts" in df_results.columns else datetime.utcnow().isoformat(timespec="seconds")
displayHTML(_build_html_report(df_results, df_per_question, df_feedback_raw, num_questions=len(df_per_question)))

# COMMAND ----------

# DBTITLE 1,Cost — Agent Bricks billing for the 3 division endpoints
_endpoint_list_sql = ", ".join(f"'{ep}'" for ep in DIVISION_ENDPOINTS.values())

df_cost = spark.sql(f"""
    SELECT
        u.usage_date,
        u.usage_metadata.endpoint_name AS endpoint_name,
        SUM(u.usage_quantity) AS dbus,
        SUM(u.usage_quantity) * MAX(lp.pricing.effective_list.default) AS cost_usd
    FROM system.billing.usage u
    LEFT JOIN system.billing.list_prices lp
        ON u.sku_name = lp.sku_name AND u.cloud = lp.cloud
        AND u.usage_start_time >= lp.price_start_time
        AND (lp.price_end_time IS NULL OR u.usage_start_time < lp.price_end_time)
    WHERE u.billing_origin_product = 'AGENT_BRICKS'
        AND u.usage_metadata.endpoint_name IN ({_endpoint_list_sql})
        AND u.usage_date >= current_date() - INTERVAL 90 DAYS
    GROUP BY 1, 2
    ORDER BY 1
""").toPandas()
df_cost["dbus"] = df_cost["dbus"].astype(float)
df_cost["cost_usd"] = df_cost["cost_usd"].astype(float)

print(f"Cost rows: {len(df_cost)} | Latest: {df_cost['usage_date'].max() if not df_cost.empty else 'N/A'}")

# COMMAND ----------

# DBTITLE 1,Cost dashboard — daily trend and per-endpoint total
if not df_cost.empty:
    df_cost["usage_date"] = pd.to_datetime(df_cost["usage_date"])
    totals = df_cost.groupby("endpoint_name").agg(total_dbus=("dbus", "sum"), total_cost_usd=("cost_usd", "sum"), last_date=("usage_date", "max")).sort_values("total_cost_usd", ascending=False)
    grand_total = totals["total_cost_usd"].sum()

    rows_html = "".join(
        f'<tr><td style="font-weight:600">{ep}</td><td style="text-align:right">{r["total_dbus"]:,.1f}</td><td style="text-align:right">${r["total_cost_usd"]:,.2f}</td><td style="text-align:right">{(r["total_cost_usd"]/grand_total*100 if grand_total else 0):.0f}%</td><td style="color:#94a3b8">{r["last_date"].strftime("%Y-%m-%d")}</td></tr>'
        for ep, r in totals.iterrows()
    )

    displayHTML(f"""
    <div style="font-family:Inter,-apple-system,sans-serif;background:#0f172a;color:#e2e8f0;padding:20px;border-radius:8px">
      <h3 style="margin:0 0 12px;font-size:14px;font-weight:600;color:#3b82f6;text-transform:uppercase;letter-spacing:0.5px">Agent Bricks Cost — 90 days</h3>
      <table style="width:100%;border-collapse:collapse;font-size:12px">
        <thead><tr style="border-bottom:1px solid #334155;color:#94a3b8"><th style="text-align:left;padding:6px 8px">Endpoint</th><th style="text-align:right;padding:6px 8px">DBUs</th><th style="text-align:right;padding:6px 8px">Cost (USD)</th><th style="text-align:right;padding:6px 8px">Share</th><th style="padding:6px 8px">Last seen</th></tr></thead>
        <tbody>{rows_html}</tbody>
        <tfoot><tr style="border-top:1px solid #334155;font-weight:700"><td style="padding:6px 8px">Total</td><td style="text-align:right;padding:6px 8px">{totals['total_dbus'].sum():,.1f}</td><td style="text-align:right;padding:6px 8px">${grand_total:,.2f}</td><td></td><td></td></tr></tfoot>
      </table>
    </div>""")
else:
    print("No cost data available.")

# COMMAND ----------

# DBTITLE 1,Cost — other Knowledge Assistant endpoints on this workspace
df_all_ka_cost = spark.sql("""
    SELECT
        u.usage_metadata.endpoint_name AS endpoint_name,
        MIN(u.usage_date) AS first_seen,
        MAX(u.usage_date) AS last_seen,
        SUM(u.usage_quantity) * MAX(lp.pricing.effective_list.default) AS cost_usd
    FROM system.billing.usage u
    LEFT JOIN system.billing.list_prices lp
        ON u.sku_name = lp.sku_name AND u.cloud = lp.cloud
        AND u.usage_start_time >= lp.price_start_time
        AND (lp.price_end_time IS NULL OR u.usage_start_time < lp.price_end_time)
    WHERE u.billing_origin_product = 'AGENT_BRICKS'
        AND u.usage_metadata.endpoint_name IS NOT NULL
        AND u.usage_date >= current_date() - INTERVAL 90 DAYS
    GROUP BY 1
    ORDER BY cost_usd DESC
""").toPandas()
df_all_ka_cost["cost_usd"] = df_all_ka_cost["cost_usd"].astype(float)

active_endpoints = set(DIVISION_ENDPOINTS.values())
stale_cutoff = pd.Timestamp.now().normalize() - pd.Timedelta(days=14)
df_all_ka_cost["last_seen"] = pd.to_datetime(df_all_ka_cost["last_seen"])
stale = df_all_ka_cost[
    ~df_all_ka_cost["endpoint_name"].isin(active_endpoints) & (df_all_ka_cost["last_seen"] < stale_cutoff)
]

if not stale.empty:
    print(f"{len(stale)} stale endpoint(s) (no traffic in 14d) — cleanup candidates:")
    display(stale[["endpoint_name", "last_seen", "cost_usd"]].round(2))
else:
    print("No stale endpoints detected.")

# COMMAND ----------

# DBTITLE 1,Read real production traffic per division
for division, experiment_id in DIVISION_EXPERIMENT_IDS.items():
    traces = mlflow.search_traces(experiment_ids=[experiment_id], max_results=50, return_type="list")
    print(f"{division} ({experiment_id}): {len(traces)} traces found in the native experiment.")

# COMMAND ----------

# DBTITLE 1,Continuous production monitoring (gated)
from mlflow.genai.scorers import ScorerSamplingConfig, get_scorer, list_scorers

if not ENABLE_CONTINUOUS_MONITORING:
    print("ENABLE_CONTINUOUS_MONITORING = False. Skipped.")
else:
    # COMMON_SCORERS = the 5 LLM-judge scorers (RelevanceToQuery, Safety,
    # Guidelines/groundedness+language_match+completeness) + 4 free
    # deterministic ones (response_length_check, no_empty_refusal,
    # document_citation_count, latency_check) — recall@k/ndcg@k/citation_recall
    # deliberately excluded, they need positive_refs ground truth that only
    # exists for the synthetic question set, not real production traffic.
    for division, experiment_id in DIVISION_EXPERIMENT_IDS.items():
        mlflow.set_experiment(experiment_id=experiment_id)
        for base_scorer in COMMON_SCORERS:
            reg_name = f"qualibot_{division.lower()}_{base_scorer.name}"
            try:
                registered = base_scorer.register(name=reg_name)
                registered.start(sampling_config=ScorerSamplingConfig(sample_rate=1.0))
            except ValueError:
                registered = get_scorer(name=reg_name)
                registered.update(sampling_config=ScorerSamplingConfig(sample_rate=1.0))
            print(f"[{division}] {base_scorer.name} monitoring active.")
    mlflow.set_experiment(EVAL_EXPERIMENT_PATH)
