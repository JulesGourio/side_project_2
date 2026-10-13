# Databricks notebook source
# DBTITLE 1,Header
# MAGIC %md
# MAGIC # Synthetic Retrieval Questions — Generation (multi-positive + hard negatives)
# MAGIC
# MAGIC For each of a few real chunks per division, generates 3 question styles
# MAGIC (troubleshooting, conceptual, keyword search), calls the real KA endpoint,
# MAGIC reads the retriever's actual top-K from the response trace, and has an LLM
# MAGIC judge every retrieved chunk as FULL / PARTIAL / NO. Nothing is rejected —
# MAGIC every judged chunk is kept: FULL/PARTIAL become ground truth, NO chunks
# MAGIC returned by a real semantic retriever are hard negatives (topically close,
# MAGIC factually wrong), which is exactly the useful signal for Recall@K/NDCG@K.
# MAGIC
# MAGIC Writes to `{CATALOG_SCHEMA}.synthetic_retrieval_questions_v2` (append-only).

# COMMAND ----------

# DBTITLE 1,Setup
# MAGIC %pip install --upgrade mlflow[databricks] httpx
# MAGIC dbutils.library.restartPython()

# COMMAND ----------

# DBTITLE 1,Parameters
CATALOG_SCHEMA = "uat_landingzone.qualibot"
CHUNKS_TABLE = f"{CATALOG_SCHEMA}.chunks_v1"
TARGET_TABLE = f"{CATALOG_SCHEMA}.synthetic_retrieval_questions_v2"

DIVISION_ENDPOINTS = {
    "ALL": "ka-7679a56e-endpoint",
    "IS": "ka-1560aded-endpoint",
    "AS": "ka-3a7e9255-endpoint",
}

CHUNKS_PER_DIVISION = 4
GEN_MODEL = "databricks-gpt-5-6-luna"
GEN_MAX_TOKENS = 2000  # this model is a "thinking" model — a low budget silently returns empty content
RETRIEVER_TOPK = 10  # KA endpoints never return more than 10 retrieved chunks

# COMMAND ----------

# DBTITLE 1,Auth — cluster's attached identity
import json
import re
import uuid
from datetime import datetime, timezone

import httpx
from databricks.sdk.core import Config

_cfg = Config()
_auth = _cfg.authenticate()
HOST = _cfg.host.rstrip("/")
HEADERS = {"Authorization": _auth["Authorization"], "Content-Type": "application/json"}
GENERATION_RUN_ID = str(uuid.uuid4())
print(f"Host: {HOST} | generation_run_id={GENERATION_RUN_ID}")

# COMMAND ----------

# DBTITLE 1,LLM call helper + retriever span extraction
def call_llm(prompt: str) -> str:
    resp = httpx.post(
        f"{HOST}/serving-endpoints/{GEN_MODEL}/invocations",
        json={"messages": [{"role": "user", "content": prompt}], "max_tokens": GEN_MAX_TOKENS},
        headers=HEADERS, timeout=60,
    )
    resp.raise_for_status()
    choice = resp.json().get("choices", [{}])[0].get("message", {}).get("content", "")
    if isinstance(choice, list):
        choice = "".join(b.get("text", "") for b in choice if isinstance(b, dict) and b.get("type") == "text")
    return choice or ""


def call_ka(question: str, endpoint_name: str) -> dict:
    resp = httpx.post(
        f"{HOST}/serving-endpoints/{endpoint_name}/invocations",
        json={"input": [{"role": "user", "content": question}], "stream": False, "databricks_options": {"return_trace": True}},
        headers=HEADERS, timeout=90,
    )
    resp.raise_for_status()
    return resp.json()


def retriever_topk(data: dict, k: int = RETRIEVER_TOPK) -> list:
    """Ordered [{ref, content}] from the response's own RETRIEVER trace span —
    the actual ranked output of the retriever, not a separate Vector Search call."""
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
        results = []
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
                results.append({"ref": ref, "content": content})
        return results[:k]
    return []


def parse_json_object(text: str) -> dict:
    m = re.search(r"\{.*\}", text, re.DOTALL)
    return json.loads(m.group(0)) if m else {}


print("Helpers ready.")

# COMMAND ----------

# DBTITLE 1,Sample chunks per division
chunks_by_division = {}
for division in DIVISION_ENDPOINTS:
    # chunks_v1.division is the source document's division (AS/IS) — there is no
    # "ALL" value there, since ALL means "AS+IS combined" at the endpoint level.
    division_filter = "division IN ('AS', 'IS')" if division == "ALL" else f"division = '{division}'"
    rows = spark.sql(f"""
        SELECT REF, chunk_id, chunk_text, semantic_headers
        FROM {CHUNKS_TABLE}
        WHERE {division_filter} AND chunk_content_type IN ('text', 'table', 'mixed') AND chunk_token_count > 80
        ORDER BY rand()
        LIMIT {CHUNKS_PER_DIVISION}
    """).collect()
    chunks_by_division[division] = rows
    print(f"{division}: {len(rows)} chunk(s) sampled.")

# COMMAND ----------

