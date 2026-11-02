# Databricks notebook source
# MAGIC %md
# MAGIC # Qualibot — Golden Retrieval Evaluation
# MAGIC
# MAGIC Runs every golden question of `{pipeline_read_env}_proj.qualibot.eval_golden_retrieval` through the search of the
# MAGIC deployed app (`retrieve_for_turn`: translation or rewrite, Vector Search, REF and exact-citation lookups), once per
# MAGIC configuration of the `preset` widget (or of `indexes` with `preset = perso`). No answer is generated. The golden
# MAGIC table is written by `create_golden_retrieval_set.py`; the job `Z_4_Qualibot_Retrieval_Eval` runs both.
# MAGIC
# MAGIC **Scores** (K counts chunks: the passages sent to the LLM, in the order of the search; the target stays the
# MAGIC document, paired questions only):
# MAGIC - **Recall@K**: share of the expected documents with at least one chunk among the first K chunks.
# MAGIC - **MRR@K**: 1 / rank of the first chunk of an expected document (0 when none is among the first K).
# MAGIC - **AllRefs@K**: share of questions with every expected document among the first K chunks.
# MAGIC - **@all**: the same over every chunk the configuration sends. A K above the chunks a question gets scores its
# MAGIC   whole context.
# MAGIC
# MAGIC **Outputs**
# MAGIC - Delta table `{pipeline_write_env}_proj.qualibot.<results_table_name>`: one row per configuration and question.
# MAGIC - MLflow experiment `mlflow_experiment`: one run per configuration (one row per question, its documents and
# MAGIC   scores) and, in the experiment description, the ranking, the effect of each setting and the documents every
# MAGIC   configuration misses.
# MAGIC - At the end of the notebook: corpus and index check (expected documents absent from the chunks), key indicators
# MAGIC   per configuration, ranking, rank per question, quality against cost, recall curves and every expected document
# MAGIC   missed in the top 10 with its cause.

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
# MAGIC | `preset` | `deploye` | configurations to measure: `deploye` (the app as deployed), `tailles` (`deploye`, `top16`, `top32`, `top48` without raw search), `complet` (`deploye`, every top-k with and without raw search and RRF, `top16-bm25-30/50/70`), `perso` (the `indexes` widget) |
# MAGIC | `indexes` | `chat` | configurations to measure when `preset = perso`, see below |
# MAGIC | `reference` | empty | configuration every other one is compared with; empty = the first of `indexes` |
# MAGIC | `ks` | `1,3,5,10,20` | K values of Recall@K, MRR@K, AllRefs@K (chunks sent to the LLM); a K above the chunks sent scores the whole context; the scoreboard adds the whole context (`all`) |
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
# MAGIC | `order` | `score` or `rank` | order of the merged passages before the cap: best reranker score or best rank (`CHAT_PASSAGE_ORDER`) |
# MAGIC | `translate` | `on`, `off` | opening message only translated (two queries) instead of rewritten (`CHAT_TRANSLATE_ONLY`) |
# MAGIC | `rewrite` | endpoint name | model of the rewrite and of the translation (`CHAT_REWRITE_ENDPOINT`) |
# MAGIC | `effort` | `off`, `minimal`, `low`, `medium`, `high` | reasoning effort of that model (`CHAT_REWRITE_REASONING_EFFORT`) |
# MAGIC | `codenorm` | `on`, `off` | `off`: no code normalization at all in the app (query, exact-citation lookup, language variant choice) |
# MAGIC | `merge` | `best`, `rrf` | merge of the result lists of a turn (REF and citation lookups unchanged). `best` = the app: one row per passage at its best rank, lists interleaved rank by rank. `rrf` = each passage scores the sum of `1 / (60 + rank)` over the lists that found it, so a passage found by several queries comes first |
# MAGIC | `split` | BM25 weight from 0 to 1 | each reranked HYBRID query becomes two reranked queries, `ANN` (embeddings) and `FULL_TEXT` (BM25), of k passages each, merged by `weight / (60 + BM25 rank) + (1 - weight) / (60 + embeddings rank)`, k kept |
# MAGIC
# MAGIC In the presets, `topN` = N reranked passages per query of an opening message (12, 21 or 32 for the other turns),
# MAGIC `-brute` = the raw search on, `-rrf` = the RRF merge, `bm25-30` = ANN and BM25 split with a BM25 weight of 0.3.
# MAGIC
# MAGIC Examples with `preset = perso`:
# MAGIC - code normalization against the index without it (DEV only, the only workspace with that index):
# MAGIC   `deploye,baseline=dev_proj.qualibot.chunks_baseline_index|codenorm=off|pairrerank=16|rerank=12|pairraw=16|raw=12`
# MAGIC - query side only: `deploye,query_only|codenorm=off`
# MAGIC - another index: `deploye,uat=uat_proj.qualibot.chunks_index`
# MAGIC - search sizes: `deploye,wide|cap=100|rerank=60`

# COMMAND ----------
for name, default in [
    ("pipeline_write_env", "dev"),
    ("pipeline_read_env", "dev"),
    ("app_code_env", "dev"),
    ("app_code_path", ""),
    ("golden_table_name", "eval_golden_retrieval"),
    ("results_table_name", "eval_retrieval_runs_golden"),
    ("indexes", "chat"),
    ("reference", ""),
    ("ks", "1,3,5,10,20,50"),
    ("max_parallel", "1"),
    ("mlflow_experiment", ""),
]:
    dbutils.widgets.text(name, default)
