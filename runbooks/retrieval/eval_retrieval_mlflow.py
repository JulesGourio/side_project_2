# Databricks notebook source
# MAGIC %md
# MAGIC # Qualibot — Golden Retrieval Evaluation
# MAGIC
# MAGIC Runs every golden question of `{pipeline_read_env}_proj.qualibot.eval_golden_retrieval` through the search of the
# MAGIC deployed app (`retrieve_for_turn`: translation or rewrite, Vector Search, REF and exact-citation lookups), once per
# MAGIC configuration of the `indexes` widget. No answer is generated.
# MAGIC
# MAGIC **Scores** (distinct documents in the order sent to the LLM, paired questions only):
# MAGIC - **Recall@K**: share of the expected documents among the first K.
# MAGIC - **MRR@K**: 1 / rank of the first expected document (0 when missed).
# MAGIC - **AllRefs@K**: share of questions with every expected document among the first K.
# MAGIC - **@all**: the same over every document the configuration sends. A K above the documents a question gets
# MAGIC   scores its whole context.
# MAGIC
# MAGIC **Outputs**
# MAGIC - Delta table `{pipeline_write_env}_proj.qualibot.<results_table_name>`: one row per configuration and question.
# MAGIC - MLflow experiment `mlflow_experiment`: one evaluation run per configuration (one row per question, its
# MAGIC   documents and scores).
# MAGIC - At the end of the notebook: corpus and index check (expected documents absent from the chunks), key indicators
# MAGIC   per configuration, recall curves, every expected document missed in the top 10 with its cause, rank per question
# MAGIC   and MRR by slice.

# COMMAND ----------
# MAGIC %md
# MAGIC ## Install dependencies from the measured app
# MAGIC `app_code_env` controls where the app is loaded from; `indexes` independently chooses
# MAGIC the index. For example, `indexes = chat,uat=uat_proj.qualibot.chunks_index`.

# COMMAND ----------
dbutils.widgets.text("pipeline_write_env", "dev")
dbutils.widgets.text("app_code_env", "dev")
dbutils.widgets.text("app_code_path", "")
APP = (dbutils.widgets.get("app_code_path").strip()
       or f"/Workspace/.bundle/qualibot/{dbutils.widgets.get('app_code_env')}/files").rstrip("/")

# COMMAND ----------
# MAGIC %pip install -q -r $APP/requirements.txt fastapi pyyaml "databricks-sdk>=0.102" "mlflow[databricks]>=3.4" databricks-agents tqdm

# COMMAND ----------
dbutils.library.restartPython()

# COMMAND ----------
import asyncio
import glob
import hashlib
import inspect
import json
import logging
import math
import os
import re
import shlex
import sys
import threading
import time
import traceback
import uuid
from concurrent.futures import as_completed
from datetime import datetime, timezone

import mlflow
import pandas as pd
import pyspark.sql.functions as F
import yaml
from databricks.sdk import WorkspaceClient
from pyspark.sql import Window
from pyspark.sql.types import (
    ArrayType, BooleanType, DoubleType, IntegerType, LongType,
    MapType, StringType, StructField, StructType, TimestampType,
)
from tqdm.auto import tqdm

log = logging.getLogger("qualibot_retrieval_golden")
log.setLevel(logging.INFO)
if not log.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    log.addHandler(handler)
log.propagate = False

# COMMAND ----------
# MAGIC %md
# MAGIC ## Widgets
# MAGIC | Widget | Default | Meaning |
# MAGIC |---|---|---|
# MAGIC | `pipeline_write_env` | `dev` | catalog of the results table `{env}_proj.qualibot.<results_table_name>` |
# MAGIC | `pipeline_read_env` | `dev` | catalog of the golden table |
# MAGIC | `app_code_env` | `dev` | app code loaded from `/Workspace/.bundle/qualibot/{env}/files` |
# MAGIC | `app_code_path` | empty | another app code folder; wins over `app_code_env` |
# MAGIC | `golden_table_name` | `eval_golden_retrieval` | golden questions |
# MAGIC | `results_table_name` | `eval_retrieval_runs_golden` | one row per (configuration, question) and run |
# MAGIC | `indexes` | `chat` | configurations to measure, see below |
# MAGIC | `ks` | `1,3,5,10,20` | K values of Recall@K, MRR@K, AllRefs@K (distinct documents sent to the LLM); a K above the documents sent scores the whole context; the scoreboard adds the whole context (`all`) |
# MAGIC | `max_parallel` | `1` | questions searched at once (each sends 2 to 5 Vector Search queries) |
# MAGIC | `mlflow_experiment` | empty | MLflow experiment; empty = `/Shared/qualibot/{write_env}_retrieval_eval` |
# MAGIC | `rerun_existing` | `true` | `false` reuses the saved successful rows of an unchanged question, configuration and app code |
# MAGIC | `debug` | `false` | logs the traceback of every failed attempt |
# MAGIC
# MAGIC `indexes`: comma-separated configurations, each `label[=catalog.schema.index][|option=value]...`.
# MAGIC `chat` alone = the app as deployed (its `CHAT_INDEX` and search sizes). Options:
# MAGIC
# MAGIC | Option | Values | What it changes |
# MAGIC |---|---|---|
# MAGIC | `topk` | integer | every size below at once (`rerank`, `raw`, `pairrerank`, `pairraw`) |
# MAGIC | `pairrerank` | integer | reranked passages per query of an opening message in French or English (`CHAT_PAIR_RERANK_TOP_K`) |
# MAGIC | `pairraw` | integer | raw hybrid passages of that opening message (`CHAT_PAIR_RAW_TOP_K`) |
# MAGIC | `rerank` | integer | reranked passages per query of a follow-up or another language (`CHAT_RERANK_TOP_K`) |
# MAGIC | `raw` | integer | raw hybrid passages per query there, 0 = reranked only (`CHAT_RAW_TOP_K`) |
# MAGIC | `rawon` | `question`, `fr`, `en`, joined by `+` | queries that also get a raw search (`CHAT_RAW_ON`) |
# MAGIC | `cap` | integer, 0 = none | cap on the merged passages before the REF lookups (`CHAT_MAX_SEARCH_PASSAGES`) |
# MAGIC | `translate` | `on`, `off` | opening message only translated (two queries) instead of rewritten (`CHAT_TRANSLATE_ONLY`) |
# MAGIC | `rewrite` | endpoint name | model of the rewrite and of the translation (`CHAT_REWRITE_ENDPOINT`) |
# MAGIC | `effort` | `off`, `minimal`, `low`, `medium`, `high` | reasoning effort of that model (`CHAT_REWRITE_REASONING_EFFORT`) |
# MAGIC | `codenorm` | `on`, `off` | `off`: no code normalization at all in the app (query, exact-citation lookup, language variant choice) |
# MAGIC | `merge` | `best`, `rrf` | merge of the result lists of a turn (REF and citation lookups unchanged). `best` = the app: one row per passage at its best rank, lists interleaved rank by rank. `rrf` = each passage scores the sum of `1 / (60 + rank)` over the lists that found it, so a passage found by several queries comes first |
# MAGIC | `split` | BM25 weight from 0 to 1 | each reranked HYBRID query becomes two reranked queries, `ANN` (embeddings) and `FULL_TEXT` (BM25), of k passages each, merged by `weight / (60 + BM25 rank) + (1 - weight) / (60 + embeddings rank)`, k kept |
# MAGIC
# MAGIC Examples:
# MAGIC - code normalization benchmark: `chat,baseline=dev_proj.qualibot.chunks_baseline_index|codenorm=off`
# MAGIC - the same, deeper: `chat|topk=50,baseline=dev_proj.qualibot.chunks_baseline_index|codenorm=off|topk=50`
# MAGIC - query side only, without the baseline index: `chat,query_only=dev_proj.qualibot.chunks_index|codenorm=off`
# MAGIC - search sizes: `chat,wide|cap=100|rerank=60`
# MAGIC - merge and BM25 weight: `app|pairrerank=16|rerank=12,rrf|pairrerank=16|rerank=12|merge=rrf,r05|pairrerank=16|rerank=12|split=0.5`