# DBTITLE 1,Generate 3 typed questions per chunk
GEN_PROMPT = """Voici un extrait d'un document technique/qualité, avec le contexte hiérarchique du document.

Contexte (titres de sections précédents): {headers}
Extrait: {chunk_text}

Génère 3 requêtes utilisateur DISTINCTES, en français, dont la réponse se trouve entièrement dans cet extrait.
Chaque requête doit nommer le système/processus concerné (pas seulement le composant isolé), pour ressembler à une vraie question métier.

- "troubleshooting": une question posée par un technicien pressé face à un problème concret (style urgent/télégraphique).
- "conceptual": une question d'un nouvel arrivant qui cherche à comprendre le processus.
- "keyword": 3-4 mots-clés bruts, sans grammaire, comme tapés dans une barre de recherche.

Réponds STRICTEMENT en JSON, sans texte autour: {{"troubleshooting": "...", "conceptual": "...", "keyword": "..."}}"""

generated = []  # {division, query_type, question, source_ref, source_chunk_id}
for division, rows in chunks_by_division.items():
    for row in rows:
        chunk_text = (row["chunk_text"] or "")[:1500]
        headers = row["semantic_headers"] or "(aucun)"
        try:
            raw = call_llm(GEN_PROMPT.format(headers=headers, chunk_text=chunk_text))
            qs = parse_json_object(raw)
        except Exception as e:
            print(f"generation failed for {row['REF']}/{row['chunk_id']}: {e}")
            continue
        for query_type in ("troubleshooting", "conceptual", "keyword"):
            q = (qs.get(query_type) or "").strip()
            if q:
                generated.append({
                    "division": division, "query_type": query_type, "question": q,
                    "source_ref": row["REF"], "source_chunk_id": row["chunk_id"],
                })

print(f"{len(generated)} question(s) generated across {len(DIVISION_ENDPOINTS)} division(s).")

# COMMAND ----------

# DBTITLE 1,Judge every retrieved chunk — FULL / PARTIAL / NO
JUDGE_PROMPT = """Question utilisateur: {question}

Extrait de document: {content}

Cet extrait répond-il à la question ? Réponds strictement par un seul mot: FULL, PARTIAL ou NO."""

records = []
for item in generated:
    data = call_ka(item["question"], DIVISION_ENDPOINTS[item["division"]])
    topk = retriever_topk(data)
    seen_refs, positive, partial, hard_negative = set(), [], [], []
    for cand in topk:
        ref = cand["ref"]
        if ref in seen_refs:
            continue
        seen_refs.add(ref)
        try:
            verdict = call_llm(JUDGE_PROMPT.format(question=item["question"], content=cand["content"][:1200])).strip().upper()
        except Exception as e:
            print(f"judge failed for ref={ref}: {e}")
            continue
        if "FULL" in verdict:
            positive.append(ref)
        elif "PARTIAL" in verdict:
            partial.append(ref)
        else:
            hard_negative.append(ref)

    records.append({
        "question_id": str(uuid.uuid4()),
        "question": item["question"],
        "query_type": item["query_type"],
        "division": item["division"],
        "source_ref": item["source_ref"],
        "source_chunk_id": item["source_chunk_id"],
        "positive_refs": positive,
        "partial_refs": partial,
        "hard_negative_refs": hard_negative,
        "retriever_topk_raw": [c["ref"] for c in topk],
        "generation_model": GEN_MODEL,
        "generation_run_id": GENERATION_RUN_ID,
        "created_at": datetime.now(timezone.utc),
    })
    print(f"[{item['division']}/{item['query_type']}] positives={positive} partial={partial} hard_neg={hard_negative}")

print(f"\n{len(records)} question(s) judged.")

# COMMAND ----------

# DBTITLE 1,Persist to Delta (append-only, explicit schema)
from pyspark.sql.types import ArrayType, StringType, StructField, StructType, TimestampType

spark.sql(f"""
CREATE TABLE IF NOT EXISTS {TARGET_TABLE} (
    question_id STRING, question STRING, query_type STRING, division STRING,
    source_ref STRING, source_chunk_id STRING,
    positive_refs ARRAY<STRING>, partial_refs ARRAY<STRING>, hard_negative_refs ARRAY<STRING>,
    retriever_topk_raw ARRAY<STRING>,
    generation_model STRING, generation_run_id STRING, created_at TIMESTAMP
) USING DELTA
""")

schema = StructType([
    StructField("question_id", StringType()), StructField("question", StringType()),
    StructField("query_type", StringType()), StructField("division", StringType()),
    StructField("source_ref", StringType()), StructField("source_chunk_id", StringType()),
    StructField("positive_refs", ArrayType(StringType())), StructField("partial_refs", ArrayType(StringType())),
    StructField("hard_negative_refs", ArrayType(StringType())), StructField("retriever_topk_raw", ArrayType(StringType())),
    StructField("generation_model", StringType()), StructField("generation_run_id", StringType()),
    StructField("created_at", TimestampType()),
])
spark.createDataFrame(records, schema=schema).write.mode("append").saveAsTable(TARGET_TABLE)
print(f"Appended {len(records)} row(s) to {TARGET_TABLE}.")

# COMMAND ----------

# DBTITLE 1,Summary
summary = spark.sql(f"""
    SELECT division,
           COUNT(*) AS questions,
           SUM(size(positive_refs)) AS total_positive,
           SUM(size(hard_negative_refs)) AS total_hard_negative
    FROM {TARGET_TABLE}
    WHERE generation_run_id = '{GENERATION_RUN_ID}'
    GROUP BY division ORDER BY division
""")
display(summary)