for name, default, choices in [
    ("preset", "deploye", ["deploye", "tailles", "complet", "perso"]),
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
# Reranked passages per query of an opening message -> per query of the other turns.
PRESET_TOP_K = {16: 12, 32: 21, 48: 32}


def preset_configs(preset: str) -> str:
    """Configurations of a preset, in the syntax of the ``indexes`` widget.

    :param preset: ``deploye``, ``tailles`` or ``complet``
    :return: comma-separated configurations, the app as deployed (``deploye``) first
    """
    def sizes(k: int, raw: bool) -> str:
        """Search sizes of one configuration.

        :param k: reranked passages per query of an opening message
        :param raw: True to add the raw search with as many passages
        :return: the options ``pairrerank``, ``rerank``, ``pairraw`` and ``raw``
        """
        other = PRESET_TOP_K[k]
        return f"pairrerank={k}|rerank={other}|pairraw={k if raw else 0}|raw={other if raw else 0}"

    configs = ["deploye"]
    if preset == "tailles":
        configs += [f"top{k}|{sizes(k, False)}" for k in PRESET_TOP_K]
    elif preset == "complet":
        configs += [f"top{k}{'-brute' if raw else ''}{'-rrf' if rrf else ''}|{sizes(k, raw)}{'|merge=rrf' if rrf else ''}"
                    for k in PRESET_TOP_K for raw in (True, False) for rrf in (False, True)]
        configs += [f"top16-bm25-{round(w * 100)}|{sizes(16, False)}|split={w}" for w in (0.3, 0.5, 0.7)]
    return ",".join(configs)


PRESET = dbutils.widgets.get("preset")
INDEXES = dbutils.widgets.get("indexes").strip() if PRESET == "perso" else preset_configs(PRESET)
# K = 50 is always measured: the presets send up to about 50 documents.
KS = sorted({int(x.strip()) for x in dbutils.widgets.get("ks").split(",") if x.strip()} | {50})
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
log.info("Golden only | table=%s | results=%s | preset=%s | ks=%s | execution=%s",
         GOLDEN_TABLE, RESULTS_TABLE, PRESET, KS, EXECUTION_ID)

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
    "order": "CHAT_PASSAGE_ORDER",
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
    """Configurations of the ``indexes`` syntax.

    :param raw: comma-separated ``label[=catalog.schema.index][|option=value]...``
    :return: label -> ``index``, ``overrides`` (environment variables) and ``spec`` (the text of the configuration)
    """
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
    """Set the environment variables of a configuration over the app's, before a search.

    :param cfg: one value of ``parse_configs``
    """
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
    """SHA-256 of a JSON-serialisable value, keys sorted.

    :param value: the value
    :return: hexadecimal digest
    """
    data = json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def app_fingerprint(root: str) -> str:
    """SHA-256 of the app files that change the search.

    :param root: folder of the app code
    :return: hexadecimal digest
    """
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
REFERENCE = dbutils.widgets.get("reference").strip() or RUN[0]
if REFERENCE not in CONFIGS:
    raise ValueError(f"reference {REFERENCE!r} is not one of the configurations: {', '.join(RUN)}")
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
    """Canonicalize and deduplicate references, preserving document order.

    :param values: list of REF, or a JSON list as text, or one REF
    :return: canonical REF without duplicates
    """
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
    """A failed search, with the step it failed at."""

    def __init__(self, stage: str, original: Exception) -> None:
        """Keep the step and the original error.

        :param stage: step of ``search`` that failed
        :param original: the error raised there
        """
        self.stage, self.original = stage, original
        super().__init__(f"[{stage}] {type(original).__name__}: {original}")


def error_description(exc: Exception) -> str:
    """Short text of an error for the results table.

    :param exc: the error
    :return: ``[stage] Type: message``, message cut at 500 characters
    """
    if isinstance(exc, RetrievalStageError):
        return f"[{exc.stage}] {type(exc.original).__name__}: {str(exc.original)[:500]}"
    return f"[runner] {type(exc).__name__}: {str(exc)[:500]}"


async def search(case: dict) -> dict:
    """Search one golden question as the app does, without an answer.

    :param case: golden question with ``question`` and ``division``
    :return: ``retrieved`` (REF in context order), ``retrieved_chunks`` (the REF of each passage sent, in search
        order), ``n_passages`` and ``chars`` of the passages sent
    """
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
            "retrieved_chunks": [row["REF"] for row in found["rows"]],
            "n_passages": len(found["rows"]),
            "chars": sum(len(row.get("chunk_text") or "") for row in found["rows"]),
        }
    except Exception as exc:
        raise RetrievalStageError(stage, exc) from exc


async def run_case(case: dict, semaphore: asyncio.Semaphore) -> dict:
    """Search one question, retried twice after 5 and 20 seconds.

    :param case: golden question
    :param semaphore: limit on simultaneous searches
    :return: output of ``search`` with ``latency_s``, or ``error`` and ``latency_s`` after the last failure
    """
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

    def run_forever() -> None:
        """Run the event loop in its thread until the Python session ends."""
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


async def make_semaphore(limit: int) -> asyncio.Semaphore:
    """Semaphore created inside the persistent loop.

    :param limit: simultaneous searches allowed
    :return: the semaphore
    """
    return asyncio.Semaphore(limit)

# COMMAND ----------
# MAGIC %md
# MAGIC ## Resume and scoring
# MAGIC `rerun_existing=false` reuses successful rows only if the **question, expected refs,
# MAGIC configuration and application code** have not changed. The 4 selected K values are
# MAGIC recomputed from the stored document ranking, so they do not require a new search.

# COMMAND ----------
def chunk_refs(values) -> list[str]:
    """Canonical REF of each chunk, duplicates and order kept.

    :param values: REF of the chunks sent, in search order
    :return: one canonical REF per chunk
    """
    return [canon_ref(str(value)) for value in values or [] if value is not None and str(value).strip()]


def chunks_of(row) -> list[str]:
    """REF of each chunk sent for a result row; a row saved before chunks were recorded gives its documents.

    :param row: result row (dict or pandas row) with ``retrieved`` and maybe ``retrieved_chunks``
    :return: the REF list K is counted on
    """
    chunks = row.get("retrieved_chunks")
    return list(chunks) if chunks is not None and len(chunks) else list(row.get("retrieved") or [])


def score_at_k(expected: list[str], chunks: list[str], k: int | str) -> dict[str, float]:
    """Recall, MRR and AllRefs of one question at K chunks.

    :param expected: expected REF
    :param chunks: REF of each chunk sent to the model, in search order (one entry per chunk)
    :param k: number of chunks looked at, or ``all`` for every chunk sent
    :return: ``recall_at_<k>``, ``mrr_at_<k>``, ``all_refs_at_<k>``
    """
    top = chunk_refs(chunks) if k == "all" else chunk_refs(chunks)[:k]
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


