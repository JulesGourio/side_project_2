# Databricks notebook source
# MAGIC %md
# MAGIC # Qualibot — Golden Retrieval Evaluation (simplified)
# MAGIC
# MAGIC **Scope:** only `{pipeline_read_env}_proj.qualibot.eval_golden_retrieval`.
# MAGIC Search uses the deployed Qualibot application code (translation bridge, history/date
# MAGIC preparation, rewrite and Vector Search), without generating answers or using a judge.
# MAGIC
# MAGIC **Relevance metrics on distinct document references in LLM-context order:**
# MAGIC - **Recall@K**: share of annotated `expected_refs` present among the first K documents.
# MAGIC - **MRR@K**: average reciprocal rank of the first annotated reference (0 if absent).
# MAGIC - **AllRefs@K**: share of questions for which *all* annotated references appear in top K.
# MAGIC
# MAGIC **K:** `1, 3, 5, 10, 20, 50` (widget `ks`); each metric is also logged against K as an MLflow step, which
# MAGIC draws the Recall@K curve in the run.
# MAGIC
# MAGIC **Technical metrics:** `search_p95_s`, `success_fraction`, `n_questions`,
# MAGIC `n_docs`, `n_passages`, `chars`. The last three are logged as **averages per successful question**
# MAGIC in MLflow and kept as exact counts in per-question results.
# MAGIC
# MAGIC Questions without `expected_refs` are skipped. Golden questions where an `unknown_ref`
# MAGIC is also an `expected_ref` are retrieved and saved, but excluded from relevance scores.
# MAGIC Only questions successfully evaluated by **every selected configuration** contribute to
# MAGIC Recall/MRR/AllRefs, so comparisons remain paired. `success_fraction` and the technical
# MAGIC averages are computed per configuration independently.
# MAGIC
# MAGIC **Important:** K refers to **distinct documents actually sent to the LLM**, not the
# MAGIC Vector Search raw top K. If the application returns fewer than 50 documents, the
# MAGIC `@50` metrics cannot improve beyond that available context. The notebook warns about it
# MAGIC without silently modifying the production search parameters.
# MAGIC
# MAGIC The retrieval runs on a **single persistent asyncio event loop**, even with concurrent
# MAGIC questions, avoiding `Event loop is closed` and `bound to a different event loop`.
# MAGIC
# MAGIC **Output:** `{pipeline_write_env}_proj.qualibot.eval_retrieval_runs_golden`, one MLflow run per configuration and
# MAGIC one comparison run of the first two configurations (Recall@K curve, MRR by language and by "question names a
# MAGIC code", questions that improve or regress). This separate output table leaves previous V3 tables intact.

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
# MAGIC %pip install -q -r $APP/requirements.txt fastapi pyyaml "databricks-sdk>=0.102" "mlflow[databricks]>=3.4" tqdm

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
# MAGIC | `ks` | `1,3,5,10,20,50` | K values of Recall@K, MRR@K, AllRefs@K (distinct documents sent to the LLM) |
# MAGIC | `max_parallel` | `1` | questions searched at once (each sends 2 to 5 Vector Search queries) |
# MAGIC | `mlflow_experiment` | empty | MLflow experiment; empty = `/Shared/qualibot/{write_env}_retrieval_eval_golden` |
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
# MAGIC
# MAGIC Examples:
# MAGIC - code normalization benchmark: `chat,baseline=dev_proj.qualibot.chunks_baseline_index|codenorm=off`
# MAGIC - the same, deeper: `chat|topk=50,baseline=dev_proj.qualibot.chunks_baseline_index|codenorm=off|topk=50`
# MAGIC - query side only, without the baseline index: `chat,query_only=dev_proj.qualibot.chunks_index|codenorm=off`
# MAGIC - search sizes: `chat,wide|cap=100|rerank=60`