# COMMAND ----------
for name, default in [
    ("pipeline_write_env", "dev"),
    ("pipeline_read_env", "dev"),
    ("app_code_env", "dev"),
    ("app_code_path", ""),
    ("golden_table_name", "eval_golden_retrieval"),
    ("results_table_name", "eval_retrieval_runs_golden"),
    ("indexes", "chat"),
    ("ks", "1,3,5,10,20"),
    ("max_parallel", "1"),
    ("mlflow_experiment", ""),
]:
    dbutils.widgets.text(name, default)
for name, default, choices in [
    ("rerun_existing", "true", ["true", "false"]),
    ("debug", "false", ["false", "true"]),
]:
    dbutils.widgets.dropdown(name, default, choices)

WRITE_ENV = dbutils.widgets.get("pipeline_write_env").strip()
READ_ENV = dbutils.widgets.get("pipeline_read_env").strip()
APP_ENV = dbutils.widgets.get("app_code_env").strip()
for name, env in [("pipeline_write_env", WRITE_ENV), ("pipeline_read_env", READ_ENV),
                  ("app_code_env", APP_ENV)]:
    if env not in {"dev", "uat", "prod"}:
        raise ValueError(f"Invalid {name}={env!r}; expected dev, uat or prod")

APP = (dbutils.widgets.get("app_code_path").strip()
       or f"/Workspace/.bundle/qualibot/{APP_ENV}/files").rstrip("/")
GOLDEN_TABLE = f"{READ_ENV}_proj.qualibot.{dbutils.widgets.get('golden_table_name').strip()}"
RESULTS_TABLE = f"{WRITE_ENV}_proj.qualibot.{dbutils.widgets.get('results_table_name').strip()}"
INDEXES = dbutils.widgets.get("indexes").strip()
KS = sorted({int(x.strip()) for x in dbutils.widgets.get("ks").split(",") if x.strip()})
if not KS or any(x < 1 for x in KS):
    raise ValueError("ks must contain positive integers")
# "all" = every document the configuration sends to the model.
KS_ALL = [*KS, "all"]
MAX_PARALLEL = max(1, int(dbutils.widgets.get("max_parallel")))
RERUN = dbutils.widgets.get("rerun_existing") == "true"
DEBUG = dbutils.widgets.get("debug") == "true"
EXPERIMENT = (dbutils.widgets.get("mlflow_experiment").strip()
              or f"/Shared/qualibot/{WRITE_ENV}_retrieval_eval")
EXECUTION_ID = uuid.uuid4().hex
log.info("Golden only | table=%s | results=%s | ks=%s | execution=%s",
         GOLDEN_TABLE, RESULTS_TABLE, KS, EXECUTION_ID)

# COMMAND ----------
# MAGIC %md
# MAGIC ## Load the real Qualibot engine and its app configuration
# MAGIC Variables in `app.yaml` are loaded first, then `target_config.env`.
# MAGIC The index and search parameters are overridden only **between configurations**.

# COMMAND ----------
# Settings left by a previous execution in this Python session must not leak into the base configuration.
for name in [n for n in os.environ if n.startswith(("CHAT_", "EVAL_"))]:
    del os.environ[name]

with open(f"{APP}/app.yaml", encoding="utf-8-sig") as fp:
    for item in (yaml.safe_load(fp) or {}).get("env") or []:
        if "value" in item:
            os.environ[item["name"]] = str(item["value"])

target_path = f"{APP}/target_config.env"
if os.path.isfile(target_path):
    with open(target_path, encoding="utf-8") as fp:
        for line in fp:
            match = re.match(r"\s*export\s+([A-Z0-9_]+)=(.*)$", line)
            if match:
                os.environ[match.group(1)] = (shlex.split(match.group(2)) or [""])[0]

os.environ["CHAT_REWRITE_FALLBACK_ENDPOINTS"] = (
    os.environ.get("CHAT_REWRITE_ENDPOINT") or os.environ["CHAT_LLM_ENDPOINT"]
)
sys.path.insert(0, APP)

SIZE_KEYS = {
    "rerank": "CHAT_RERANK_TOP_K",
    "raw": "CHAT_RAW_TOP_K",
    "rawon": "CHAT_RAW_ON",
    "cap": "CHAT_MAX_SEARCH_PASSAGES",
    "translate": "CHAT_TRANSLATE_ONLY",
    "pairrerank": "CHAT_PAIR_RERANK_TOP_K",
    "pairraw": "CHAT_PAIR_RAW_TOP_K",
    "rewrite": "CHAT_REWRITE_ENDPOINT",
    "effort": "CHAT_REWRITE_REASONING_EFFORT",
    "codenorm": "EVAL_CODE_NORMALIZATION",
    "merge": "EVAL_MERGE",
    "split": "EVAL_SPLIT_BM25",
}
TOPK_KEYS = ["CHAT_RERANK_TOP_K", "CHAT_RAW_TOP_K", "CHAT_PAIR_RERANK_TOP_K", "CHAT_PAIR_RAW_TOP_K"]
BASE_SEARCH_ENV = {env_name: os.environ.get(env_name) for env_name in SIZE_KEYS.values()}


def parse_configs(raw: str) -> dict[str, dict]:
    configs = {}
    for item in [part.strip() for part in raw.split(",") if part.strip()]:
        head, *options = [part.strip() for part in item.split("|") if part.strip()]
        label, sep, index = head.partition("=")
        label = label.strip()
        if not sep and re.fullmatch(r"[A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+){2}", label):
            index, label = label, label.replace(".", "_")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", label):
            raise ValueError(f"Invalid config {label!r}: use label=catalog.schema.index")
        if label in configs:
            raise ValueError(f"Duplicate config {label!r}")
        index = index.strip() or os.environ["CHAT_INDEX"]
        overrides = {}
        for option in options:
            key, separator, value = option.partition("=")
            key, value = key.strip(), value.strip()
            if key == "topk" and separator:
                if not value.isdigit() or int(value) < 1:
                    raise ValueError(f"Invalid topk={value!r}")
                overrides.update({env_name: value for env_name in TOPK_KEYS})
                continue
            if key not in SIZE_KEYS or not separator:
                raise ValueError(f"Invalid option {option!r} in {item!r}")
            valid = (
                bool(re.fullmatch(r"(question|fr|en)(\+(question|fr|en))*", value))
                if key == "rawon" else
                value in {"on", "off"} if key in ("translate", "codenorm") else
                value in {"off", "low", "minimal", "medium", "high"} if key == "effort" else
                value in {"best", "rrf"} if key == "merge" else
                bool(re.fullmatch(r"(0(\.\d+)?|1(\.0+)?)", value)) if key == "split" else
                bool(value) if key == "rewrite" else value.isdigit()
            )
            if not valid:
                raise ValueError(f"Invalid {key}={value!r}")
            overrides[SIZE_KEYS[key]] = "" if key == "effort" and value == "off" else value
        configs[label] = {"index": index, "overrides": overrides, "spec": item}
    if not configs:
        raise ValueError("indexes cannot be empty")
    return configs


def apply_config(cfg: dict) -> None:
    os.environ["CHAT_INDEX"] = cfg["index"]
    for key, value in BASE_SEARCH_ENV.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
    os.environ.update(cfg["overrides"])
    os.environ["CHAT_REWRITE_FALLBACK_ENDPOINTS"] = (
        os.environ.get("CHAT_REWRITE_ENDPOINT") or os.environ["CHAT_LLM_ENDPOINT"]
    )
    set_code_normalization(os.environ.get("EVAL_CODE_NORMALIZATION", "on") != "off")


CODE_NORMALIZATION_STEPS = {}   # (module, attribute) -> app function, filled once the engine is imported


def set_code_normalization(enabled: bool) -> None:
    """Switch every code normalization step of the imported engine on (app functions) or off (no-ops).

    :param enabled: False for the baseline without code normalization
    """
    no_op = {"normalize_query": lambda query: query, "canonical_codes": lambda *a, **k: [],
             "cited_codes_asked": lambda *a, **k: []}
    for (module, attribute), original in CODE_NORMALIZATION_STEPS.items():
        setattr(module, attribute, original if enabled else no_op[attribute])