def scores(expected: list[str], chunks: list[str]) -> dict[str, float]:
    """Every score of one question, for every K and the whole context.

    :param expected: expected REF
    :param chunks: REF of each chunk sent to the model, in search order
    :return: the outputs of ``score_at_k`` merged
    """
    result = {}
    for k in KS_ALL:
        result.update(score_at_k(expected, chunks, k))
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
    StructField("retrieved_chunks", ArrayType(StringType()), True),
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
                chunks = [str(ref) for ref in result.get("retrieved_chunks") or []]
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
                    "retrieved_chunks": chunks,
                    "metrics": scores(case["expected"], chunks) if not error else {},
                    "n_docs": len(retrieved),
                    "n_passages": result.get("n_passages"),
                    "chars": result.get("chars"),
                    "latency_s": result.get("latency_s"),
                    "error": error,
                })
            spark.createDataFrame(batch, schema=SCHEMA).write.mode("append").format("delta").option("mergeSchema", "true").saveAsTable(RESULTS_TABLE)
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
    scores(r["expected"], chunks_of(r)) if not r.get("error") else {}
    for r in EVAL_ROWS
]
pdf["chunks"] = [chunks_of(r) for r in EVAL_ROWS]
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
if not success.empty and int(success["n_passages"].max()) < max(KS):
    log.warning("Only %d chunks at most in retrieved context; @%d will be capped by the app context",
                int(success["n_passages"].max()), max(KS))

# COMMAND ----------
# MAGIC %md
# MAGIC ## Results
# MAGIC 1. **Corpus and index**: an expected document absent from the chunks can never be found. It is a data gap, not a
# MAGIC    search failure, and it explains most gaps between two environments.
# MAGIC 2. **Key indicators**: one row per configuration, on the same questions.
# MAGIC 3. **Ranking**: Recall@1, @5, @10 and @all per configuration, best Recall@5 first, gap with the reference.
# MAGIC 4. **Rank per question**: rank of the first chunk of an expected document in each configuration, red when never sent.
# MAGIC 5. **Quality against cost**: Recall@5 against the context sent to the model.
# MAGIC 6. **Recall curves**: one panel per top-k, the reference dotted on each.
# MAGIC 7. **Every expected document missed in the top 10**, with its cause.
# COMMAND ----------
import matplotlib.pyplot as plt
import numpy as np
from matplotlib.colors import LinearSegmentedColormap

GRID, TEXT, MUTED = "#e1e0d9", "#0b0b0b", "#52514e"
plt.rcParams.update({"axes.spines.top": False, "axes.spines.right": False, "axes.grid": True,
                     "grid.color": GRID, "grid.linewidth": 0.8, "axes.edgecolor": MUTED,
                     "xtick.color": MUTED, "ytick.color": MUTED, "lines.linewidth": 2,
                     "font.size": 10, "legend.frameon": False})
K_CURVE = list(range(1, max(KS) + 1))
K_MAIN = 10 if 10 in KS else max(KS)
GREEN, RED, ORANGE, BLUE, PURPLE = "#0ca30c", "#d03b3b", "#eb6834", "#2a78d6", "#8a4fd0"


def reciprocal(rank) -> float:
    """Reciprocal rank, 0 for a missed document.

    :param rank: 1-based rank, None or NaN when missed
    :return: 1 / rank, or 0
    """
    return 0.0 if rank is None or pd.isna(rank) else 1 / rank


def first_rank(expected: list[str], chunks: list[str]) -> int | None:
    """Rank (1-based) of the first chunk of an expected document among the chunks sent.

    :param expected: canonical expected references
    :param chunks: REF of each chunk sent, in search order
    :return: the rank, None when no expected document was sent
    """
    relevant = set(refs(expected))
    return next((i for i, ref in enumerate(chunk_refs(chunks), 1) if ref in relevant), None)




def field(row, key: str) -> object:
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
# MAGIC ### Tableau, graphiques et documents ratés
# MAGIC | Colonne | Sens |
# MAGIC |---|---|
# MAGIC | Top-k | passages reclassés + bruts par requête, premier message / suite de conversation |
# MAGIC | ≥1 doc @K | questions avec au moins un document attendu dans les K premiers chunks |
# MAGIC | R@K | part des documents attendus ayant un chunk dans les K premiers chunks ; « indexés » ne compte que les documents présents dans les chunks |
# MAGIC | AllRefs@K | questions avec tous leurs documents attendus dans les K premiers chunks |
# MAGIC | MRR | 1 / rang du premier document attendu (1 = toujours premier) |
# MAGIC | Docs, Req. VS | documents envoyés au modèle, requêtes Vector Search par question |
# MAGIC
# MAGIC Lignes dans l'ordre du classement, référence surlignée, meilleure valeur en gras.
# COMMAND ----------
K5 = 5 if 5 in KS else min(KS)
NOISE = 1 / max(1, len(common_ids))  # one question
CHARS_PER_TOKEN = 3.5  # technical French, the constant of the parsing pipeline


def config_env(name: str) -> dict[str, str]:
    """Search variables of a configuration: the app's, then its overrides.

    :param name: configuration label
    :return: variable name -> value
    """
    return {**{k: v for k, v in BASE_SEARCH_ENV.items() if v is not None}, **CONFIGS[name]["overrides"]}


def vs_queries(name: str) -> int:
    """Vector Search queries sent for an opening question in French or English, REF lookups apart.

    :param name: configuration label
    :return: the reranked queries (two, four when ANN and BM25 are split) plus the raw ones
    """
    s, env = SETTINGS[name], config_env(name)
    raw_k = s["pair_raw_top_k"] if s["translate_only"] else s["raw_top_k"]
    return 2 * (2 if env.get("EVAL_SPLIT_BM25") else 1) + (len(s["raw_on"]) if raw_k else 0)


def settings(name: str) -> dict[str, str]:
    """Search settings of a configuration, as short MLflow parameters.

    :param name: configuration label
    :return: ``top_k`` (first message / other turns), ``brute``, ``fusion``, ``recherche``, ``codes``, ``index``,
        ``requetes_vs``
    """
    s, env = SETTINGS[name], config_env(name)
    return {
        "top_k": f"{s['pair_rerank_top_k']}/{s['rerank_top_k']}",
        "brute": "on" if max(s["pair_raw_top_k"], s["raw_top_k"]) else "off",
        "fusion": "rrf" if env.get("EVAL_MERGE") == "rrf" else "best",
        "recherche": f"ann+bm25 {env['EVAL_SPLIT_BM25']}" if env.get("EVAL_SPLIT_BM25") else "hybrid",
        "codes": env.get("EVAL_CODE_NORMALIZATION", "on"),
        "index": CONFIGS[name]["index"].rsplit(".", 1)[-1],
        "requetes_vs": str(vs_queries(name)),
    }