# COMMAND ----------
for name, default in [
    ("pipeline_write_env", "dev"),
    ("pipeline_read_env", "dev"),
    ("app_code_env", "dev"),
    ("app_code_path", ""),
    ("golden_table_name", "eval_golden_retrieval"),
    ("results_table_name", "eval_retrieval_runs_golden"),
    ("indexes", "chat"),
    ("ks", "1,3,5,10,20,50"),
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
MAX_PARALLEL = max(1, int(dbutils.widgets.get("max_parallel")))
RERUN = dbutils.widgets.get("rerun_existing") == "true"
DEBUG = dbutils.widgets.get("debug") == "true"
EXPERIMENT = (dbutils.widgets.get("mlflow_experiment").strip()
              or f"/Shared/qualibot/{WRITE_ENV}_retrieval_eval_golden")
EXECUTION_ID = uuid.uuid4().hex
log.info("Golden only | table=%s | results=%s | ks=%s | execution=%s",
         GOLDEN_TABLE, RESULTS_TABLE, KS, EXECUTION_ID)

# COMMAND ----------
# MAGIC %md
# MAGIC ## Load the real Qualibot engine and its app configuration
# MAGIC Variables in `app.yaml` are loaded first, then `target_config.env`.
# MAGIC The index and search parameters are overridden only **between configurations**.

# COMMAND ----------
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

SETTINGS = {}
for label, cfg in CONFIGS.items():
    apply_config(cfg)
    SETTINGS[label] = json.loads(json.dumps(engine.settings(), default=str))
    cfg["signature"] = fingerprint({
        "index": cfg["index"], "overrides": cfg["overrides"],
        "settings": SETTINGS[label], "app_code": APP_FINGERPRINT,
        "bridge": TRANSLATE_BRIDGE_ENABLED, "runner": RUNNER_VERSION,
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
def score_at_k(expected: list[str], retrieved: list[str], k: int) -> dict[str, float]:
    top = refs(retrieved)[:k]
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
    for k in KS:
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
for k in KS:
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
    for k in KS:
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
# MAGIC ## MLflow: 3 relevance metrics x K + 6 technical metrics
# MAGIC Each configuration gets one MLflow run. The same case-level ranking, latency and error
# MAGIC information is also saved as an artifact (`per_question.json`). No additional KPI families,
# MAGIC category/locale breakdowns or duplicate monitoring namespace.

# COMMAND ----------
def log_number(key: str, value) -> None:
    if value is None or pd.isna(value):
        return
    number = float(value)
    if math.isfinite(number):
        mlflow.log_metric(key, number)


client.workspace.mkdirs(EXPERIMENT.rsplit("/", 1)[0])
mlflow.set_experiment(EXPERIMENT)
run_label = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M")
metric_columns = [f"{metric}_at_{k}" for k in KS
                  for metric in ("recall", "mrr", "all_refs")]
technical_columns = ["search_p95_s", "success_fraction", "n_questions", "n_docs", "n_passages", "chars"]

for name in RUN:
    row = summary[summary["config"] == name].iloc[0]
    subset = pdf[pdf["config"] == name].copy()
    export = subset[["case_id", "question", "expected", "unknown_overlap", "score_eligible",
                     "retrieved", "n_docs", "n_passages", "chars", "latency_s", "error"]
                    + metric_columns].copy()
    for col in ["expected", "unknown_overlap", "retrieved"]:
        export[col] = export[col].map(lambda values: ", ".join(values or []))

    with mlflow.start_run(run_name=f"golden-retrieval-{name}-{run_label}") as active_run:
        mlflow.set_tags({
            "eval": "retrieval", "source": "golden", "execution_id": EXECUTION_ID,
            "runner": RUNNER_VERSION, "config_signature": CONFIGS[name]["signature"],
        })
        mlflow.log_params({
            "config": name, "index": CONFIGS[name]["index"],
            "spec": CONFIGS[name]["spec"], "ks": ",".join(map(str, KS)),
            "golden_table": GOLDEN_TABLE, "app_code_path": APP,
            "rerun_existing": str(RERUN),
        })
        mlflow.log_dict(SETTINGS[name], "engine_settings.json")
        for metric in metric_columns + technical_columns:
            log_number(f"golden/{metric}", row[metric])
        for k in KS:
            for metric in ("recall", "mrr", "all_refs"):
                value = row[f"{metric}_at_{k}"]
                if value is not None and math.isfinite(float(value)):
                    mlflow.log_metric(f"golden/{metric}_curve", float(value), step=k)
        mlflow.log_table(export, artifact_file="per_question.json")
    log.info("MLflow run=%s | config=%s | scored_questions=%d",
             active_run.info.run_id, name, row["n_questions"])

# COMMAND ----------
# MAGIC %md
# MAGIC ## Summary and misses

# COMMAND ----------
display(summary.sort_values("config"))

misses = paired[paired[f"recall_at_{KS[0]}"] < 1].copy()
if not misses.empty:
    k = KS[0]
    misses["missing_expected"] = misses.apply(
        lambda r: [ref for ref in r["expected"] if ref not in set(refs(r["retrieved"])[:k])], axis=1
    )
    display(misses[["config", "question", "expected", "missing_expected", f"recall_at_{k}"]]
            .sort_values(["config", f"recall_at_{k}"]))

error_rows = pdf[pdf["error"].notna()]
if not error_rows.empty:
    display(error_rows[["config", "question", "error"]])

log.info("Golden retrieval completed: execution=%s | fresh_rows=%d | paired=%d | skipped=%d",
         EXECUTION_ID, len(ALL_ROWS), len(common_ids), len(SKIPPED))

# COMMAND ----------
# MAGIC %md
# MAGIC ## Comparison of the first two configurations
# MAGIC The first configuration of `indexes` is the reference, the second the candidate (for the benchmark: `chat` against
# MAGIC `baseline`). Same paired questions as above. A question **improves** when the first expected document comes
# MAGIC earlier in the candidate, **regresses** when it comes later; a miss ranks after every document.
# MAGIC Logged as one more MLflow run: Recall@K curve, MRR by language and by "question names a code", the questions
# MAGIC that moved.

# COMMAND ----------
import matplotlib.pyplot as plt
import numpy as np

COLOR = {"ref": "#2a78d6", "cand": "#eb6834", "improved": "#0ca30c", "regressed": "#d03b3b", "same": "#8c8b87",
         "grid": "#e1e0d9", "text": "#0b0b0b", "muted": "#52514e"}
plt.rcParams.update({"axes.spines.top": False, "axes.spines.right": False, "axes.grid": True,
                     "grid.color": COLOR["grid"], "grid.linewidth": 0.8, "axes.edgecolor": COLOR["muted"],
                     "xtick.color": COLOR["muted"], "ytick.color": COLOR["muted"], "lines.linewidth": 2,
                     "font.size": 10, "legend.frameon": False})
K_CURVE = list(range(1, max(KS) + 1))


def first_rank(expected: list[str], retrieved: list[str]) -> int | None:
    """Rank (1-based) of the first expected document among the retrieved ones.

    :param expected: canonical expected references
    :param retrieved: canonical retrieved references, in context order
    :return: the rank, None when no expected document was retrieved
    """
    relevant = set(refs(expected))
    return next((i for i, ref in enumerate(refs(retrieved), 1) if ref in relevant), None)


def curve_figure(frames: dict) -> plt.Figure:
    """Recall@K and AllRefs@K against K for each configuration (two panels, one y scale each).

    :param frames: configuration label -> its paired rows
    :return: matplotlib figure
    """
    fig, axes = plt.subplots(1, 2, figsize=(12, 4.2), sharex=True)
    for ax, metric, title in [(axes[0], "recall", "Recall@K: share of expected documents in the first K"),
                              (axes[1], "all_refs", "AllRefs@K: every expected document in the first K")]:
        for label, color in zip(frames, (COLOR["ref"], COLOR["cand"])):
            values = [np.mean([score_at_k(e, r, k)[f"{metric}_at_{k}"]
                               for e, r in zip(frames[label]["expected"], frames[label]["retrieved"])])
                      for k in K_CURVE]
            ax.plot(K_CURVE, values, color=color, label=label, marker="o", markersize=3,
                    markevery=[k - 1 for k in KS])
            for k in (10, max(KS)):
                if k <= max(KS):
                    ax.annotate(f"{values[k - 1]:.0%}", (k, values[k - 1]),
                                xytext=(5, 6 if color == COLOR["ref"] else -8), textcoords="offset points",
                                va="center", fontsize=8, color=COLOR["text"])
        ax.set_xscale("log")
        ax.set_xticks(KS)
        ax.set_xticklabels([str(k) for k in KS])
        ax.minorticks_off()
        ax.set_ylim(0, 1.02)
        ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda y, _: f"{y:.0%}"))
        ax.set_xlabel("K (distinct documents sent to the LLM, log scale)")
        ax.set_title(title, loc="left", fontsize=10)
    fig.legend(*axes[0].get_legend_handles_labels(), loc="upper right", ncol=2)
    fig.suptitle(f"{len(next(iter(frames.values())))} paired questions", x=0.01, ha="left", fontsize=11)
    fig.tight_layout(rect=(0, 0, 1, 0.93))
    return fig


def slice_figure(table: pd.DataFrame, labels: list[str]) -> plt.Figure:
    """MRR@max(K) per slice value, the two configurations side by side, n in the label.

    :param table: rows ``slice``, ``value``, ``n`` and one MRR column per configuration
    :param labels: the two configuration labels
    :return: matplotlib figure
    """
    fig, ax = plt.subplots(figsize=(8, 0.42 * len(table) + 1.2))
    y = np.arange(len(table))
    for i, (label, color) in enumerate(zip(labels, (COLOR["ref"], COLOR["cand"]))):
        bars = ax.barh(y + (i - 0.5) * 0.38, table[label], height=0.36, color=color, label=label,
                       edgecolor="white", linewidth=1)
        for bar, value in zip(bars, table[label]):
            ax.text(bar.get_width() + 0.01, bar.get_y() + bar.get_height() / 2, f"{value:.2f}", va="center",
                    fontsize=8, color=COLOR["text"])
    ax.set_yticks(y)
    ax.set_yticklabels([f"{s} = {v}  (n={n})" for s, v, n in table[["slice", "value", "n"]].values])
    ax.invert_yaxis()
    ax.set_xlim(0, 1.1)
    ax.set_xlabel(f"MRR@{max(KS)}")
    ax.grid(axis="y", visible=False)
    ax.legend(loc="lower right")
    ax.set_title("MRR by language and by question naming a code", loc="left", fontsize=10)
    fig.tight_layout()
    return fig


def moved_figure(moved: pd.DataFrame, labels: list[str], limit: int = 40) -> plt.Figure | None:
    """Reciprocal rank of each question that moved, reference to candidate (dumbbell), worst regression first.

    :param moved: comparison rows whose change is not ``same``
    :param labels: the two configuration labels
    :param limit: most questions drawn (the largest changes)
    :return: matplotlib figure, None when no question moved
    """
    if moved.empty:
        return None
    moved = moved.reindex(moved["delta_rr"].abs().sort_values(ascending=False).index)[:limit]
    moved = moved.sort_values("delta_rr", ascending=False)
    fig, ax = plt.subplots(figsize=(10, 0.32 * len(moved) + 1.3))
    y = np.arange(len(moved))
    for i, (_, r) in enumerate(moved.iterrows()):
        ax.plot([r["ref_rr"], r["cand_rr"]], [i, i], color=COLOR[r["change"]], zorder=1)
    for column, label, color in (("ref_rr", labels[0], COLOR["ref"]), ("cand_rr", labels[1], COLOR["cand"])):
        ax.scatter(moved[column], y, s=40, color=color, label=label, zorder=2, edgecolor="white", linewidth=1)
    ax.set_yticks(y)
    ax.set_yticklabels([(q[:70] + "…") if len(q) > 70 else q for q in moved["question"]], fontsize=8)
    ax.set_xlim(-0.03, 1.03)
    ax.set_xlabel("reciprocal rank of the first expected document (1 = first, 0 = missed)")
    ax.grid(axis="y", visible=False)
    ax.legend(loc="lower right", fontsize=8)
    ax.set_title(f"Questions that moved: green = {labels[1]} better, red = {labels[1]} worse", loc="left",
                 fontsize=10)
    fig.tight_layout()
    return fig


if len(RUN) < 2 or not common_ids:
    log.info("Comparison skipped: it needs two configurations with paired questions")
else:
    REF, CAND = RUN[0], RUN[1]
    golden_by_id = {str(r["question_id"]): r.asDict() for r in golden_rows}
    frames = {label: paired[paired["config"] == label].sort_values("case_id").reset_index(drop=True)
              for label in (REF, CAND)}
    compare = frames[REF][["case_id", "question", "expected"]].copy()
    compare["lang"] = compare["case_id"].map(lambda c: str(golden_by_id[c].get("lang") or "unknown").lower())
    compare["has_code"] = compare["question"].map(lambda q: bool(find_codes(q, accept_lowercase=True)))
    for prefix, label in (("ref", REF), ("cand", CAND)):
        f = frames[label]
        compare[f"{prefix}_rank"] = [first_rank(e, r) for e, r in zip(f["expected"], f["retrieved"])]
        compare[f"{prefix}_rr"] = compare[f"{prefix}_rank"].map(lambda r: 1 / r if r else 0.0)
        compare[f"{prefix}_recall_at_10"] = [score_at_k(e, r, 10)["recall_at_10"]
                                             for e, r in zip(f["expected"], f["retrieved"])]
        compare[f"{prefix}_top10"] = f["retrieved"].map(lambda r: refs(r)[:10])
    never = max(KS) + 10**6
    ref_rank, cand_rank = compare["ref_rank"].fillna(never), compare["cand_rank"].fillna(never)
    compare["change"] = np.where(cand_rank < ref_rank, "improved", np.where(cand_rank > ref_rank, "regressed", "same"))
    compare["delta_rr"] = compare["cand_rr"] - compare["ref_rr"]

    slices = []
    for column in ("lang", "has_code"):
        for value, group in compare.groupby(column):
            slices.append({"slice": column, "value": str(value), "n": len(group),
                           REF: group["ref_rr"].mean(), CAND: group["cand_rr"].mean()})
    slices = pd.DataFrame(slices)
    moved = compare[compare["change"] != "same"].sort_values("delta_rr")
    log.info("%s vs %s: %d improved, %d regressed, %d same", CAND, REF, (compare["change"] == "improved").sum(),
             (compare["change"] == "regressed").sum(), (compare["change"] == "same").sum())
    display(slices.round(3))
    display(moved[["change", "question", "lang", "has_code", "expected", "ref_rank", "cand_rank",
                   "ref_top10", "cand_top10"]])

    figures = {"recall_curve.png": curve_figure(frames), "mrr_by_slice.png": slice_figure(slices, [REF, CAND]),
               "moved_questions.png": moved_figure(moved, [REF, CAND])}
    figures = {name: fig for name, fig in figures.items() if fig is not None}
    for fig in figures.values():
        display(fig)

    export = compare.copy()
    for column in ("expected", "ref_top10", "cand_top10"):
        export[column] = export[column].map(lambda values: ", ".join(values or []))
    with mlflow.start_run(run_name=f"golden-compare-{CAND}-vs-{REF}-{run_label}") as compare_run:
        mlflow.set_tags({"eval": "retrieval", "source": "golden", "execution_id": EXECUTION_ID,
                         "runner": RUNNER_VERSION, "role": "comparison", "reference": REF, "candidate": CAND})
        mlflow.log_params({"reference": REF, "candidate": CAND, "reference_spec": CONFIGS[REF]["spec"],
                           "candidate_spec": CONFIGS[CAND]["spec"], "ks": ",".join(map(str, KS)),
                           "golden_table": GOLDEN_TABLE})
        for k in KS:
            for metric in ("recall", "mrr", "all_refs"):
                column = f"{metric}_at_{k}"
                delta = frames[CAND][column].mean() - frames[REF][column].mean()
                log_number(f"compare/delta_{column}", delta)
                mlflow.log_metric(f"compare/delta_{metric}_curve", float(delta), step=k)
        for change in ("improved", "regressed", "same"):
            mlflow.log_metric(f"compare/{change}", int((compare["change"] == change).sum()))
        for _, row in slices.iterrows():
            key = re.sub(r"[^A-Za-z0-9_\-./]", "_", f"compare/slice.{row['slice']}.{row['value']}")
            log_number(f"{key}.delta_mrr", row[CAND] - row[REF])
        mlflow.log_table(export, artifact_file="comparison.json")
        mlflow.log_table(export[export["change"] == "regressed"], artifact_file="regressed.json")
        mlflow.log_table(export[export["change"] == "improved"], artifact_file="improved.json")
        mlflow.log_table(slices, artifact_file="slices.json")
        for name, fig in figures.items():
            mlflow.log_figure(fig, f"figures/{name}")
    log.info("MLflow comparison run=%s", compare_run.info.run_id)