RRF_K = 60
SEARCH_ORIGINAL = {}   # "merge_by_rank" and "query" -> app functions, filled once the engine is imported


class QueryLists(list):
    """A group of result lists merged by the app, with the lists it came from."""

    parts: list


def rrf_merge(lists: list) -> list:
    """Merge result lists by reciprocal rank fusion: a passage scores the sum of ``1 / (60 + rank)`` over its lists.

    :param lists: result lists, each in rank order
    :return: one row per chunk (hits of every copy kept), best score first; ties keep the app's order
    """
    rows = SEARCH_ORIGINAL["merge_by_rank"](lists)
    score = {}
    for part in lists:
        for rank, row in enumerate(part):
            score[row["chunk_id"]] = score.get(row["chunk_id"], 0.0) + 1 / (RRF_K + rank + 1)
    return sorted(rows, key=lambda row: -score[row["chunk_id"]])


def eval_merge_by_rank(lists: list) -> list:
    """Stand-in for the app's merge of the Vector Search results: the app's merge unless ``merge=rrf``.

    The app merges the reranked lists, then the raw lists, then both groups; with ``merge=rrf`` the last call
    merges every list of the turn at once.

    :param lists: result lists in rank order, or the groups returned by the first calls
    :return: one row per chunk, best first
    """
    original = SEARCH_ORIGINAL["merge_by_rank"]
    if os.environ.get("EVAL_MERGE", "best") != "rrf":
        return original(lists)
    if lists and all(isinstance(group, QueryLists) for group in lists):
        return rrf_merge([part for group in lists for part in group.parts])
    merged = QueryLists(original(lists))
    merged.parts = [list(rows) for rows in lists]
    return merged


async def eval_query(host: str, token: str, index: str, query_text: str, k: int, filters: dict,
                     rerank: bool) -> list:
    """Stand-in for the app's Vector Search query: the app's query unless ``split`` applies.

    With ``EVAL_SPLIT_BM25`` set, a reranked query (not a lookup restricted to REFs) is an ``ANN`` and a ``FULL_TEXT``
    query, each reranked, of k results, merged by weighted reciprocal rank.

    :param host: Databricks workspace URL
    :param token: Databricks access token
    :param index: full name of the Vector Search index
    :param query_text: text searched
    :param k: number of results kept
    :param filters: Vector Search filters, empty for none
    :param rerank: whether the reranker orders the results
    :return: the rows, best first, ``score`` = the weighted reciprocal rank
    """
    weight = os.environ.get("EVAL_SPLIT_BM25")
    if not rerank or not weight or "REF" in (filters or {}):
        return await SEARCH_ORIGINAL["query"](host, token, index, query_text, k, filters, rerank=rerank)
    bm25 = float(weight)
    text = engine_search.normalize_query(query_text)[:engine_search._MAX_QUERY_CHARS]
    reranker = {"model": getattr(engine_search, "_RERANKER_MODEL", "databricks_reranker"),
                "parameters": {"columns_to_rerank": getattr(engine_search, "_RERANK_COLUMNS",
                                                            ["REF", "semantic_headers", "chunk_text"])}}

    async def one(query_type: str) -> list:
        """One reranked query of the given type.

        :param query_type: ``ANN`` or ``FULL_TEXT``
        :return: the rows, best first
        """
        payload = {"query_text": text, "columns": engine_search._COLUMNS, "num_results": k,
                   "query_type": query_type, "reranker": reranker}
        if filters:
            payload["filters_json"] = json.dumps(filters)
        resp = await engine_search._post_query(host, token, index, payload)
        if resp.status_code == 400 and "rerank" in resp.text.lower():
            raise engine_search.RerankUnavailable(resp.text[:300])
        resp.raise_for_status()
        return engine_search._rows(resp)

    ann, full_text = await asyncio.gather(one("ANN"), one("FULL_TEXT"))
    score, rows = {}, {}
    for part, part_weight in ((ann, 1 - bm25), (full_text, bm25)):
        for rank, row in enumerate(part):
            cid = row["chunk_id"]
            rows.setdefault(cid, row)
            score[cid] = score.get(cid, 0.0) + part_weight / (RRF_K + rank + 1)
    best = sorted(rows, key=lambda cid: -score[cid])[:k]
    return [{**rows[cid], "score": score[cid]} for cid in best]


def fingerprint(value) -> str:
    data = json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def app_fingerprint(root: str) -> str:
    paths = [
        "app.yaml", "target_config.env",
        "server/services/chat/config.py", "server/services/chat/translation_bridge.py",
        "server/routers/chat/helpers.py", "server/routers/chat/ws.py",
        "server/services/infra/doc_catalog.py", "server/services/infra/vector_search.py",
        "utils/parsing_pipeline/code_normalization.py",
    ] + sorted(os.path.relpath(path, root) for path in glob.glob(f"{root}/server/services/chat/engine/*.py"))
    digest = hashlib.sha256()
    for rel in paths:
        full_path = os.path.join(root, rel)
        if os.path.isfile(full_path):
            digest.update(rel.encode("utf-8"))
            with open(full_path, "rb") as fp:
                digest.update(fp.read())
    return digest.hexdigest()


CONFIGS = parse_configs(INDEXES)
RUN = list(CONFIGS)
APP_FINGERPRINT = app_fingerprint(APP)
RUNNER_VERSION = "golden-only-persistent-loop-v1"

# Ensure the imported engine really comes from the selected deployment.
for module in [name for name in sys.modules if name.split(".")[0] in ("server", "utils")]:
    del sys.modules[module]
from server.services.chat import engine
from server.routers.chat import TRANSLATE_BRIDGE_ENABLED, _trim_history, _with_today_date
from server.services.infra.doc_catalog import canon_ref
from server.services.chat.translation_bridge import translate_question_to_en
from server.services.chat.engine import retrieval as engine_retrieval, search as engine_search
from server.services.infra import vector_search as infra_vector_search
from utils.parsing_pipeline.code_normalization import find_codes

SEARCH_ORIGINAL.update({"merge_by_rank": engine_search._merge_by_rank, "query": engine_search._query})
engine_search._merge_by_rank = eval_merge_by_rank
engine_search._query = eval_query

CODE_NORMALIZATION_STEPS.update({
    (engine_search, "normalize_query"): engine_search.normalize_query,
    (infra_vector_search, "normalize_query"): infra_vector_search.normalize_query,
    (engine_retrieval, "normalize_query"): engine_retrieval.normalize_query,
    (engine_retrieval, "canonical_codes"): engine_retrieval.canonical_codes,
    (engine_retrieval, "cited_codes_asked"): engine_retrieval.cited_codes_asked,
})


if not hasattr(engine, "retrieve_for_turn"):
    raise RuntimeError(f"Missing engine.retrieve_for_turn in {APP}")
engine_path = os.path.realpath(inspect.getfile(engine))
if not engine_path.startswith(os.path.realpath(APP) + os.sep):
    raise RuntimeError(f"Qualibot engine loaded from {engine_path}, expected {APP}")

# The app reads doc_references from Lakebase; the same rows come from the cited_codes of chunks (parsing task 6).
from server.services.infra import doc_references

CHUNKS_TABLE = f"{READ_ENV}_proj.qualibot.chunks"
reference_rows = [r.asDict() for r in spark.sql(f"""
    SELECT code AS cited_code, trim(REF) AS citing_ref, first(division, true) AS division, count(*) AS passages
    FROM {CHUNKS_TABLE} LATERAL VIEW explode(split(cited_codes, ' ')) cited AS code
    WHERE code <> '' AND REF IS NOT NULL AND trim(REF) <> ''
      AND NOT (chunk_content_type <=> 'archive_notice')
    GROUP BY code, trim(REF)
""").collect()]
doc_references.set_references(reference_rows)
if not doc_references.loaded():
    raise RuntimeError(f"No cited_codes in {CHUNKS_TABLE}: the exact citations cannot be evaluated")