def describe(params: dict[str, str]) -> str:
    """The settings of a configuration in plain French.

    :param params: output of ``settings``
    :return: e.g. ``Top-k 16/12 · brute coupée · fusion meilleur rang · HYBRID · codes normalisés · 2 requêtes``
    """
    parts = [f"Top-k {params['top_k']}", "brute active" if params["brute"] == "on" else "brute coupée",
             "fusion RRF" if params["fusion"] == "rrf" else "fusion meilleur rang",
             "HYBRID" if params["recherche"] == "hybrid"
             else f"ANN + BM25 séparés (poids BM25 {params['recherche'][9:]})",
             "codes normalisés" if params["codes"] == "on" else "sans normalisation des codes",
             f"{params['requetes_vs']} requêtes Vector Search par question"]
    if params["index"] != MAIN_INDEX.rsplit(".", 1)[-1]:
        parts.append(f"index {params['index']}")
    return " · ".join(parts)


def tokens_k(row) -> float:
    """Approximate thousands of tokens of passages a configuration sends to the LLM.

    :param row: the configuration's row of ``summary``
    :return: thousands of tokens
    """
    return row["chars"] / CHARS_PER_TOKEN / 1000


def ranking(rows: dict[str, dict]) -> list[str]:
    """Configurations, best Recall@5 first, then best MRR.

    :param rows: label -> row of ``summary``
    :return: labels
    """
    return sorted(rows, key=lambda n: (-rows[n][f"recall_at_{K5}"], -rows[n]["mrr_at_all"]))


ROWS = {name: summary[summary["config"] == name].iloc[0].to_dict() for name in RUN}
PARAMS = {name: settings(name) for name in RUN}
ORDER = ranking(ROWS)
INDEX_COUNTS = pd.Series([CONFIGS[n]["index"] for n in RUN]).value_counts()
MAIN_INDEX = CONFIGS[REFERENCE]["index"] if INDEX_COUNTS.get(CONFIGS[REFERENCE]["index"]) == INDEX_COUNTS.max() \
    else INDEX_COUNTS.index[0]
# Configurations that differ only by top-k, raw search and fusion: the grid and the curve panels.
MAIN = [n for n in RUN if PARAMS[n]["recherche"] == "hybrid" and PARAMS[n]["codes"] == "on"
        and CONFIGS[n]["index"] == MAIN_INDEX]


def search_sizes(name: str) -> str:
    """Passages per query of a configuration, as the app runs it.

    :param name: configuration label
    :return: ``reranked+raw`` of an opening message in French or English, then of the other queries
    """
    s = SETTINGS[name]
    return f"{s['pair_rerank_top_k']}+{s['pair_raw_top_k']} / {s['rerank_top_k']}+{s['raw_top_k']}"


def recall_in_corpus(rows: pd.DataFrame, k: int) -> float:
    """Recall@k counting only the expected documents present in the chunks.

    :param rows: paired rows of one configuration
    :param k: number of chunks looked at
    :return: mean recall, NaN when no question has an expected document in the chunks
    """
    values = []
    for expected, chunks in zip(rows["expected"], rows["chunks"]):
        present = [ref for ref in expected if ref in corpus_refs]
        if present:
            values.append(len(set(present) & set(chunk_refs(chunks)[:k])) / len(present))
    return float(np.mean(values)) if values else float("nan")


KPI_SHARES = [f"≥1 doc @{K5}", f"≥1 doc @{K_MAIN}", f"R@{K_MAIN}", f"R@{K_MAIN} indexés", f"AllRefs@{K_MAIN}", "R@50",
              "R@all"]
KPI_DELTA = f"Δ R@{K_MAIN}"


def kpi_table() -> pd.DataFrame:
    """Key indicators, one row per configuration, on the paired questions.

    :return: the table, configurations in ranking order
    """
    rows = []
    for name in ORDER:
        f = paired[paired["config"] == name]
        own = success[success["config"] == name]
        rows.append(dict(zip(["Config", "Top-k", "Questions", "Erreurs", *KPI_SHARES, "MRR", "Docs", "Chunks", "Req. VS"], [
            name, search_sizes(name), len(f), int(((pdf["config"] == name) & pdf["error"].notna()).sum()),
            (f[f"mrr_at_{K5}"] > 0).mean(), (f[f"mrr_at_{K_MAIN}"] > 0).mean(), f[f"recall_at_{K_MAIN}"].mean(),
            recall_in_corpus(f, K_MAIN), f[f"all_refs_at_{K_MAIN}"].mean(), f["recall_at_50"].mean(),
            f["recall_at_all"].mean(), f["mrr_at_all"].mean(), own["n_docs"].mean(), own["n_passages"].mean(),
            vs_queries(name)])))
    table = pd.DataFrame(rows)
    main = f"R@{K_MAIN}"
    table[KPI_DELTA] = table[main] - table.loc[table["Config"] == REFERENCE, main].iloc[0]
    return table


def styled_kpi(table: pd.DataFrame) -> "pd.io.formats.style.Styler":
    """Key indicators as compact percentages, the reference row highlighted, the best value of each score in bold.

    :param table: output of ``kpi_table``
    :return: pandas Styler
    """
    fmt = {**{c: "{:.0%}" for c in KPI_SHARES}, KPI_DELTA: lambda v: f"{100 * v:+.1f}",
           "MRR": "{:.2f}", "Docs": "{:.0f}", "Chunks": "{:.0f}"}

    def sign(value: float) -> str:
        """CSS color of a delta.

        :param value: the delta
        :return: CSS declaration
        """
        return "" if pd.isna(value) or abs(value) < NOISE - 1e-9 else f"color: {GREEN if value > 0 else RED}"

    def reference_row(row: pd.Series) -> list[str]:
        """Background of the reference row.

        :param row: one row of the table
        :return: one CSS declaration per cell
        """
        return ["background-color: #f1efe8" if row["Config"] == REFERENCE else ""] * len(row)

    style = (table.style.format(fmt, na_rep="–").hide(axis="index")
             .highlight_max(subset=KPI_SHARES + ["MRR"], props="font-weight: bold")
             .apply(reference_row, axis=1)
             .set_table_styles([
                 {"selector": "", "props": "border-collapse: collapse; font-size: 13px;"},
                 {"selector": "th", "props": "text-align: center; white-space: nowrap; padding: 6px 10px; "
                                             "border-bottom: 2px solid #52514e; font-weight: 600;"},
                 {"selector": "td", "props": "text-align: center; white-space: nowrap; padding: 5px 10px; "
                                             "border-bottom: 1px solid #e1e0d9;"},
                 {"selector": "td.col0, th.col_heading.col0", "props": "text-align: left;"},
             ]))
    return (style.map if hasattr(style, "map") else style.applymap)(sign, subset=[KPI_DELTA])


