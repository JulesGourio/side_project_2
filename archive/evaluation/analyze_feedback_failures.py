# Databricks notebook source
# DBTITLE 1,Header
# MAGIC %md
# MAGIC # Feedback-Driven Failure Catalogue
# MAGIC
# MAGIC Turns real negative user feedback (`chat_feedbacks`, vote=down + comment) into
# MAGIC a regression-test catalogue: extracts the failure category and, when the user
# MAGIC explicitly named it, the document they expected — then replays the original
# MAGIC question against the CURRENT endpoint to check whether the failure still
# MAGIC reproduces. Writes to `{CATALOG_SCHEMA}.feedback_failure_cases` (append-only).

# COMMAND ----------

# DBTITLE 1,Setup
# MAGIC %pip install --upgrade mlflow[databricks] httpx
# MAGIC dbutils.library.restartPython()

# COMMAND ----------

# DBTITLE 1,Parameters
CATALOG_SCHEMA = "uat_landingzone.qualibot"
TARGET_TABLE = f"{CATALOG_SCHEMA}.feedback_failure_cases"

STAGING_VOLUME = "/Volumes/uat_landingzone/qualibot/staging/lakebase_export"
CHAT_MESSAGES_PATH = f"{STAGING_VOLUME}/chat_messages.json"
CHAT_FEEDBACKS_PATH = f"{STAGING_VOLUME}/chat_feedbacks.json"

DIVISION_ENDPOINTS = {
    "ALL": "ka-7679a56e-endpoint",
    "IS": "ka-1560aded-endpoint",
    "AS": "ka-3a7e9255-endpoint",
}

LLM_MODEL = "databricks-gpt-5-6-luna"
LLM_MAX_TOKENS = 2000  # this model is a "thinking" model — a low budget silently returns empty content

# COMMAND ----------

# DBTITLE 1,Auth — cluster's attached identity
import json
import re
from datetime import datetime, timezone

import httpx
import pandas as pd
from databricks.sdk.core import Config

_cfg = Config()
_auth = _cfg.authenticate()
HOST = _cfg.host.rstrip("/")
HEADERS = {"Authorization": _auth["Authorization"], "Content-Type": "application/json"}
print(f"Host: {HOST}")

# COMMAND ----------

# DBTITLE 1,Helpers
def call_llm(prompt: str) -> str:
    resp = httpx.post(
        f"{HOST}/serving-endpoints/{LLM_MODEL}/invocations",
        json={"messages": [{"role": "user", "content": prompt}], "max_tokens": LLM_MAX_TOKENS},
        headers=HEADERS, timeout=60,
    )
    resp.raise_for_status()
    choice = resp.json().get("choices", [{}])[0].get("message", {}).get("content", "")
    if isinstance(choice, list):
        choice = "".join(b.get("text", "") for b in choice if isinstance(b, dict) and b.get("type") == "text")
    return choice or ""


def parse_json_object(text: str) -> dict:
    m = re.search(r"\{.*\}", text, re.DOTALL)
    return json.loads(m.group(0)) if m else {}


def extract_agent_sources(content_items: list) -> set:
    import urllib.parse
    refs = set()
    for item in content_items:
        for ann in item.get("annotations", []) or []:
            if ann.get("type") != "url_citation":
                continue
            url = ann.get("url", "")
            ref = urllib.parse.parse_qs(urllib.parse.urlparse(url).query).get("ref", [""])[0]
            m = re.search(r"\[Source:\s*([^|]+)\|", urllib.parse.unquote(url))
            if not ref and m:
                ref = m.group(1).strip()
            if ref:
                refs.add(ref)
    return refs


def call_ka(question: str, endpoint_name: str) -> tuple:
    resp = httpx.post(
        f"{HOST}/serving-endpoints/{endpoint_name}/invocations",
        json={"input": [{"role": "user", "content": question}], "stream": False, "databricks_options": {"return_trace": True}},
        headers=HEADERS, timeout=90,
    )
    resp.raise_for_status()
    data = resp.json()
    answer_parts, sources = [], set()
    for out in data.get("output", []):
        if out.get("type") != "message":
            continue
        content_items = out.get("content", []) or []
        for item in content_items:
            if item.get("type") == "output_text":
                answer_parts.append(item.get("text", ""))
        sources |= extract_agent_sources(content_items)
    return "".join(answer_parts), sources


print("Helpers ready.")

# COMMAND ----------

# DBTITLE 1,Join negative feedback to the original question + answer
df_msgs = spark.read.json(CHAT_MESSAGES_PATH).toPandas()
df_fb = spark.read.json(CHAT_FEEDBACKS_PATH).toPandas()

df_fb = df_fb[(df_fb["vote"] == "down") & df_fb["comment"].notna() & (df_fb["comment"].str.strip() != "")]
df_msgs["created_at"] = pd.to_datetime(df_msgs["created_at"])

df_answers = df_msgs.set_index("id")
user_msgs = df_msgs[df_msgs["role"] == "user"].sort_values("created_at")