DOC_REFERENCES = {"rows": len(reference_rows), "codes": len({r["cited_code"] for r in reference_rows})}
log.info("doc_references from %s: %d rows, %d codes", CHUNKS_TABLE, DOC_REFERENCES["rows"], DOC_REFERENCES["codes"])

SETTINGS = {}
for label, cfg in CONFIGS.items():
    apply_config(cfg)
    SETTINGS[label] = json.loads(json.dumps(engine.settings(), default=str))
    cfg["signature"] = fingerprint({
        "index": cfg["index"], "overrides": cfg["overrides"],
        "settings": SETTINGS[label], "app_code": APP_FINGERPRINT,
        "bridge": TRANSLATE_BRIDGE_ENABLED, "runner": RUNNER_VERSION,
        "code_normalization": os.environ.get("EVAL_CODE_NORMALIZATION", "on"),
        "doc_references": DOC_REFERENCES,
    })
log.info("Configs: %s", {name: cfg["spec"] for name, cfg in CONFIGS.items()})

client = WorkspaceClient()
HOST = client.config.host.rstrip("/")


def token() -> str:
    return client.config.authenticate()["Authorization"].split(" ", 1)[1]


def refs(values) -> list[str]:
    """Canonicalize and deduplicate references, preserving document order."""
    if isinstance(values, str):
        try:
            parsed = json.loads(values)
            values = parsed if isinstance(parsed, list) else [values]
        except (json.JSONDecodeError, TypeError):
            values = [values]
    seen, unique = set(), []
    for value in values or []:
        if value is None:
            continue
        ref = canon_ref(str(value))
        if ref and ref not in seen:
            seen.add(ref)
            unique.append(ref)
    return unique

# COMMAND ----------
# MAGIC %md
# MAGIC ## Golden questions
# MAGIC Skip `no_document` / `to_complete` rows without expected references.
# MAGIC Keep unknown-reference conflicts in the raw results for inspection, but exclude them
# MAGIC from aggregated scores to avoid treating uncertain labels as failures.

# COMMAND ----------
golden_rows = spark.read.table(GOLDEN_TABLE).collect()
CASES, SKIPPED = [], []
for row in golden_rows:
    r = row.asDict(recursive=True)
    question = str(r.get("question") or "").strip()
    expected = refs(r.get("expected_refs"))
    if not question or not expected:
        SKIPPED.append(str(r.get("question_id")))
        continue
    overlap = sorted(set(expected) & set(refs(r.get("unknown_refs"))))
    case = {
        "case_id": str(r["question_id"]),
        "question": question,
        "division": str(r.get("division") or "ALL").upper(),
        "expected": expected,
        "unknown_overlap": overlap,
        "score_eligible": not bool(overlap),
    }
    case["case_fingerprint"] = fingerprint(case)
    CASES.append(case)

if len({c["case_id"] for c in CASES}) != len(CASES):
    raise ValueError("Duplicate question_id in golden dataset")
if not CASES:
    raise ValueError("No golden questions with expected_refs")
ELIGIBLE = [c for c in CASES if c["score_eligible"]]
# Version of the golden set: runs tagged with the same value were scored on the same questions and labels.
GOLDEN_VERSION = fingerprint(sorted(c["case_fingerprint"] for c in CASES))[:12]
if not ELIGIBLE:
    raise ValueError("No eligible golden questions: review unknown_refs/expected_refs")
log.info("Golden: %d total | %d with expected refs | %d eligible | %d unknown conflicts",
         len(golden_rows), len(CASES), len(ELIGIBLE), len(CASES) - len(ELIGIBLE))

# COMMAND ----------
# MAGIC %md
# MAGIC ## Retrieval with a persistent asyncio loop
# MAGIC This preserves Qualibot's translation bridge and retrieval behavior. A single loop serves
# MAGIC every question and configuration. The semaphore limits simultaneous search attempts.
# MAGIC Retries release their semaphore slot during the async backoff.

# COMMAND ----------
class RetrievalStageError(RuntimeError):
    def __init__(self, stage: str, original: Exception):
        self.stage, self.original = stage, original
        super().__init__(f"[{stage}] {type(original).__name__}: {original}")


def error_description(exc: Exception) -> str:
    if isinstance(exc, RetrievalStageError):
        return f"[{exc.stage}] {type(exc.original).__name__}: {str(exc.original)[:500]}"
    return f"[runner] {type(exc).__name__}: {str(exc)[:500]}"


async def search(case: dict) -> dict:
    stage = "authentication"
    try:
        tok = token()
        stage = "trim_history"
        messages = _trim_history([{"role": "user", "content": case["question"]}])
        user_content = next((m["content"] for m in reversed(messages) if m["role"] == "user"), "")
        stage = "translation"
        if TRANSLATE_BRIDGE_ENABLED and user_content:
            english, context = await translate_question_to_en(user_content, HOST, tok)
            if context.needs_translation:
                messages = [dict(m) for m in messages]
                messages[-1]["content"] = english
        stage = "prepare_context"
        conversation = engine._clean_history(_with_today_date(messages))
        stage = "retrieve_for_turn"
        found = await engine.retrieve_for_turn(
            HOST, tok, engine.normalize_division(case["division"]), conversation
        )
        stage = "group_documents"
        documents = engine.group_documents(found["rows"])
        return {
            "retrieved": refs([ref for ref, _ in documents]),
            "n_passages": len(found["rows"]),
            "chars": sum(len(row.get("chunk_text") or "") for row in found["rows"]),
        }
    except Exception as exc:
        raise RetrievalStageError(stage, exc) from exc


async def run_case(case: dict, semaphore: asyncio.Semaphore) -> dict:
    started = time.monotonic()
    for attempt, wait in enumerate((5, 20, None), 1):
        try:
            async with semaphore:
                result = await search(case)
            result["latency_s"] = round(time.monotonic() - started, 3)
            return result
        except Exception as exc:
            log.warning("case=%s attempt=%d/3 %s", case["case_id"], attempt, error_description(exc))
            if DEBUG or wait is None:
                log.warning("traceback case=%s\n%s", case["case_id"],
                            "".join(traceback.format_exception(exc))[-4000:])
            if wait is None:
                return {
                    "error": error_description(exc),
                    "latency_s": round(time.monotonic() - started, 3),
                }
            await asyncio.sleep(wait)
    return {"error": "[runner] Unexpected failure"}


def get_retrieval_loop() -> asyncio.AbstractEventLoop:
    previous = globals().get("_QUALIBOT_RETRIEVAL_LOOP")
    if previous is not None:
        if not previous.is_running():
            raise RuntimeError("Cached async loop closed: restart Python and rerun notebook")
        return previous

    loop = asyncio.new_event_loop()
    ready = threading.Event()

    def run_forever():
        asyncio.set_event_loop(loop)
        loop.call_soon(ready.set)
        loop.run_forever()

    thread = threading.Thread(name="qualibot-golden-loop", target=run_forever, daemon=True)
    thread.start()
    if not ready.wait(10):
        raise RuntimeError("Cannot start persistent retrieval loop")
    globals()["_QUALIBOT_RETRIEVAL_LOOP"] = loop
    globals()["_QUALIBOT_RETRIEVAL_THREAD"] = thread
    return loop


async def make_semaphore(limit: int):
    return asyncio.Semaphore(limit)

# COMMAND ----------
# MAGIC %md
# MAGIC ## Resume and scoring
# MAGIC `rerun_existing=false` reuses successful rows only if the **question, expected refs,
# MAGIC configuration and application code** have not changed. The 4 selected K values are
# MAGIC recomputed from the stored document ranking, so they do not require a new search.

# COMMAND ----------
def score_at_k(expected: list[str], retrieved: list[str], k: int | str) -> dict[str, float]:
    top = refs(retrieved) if k == "all" else refs(retrieved)[:k]
    relevant = set(refs(expected))
    if not relevant:
        raise ValueError("Expected documents cannot be empty")
    matched = relevant & set(top)
    first_rank = next((i for i, ref in enumerate(top, 1) if ref in relevant), None)
    return {
        f"recall_at_{k}": len(matched) / len(relevant),
        f"mrr_at_{k}": 1.0 / first_rank if first_rank is not None else 0.0,
        f"all_refs_at_{k}": float(len(matched) == len(relevant)),
    }