def doc_rank(ref: str, chunks: list[str]) -> int | None:
    """Rank (1-based) of the first chunk of one document among the chunks sent.

    :param ref: canonical reference
    :param chunks: REF of each chunk sent, in search order
    :return: the rank, None when not sent
    """
    ordered = chunk_refs(chunks)
    return ordered.index(ref) + 1 if ref in ordered else None


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
            ranks = {name: doc_rank(ref, by_key[(name, case["case_id"])]["chunks"]) for name in RUN}
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
    base = paired[paired["config"] == REFERENCE][["case_id", "question", "expected"]].copy()
    base["lang"] = base["case_id"].map(lambda c: GOLDEN_LANG.get(c, "unknown"))
    base["has_code"] = base["question"].map(lambda q: bool(find_codes(q, accept_lowercase=True)))
    for name in RUN:
        f = paired[paired["config"] == name].set_index("case_id")
        base[name] = [first_rank(f.at[c, "expected"], f.at[c, "chunks"]) for c in base["case_id"]]
    return base.reset_index(drop=True)


def percent_axis(axis) -> None:
    """Format an axis as percentages.

    :param axis: ``ax.xaxis`` or ``ax.yaxis``
    """
    axis.set_major_formatter(plt.FuncFormatter(lambda v, _: f"{v:.0%}"))


def ranking_figure() -> plt.Figure:
    """Recall@1, @5, @10 and @all of every configuration, best Recall@5 first, with the gap to the reference.

    :return: matplotlib figure
    """
    marks = [(f"recall_at_{k}", f"R@{k}", marker, color)
             for k, marker, color in [(min(KS), "o", PURPLE), (K5, "D", BLUE), (K_MAIN, "s", ORANGE)]]
    marks = list({m[0]: m for m in marks}.values()) + [("recall_at_all", "R@all", "|", MUTED)]
    ref = ROWS[REFERENCE]
    fig, ax = plt.subplots(figsize=(12, 0.42 * len(ORDER) + 1.8))
    for i, name in enumerate(ORDER):
        r = ROWS[name]
        ax.plot([r[f"recall_at_{min(KS)}"], r["recall_at_all"]], [i, i], color=GRID, lw=6, solid_capstyle="round",
                zorder=1)
        for key, label, marker, color in marks:
            ax.scatter(r[key], i, marker=marker, s=200 if marker == "|" else 60, color=color, linewidths=2, zorder=3,
                       label=label if i == 0 else None)
        delta = r[f"recall_at_{K5}"] - ref[f"recall_at_{K5}"]
        color = MUTED if name == REFERENCE or abs(delta) < NOISE - 1e-9 else GREEN if delta > 0 else RED
        text = "référence" if name == REFERENCE else f"{100 * delta:+.1f} pts"
        ax.text(1.01, i, text, va="center", fontsize=9, color=color, transform=ax.get_yaxis_transform())
        ax.text(1.12, i, f"{tokens_k(r):.0f}k tokens · {vs_queries(name)} requêtes", va="center", fontsize=8,
                color=MUTED, transform=ax.get_yaxis_transform())
    ax.set_yticks(range(len(ORDER)))
    ax.set_yticklabels([f"▶ {n}" if n == REFERENCE else n for n in ORDER])
    ax.invert_yaxis()
    low = min(ROWS[n][f"recall_at_{min(KS)}"] for n in ORDER)
    ax.set_xlim(max(0.0, np.floor(low * 10) / 10 - 0.05), 1.0)
    percent_axis(ax.xaxis)
    ax.grid(axis="y", visible=False)
    ax.legend(loc="lower left", ncol=len(marks), bbox_to_anchor=(0, 1.0), fontsize=9)
    ax.set_title(f"Classement, meilleur R@{K5} d'abord · écart de R@{K5} avec {REFERENCE} "
                 f"(vert / rouge = au moins une question d'écart)", loc="left", fontsize=11, pad=28)
    fig.tight_layout()
    fig.subplots_adjust(right=0.72)
    return fig


def rank_figure(ranks: pd.DataFrame) -> plt.Figure:
    """Rank of the first expected document per question and configuration, hardest questions first.

    :param ranks: output of ``ranks_table``
    :return: matplotlib figure
    """
    observed = ranks[ORDER].astype(float).max().max()
    missed = int(max(max(KS), 0 if pd.isna(observed) else observed)) + 1
    values = ranks[ORDER].astype(float).fillna(missed)
    order = values.mean(axis=1).sort_values(ascending=False).index
    values, labels = values.loc[order], ranks.loc[order, "question"]
    cmap = LinearSegmentedColormap.from_list("rank", [GREEN, "#f2c94c", ORANGE, RED])
    fig, ax = plt.subplots(figsize=(0.7 * len(ORDER) + 7, 0.24 * len(values) + 2.2))
    ax.imshow(np.log(values.to_numpy()), cmap=cmap, aspect="auto", vmin=0, vmax=np.log(missed))
    for i in range(values.shape[0]):
        for j in range(values.shape[1]):
            rank = values.iat[i, j]
            ax.text(j, i, "-" if rank >= missed else str(int(rank)), ha="center", va="center", fontsize=7,
                    color="white" if rank >= 5 else TEXT)
    ax.set_xticks(range(len(ORDER)))
    ax.set_xticklabels([f"▶ {n}" if n == REFERENCE else n for n in ORDER], rotation=45, ha="left", fontsize=8)
    ax.xaxis.tick_top()
    ax.set_yticks(range(len(values)))
    ax.set_yticklabels([(q[:80] + "…") if len(q) > 80 else q for q in labels], fontsize=7)
    ax.grid(False)
    ax.set_title("Rang du premier document attendu dans le contexte envoyé (- en rouge = jamais envoyé), "
                 "questions les plus dures en haut, configurations dans l'ordre du classement",
                 loc="left", fontsize=10, pad=90)
    fig.tight_layout()
    return fig