cases = []
for _, fb in df_fb.iterrows():
    if fb["message_id"] not in df_answers.index:
        continue
    ans = df_answers.loc[fb["message_id"]]
    prior = user_msgs[(user_msgs["session_id"] == fb["session_id"]) & (user_msgs["created_at"] < ans["created_at"])]
    question = prior.iloc[-1]["content"] if not prior.empty else None
    if not question:
        continue
    cases.append({
        "message_id": fb["message_id"], "session_id": fb["session_id"], "division": ans.get("division") or "ALL",
        "vote": fb["vote"], "comment": fb["comment"], "question": question,
        "original_answer": ans.get("content") or "", "original_sources": ans.get("sources_json") or "",
    })

print(f"{len(cases)} negative-feedback case(s) with a resolvable question.")

# COMMAND ----------

# DBTITLE 1,Extract failure category + expected ref, then replay against the current endpoint
EXTRACT_PROMPT = """Voici un commentaire d'un utilisateur insatisfait de la réponse d'un assistant documentaire.
Commentaire: "{comment}"

Réponds STRICTEMENT en JSON, sans texte autour:
{{"category": "wrong_document|off_topic|wrong_language|formatting|other", "expected_ref": "<référence exacte si explicitement mentionnée, sinon null>"}}"""

HYPOTHESIS_PROMPT = """Question utilisateur: {question}
Réponse de l'agent: {answer}
Commentaire négatif: {comment}
Catégorie d'échec: {category}

En une ou deux phrases, propose une hypothèse concrète d'amélioration (prompt système, métadonnée à indexer, configuration de retrieval, etc.)."""

results = []
for case in cases:
    try:
        extracted = parse_json_object(call_llm(EXTRACT_PROMPT.format(comment=case["comment"])))
    except Exception as e:
        extracted = {}
        print(f"extraction failed for message_id={case['message_id']}: {e}")
    category = extracted.get("category") or "other"
    expected_ref = extracted.get("expected_ref") or None

    endpoint = DIVISION_ENDPOINTS.get(case["division"], DIVISION_ENDPOINTS["ALL"])
    try:
        replay_answer, replay_refs = call_ka(case["question"], endpoint)
    except Exception as e:
        replay_answer, replay_refs = "", set()
        print(f"replay failed for message_id={case['message_id']}: {e}")

    if expected_ref:
        replay_status = "fixed" if expected_ref in replay_refs else "still_failing"
    else:
        replay_status = "not_verifiable"

    hypothesis = ""
    if replay_status != "fixed":
        try:
            hypothesis = call_llm(HYPOTHESIS_PROMPT.format(
                question=case["question"], answer=case["original_answer"][:1000],
                comment=case["comment"], category=category,
            )).strip()
        except Exception as e:
            print(f"hypothesis failed for message_id={case['message_id']}: {e}")

    results.append({
        **case, "extracted_expected_ref": expected_ref, "failure_category": category,
        "replay_status": replay_status, "replay_answer_excerpt": replay_answer[:500],
        "improvement_hypothesis": hypothesis, "checked_at": datetime.now(timezone.utc),
    })
    print(f"[{case['division']}] category={category} expected_ref={expected_ref} -> {replay_status}")

print(f"\n{len(results)} case(s) processed.")

# COMMAND ----------

# DBTITLE 1,Persist to Delta (append-only, explicit schema)
from pyspark.sql.types import StringType, StructField, StructType, TimestampType

spark.sql(f"""
CREATE TABLE IF NOT EXISTS {TARGET_TABLE} (
    message_id BIGINT, session_id STRING, division STRING, vote STRING, comment STRING,
    question STRING, original_answer STRING, original_sources STRING,
    extracted_expected_ref STRING, failure_category STRING, replay_status STRING,
    replay_answer_excerpt STRING, improvement_hypothesis STRING, checked_at TIMESTAMP
) USING DELTA
""")

schema = StructType([
    StructField("message_id", StringType()), StructField("session_id", StringType()),
    StructField("division", StringType()), StructField("vote", StringType()), StructField("comment", StringType()),
    StructField("question", StringType()), StructField("original_answer", StringType()),
    StructField("original_sources", StringType()), StructField("extracted_expected_ref", StringType()),
    StructField("failure_category", StringType()), StructField("replay_status", StringType()),
    StructField("replay_answer_excerpt", StringType()), StructField("improvement_hypothesis", StringType()),
    StructField("checked_at", TimestampType()),
])
df_out = pd.DataFrame(results)
if not df_out.empty:
    df_out["message_id"] = df_out["message_id"].astype(str)
spark.createDataFrame(df_out, schema=schema).write.mode("append").saveAsTable(TARGET_TABLE)
print(f"Appended {len(results)} row(s) to {TARGET_TABLE}.")

# COMMAND ----------

# DBTITLE 1,Summary
if results:
    display(pd.DataFrame(results)[["division", "failure_category", "replay_status"]].value_counts().reset_index(name="count"))
else:
    print("No cases to summarize.")