def scores(expected: list[str], retrieved: list[str]) -> dict[str, float]:
    result = {}
    for k in KS_ALL:
        result.update(score_at_k(expected, retrieved, k))
    return result


SCHEMA = StructType([
    StructField("execution_id", StringType(), False),
    StructField("config", StringType(), False),
    StructField("signature", StringType(), False),
    StructField("config_spec", StringType(), False),
    StructField("run_ts", TimestampType(), False),
    StructField("case_id", StringType(), False),
    StructField("case_fingerprint", StringType(), False),
    StructField("question", StringType(), False),
    StructField("division", StringType(), False),
    StructField("expected", ArrayType(StringType()), False),
    StructField("unknown_overlap", ArrayType(StringType()), False),
    StructField("score_eligible", BooleanType(), False),
    StructField("retrieved", ArrayType(StringType()), False),
    StructField("metrics", MapType(StringType(), DoubleType()), False),
    StructField("n_docs", IntegerType(), False),
    StructField("n_passages", IntegerType(), True),
    StructField("chars", LongType(), True),
    StructField("latency_s", DoubleType(), True),
    StructField("error", StringType(), True),
])

DONE = set()
if spark.catalog.tableExists(RESULTS_TABLE) and not RERUN:
    previous = (spark.table(RESULTS_TABLE)
                .where(F.col("config").isin(RUN) & F.col("error").isNull())
                .select("config", "signature", "case_id", "case_fingerprint")
                .distinct().collect())
    DONE = {(r["config"], r["signature"], r["case_id"], r["case_fingerprint"]) for r in previous}

TODO = {
    name: [case for case in CASES if
           (name, CONFIGS[name]["signature"], case["case_id"], case["case_fingerprint"]) not in DONE]
    for name in RUN
}
TOTAL = sum(len(cases) for cases in TODO.values())
log.info("Pending: %s (%d questions total)", {name: len(cases) for name, cases in TODO.items()}, TOTAL)

# COMMAND ----------
# MAGIC %md
# MAGIC ## Execute and save each configuration
# MAGIC Errors remain in the results table rather than being scored as zero. Saving after
# MAGIC each config allows a later run to resume after interruption.

# COMMAND ----------
ALL_ROWS = []
if TOTAL:
    retrieval_loop = get_retrieval_loop()
    with tqdm(total=TOTAL, desc="Golden retrieval", unit="question") as progress:
        for name, cases in TODO.items():
            if not cases:
                continue
            apply_config(CONFIGS[name])
            log.info("[%s] index=%s | cases=%d | concurrency=%d", name,
                     CONFIGS[name]["index"], len(cases), MAX_PARALLEL)
            progress.set_description(f"Golden | {name}")
            semaphore = asyncio.run_coroutine_threadsafe(
                make_semaphore(MAX_PARALLEL), retrieval_loop
            ).result()
            futures = {
                asyncio.run_coroutine_threadsafe(run_case(case, semaphore), retrieval_loop): i
                for i, case in enumerate(cases)
            }
            outputs = [None] * len(cases)
            for future in as_completed(futures):
                i = futures[future]
                try:
                    outputs[i] = future.result()
                except Exception as exc:
                    outputs[i] = {"error": error_description(exc)}
                    log.exception("Unexpected runner error case=%s", cases[i]["case_id"])
                progress.update(1)

            now = datetime.now(timezone.utc)
            batch = []
            for case, result in zip(cases, outputs):
                error = result.get("error")
                retrieved = refs(result.get("retrieved") or [])
                batch.append({
                    "execution_id": EXECUTION_ID,
                    "config": name,
                    "signature": CONFIGS[name]["signature"],
                    "config_spec": CONFIGS[name]["spec"],
                    "run_ts": now,
                    "case_id": case["case_id"],
                    "case_fingerprint": case["case_fingerprint"],
                    "question": case["question"],
                    "division": case["division"],
                    "expected": case["expected"],
                    "unknown_overlap": case["unknown_overlap"],
                    "score_eligible": case["score_eligible"],
                    "retrieved": retrieved,
                    "metrics": scores(case["expected"], retrieved) if not error else {},
                    "n_docs": len(retrieved),
                    "n_passages": result.get("n_passages"),
                    "chars": result.get("chars"),
                    "latency_s": result.get("latency_s"),
                    "error": error,
                })
            spark.createDataFrame(batch, schema=SCHEMA).write.mode("append").format("delta").saveAsTable(RESULTS_TABLE)
            ALL_ROWS.extend(batch)
            log.info("[%s] saved=%d | errors=%d", name, len(batch),
                     sum(bool(row["error"]) for row in batch))
else:
    log.info("Nothing to retrieve; will summarize previously saved matching results")

# COMMAND ----------
# MAGIC %md
# MAGIC ## Paired relevance scores + technical metrics
# MAGIC - `n_questions`: count of successful **paired** golden questions used for Recall/MRR/AllRefs.
# MAGIC - `success_fraction`: successful eligible questions / all eligible questions for this config.
# MAGIC - `search_p95_s`: P95 end-to-end retrieval latency on successful eligible questions
# MAGIC   (translation, search and any retry backoff included).
# MAGIC - `n_docs`, `n_passages`, `chars`: arithmetic means over successful eligible questions.
# MAGIC
# MAGIC `metrics` are recomputed using the chosen K values, including on resumed searches.

# COMMAND ----------
if RERUN:
    EVAL_ROWS = ALL_ROWS
else:
    key_rows = [(name, CONFIGS[name]["signature"], c["case_id"], c["case_fingerprint"])
                for name in RUN for c in CASES]
    keys = spark.createDataFrame(
        key_rows, "config string, signature string, case_id string, case_fingerprint string"
    )
    matched = spark.table(RESULTS_TABLE).join(
        F.broadcast(keys), on=["config", "signature", "case_id", "case_fingerprint"], how="inner"
    )
    window = (Window.partitionBy("config", "case_id")
              .orderBy(F.col("error").isNull().desc(), F.col("run_ts").desc()))
    EVAL_ROWS = [row.asDict(recursive=True) for row in
                 matched.withColumn("row_num", F.row_number().over(window))
                 .where(F.col("row_num") == 1).drop("row_num").collect()]

if not EVAL_ROWS:
    raise RuntimeError("No golden retrieval rows available")

pdf = pd.DataFrame(EVAL_ROWS)
case_scores = [
    scores(r["expected"], r["retrieved"]) if not r.get("error") else {}
    for r in EVAL_ROWS
]
for k in KS_ALL:
    for metric in ("recall", "mrr", "all_refs"):
        column = f"{metric}_at_{k}"
        pdf[column] = [result.get(column, float("nan")) for result in case_scores]

success = pdf[(pdf["score_eligible"] == True) & pdf["error"].isna()].copy()
ids_by_config = {
    name: set(success.loc[success["config"] == name, "case_id"])
    for name in RUN
}
common_ids = set.intersection(*ids_by_config.values()) if ids_by_config else set()
paired = success[success["case_id"].isin(common_ids)].copy()

SUMMARY = []
for name in RUN:
    own = success[success["config"] == name]
    common = paired[paired["config"] == name]
    row = {
        "config": name,
        "n_questions": int(len(common)),
        "success_fraction": float(len(own) / len(ELIGIBLE)),
        "search_p95_s": float(own["latency_s"].quantile(0.95)) if not own.empty else float("nan"),
        "n_docs": float(own["n_docs"].mean()) if not own.empty else float("nan"),
        "n_passages": float(own["n_passages"].mean()) if not own.empty else float("nan"),
        "chars": float(own["chars"].mean()) if not own.empty else float("nan"),
    }
    for k in KS_ALL:
        for metric in ("recall", "mrr", "all_refs"):
            col = f"{metric}_at_{k}"
            row[col] = float(common[col].mean()) if not common.empty else float("nan")
    SUMMARY.append(row)