def config_style(name: str, i: int) -> tuple[str, str, str]:
    """Color, line style and marker of a configuration: raw search on or off, RRF fusion, other settings.

    :param name: configuration label
    :param i: position among the configurations of the same panel, for the other settings
    :return: color, line style, marker
    """
    p = PARAMS[name]
    if name in MAIN:
        return ORANGE if p["brute"] == "on" else BLUE, "--" if p["fusion"] == "rrf" else "-", \
            "^" if p["fusion"] == "rrf" else "o"
    if p["recherche"] != "hybrid":
        return [PURPLE, "#c9a3f0", "#4b2483", "#1aa5a5"][i % 4], "-", "s"
    return ["#9a998f", "#b8860b", "#1aa5a5"][i % 3], ":", "s"


def cost_figure() -> plt.Figure:
    """Recall@5 against the context sent to the model, one point per configuration.

    :return: matplotlib figure
    """
    from matplotlib.lines import Line2D

    fig, ax = plt.subplots(figsize=(11, 6.5))
    for i, name in enumerate(RUN):
        color, _, marker = config_style(name, i)
        r = ROWS[name]
        ax.scatter(tokens_k(r), r[f"recall_at_{K5}"], s=150, color=color, marker=marker, alpha=0.9,
                   edgecolor="white", zorder=3)
        ax.annotate(f"{name} (réf.)" if name == REFERENCE else name, (tokens_k(r), r[f"recall_at_{K5}"]),
                    xytext=(7, 4), textcoords="offset points", fontsize=8, color=TEXT)
    ax.legend(handles=[Line2D([], [], marker="o", ls="", color=BLUE, label="brute off"),
                       Line2D([], [], marker="o", ls="", color=ORANGE, label="brute on"),
                       Line2D([], [], marker="^", ls="", color=MUTED, label="fusion RRF"),
                       Line2D([], [], marker="s", ls="", color=PURPLE, label="ANN + BM25 séparés"),
                       Line2D([], [], marker="s", ls="", color="#9a998f", label="autre index ou codes")],
              loc="lower right", fontsize=8)
    ax.set_xlabel("Contexte envoyé au modèle (k tokens) : plus cher, plus lent à répondre")
    ax.set_ylabel(f"R@{K5}")
    percent_axis(ax.yaxis)
    ax.set_title(f"Qualité contre coût : R@{K5} contre contexte envoyé (meilleur = en haut à gauche)", loc="left",
                 fontsize=11)
    fig.tight_layout()
    return fig


CURVES: dict[str, list[float]] = {}


def curve(name: str) -> list[float]:
    """Recall@K of a configuration for every K from 1 to the largest of ``ks``.

    :param name: configuration label
    :return: one mean recall per K of ``K_CURVE``
    """
    if name not in CURVES:
        f = paired[paired["config"] == name]
        CURVES[name] = [np.mean([score_at_k(e, r, k)[f"recall_at_{k}"] for e, r in zip(f["expected"], f["chunks"])])
                        for k in K_CURVE]
    return CURVES[name]


def curves_figure() -> plt.Figure:
    """Recall@K curves, one panel per top-k and one for the other settings, the reference dotted on each.

    :return: matplotlib figure
    """
    tops = sorted({PARAMS[n]["top_k"] for n in MAIN}, key=lambda v: [int(x) for x in v.split("/")])
    groups = [(f"top-k {t}", [n for n in MAIN if PARAMS[n]["top_k"] == t]) for t in tops]
    others = [n for n in RUN if n not in MAIN]
    if others:
        groups.append(("autres réglages", others))
    ncols = min(4, len(groups))
    nrows = math.ceil(len(groups) / ncols)
    fig, axes = plt.subplots(nrows, ncols, figsize=(4.2 * ncols, 3.9 * nrows + 0.6), sharey=True, squeeze=False)
    for ax, (title, names) in zip(axes.flat, groups):
        if REFERENCE not in names:
            ax.plot(K_CURVE, curve(REFERENCE), color=MUTED, ls=":", lw=1.5, label=f"{REFERENCE} (réf.)")
        for i, name in enumerate(names):
            color, style, _ = config_style(name, i)
            ax.plot(K_CURVE, curve(name), color=color, ls=style, marker="o", markersize=3,
                    markevery=[k - 1 for k in KS], label=f"{name} (réf.)" if name == REFERENCE else name)
        ax.set_xscale("log")
        ax.set_xticks(KS)
        ax.set_xticklabels([str(k) for k in KS])
        ax.minorticks_off()
        percent_axis(ax.yaxis)
        ax.set_xlabel("K premiers chunks")
        ax.set_title(title, loc="left", fontsize=10)
        ax.legend(fontsize=7, loc="lower right")
    for ax in list(axes.flat)[len(groups):]:
        ax.set_visible(False)
    axes[0][0].set_ylabel("Recall@K")
    fig.suptitle("Courbes Recall@K (orange = brute on, bleu = brute off, tirets = fusion RRF, pointillé gris = "
                 "référence)", x=0.01, ha="left", fontsize=11)
    fig.tight_layout()
    return fig


if not common_ids:
    log.info("Comparison skipped: no paired question")
else:
    display(styled_kpi(kpi_table()))
    ranks = ranks_table()
    for fig in (ranking_figure(), rank_figure(ranks), cost_figure(), curves_figure()):
        display(fig)
    misses = misses_table()
    if not misses.empty:
        display(misses.groupby("cause").size().rename("expected documents").reset_index())
        display(misses)

error_rows = pdf[pdf["error"].notna()]
if not error_rows.empty:
    display(error_rows[["config", "question", "error"]])
# Figures still open at the end of a cell are drawn a second time by Databricks.
plt.close("all")

# COMMAND ----------
# MAGIC %md
# MAGIC ## MLflow
# MAGIC Native MLflow only, one run per configuration and no other run, no artifact to open.
# MAGIC - **Experiment page, description**: the ranking of the configurations, the effect of each setting and the questions
# MAGIC   every configuration misses, rewritten at each execution.
# MAGIC - **Runs > Table**: one row per configuration. Settings `top_k`, `brute`, `fusion`, `recherche`, `codes`, `index`,
# MAGIC   `requetes_vs`; scores `recall_01` … `recall_50`, `recall_all`, `allrefs_10`, `allrefs_50`,
# MAGIC   `allrefs_all`, `mrr_01` … `mrr_50`, `mrr_all`, `docs`, `chunks` (MLflow adds `/mean`),
# MAGIC   `tokens_k`.
# MAGIC   Sort by clicking a column; group by a setting with **Group by**.
# MAGIC - **Runs > Chart**: `recall_by_k`, one line per configuration, K on the x axis. The other charts are the bar charts
# MAGIC   MLflow adds by itself; hide them on the screen.
# MAGIC - **A run**: its settings in plain words as description, then the evaluation tab, one row per question with its
# MAGIC   expected and retrieved documents and its scores. **Compare** on two runs shows the questions side by side.