summary = pd.DataFrame(SUMMARY)
if not common_ids:
    log.warning("No successful question common to all configs; relevance scores are unavailable")
if not success.empty and int(success["n_docs"].max()) < max(KS):
    log.warning("Only %d distinct documents at most in retrieved context; @%d will be capped by the app context",
                int(success["n_docs"].max()), max(KS))

# COMMAND ----------
# MAGIC %md
# MAGIC ## MLflow evaluation runs
# MAGIC One run per configuration, named after it, on the paired questions, and no other run.
# MAGIC - **Runs > Table**: one row per configuration. Parameters `top_k`, `brute`, `fusion`, `bm25`, `codes`; metrics
# MAGIC   `recall_05`, `recall_10`, `recall_all`, `allrefs_10`, `allrefs_all`, `mrr`, `p95_s`, `docs`, `chars`.
# MAGIC - **Runs > Chart**, charts `recall_by_k` and `allrefs_by_k`: one line per configuration, K on the x axis (step).
# MAGIC - A run opens on one row per question with its expected and retrieved documents and its scores.

# COMMAND ----------
from mlflow.genai.scorers import scorer


def log_number(key: str, value) -> None:
    """Log a metric unless it is missing or not finite.

    :param key: metric name
    :param value: metric value, None or NaN for none
    """
    if value is None or pd.isna(value):
        return
    number = float(value)
    if math.isfinite(number):
        mlflow.log_metric(key, number)


client.workspace.mkdirs(EXPERIMENT.rsplit("/", 1)[0])
mlflow.set_experiment(EXPERIMENT)
metric_columns = [f"{metric}_at_{k}" for k in KS_ALL for metric in ("recall", "mrr", "all_refs")]


def metric_scorer(column: str):
    """Scorer returning one metric of ``score_at_k`` for a question.

    :param column: ``<metric>_at_<k>``, e.g. ``recall_at_10`` or ``recall_at_all``
    :return: MLflow scorer named after the column
    """
    suffix = column.rsplit("_", 1)[1]
    k = suffix if suffix == "all" else int(suffix)

    def score(inputs: dict, outputs: dict, expectations: dict) -> float:
        """Metric of one question.

        :param inputs: the question
        :param outputs: retrieved references in context order
        :param expectations: expected references
        :return: the metric value
        """
        return score_at_k(expectations["expected_refs"], outputs["retrieved"], k)[column]

    return scorer(score, name=column)


def output_scorer(field: str):
    """Scorer returning a technical value stored in the outputs (documents, passages, latency).

    :param field: key of ``outputs``
    :return: MLflow scorer named after the field
    """
    def score(outputs: dict) -> float:
        """Value of one question.

        :param outputs: outputs of the question
        :return: the value
        """
        return float(outputs[field])

    return scorer(score, name=field)


# The evaluation page shows one column per scorer: only the headline scores.
HEADLINE_METRICS = [c for c in metric_columns
                    if c in {f"recall_at_{k}" for k in (1, 3, 5, 10, 20)}
                    | {"all_refs_at_5", "all_refs_at_10", "recall_at_all", "mrr_at_all", "all_refs_at_all"}]


def config_params(name: str) -> dict[str, str]:
    """Search settings of a configuration as short MLflow parameters, for the runs table.

    :param name: configuration label
    :return: ``top_k`` (first message / other turns), ``brute``, ``fusion``, ``bm25``, ``codes``, ``index``
    """
    env = {**{k: v for k, v in BASE_SEARCH_ENV.items() if v is not None}, **CONFIGS[name]["overrides"]}
    raw = max(int(env.get("CHAT_PAIR_RAW_TOP_K") or 0), int(env.get("CHAT_RAW_TOP_K") or 0))
    return {
        "top_k": f"{env.get('CHAT_PAIR_RERANK_TOP_K', '?')}/{env.get('CHAT_RERANK_TOP_K', '?')}",
        "brute": "on" if raw else "off",
        "fusion": env.get("EVAL_MERGE", "best"),
        "bm25": f"split {env['EVAL_SPLIT_BM25']}" if env.get("EVAL_SPLIT_BM25") else "hybrid",
        "codes": env.get("EVAL_CODE_NORMALIZATION", "on"),
        "index": CONFIGS[name]["index"],
    }


def log_config_metrics(row: pd.Series) -> None:
    """Short scalar metrics for the runs table, and Recall / AllRefs against K as steps for the line charts.

    :param row: the configuration's row of ``summary``
    """
    for key, column in (("recall_05", "recall_at_5"), ("recall_10", "recall_at_10"), ("recall_all", "recall_at_all"),
                        ("allrefs_10", "all_refs_at_10"), ("allrefs_all", "all_refs_at_all"), ("mrr", "mrr_at_all"),
                        ("p95_s", "search_p95_s"), ("docs", "n_docs"), ("passages", "n_passages"),
                        ("chars", "chars"), ("success_fraction", "success_fraction")):
        if column in row:
            log_number(key, row[column])
    for k in KS:
        mlflow.log_metric("recall_by_k", float(row[f"recall_at_{k}"]), step=k)
        mlflow.log_metric("allrefs_by_k", float(row[f"all_refs_at_{k}"]), step=k)


EVAL_SCORERS = [metric_scorer(c) for c in HEADLINE_METRICS] + [output_scorer(f) for f in ("n_docs", "latency_s")]


for name in RUN:
    rows = paired[paired["config"] == name]
    if rows.empty:
        continue
    data = [{"inputs": {"question": r["question"], "case_id": r["case_id"], "division": r["division"]},
             "outputs": {"retrieved": refs(r["retrieved"]), "n_docs": int(r["n_docs"]),
                         "n_passages": int(r["n_passages"] or 0), "latency_s": float(r["latency_s"] or 0)},
             "expectations": {"expected_refs": refs(r["expected"])}}
            for _, r in rows.iterrows()]
    row = summary[summary["config"] == name].iloc[0]
    with mlflow.start_run(run_name=name) as eval_run:
        mlflow.set_tags({"eval": "retrieval", "source": "golden", "golden_version": GOLDEN_VERSION,
            "golden_questions": str(len(ELIGIBLE)), "execution_id": EXECUTION_ID,
                         "runner": RUNNER_VERSION, "config_signature": CONFIGS[name]["signature"]})
        mlflow.log_params({**config_params(name), "spec": CONFIGS[name]["spec"], "golden_table": GOLDEN_TABLE})
        mlflow.log_dict(SETTINGS[name], "engine_settings.json")
        mlflow.genai.evaluate(data=data, scorers=EVAL_SCORERS)
        log_config_metrics(row)
    log.info("MLflow evaluation run=%s | config=%s | questions=%d", eval_run.info.run_id, name, len(data))

# COMMAND ----------
# MAGIC %md
# MAGIC ## Results
# MAGIC 1. **Corpus and index**: an expected document absent from the chunks can never be found. It is a data gap, not a
# MAGIC    search failure, and it explains most gaps between two environments.
# MAGIC 2. **Key indicators**: one row per configuration, on the same questions.
# MAGIC 3. **Recall curves**: share of the expected documents found in the first K.
# MAGIC 4. **Every expected document missed in the top 10**: its rank in each configuration and the cause.
# MAGIC 5. **Rank per question** and **MRR by language and by question naming a code**.
# COMMAND ----------
import matplotlib.pyplot as plt
import numpy as np
from matplotlib.colors import LinearSegmentedColormap

PALETTE = ["#2a78d6", "#eb6834", "#0ca30c", "#8a4fd0", "#d03b3b", "#1aa5a5", "#b8860b", "#52514e"]
GRID, TEXT, MUTED = "#e1e0d9", "#0b0b0b", "#52514e"
plt.rcParams.update({"axes.spines.top": False, "axes.spines.right": False, "axes.grid": True,
                     "grid.color": GRID, "grid.linewidth": 0.8, "axes.edgecolor": MUTED,
                     "xtick.color": MUTED, "ytick.color": MUTED, "lines.linewidth": 2,
                     "font.size": 10, "legend.frameon": False})
K_CURVE = list(range(1, max(KS) + 1))
K_MAIN = 10 if 10 in KS else max(KS)
COLORS = {name: PALETTE[i % len(PALETTE)] for i, name in enumerate(RUN)}


def reciprocal(rank) -> float:
    """Reciprocal rank, 0 for a missed document.

    :param rank: 1-based rank, None or NaN when missed
    :return: 1 / rank, or 0
    """
    return 0.0 if rank is None or pd.isna(rank) else 1 / rank


def first_rank(expected: list[str], retrieved: list[str]) -> int | None:
    """Rank (1-based) of the first expected document among the retrieved ones.

    :param expected: canonical expected references
    :param retrieved: canonical retrieved references, in context order
    :return: the rank, None when no expected document was retrieved
    """
    relevant = set(refs(expected))
    return next((i for i, ref in enumerate(refs(retrieved), 1) if ref in relevant), None)




def field(row, key: str):
    """Value of a golden row, whether it is a dict or a Spark row.

    :param row: golden row
    :param key: column name
    :return: the value, None when the column is missing
    """
    return (row if isinstance(row, dict) else row.asDict()).get(key)


GOLDEN_LANG = {str(field(r, "question_id")): str(field(r, "lang") or "unknown").lower() for r in golden_rows}

corpus_refs = {canon_ref(r["ref"]) for r in spark.sql(
    f"SELECT DISTINCT trim(REF) AS ref FROM {CHUNKS_TABLE} WHERE REF IS NOT NULL AND trim(REF) <> ''").collect()}
corpus_refs -= {None, ""}
chunk_rows = spark.table(CHUNKS_TABLE).count()
expected_docs = sorted({ref for case in ELIGIBLE for ref in case["expected"]})
absent_docs = [ref for ref in expected_docs if ref not in corpus_refs]


def index_status(name: str) -> dict:
    """State of a Vector Search index, read only.

    :param name: full index name
    :return: ``index``, ``ready``, ``indexed rows`` and the status message
    """
    try:
        status = client.vector_search_indexes.get_index(name).status
        return {"index": name, "ready": bool(status.ready), "indexed rows": status.indexed_row_count,
                "chunks table rows": chunk_rows, "status": (status.message or "")[:150]}
    except Exception as exc:
        return {"index": name, "ready": False, "indexed rows": None, "chunks table rows": chunk_rows,
                "status": f"{type(exc).__name__}: {str(exc)[:150]}"}


display(pd.DataFrame([{
    "chunks table": CHUNKS_TABLE,
    "documents in the chunks": len(corpus_refs),
    "expected documents": len(expected_docs),
    "expected documents absent from the chunks": len(absent_docs),
}]))
display(pd.DataFrame([index_status(name) for name in sorted({cfg["index"] for cfg in CONFIGS.values()})]))
if absent_docs:
    display(pd.DataFrame([{"expected document absent from the chunks": ref,
                           "questions": " | ".join(c["question"] for c in ELIGIBLE if ref in c["expected"])}
                          for ref in absent_docs]))
log.info("Corpus %s: %d documents | %d of %d expected documents absent",
         CHUNKS_TABLE, len(corpus_refs), len(absent_docs), len(expected_docs))

# COMMAND ----------
# MAGIC %md
# MAGIC ### Key indicators, curves and misses
# MAGIC - **≥1 expected doc in top K**: share of questions where the LLM gets at least one right document among the first K.
# MAGIC - **expected docs found in top 10**: share of the expected documents among the first 10 (Recall@10); the next column
# MAGIC   counts only the documents present in the chunks, so it measures the search alone.
# MAGIC - **every expected doc in top 10**: share of questions fully covered.
# MAGIC - **MRR**: 1 / rank of the first right document, averaged (1 = always first, 0.5 = second on average).
# MAGIC - **Misses**: `absent from the chunks` = data gap; `in the chunks, never sent` = search failure;
# MAGIC   `sent, but after rank 10` = ranking issue.
# COMMAND ----------
K5 = 5 if 5 in KS else min(KS)


def search_sizes(name: str) -> str:
    """Passages per query of a configuration, as the app runs it.

    :param name: configuration label
    :return: ``reranked+raw`` of an opening message in French or English, then of the other queries, and the
        merge or BM25 options when set
    """
    env = {**{k: v for k, v in BASE_SEARCH_ENV.items() if v is not None}, **CONFIGS[name]["overrides"]}
    text = (f"{env.get('CHAT_PAIR_RERANK_TOP_K', '?')}+{env.get('CHAT_PAIR_RAW_TOP_K') or 0}"
            f" / {env.get('CHAT_RERANK_TOP_K', '?')}+{env.get('CHAT_RAW_TOP_K') or 0}")
    extras = [f"merge={env['EVAL_MERGE']}" if env.get("EVAL_MERGE") else "",
              f"split={env['EVAL_SPLIT_BM25']}" if env.get("EVAL_SPLIT_BM25") else "",
              "codenorm=off" if env.get("EVAL_CODE_NORMALIZATION") == "off" else ""]
    return " ".join([text, *[e for e in extras if e]])


def recall_in_corpus(rows: pd.DataFrame, k: int) -> float:
    """Recall@k counting only the expected documents present in the chunks.

    :param rows: paired rows of one configuration
    :param k: number of distinct documents looked at
    :return: mean recall, NaN when no question has an expected document in the chunks
    """
    values = []
    for expected, retrieved in zip(rows["expected"], rows["retrieved"]):
        present = [ref for ref in expected if ref in corpus_refs]
        if present:
            values.append(len(set(present) & set(refs(retrieved)[:k])) / len(present))
    return float(np.mean(values)) if values else float("nan")


def kpi_table() -> pd.DataFrame:
    """Key indicators, one row per configuration, on the paired questions.

    :return: the table, configurations in the order of the run
    """
    rows = []
    for name in RUN:
        f = paired[paired["config"] == name]
        own = success[success["config"] == name]
        rows.append({
            "configuration": name,
            "passages per query (first msg / other)": search_sizes(name),
            "questions": len(f),
            "errors": int(((pdf["config"] == name) & pdf["error"].notna()).sum()),
            f"≥1 expected doc in top {K5}": (f[f"mrr_at_{K5}"] > 0).mean(),
            f"≥1 expected doc in top {K_MAIN}": (f[f"mrr_at_{K_MAIN}"] > 0).mean(),
            f"expected docs found in top {K_MAIN}": f[f"recall_at_{K_MAIN}"].mean(),
            "same, docs present in the chunks only": recall_in_corpus(f, K_MAIN),
            f"every expected doc in top {K_MAIN}": f[f"all_refs_at_{K_MAIN}"].mean(),
            "expected docs found in the whole context": f["recall_at_all"].mean(),
            "MRR": f["mrr_at_all"].mean(),
            "docs sent to the LLM": own["n_docs"].mean(),
            "p95 latency (s)": own["latency_s"].quantile(0.95),
        })
    table = pd.DataFrame(rows)
    main = f"expected docs found in top {K_MAIN}"
    table[f"Δ {main} vs {RUN[0]}"] = table[main] - table[main].iloc[0]
    return table


def styled_kpi(table: pd.DataFrame):
    """Key indicators with percentages, the best value of each share in bold and the delta in green or red.

    :param table: output of ``kpi_table``
    :return: pandas Styler
    """
    delta = [c for c in table.columns if c.startswith("Δ")]
    share = [c for c in table.columns if c.startswith(("≥1", "expected", "same", "every"))]
    fmt = {**{c: "{:.0%}" for c in share}, **{c: "{:+.0%}" for c in delta},
           "MRR": "{:.2f}", "docs sent to the LLM": "{:.1f}", "p95 latency (s)": "{:.1f}"}

    def sign(value: float) -> str:
        """CSS color of a delta.

        :param value: the delta
        :return: CSS declaration
        """
        return "" if pd.isna(value) or abs(value) < 1e-9 else f"color: {'#0ca30c' if value > 0 else '#d03b3b'}"

    style = (table.style.format(fmt, na_rep="n/a").hide(axis="index")
             .highlight_max(subset=share + ["MRR"], props="font-weight: bold"))
    return (style.map if hasattr(style, "map") else style.applymap)(sign, subset=delta)