# COMMAND ----------
from mlflow.genai.scorers import scorer
from mlflow.tracking import MlflowClient

K10 = 10 if 10 in KS else max(KS)
SETTINGS_ORDER = ["top_k", "brute", "fusion", "recherche", "data"]
SETTING_LABELS = {"top_k": "Top-k", "brute": "Recherche brute", "fusion": "Fusion", "recherche": "Recherche",
                  "data": "Index et codes"}
# MLflow column name -> (score column of ``score_at_k``, K)
SCORER_COLUMNS = {**{f"recall_{k:02d}": (f"recall_at_{k}", k) for k in KS}, "recall_all": ("recall_at_all", "all"),
                  f"allrefs_{K10}": (f"all_refs_at_{K10}", K10), "allrefs_50": ("all_refs_at_50", 50),
                  "allrefs_all": ("all_refs_at_all", "all"),
                  **{f"mrr_{k:02d}": (f"mrr_at_{k}", k) for k in sorted({*KS, 50})}, "mrr_all": ("mrr_at_all", "all")}


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


def metric_scorer(name: str, column: str, k: int | str) -> object:
    """Scorer returning one score of ``score_at_k`` for a question.

    :param name: MLflow name, e.g. ``recall_05``
    :param column: score column, e.g. ``recall_at_5``
    :param k: K of that score, or ``all``
    :return: MLflow scorer
    """
    def score(inputs: dict, outputs: dict, expectations: dict) -> float:
        """Score of one question.

        :param inputs: the question
        :param outputs: ``retrieved_chunks``, the REF of each chunk sent, in search order
        :param expectations: expected references
        :return: the score
        """
        return score_at_k(expectations["expected_refs"], outputs["retrieved_chunks"], k)[column]

    return scorer(score, name=name)


def docs_scorer() -> object:
    """Scorer returning the documents sent to the LLM for a question.

    :return: MLflow scorer named ``docs``
    """
    def docs(outputs: dict) -> float:
        """Documents of one question.

        :param outputs: outputs of the question
        :return: the number of distinct documents
        """
        return float(outputs["n_docs"])

    return scorer(docs, name="docs")


def chunks_scorer() -> object:
    """Scorer returning the passages sent to the LLM for a question.

    :return: MLflow scorer named ``chunks``
    """
    def chunks(outputs: dict) -> float:
        """Passages of one question.

        :param outputs: outputs of the question
        :return: the number of passages sent
        """
        return float(outputs.get("n_passages") or 0)

    return scorer(chunks, name="chunks")


EVAL_SCORERS = ([metric_scorer(name, column, k) for name, (column, k) in SCORER_COLUMNS.items()]
                + [docs_scorer(), chunks_scorer()])


def pct(value: float) -> str:
    """A share as a French percentage with one decimal.

    :param value: share from 0 to 1, NaN for none
    :return: e.g. ``82,9 %``
    """
    return "–" if pd.isna(value) else f"{100 * value:.1f} %".replace(".", ",")


def num(value: float, digits: int = 1) -> str:
    """A number in French notation.

    :param value: the number, NaN for none
    :param digits: decimals
    :return: e.g. ``13,4``
    """
    return "–" if pd.isna(value) else f"{value:.{digits}f}".replace(".", ",")


def points(value: float, digits: int = 1, scale: float = 100.0) -> str:
    """A delta with its sign, in points by default.

    :param value: the delta
    :param digits: decimals
    :param scale: 100 for points of a share, 1 for a raw value
    :return: e.g. ``+9,4``
    """
    if pd.isna(value):
        return "–"
    v = value * scale
    return ("+" if v > 1e-9 else "−" if v < -1e-9 else "±") + f"{abs(v):.{digits}f}".replace(".", ",")


def verdict(deltas: list[float]) -> str:
    """One word on a change, read against the noise of one question.

    :param deltas: one delta per pair of configurations, as share differences
    :return: ``gagne``, ``perd``, ``sans effet`` or ``mitigé``
    """
    if all(d > NOISE / 2 for d in deltas):
        return "**gagne**"
    if all(d < -NOISE / 2 for d in deltas):
        return "**perd**"
    if all(abs(d) <= NOISE for d in deltas):
        return "sans effet"
    return "mitigé"


def ranking_markdown(rows: dict[str, dict], params: dict[str, dict], reference: str) -> str:
    """Markdown table of the ranking.

    :param rows: label -> row of ``summary``
    :param params: label -> output of ``settings``
    :param reference: label of the reference configuration
    :return: markdown
    """
    k5 = 5 if 5 in KS else min(KS)
    head = ([f"R@{k}" for k in KS] + ["R@all", f"AllRefs@{K10}", "AllRefs@all", "MRR", "Docs", "Chunks", "k tokens",
                                      f"Δ R@{k5} vs réf."])
    lines = ["| # | Configuration | Réglages | " + " | ".join(head) + " |", "|" + "---|" * (len(head) + 3)]
    ref = rows[reference]
    for i, name in enumerate(ranking(rows), 1):
        r = rows[name]
        cells = ([pct(r[f"recall_at_{k}"]) for k in KS]
                 + [pct(r["recall_at_all"]), pct(r[f"all_refs_at_{K10}"]), pct(r["all_refs_at_all"]),
                    num(r["mrr_at_all"], 3), num(r["n_docs"]), num(r["n_passages"]),
                    num(tokens_k(r)), points(r[f"recall_at_{k5}"] - ref[f"recall_at_{k5}"])])
        label = f"**{name}** (réf.)" if name == reference else f"**{name}**"
        lines.append(f"| {i} | {label} | {describe(params[name])} | " + " | ".join(cells) + " |")
    return "\n".join(lines)