def doc_rank(ref: str, retrieved: list[str]) -> int | None:
    """Rank (1-based) of one document among the retrieved ones.

    :param ref: canonical reference
    :param retrieved: retrieved references, in context order
    :return: the rank, None when not retrieved
    """
    docs = refs(retrieved)
    return docs.index(ref) + 1 if ref in docs else None


def misses_table() -> pd.DataFrame:
    """Every expected document outside the top 10 of at least one configuration, with its rank everywhere.

    :return: rows ``cause``, ``question``, ``expected document`` and one rank column per configuration
        (``-`` = not sent to the LLM)
    """
    by_key = {(r["config"], r["case_id"]): r for _, r in paired.iterrows()}
    rows = []
    for case in ELIGIBLE:
        if case["case_id"] not in common_ids:
            continue
        for ref in case["expected"]:
            ranks = {name: doc_rank(ref, by_key[(name, case["case_id"])]["retrieved"]) for name in RUN}
            if all(rank is not None and rank <= K_MAIN for rank in ranks.values()):
                continue
            cause = ("absent from the chunks" if ref not in corpus_refs else
                     "in the chunks, never sent" if all(rank is None for rank in ranks.values()) else
                     f"sent, but after rank {K_MAIN} somewhere")
            rows.append({"cause": cause, "question": case["question"], "expected document": ref,
                         **{name: "-" if rank is None else str(rank) for name, rank in ranks.items()}})
    return pd.DataFrame(rows).sort_values(["cause", "question"]) if rows else pd.DataFrame()


def ranks_table() -> pd.DataFrame:
    """Rank of the first expected document per question (rows) and configuration (columns), None = missed.

    :return: rows ``case_id``, ``question``, ``lang``, ``has_code``, ``expected`` and one column per configuration
    """
    base = paired[paired["config"] == RUN[0]][["case_id", "question", "expected"]].copy()
    base["lang"] = base["case_id"].map(lambda c: GOLDEN_LANG.get(c, "unknown"))
    base["has_code"] = base["question"].map(lambda q: bool(find_codes(q, accept_lowercase=True)))
    for name in RUN:
        f = paired[paired["config"] == name].set_index("case_id")
        base[name] = [first_rank(f.at[c, "expected"], f.at[c, "retrieved"]) for c in base["case_id"]]
    return base.reset_index(drop=True)


def curve_figure() -> plt.Figure:
    """Recall@K and AllRefs@K against K, every configuration on the same two panels.

    :return: matplotlib figure
    """
    fig, axes = plt.subplots(1, 2, figsize=(13, 4.6), sharex=True)
    for ax, metric, title in [(axes[0], "recall", "Recall@K: share of expected documents in the first K"),
                              (axes[1], "all_refs", "AllRefs@K: every expected document in the first K")]:
        for name in RUN:
            f = paired[paired["config"] == name]
            values = [np.mean([score_at_k(e, r, k)[f"{metric}_at_{k}"] for e, r in zip(f["expected"], f["retrieved"])])
                      for k in K_CURVE]
            ax.plot(K_CURVE, values, color=COLORS[name], label=name, marker="o", markersize=3,
                    markevery=[k - 1 for k in KS])
        ax.set_xscale("log")
        ax.set_xticks(KS)
        ax.set_xticklabels([str(k) for k in KS])
        ax.minorticks_off()
        ax.set_ylim(0, 1.02)
        ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda y, _: f"{y:.0%}"))
        ax.set_xlabel("K (distinct documents, log scale)")
        ax.set_title(title, loc="left", fontsize=10)
    fig.legend(*axes[0].get_legend_handles_labels(), loc="upper right", ncol=min(len(RUN), 5))
    fig.suptitle(f"{len(common_ids)} paired questions", x=0.01, ha="left", fontsize=11)
    fig.tight_layout(rect=(0, 0, 1, 0.9))
    return fig


def rank_figure(ranks: pd.DataFrame) -> plt.Figure:
    """Heatmap of the rank of the first expected document, questions hardest first, one column per configuration.

    :param ranks: output of ``ranks_table``
    :return: matplotlib figure
    """
    observed = ranks[RUN].astype(float).max().max()
    missed = int(max(max(KS), 0 if pd.isna(observed) else observed)) + 1
    values = ranks[RUN].astype(float).fillna(missed)
    order = values.mean(axis=1).sort_values(ascending=False).index
    values, labels = values.loc[order], ranks.loc[order, "question"]
    cmap = LinearSegmentedColormap.from_list("rank", ["#0ca30c", "#f2c94c", "#eb6834", "#d03b3b"])
    fig, ax = plt.subplots(figsize=(1.3 * len(RUN) + 6, 0.24 * len(values) + 1.6))
    ax.imshow(np.log(values.to_numpy()), cmap=cmap, aspect="auto", vmin=0, vmax=np.log(missed))
    for i in range(values.shape[0]):
        for j in range(values.shape[1]):
            rank = values.iat[i, j]
            ax.text(j, i, "-" if rank >= missed else str(int(rank)), ha="center", va="center", fontsize=7,
                    color="white" if rank >= 5 else TEXT)
    ax.set_xticks(range(len(RUN)))
    ax.set_xticklabels(RUN, rotation=30, ha="right")
    ax.xaxis.tick_top()
    ax.set_yticks(range(len(values)))
    ax.set_yticklabels([(q[:80] + "…") if len(q) > 80 else q for q in labels], fontsize=7)
    ax.grid(False)
    ax.set_title("Rank of the first expected document (- = missed), hardest questions first", loc="left",
                 fontsize=10, pad=40)
    fig.tight_layout()
    return fig


def slice_figure(ranks: pd.DataFrame) -> tuple[plt.Figure, pd.DataFrame]:
    """MRR per language and per "the question names a code", one bar per configuration.

    :param ranks: output of ``ranks_table``
    :return: the figure and its table (``slice``, ``value``, ``n`` and one MRR column per configuration)
    """
    rows = []
    for column in ("lang", "has_code"):
        for value, group in ranks.groupby(column):
            row = {"slice": column, "value": str(value), "n": len(group)}
            row.update({name: group[name].map(reciprocal).mean() for name in RUN})
            rows.append(row)
    table = pd.DataFrame(rows)
    fig, ax = plt.subplots(figsize=(9, 0.5 * len(table) * max(1, len(RUN) / 2) + 1.2))
    y = np.arange(len(table))
    height = 0.8 / len(RUN)
    for i, name in enumerate(RUN):
        bars = ax.barh(y + (i - (len(RUN) - 1) / 2) * height, table[name], height=height * 0.95,
                       color=COLORS[name], label=name)
        for bar, value in zip(bars, table[name]):
            ax.text(bar.get_width() + 0.01, bar.get_y() + bar.get_height() / 2, f"{value:.2f}", va="center",
                    fontsize=7, color=TEXT)
    ax.set_yticks(y)
    ax.set_yticklabels([f"{s} = {v}  (n={n})" for s, v, n in table[["slice", "value", "n"]].values])
    ax.invert_yaxis()
    ax.set_xlim(0, 1.12)
    ax.set_xlabel("MRR (whole context)")
    ax.grid(axis="y", visible=False)
    ax.legend(loc="lower right", fontsize=8)
    ax.set_title("MRR by language and by question naming a code", loc="left", fontsize=10)
    fig.tight_layout()
    return fig, table



if not common_ids:
    log.info("Comparison skipped: no paired question")
else:
    display(styled_kpi(kpi_table()))
    display(curve_figure())
    misses = misses_table()
    if not misses.empty:
        display(misses.groupby("cause").size().rename("expected documents").reset_index())
        display(misses)
    ranks = ranks_table()
    slice_fig, _ = slice_figure(ranks)
    for fig in (rank_figure(ranks), slice_fig):
        display(fig)

error_rows = pdf[pdf["error"].notna()]
if not error_rows.empty:
    display(error_rows[["config", "question", "error"]])
# Figures still open at the end of a cell are drawn a second time by Databricks.
plt.close("all")