def effects_markdown(rows: dict[str, dict], params: dict[str, dict], reference: str) -> str:
    """Markdown table of what changing one setting does, over every pair of configurations that differ by it only.

    :param rows: label -> row of ``summary``
    :param params: label -> output of ``settings``
    :param reference: label of the reference configuration
    :return: markdown, empty when no pair differs by one setting only
    """
    values = {n: {**{s: params[n][s] for s in SETTINGS_ORDER[:-1]},
                  "data": f"{params[n]['index']} · codes {params[n]['codes']}"} for n in rows}

    def order(setting: str, value: str) -> tuple:
        """Sort key of a setting value: the reference's value first, then sizes in increasing order.

        :param setting: name of the setting
        :param value: its value
        :return: sort key
        """
        digits = re.findall(r"\d+(?:\.\d+)?", value)
        return (value != values[reference][setting], float(digits[0]) if digits else 0.0, value)

    groups: dict[tuple, list[tuple[str, str]]] = {}
    for setting in SETTINGS_ORDER:
        buckets: dict[tuple, list[str]] = {}
        for n in rows:
            buckets.setdefault(tuple(values[n][s] for s in SETTINGS_ORDER if s != setting), []).append(n)
        for members in buckets.values():
            distinct = sorted({values[n][setting] for n in members}, key=lambda v: order(setting, v))
            if len(distinct) < 2:
                continue
            base = next(n for n in members if values[n][setting] == distinct[0])
            for n in members:
                if values[n][setting] != distinct[0]:
                    groups.setdefault((setting, distinct[0], values[n][setting]), []).append((base, n))
    if not groups:
        return ""
    k5 = f"recall_at_{5 if 5 in KS else min(KS)}"
    lines = ["| Réglage changé | De → à | Paires | Δ R@5 | Δ R@all | Δ MRR | Δ contexte (k tokens) | Verdict (R@5) |",
             "|---|---|---|---|---|---|---|---|"]
    for (setting, start, end), pairs in sorted(groups.items(),
                                                key=lambda g: (SETTINGS_ORDER.index(g[0][0]), order(g[0][0], g[0][2]))):
        def mean(key: str) -> float:
            """Mean delta of a score over the pairs.

            :param key: score column of ``summary``
            :return: the mean
            """
            return sum(rows[b][key] - rows[a][key] for a, b in pairs) / len(pairs)

        ctx = sum(tokens_k(rows[b]) - tokens_k(rows[a]) for a, b in pairs) / len(pairs)
        lines.append(f"| {SETTING_LABELS[setting]} | {start} → {end} | {len(pairs)} | {points(mean(k5))} | "
                     f"{points(mean('recall_at_all'))} | {points(mean('mrr_at_all'), 3, 1)} | {points(ctx, 1, 1)} | "
                     f"{verdict([rows[b][k5] - rows[a][k5] for a, b in pairs])} |")
    return "\n".join(lines)


def missed_by_all_markdown() -> str:
    """Markdown table of the expected documents that no configuration puts in its top 10.

    :return: markdown, empty when there is none
    """
    misses = misses_table()
    if misses.empty:
        return ""
    gone = misses[misses[RUN].apply(lambda r: all(v == "-" or int(v) > K10 for v in r), axis=1)]
    if gone.empty:
        return ""
    lines = ["| Question | Document attendu | Cause |", "|---|---|---|"]
    for _, r in gone.iterrows():
        q = str(r["question"]).replace("|", "/")
        cause = {"absent from the chunks": "absent des chunks", "in the chunks, never sent": "dans les chunks, jamais envoyé"
                 }.get(r["cause"], f"envoyé, mais après le rang {K10}")
        lines.append(f"| {q} | {r['expected document']} | {cause} |")
    return "\n".join(lines)


if common_ids:
    client.workspace.mkdirs(EXPERIMENT.rsplit("/", 1)[0])
    experiment_id = mlflow.set_experiment(EXPERIMENT).experiment_id
    run_date = datetime.now(timezone.utc).strftime("%d/%m/%Y %H:%M UTC")
    tags = {"eval": "retrieval", "source": "golden", "golden_version": GOLDEN_VERSION,
            "golden_questions": str(len(ELIGIBLE)), "execution_id": EXECUTION_ID, "runner": RUNNER_VERSION}
    for name in ORDER:
        rows = paired[paired["config"] == name]
        data = [{"inputs": {"question": r["question"], "case_id": r["case_id"], "division": r["division"]},
                 "outputs": {"retrieved": refs(r["retrieved"]), "retrieved_chunks": chunk_refs(r["chunks"]),
                             "n_docs": int(r["n_docs"]),
                             "n_passages": int(r.get("n_passages") or 0)},
                 "expectations": {"expected_refs": refs(r["expected"])}}
                for _, r in rows.iterrows()]
        rank = ORDER.index(name) + 1
        with mlflow.start_run(run_name=name) as eval_run:
            mlflow.set_tags({**tags, "config_signature": CONFIGS[name]["signature"], "spec": CONFIGS[name]["spec"][:250],
                             "mlflow.note.content": f"{describe(PARAMS[name])}\n\n{rank}e sur {len(RUN)} au Recall@5 "
                                                    f"({pct(ROWS[name][f'recall_at_{5 if 5 in KS else min(KS)}'])}), "
                                                    f"exécution du {run_date}."})
            mlflow.log_params(PARAMS[name])
            mlflow.genai.evaluate(data=data, scorers=EVAL_SCORERS)
            log_number("tokens_k", tokens_k(ROWS[name]))
            for k in KS:
                mlflow.log_metric("recall_by_k", float(ROWS[name][f"recall_at_{k}"]), step=k)
        log.info("MLflow run=%s | config=%s | questions=%d", eval_run.info.run_id, name, len(data))

    effects = effects_markdown(ROWS, PARAMS, REFERENCE)
    missed = missed_by_all_markdown()
    note = "\n\n".join(part for part in [
        f"## Dernière exécution : {run_date}",
        f"{len(RUN)} configurations, {len(common_ids)} questions, référence **{REFERENCE}**. Recall@K : part des "
        f"documents attendus ayant au moins un chunk parmi les K premiers chunks envoyés au modèle (au-delà des chunks "
        f"envoyés, tout le contexte). AllRefs : part des questions dont tous les documents attendus y sont. MRR : 1 / "
        f"rang du premier chunk d'un document attendu. Une question vaut {num(100 * NOISE)} point.",
        "### Classement (meilleur Recall@5 d'abord)", ranking_markdown(ROWS, PARAMS, REFERENCE),
        "### Effet de chaque réglage" if effects else "",
        "Chaque ligne compare des paires de configurations identiques sauf ce réglage." if effects else "", effects,
        f"### Documents attendus qu'aucune configuration ne met dans son top {K10}" if missed else "", missed,
    ] if part)
    MlflowClient().set_experiment_tag(experiment_id, "mlflow.note.content", note)
    log.info("MLflow experiment %s: %d runs, description updated", EXPERIMENT, len(RUN))
