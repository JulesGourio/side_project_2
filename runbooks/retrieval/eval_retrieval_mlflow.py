# Databricks notebook source
# MAGIC %md
# MAGIC # Qualibot retrieval evaluation: code normalization vs baseline (DEV, MLflow)
# MAGIC
# MAGIC Scores the documents Qualibot retrieves for each question of `dev_proj.qualibot.eval_golden_retrieval` against its
# MAGIC `expected_refs`, for two versions on the same chunks:
# MAGIC
# MAGIC | Version | Index | Query |
# MAGIC |---|---|---|
# MAGIC | `baseline` | `chunks_text_index`: embeds `chunk_text`, syncs no canonical code column | the question as typed |
# MAGIC | `code_norm` | `chunks_index` (the app's): embeds and searches `chunk_preprocessed` | `normalize_query` (`SF-1027 [SF1027]`) |
# MAGIC
# MAGIC Two ways of searching, each run for both versions:
# MAGIC - `index`: one hybrid query with the reranker, 50 passages, deterministic. It gives the recall@k curve up to 50.
# MAGIC - `app`: the chat's own retrieval (translation, rewrite, merged queries, REF lookup, exact citations), ranked as
# MAGIC   the documents handed to the answer model. For `baseline`, every code normalization step of the app is switched
# MAGIC   off in memory (query, citation lookup, language variant choice); the deployed app is not touched.
# MAGIC
# MAGIC A hit is a document: language variants are merged (`Q0102QP_GB` = `Q0102QP`). Metrics: MRR, recall@k, hit@k,
# MAGIC nDCG@10, by language, division, "question names a code" and number of expected documents, with paired bootstrap
# MAGIC intervals on the differences. `no_document` questions only record the score of the first passage;
# MAGIC `to_complete` questions are skipped.
# MAGIC
# MAGIC Everything goes to the MLflow experiment `/Shared/qualibot/retrieval_eval_dev`: one parent run per evaluation, one
# MAGIC child run per (search, version) with comparable metric names, per-question tables and figures. The LLM-as-a-judge
# MAGIC pairwise comparison of answers is off (`RUN_PAIRWISE_JUDGE`).
# MAGIC
# MAGIC Read only: writes nothing in Unity Catalog. Identity: your own rights (SELECT on the golden table, query on the two
# MAGIC indexes, MLflow in `/Shared`). Both indexes are synced by the parsing task `5_sync_index`. Run all, about 10 minutes
# MAGIC for 100 questions.

# COMMAND ----------

# MAGIC %pip install -q fastapi asyncpg==0.31.0 httpx==0.28.1 fasttext-wheel==0.9.2 tenacity pyyaml
# MAGIC dbutils.library.restartPython()

# COMMAND ----------

GOLDEN_TABLE = "dev_proj.qualibot.eval_golden_retrieval"
NEW_INDEX = "dev_proj.qualibot.chunks_index"
BASELINE_INDEX = "dev_proj.qualibot.chunks_text_index"
# Deployed app code: the new version's query normalization and the chat retrieval are imported from it.
APP_CODE_PATH = "/Workspace/.bundle/qualibot/dev/files"
APP_TARGET = "dev"
EXPERIMENT_PATH = "/Shared/qualibot/retrieval_eval_dev"

RUN_INDEX_SEARCH = True
RUN_APP_SEARCH = True
RUN_PAIRWISE_JUDGE = False

# Search as the app does it: hybrid (keywords + embeddings), reranked on these columns, same archive notice filter.
INDEX_NUM_RESULTS = 50
RERANKER = {"model": "databricks_reranker", "parameters": {"columns_to_rerank": ["REF", "semantic_headers", "chunk_text"]}}
RESULT_COLUMNS = ["chunk_id", "IDDOC", "REF", "division", "url", "semantic_headers", "chunk_text"]
ARCHIVE_NOTICE_MARKER = "ARCHIVED DOCUMENT — CONTENT NOT INDEXED"
MAX_PARALLEL = 4                 # questions at once; each app search sends 3 to 5 Vector Search queries
K_MAX = 50                       # last point of the recall@k curve (documents)
K_REPORTED = [1, 3, 5, 10, 20, 50]
# The expected documents also listed in unknown_refs stay expected; True scores without them.
DROP_UNKNOWN_REFS = False
BOOTSTRAP_SAMPLES = 5000
SEED = 7

# Pairwise judge (second step): each version's answer from its own top passages, judged in both orders.
ANSWER_ENDPOINT = "databricks-gpt-6-luna"
JUDGE_ENDPOINT = "databricks-gpt-5-6-luna"
ANSWER_TOP_PASSAGES = 8
JUDGE_ONLY_CHANGED = True        # identical passages give identical answers: a tie, not worth a call
JUDGE_MAX_QUESTIONS = 200

VERSIONS = ["baseline", "code_norm"]
INDEX_OF = {"baseline": BASELINE_INDEX, "code_norm": NEW_INDEX}

# COMMAND ----------

import asyncio
import hashlib
import importlib.util
import json
import logging
import os
import re
import shlex
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from typing import Iterator
from datetime import datetime, timezone

import matplotlib.pyplot as plt
import mlflow
import numpy as np
import pandas as pd
import requests
from databricks.sdk import WorkspaceClient

logger = logging.getLogger("retrieval_eval")
logger.setLevel(logging.INFO)
if not logger.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S"))
    logger.addHandler(_handler)
    logger.propagate = False

w = WorkspaceClient()
HOST = w.config.host.rstrip("/")
HTTP = requests.Session()


def auth_headers() -> dict:
    """Authorization header of the notebook's identity, refreshed on each call (tokens expire during long runs).

    :return: headers with ``Authorization``
    """
    return w.config.authenticate()


def file_sha(path: str) -> str:
    """Short SHA-256 of a file, logged so a run says which code it measured.

    :param path: file path
    :return: first 12 hex characters of the hash, ``missing`` when the file does not exist
    """
    if not os.path.exists(path):
        return "missing"
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()[:12]


CODE_NORMALIZATION_FILE = f"{APP_CODE_PATH}/utils/parsing_pipeline/code_normalization.py"
assert os.path.exists(CODE_NORMALIZATION_FILE), (
    f"{CODE_NORMALIZATION_FILE} not found: set APP_CODE_PATH to the folder the DEV app code is deployed to")
_spec = importlib.util.spec_from_file_location("eval_code_normalization", CODE_NORMALIZATION_FILE)
code_norm = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = code_norm
_spec.loader.exec_module(code_norm)

CODE_FINGERPRINT = {"code_normalization_sha": file_sha(CODE_NORMALIZATION_FILE),
                    "retrieval_sha": file_sha(f"{APP_CODE_PATH}/server/services/chat/engine/retrieval.py"),
                    "search_sha": file_sha(f"{APP_CODE_PATH}/server/services/chat/engine/search.py")}
logger.info(f"app code {APP_CODE_PATH}: {CODE_FINGERPRINT}")

# Same palette in every figure: one colour per version, status colours only for improved / regressed.
COLOR = {"baseline": "#eb6834", "code_norm": "#2a78d6", "improved": "#0ca30c", "regressed": "#d03b3b",
         "same": "#8c8b87", "grid": "#e1e0d9", "text": "#0b0b0b", "muted": "#52514e"}
LABEL = {"baseline": "baseline (no code normalization)", "code_norm": "code normalization"}
plt.rcParams.update({"figure.dpi": 110, "axes.spines.top": False, "axes.spines.right": False,
                     "axes.edgecolor": COLOR["muted"], "axes.labelcolor": COLOR["text"], "axes.grid": True,
                     "grid.color": COLOR["grid"], "grid.linewidth": 0.8, "xtick.color": COLOR["muted"],
                     "ytick.color": COLOR["muted"], "lines.linewidth": 2, "font.size": 10,
                     "legend.frameon": False})

# COMMAND ----------

# Golden set. expected_refs / unknown_refs may be stored as arrays or as JSON strings: both are read.


def as_list(value) -> list:
    """A list of non-empty strings from an array column, a JSON string or None.

    :param value: column value
    :return: the items, stripped, empty ones dropped
    """
    if value is None:
        return []
    if isinstance(value, str):
        value = json.loads(value) if value.strip().startswith("[") else re.split(r"[,;\s]+", value)
    return [str(v).strip() for v in value if v is not None and str(v).strip()]


def canon_ref(ref: str) -> str:
    """Document key with language variants merged, the same rule as the app (``Q0102QP_GB`` -> ``Q0102QP``).

    :param ref: REF in any spelling
    :return: upper-cased REF without its language suffix and punctuation
    """
    stripped = re.sub(r"[-_. ]+(FR|EN|GB|MX|BG|CZ|BR|ES)$", "", ref.upper(), flags=re.IGNORECASE)
    return re.sub(r"[^A-Z0-9]", "", stripped)


golden_raw = spark.table(GOLDEN_TABLE).toPandas()
golden = golden_raw.copy()
golden["expected"] = golden["expected_refs"].map(lambda v: sorted({canon_ref(r) for r in as_list(v)}))
golden["unknown"] = golden["unknown_refs"].map(lambda v: sorted({canon_ref(r) for r in as_list(v)}))
if DROP_UNKNOWN_REFS:
    golden["expected"] = [[r for r in e if r not in set(u)] for e, u in zip(golden["expected"], golden["unknown"])]
golden["division"] = golden["division"].fillna("ALL").str.upper()
golden["lang"] = golden["lang"].fillna("unknown").str.lower()
golden["has_code"] = golden["question"].map(lambda q: bool(code_norm.find_codes(q, accept_lowercase=True)))
golden["n_expected"] = golden["expected"].map(len)
golden["expected_bucket"] = golden["n_expected"].map(lambda n: "1 doc" if n == 1 else "2 docs" if n == 2 else "3+ docs")

positives = golden[(golden["kind"] == "positive") & (golden["n_expected"] > 0)].reset_index(drop=True)
no_document = golden[golden["kind"] == "no_document"].reset_index(drop=True)
skipped = golden[~(((golden["kind"] == "positive") & (golden["n_expected"] > 0)) | (golden["kind"] == "no_document"))]
DATASET_DIGEST = hashlib.sha256(
    golden_raw.sort_values("question_id").to_json(orient="records", force_ascii=False).encode()).hexdigest()[:12]
DATASET_UPDATED = str(golden_raw["updated_at"].max())
logger.info(f"{GOLDEN_TABLE}: {len(golden)} questions (digest {DATASET_DIGEST}, updated {DATASET_UPDATED}); "
            f"{len(positives)} scored, {len(no_document)} no_document, {len(skipped)} skipped "
            f"({skipped['kind'].value_counts().to_dict()})")
display(golden.groupby(["kind", "lang", "division", "has_code"]).size().rename("questions").reset_index())

# COMMAND ----------

# Both indexes must hold the same chunks, and the baseline must not see chunk_preprocessed, or the comparison measures
# the corpus instead of the normalization.
status = {name: w.vector_search_indexes.get_index(index_name=name) for name in INDEX_OF.values()}
for name, idx in status.items():
    synced = idx.delta_sync_index_spec.columns_to_sync or ["<all columns>"]
    embedded = [c.name for c in idx.delta_sync_index_spec.embedding_source_columns]
    logger.info(f"{name}: ready={idx.status.ready}, rows={idx.status.indexed_row_count}, embeds={embedded}, syncs={synced}")
    assert idx.status.ready, f"{name} is not ready"
rows_new, rows_base = (status[NEW_INDEX].status.indexed_row_count, status[BASELINE_INDEX].status.indexed_row_count)
assert rows_new == rows_base, (f"{NEW_INDEX} has {rows_new} rows and {BASELINE_INDEX} {rows_base}: "
                               "run the parsing task 5_sync_index and start again")
_base_synced = set(status[BASELINE_INDEX].delta_sync_index_spec.columns_to_sync or [])
assert _base_synced and not _base_synced & {"chunk_preprocessed", "cited_codes"}, \
    f"{BASELINE_INDEX} syncs the canonical code columns: delete it, the parsing task 5_sync_index creates it again"
INDEX_ROWS = rows_new

# COMMAND ----------

# Search 1, "index": one reranked hybrid query per question and version. The baseline sends the question as typed,
# the new version the question with its canonical codes, exactly what the app sends to Vector Search.


def query_text_for(version: str, question: str) -> str:
    """The text a version sends to Vector Search.

    :param version: ``baseline`` or ``code_norm``
    :param question: question as typed
    :return: the question, with its canonical codes for ``code_norm``
    """
    return code_norm.normalize_query(question) if version == "code_norm" else question


def division_filter(division: str) -> dict:
    """The app's division filter: none for ALL, else the division plus the shared GROUP documents.

    :param division: AS, IS or ALL
    :return: Vector Search filter, empty for none
    """
    return {} if division == "ALL" else {"division": [division, "GROUP"]}


def vs_query(index: str, text: str, filters: dict) -> list:
    """One reranked hybrid query, retried on throttling and server errors.

    :param index: full index name
    :param text: query text
    :param filters: Vector Search filters, empty for none
    :return: result rows as dicts (``RESULT_COLUMNS`` + ``score``), archive notices left out
    """
    payload = {"query_text": text[:20000], "columns": RESULT_COLUMNS, "num_results": INDEX_NUM_RESULTS,
               "query_type": "HYBRID", "reranker": RERANKER}
    if filters:
        payload["filters_json"] = json.dumps(filters)
    for attempt, wait in enumerate((2, 8, 30, None)):
        resp = HTTP.post(f"{HOST}/api/2.0/vector-search/indexes/{index}/query", json=payload,
                         headers=auth_headers(), timeout=60)
        if resp.status_code == 200:
            data = resp.json()
            cols = [c["name"] for c in data["manifest"]["columns"]]
            rows = [dict(zip(cols, r)) for r in data.get("result", {}).get("data_array", [])]
            return [r for r in rows if ARCHIVE_NOTICE_MARKER not in (r.get("chunk_text") or "")]
        if wait is None or resp.status_code not in (429, 500, 502, 503, 504):
            resp.raise_for_status()
        time.sleep(wait)
    return []


def ranked_documents(refs: list) -> list:
    """Distinct documents in order of their best passage.

    :param refs: REF of each passage, in rank order
    :return: canonical document keys, first appearance first
    """
    return list(dict.fromkeys(canon_ref(r) for r in refs if r))


def index_search(row: pd.Series, version: str) -> dict:
    """Search one question on one version's index.

    :param row: golden row (``question``, ``division``)
    :param version: ``baseline`` or ``code_norm``
    :return: ranked documents, passages kept for the judge, first passage score, query sent, latency, error
    """
    text = query_text_for(version, row["question"])
    started = time.monotonic()
    try:
        hits = vs_query(INDEX_OF[version], text, division_filter(row["division"]))
    except Exception as exc:
        return {"docs": [], "passages": [], "top_score": None, "query": text, "latency_s": None,
                "error": f"{type(exc).__name__}: {str(exc)[:300]}"}
    return {"docs": ranked_documents([h["REF"] for h in hits]),
            "passages": [{"REF": h["REF"], "chunk_text": h["chunk_text"]} for h in hits[:ANSWER_TOP_PASSAGES]],
            "top_score": hits[0].get("score") if hits else None, "query": text,
            "latency_s": round(time.monotonic() - started, 2), "error": None}


def run_all(frame: pd.DataFrame, search_fn, version: str) -> list:
    """Run a search function on every question of a frame, in parallel.

    :param frame: golden rows
    :param search_fn: ``(row, version) -> dict``
    :param version: version name passed to ``search_fn``
    :return: one result dict per row, in row order
    """
    with ThreadPoolExecutor(max_workers=MAX_PARALLEL) as pool:
        return list(pool.map(lambda r: search_fn(r, version), [r for _, r in frame.iterrows()]))


RESULTS = {}      # (search, version) -> {"positives": [...], "no_document": [...]}
if RUN_INDEX_SEARCH:
    for version in VERSIONS:
        t0 = time.monotonic()
        RESULTS[("index", version)] = {"positives": run_all(positives, index_search, version),
                                       "no_document": run_all(no_document, index_search, version)}
        errors = sum(r["error"] is not None for r in RESULTS[("index", version)]["positives"])
        logger.info(f"index/{version}: {len(positives)} questions in {time.monotonic() - t0:.0f} s, {errors} errors")

# COMMAND ----------

# Search 2, "app": the chat's retrieval imported from the deployed code. For the baseline, each code normalization step
# the app runs is replaced in memory by a no-op while the search runs, then restored.
if RUN_APP_SEARCH:
    import yaml

    with open(f"{APP_CODE_PATH}/app.yaml", encoding="utf-8-sig") as f:
        for item in (yaml.safe_load(f) or {}).get("env") or []:
            if "value" in item:
                os.environ[item["name"]] = str(item["value"])
    _target_env = f"{APP_CODE_PATH}/target_config.env"
    if os.path.exists(_target_env):
        for line in open(_target_env, encoding="utf-8"):
            m = re.match(r"\s*export\s+([A-Z0-9_]+)=(.*)$", line)
            if m:
                os.environ[m.group(1)] = (shlex.split(m.group(2)) or [""])[0]
    else:
        with open(f"{APP_CODE_PATH}/utils/deploy/target_env.json", encoding="utf-8") as f:
            os.environ.update({k: str(v) for k, v in json.load(f)[APP_TARGET].items()})
    # One rewrite model per run: a silent fallback to another model would mix two rewrites in one measure.
    os.environ["CHAT_REWRITE_FALLBACK_ENDPOINTS"] = os.environ.get("CHAT_REWRITE_ENDPOINT") or os.environ["CHAT_LLM_ENDPOINT"]
    sys.path.insert(0, APP_CODE_PATH)
    for _m in [m for m in sys.modules if m.split(".")[0] in ("server", "utils")]:
        del sys.modules[_m]

    import httpx
    from server.services.chat import engine
    from server.services.chat import translation_bridge as tb
    from server.services.chat.config import TRANSLATE_BRIDGE_ENABLED, TRANSLATE_TIMEOUT_S
    from server.services.chat.engine import retrieval as m_retrieval, search as m_search
    from server.services.infra import vector_search as m_vector_search
    from server.routers.chat import _trim_history, _with_today_date
    from server.services.chat.translation_bridge import translate_question_to_en

    # Each question runs in its own event loop (asyncio.run in a thread); the bridge's shared client would be bound to
    # the first one.
    tb._get_http_client = lambda: httpx.AsyncClient(timeout=TRANSLATE_TIMEOUT_S)
    APP_SETTINGS = {k: os.environ.get(k) for k in sorted(os.environ) if k.startswith("CHAT_")}

    _NO_CODE_NORMALIZATION = [
        (m_search, "normalize_query", lambda q: q),
        (m_vector_search, "normalize_query", lambda q: q),
        (m_retrieval, "normalize_query", lambda q: q),
        (m_retrieval, "canonical_codes", lambda *a, **k: []),
        (m_retrieval, "cited_codes_asked", lambda *a, **k: []),
    ]

    @contextmanager
    def app_version(version: str) -> Iterator[None]:
        """Point the app's engine at a version's index, with the code normalization off for the baseline.

        :param version: ``baseline`` or ``code_norm``
        :return: context manager; the module attributes and CHAT_INDEX are restored on exit
        """
        saved_index = os.environ.get("CHAT_INDEX")
        saved = [(mod, name, getattr(mod, name)) for mod, name, _ in _NO_CODE_NORMALIZATION]
        os.environ["CHAT_INDEX"] = INDEX_OF[version]
        if version == "baseline":
            for mod, name, replacement in _NO_CODE_NORMALIZATION:
                setattr(mod, name, replacement)
        try:
            yield
        finally:
            for mod, name, original in saved:
                setattr(mod, name, original)
            os.environ["CHAT_INDEX"] = saved_index or NEW_INDEX

    async def _app_retrieve(question: str, division: str) -> dict:
        """The chat's retrieval for an opening message, as the chat route runs it.

        :param question: question as typed
        :param division: AS, IS or ALL
        :return: ranked documents, the queries the engine searched, the REFs named, latency
        """
        started = time.monotonic()
        token = auth_headers()["Authorization"].split(" ", 1)[1]
        messages = _trim_history([{"role": "user", "content": question}])
        if TRANSLATE_BRIDGE_ENABLED:
            en_question, ctx = await translate_question_to_en(question, HOST, token)
            if ctx.needs_translation:
                messages[-1]["content"] = en_question
        conversation = engine._clean_history(_with_today_date(messages))
        found = await engine.retrieve_for_turn(HOST, token, engine.normalize_division(division), conversation)
        refs = [ref for ref, _ in engine.group_documents(found["rows"])]
        return {"docs": ranked_documents(refs), "passages": [], "top_score": None,
                "query": json.dumps({"fr": found.get("fr_query"), "en": found.get("en_query")}, ensure_ascii=False),
                "named": found.get("named") or [], "latency_s": round(time.monotonic() - started, 2), "error": None}

    def app_search(row: pd.Series, version: str) -> dict:
        """One question through the chat's retrieval, retried on transient errors.

        :param row: golden row
        :param version: version name (the caller has entered ``app_version``)
        :return: same keys as ``index_search``
        """
        for wait in (5, 20, None):
            try:
                return asyncio.run(_app_retrieve(row["question"], row["division"]))
            except Exception as exc:
                if wait is None:
                    return {"docs": [], "passages": [], "top_score": None, "query": "", "latency_s": None,
                            "error": f"{type(exc).__name__}: {str(exc)[:300]}"}
                time.sleep(wait)

    for version in VERSIONS:
        t0 = time.monotonic()
        with app_version(version):
            RESULTS[("app", version)] = {"positives": run_all(positives, app_search, version), "no_document": []}
        errors = sum(r["error"] is not None for r in RESULTS[("app", version)]["positives"])
        logger.info(f"app/{version}: {len(positives)} questions in {time.monotonic() - t0:.0f} s, {errors} errors")

# COMMAND ----------

# Metrics per question. Ranks count documents, not passages. A miss has rank None and reciprocal rank 0.


def question_metrics(docs: list, expected: list) -> dict:
    """Retrieval metrics of one ranked document list.

    :param docs: canonical document keys in rank order
    :param expected: canonical keys of the expected documents (not empty)
    :return: first_rank (1-based, None on a miss), rr, recall@k / hit@k / all@k for k = 1..K_MAX, ndcg@10
    """
    exp = set(expected)
    ranks = [i + 1 for i, d in enumerate(docs) if d in exp]
    out = {"first_rank": ranks[0] if ranks else None, "rr": 1 / ranks[0] if ranks else 0.0,
           "rr_at_10": 1 / ranks[0] if ranks and ranks[0] <= 10 else 0.0}
    for k in range(1, K_MAX + 1):
        found = sum(r <= k for r in ranks)
        out[f"recall_at_{k}"] = found / len(exp)
        out[f"hit_at_{k}"] = float(found > 0)
        out[f"all_at_{k}"] = float(found == len(exp))
    dcg = sum(1 / np.log2(r + 1) for r in ranks if r <= 10)
    idcg = sum(1 / np.log2(i + 2) for i in range(min(len(exp), 10)))
    out["ndcg_at_10"] = dcg / idcg
    return out


def per_question_frame(search: str, version: str) -> pd.DataFrame:
    """One row per scored question: golden columns, what was retrieved and its metrics.

    :param search: ``index`` or ``app``
    :param version: ``baseline`` or ``code_norm``
    :return: frame aligned with ``positives``
    """
    rows = []
    for (_, g), res in zip(positives.iterrows(), RESULTS[(search, version)]["positives"]):
        rec = {"question_id": g["question_id"], "question": g["question"], "lang": g["lang"],
               "division": g["division"], "has_code": g["has_code"], "expected_bucket": g["expected_bucket"],
               "expected": g["expected"], "unknown": g["unknown"], "retrieved_top10": res["docs"][:10],
               "n_docs": len(res["docs"]), "query": res["query"], "top_score": res["top_score"],
               "latency_s": res["latency_s"], "error": res["error"]}
        rec.update(question_metrics(res["docs"], g["expected"]) if res["error"] is None else {})
        rows.append(rec)
    return pd.DataFrame(rows)


FRAMES = {key: per_question_frame(*key) for key in RESULTS}
# Questions with an error in any run are dropped from every run, so both versions are scored on the same questions.
_errored = {qid for f in FRAMES.values() for qid in f.loc[f["error"].notna(), "question_id"]}
SCORED = {key: f[~f["question_id"].isin(_errored)].reset_index(drop=True) for key, f in FRAMES.items()}
if _errored:
    logger.warning(f"{len(_errored)} questions dropped after errors: "
                   f"{[(k, f['error'].dropna().iloc[0]) for k, f in FRAMES.items() if f['error'].notna().any()][:2]}")

SLICES = ["lang", "division", "has_code", "expected_bucket"]
HEADLINE = ["rr", "rr_at_10", "ndcg_at_10"] + [f"{m}_at_{k}" for m in ("recall", "hit", "all") for k in K_REPORTED]


def summary(frame: pd.DataFrame) -> dict:
    """Mean of the headline metrics, plus misses and latency.

    :param frame: per-question frame of one run
    :return: metric name -> value (MRR is the mean of ``rr``)
    """
    out = {("mrr" if m == "rr" else "mrr_at_10" if m == "rr_at_10" else m): float(frame[m].mean()) for m in HEADLINE}
    out["miss_rate_at_50"] = float(frame["first_rank"].isna().mean())
    out["median_first_rank"] = float(frame["first_rank"].median()) if frame["first_rank"].notna().any() else float("nan")
    out["latency_p50_s"] = float(frame["latency_s"].median())
    out["questions"] = float(len(frame))
    return out


def paired_bootstrap(a: np.ndarray, b: np.ndarray) -> tuple:
    """Mean difference b - a with its 95 % paired bootstrap interval and the share of resamples where b > a.

    :param a: per-question metric of the baseline
    :param b: per-question metric of the new version, same questions in the same order
    :return: (delta, ci_low, ci_high, p_better)
    """
    rng = np.random.default_rng(SEED)
    d = b - a
    idx = rng.integers(0, len(d), size=(BOOTSTRAP_SAMPLES, len(d)))
    means = d[idx].mean(axis=1)
    return float(d.mean()), float(np.percentile(means, 2.5)), float(np.percentile(means, 97.5)), float((means > 0).mean())


def comparison(search: str) -> pd.DataFrame:
    """Question by question, baseline next to the new version, with the direction of the change.

    :param search: ``index`` or ``app``
    :return: one row per question; ``change`` is improved / regressed / same on the rank of the first expected document
    """
    a, b = SCORED[(search, "baseline")], SCORED[(search, "code_norm")]
    out = a[["question_id", "question", "lang", "division", "has_code", "expected_bucket", "expected", "unknown"]].copy()
    for name, f in (("base", a), ("new", b)):
        out[f"{name}_rank"] = f["first_rank"]
        out[f"{name}_rr"] = f["rr"]
        out[f"{name}_recall_at_10"] = f["recall_at_10"]
        out[f"{name}_top10"] = f["retrieved_top10"]
        out[f"{name}_query"] = f["query"]
    out["delta_rr"] = out["new_rr"] - out["base_rr"]
    out["delta_recall_at_10"] = out["new_recall_at_10"] - out["base_recall_at_10"]
    big = K_MAX + 1
    base_rank, new_rank = out["base_rank"].fillna(big), out["new_rank"].fillna(big)
    out["change"] = np.where(new_rank < base_rank, "improved", np.where(new_rank > base_rank, "regressed", "same"))
    return out.sort_values(["delta_rr", "delta_recall_at_10"]).reset_index(drop=True)


SEARCHES = sorted({s for s, _ in SCORED}, key=["index", "app"].index)
COMPARE = {s: comparison(s) for s in SEARCHES}
SUMMARY = pd.DataFrame([{"search": s, "version": v, **summary(SCORED[(s, v)])} for s, v in SCORED])
display(SUMMARY[["search", "version", "questions", "mrr", "mrr_at_10", "ndcg_at_10", "hit_at_1", "hit_at_5",
                 "hit_at_10", "recall_at_5", "recall_at_10", "recall_at_50", "all_at_10", "miss_rate_at_50",
                 "median_first_rank", "latency_p50_s"]].round(3))

# COMMAND ----------

# Paired differences (code_norm - baseline). An interval that excludes 0 is a change the question sample supports.
DELTAS = []
for s in SEARCHES:
    a, b = SCORED[(s, "baseline")], SCORED[(s, "code_norm")]
    for metric in ["rr", "ndcg_at_10", "hit_at_1", "hit_at_5", "recall_at_5", "recall_at_10", "recall_at_50"]:
        for label, mask in [("all", np.ones(len(a), bool)), ("has_code", a["has_code"].values),
                            ("no_code", ~a["has_code"].values)]:
            if mask.sum() == 0:
                continue
            delta, low, high, p = paired_bootstrap(a.loc[mask, metric].values, b.loc[mask, metric].values)
            DELTAS.append({"search": s, "metric": "mrr" if metric == "rr" else metric, "questions": label,
                           "n": int(mask.sum()), "baseline": a.loc[mask, metric].mean(),
                           "code_norm": b.loc[mask, metric].mean(), "delta": delta, "ci95_low": low, "ci95_high": high,
                           "p_code_norm_better": p})
DELTAS = pd.DataFrame(DELTAS)
display(DELTAS.round(3))

# COMMAND ----------

# Slices: where the normalization helps or hurts. "has_code" is where it can act; elsewhere it should change nothing.


def slice_table(search: str) -> pd.DataFrame:
    """MRR, hit@5 and recall@10 per slice value and version.

    :param search: ``index`` or ``app``
    :return: one row per (slice, value, version) with the number of questions
    """
    rows = []
    for v in VERSIONS:
        f = SCORED[(search, v)]
        for sl in SLICES:
            for value, g in f.groupby(sl):
                rows.append({"slice": sl, "value": str(value), "version": v, "n": len(g), "mrr": g["rr"].mean(),
                             "hit_at_5": g["hit_at_5"].mean(), "recall_at_10": g["recall_at_10"].mean()})
    return pd.DataFrame(rows)


SLICE_TABLES = {s: slice_table(s) for s in SEARCHES}
for s in SEARCHES:
    display(SLICE_TABLES[s].pivot_table(index=["slice", "value", "n"], columns="version",
                                        values=["mrr", "recall_at_10"]).round(3).reset_index())

# COMMAND ----------

# Questions whose first expected document moved. Regressions first: they are the ones to read before adopting.
for s in SEARCHES:
    moved = COMPARE[s][COMPARE[s]["change"] != "same"]
    logger.info(f"{s}: {(COMPARE[s]['change'] == 'improved').sum()} improved, "
                f"{(COMPARE[s]['change'] == 'regressed').sum()} regressed, {(COMPARE[s]['change'] == 'same').sum()} same")
    display(moved[["change", "question_id", "question", "lang", "has_code", "expected", "base_rank", "new_rank",
                   "delta_rr", "base_top10", "new_top10", "new_query"]])

# COMMAND ----------

# Figures. Each one is shown here and logged to the parent run.


def fig_recall_curves(search: str) -> plt.Figure:
    """Recall@k and hit@k against k for both versions (small multiples, one y scale each).

    :param search: ``index`` or ``app``
    :return: matplotlib figure
    """
    ks = np.arange(1, K_MAX + 1)
    fig, axes = plt.subplots(1, 2, figsize=(12, 4.2), sharex=True)
    for ax, metric, title in [(axes[0], "recall", "Recall@k: share of expected documents in the top k"),
                              (axes[1], "hit", "Hit@k: at least one expected document in the top k")]:
        for v in VERSIONS:
            f = SCORED[(search, v)]
            y = [f[f"{metric}_at_{k}"].mean() for k in ks]
            ax.plot(ks, y, color=COLOR[v], label=LABEL[v], marker="o", markersize=3, markevery=[k - 1 for k in K_REPORTED])
            for k in (10, K_MAX):
                ax.annotate(f"{y[k - 1]:.0%}", (k, y[k - 1]), xytext=(5, 6 if v == "code_norm" else -8),
                            textcoords="offset points", va="center", color=COLOR["text"], fontsize=8)
        ax.set_xscale("log")
        ax.set_xticks(K_REPORTED)
        ax.set_xticklabels([str(k) for k in K_REPORTED])
        ax.set_ylim(0, 1.02)
        ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda y, _: f"{y:.0%}"))
        ax.set_xlabel("k (documents, log scale)")
        ax.set_title(title, loc="left", fontsize=10, color=COLOR["text"])
    fig.suptitle(f"{search} search, {len(SCORED[(search, 'baseline')])} questions", x=0.01, ha="left", fontsize=11)
    fig.legend(*axes[0].get_legend_handles_labels(), loc="upper right", ncol=2)
    fig.tight_layout(rect=(0, 0, 1, 0.93))
    return fig


def fig_slices(search: str) -> plt.Figure:
    """MRR per slice value, both versions side by side, n in the label.

    :param search: ``index`` or ``app``
    :return: matplotlib figure
    """
    t = SLICE_TABLES[search]
    groups = t[t["version"] == "baseline"][["slice", "value", "n"]].values.tolist()
    fig, ax = plt.subplots(figsize=(8, 0.42 * len(groups) + 1.2))
    y = np.arange(len(groups))
    for i, v in enumerate(VERSIONS):
        vals = [t[(t["slice"] == sl) & (t["value"] == val) & (t["version"] == v)]["mrr"].iloc[0] for sl, val, _ in groups]
        bars = ax.barh(y + (i - 0.5) * 0.38, vals, height=0.36, color=COLOR[v], label=LABEL[v], edgecolor="white",
                       linewidth=1)
        for bar, val in zip(bars, vals):
            ax.text(bar.get_width() + 0.01, bar.get_y() + bar.get_height() / 2, f"{val:.2f}", va="center",
                    fontsize=8, color=COLOR["text"])
    ax.set_yticks(y)
    ax.set_yticklabels([f"{sl} = {val}  (n={n})" for sl, val, n in groups])
    ax.invert_yaxis()
    ax.set_xlim(0, 1.1)
    ax.set_xlabel("MRR")
    ax.grid(axis="y", visible=False)
    ax.legend(loc="lower right")
    ax.set_title(f"MRR by slice, {search} search", loc="left", fontsize=10)
    fig.tight_layout()
    return fig


def fig_rank_scatter(search: str) -> plt.Figure:
    """Rank of the first expected document, baseline against new version; misses drawn past K_MAX.

    :param search: ``index`` or ``app``
    :return: matplotlib figure
    """
    c = COMPARE[search]
    miss = K_MAX * 1.6
    x, y = c["base_rank"].fillna(miss), c["new_rank"].fillna(miss)
    jitter = np.random.default_rng(SEED).uniform(0.94, 1.06, size=(2, len(c)))
    fig, ax = plt.subplots(figsize=(6, 6))
    for change, marker in [("same", "o"), ("improved", "^"), ("regressed", "v")]:
        m = (c["change"] == change).values
        ax.scatter(x[m] * jitter[0][m], y[m] * jitter[1][m], s=40, marker=marker, color=COLOR[change],
                   edgecolor="white", linewidth=1, label=f"{change} ({m.sum()})", zorder=3)
    ax.plot([0.8, miss * 1.2], [0.8, miss * 1.2], color=COLOR["muted"], linewidth=1, linestyle="--", zorder=1)
    ticks = [1, 2, 3, 5, 10, 20, 50, miss]
    ax.set_xscale("log")
    ax.set_yscale("log")
    ax.set_xlim(0.8, miss * 1.2)
    ax.set_ylim(0.8, miss * 1.2)
    ax.set_xticks(ticks)
    ax.set_xticklabels([str(t) for t in ticks[:-1]] + ["miss"])
    ax.set_yticks(ticks)
    ax.set_yticklabels([str(t) for t in ticks[:-1]] + ["miss"])
    ax.minorticks_off()
    ax.set_xlabel("baseline: rank of the first expected document")
    ax.set_ylabel("code normalization: rank of the first expected document")
    ax.legend(loc="upper left")
    ax.set_title(f"Below the diagonal = better with code normalization ({search})", loc="left", fontsize=10)
    fig.tight_layout()
    return fig


def fig_changed_questions(search: str, limit: int = 40) -> plt.Figure | None:
    """Reciprocal rank of each question that moved, baseline to new version (dumbbell), worst regression first.

    :param search: ``index`` or ``app``
    :param limit: most questions drawn (the largest changes)
    :return: matplotlib figure, or None when no question moved
    """
    c = COMPARE[search]
    moved = c[c["change"] != "same"]
    if moved.empty:
        return None
    moved = moved.reindex(moved["delta_rr"].abs().sort_values(ascending=False).index)[:limit].sort_values("delta_rr")
    fig, ax = plt.subplots(figsize=(10, 0.32 * len(moved) + 1.3))
    y = np.arange(len(moved))
    for i, (_, r) in enumerate(moved.iterrows()):
        ax.plot([r["base_rr"], r["new_rr"]], [i, i], color=COLOR[r["change"]], linewidth=2, zorder=1)
    ax.scatter(moved["base_rr"], y, color=COLOR["baseline"], s=40, label=LABEL["baseline"], zorder=2,
               edgecolor="white", linewidth=1)
    ax.scatter(moved["new_rr"], y, color=COLOR["code_norm"], s=40, label=LABEL["code_norm"], zorder=3,
               edgecolor="white", linewidth=1)
    ax.set_yticks(y)
    ax.set_yticklabels([(q[:70] + "…") if len(q) > 70 else q for q in moved["question"]], fontsize=8)
    ax.set_xlim(-0.03, 1.03)
    ax.set_xlabel("reciprocal rank of the first expected document (1 = first, 0 = missed)")
    ax.grid(axis="y", visible=False)
    ax.legend(loc="lower right", fontsize=8)
    ax.set_title(f"Questions that moved ({search}); line colour: green improved, red regressed", loc="left",
                 fontsize=10)
    fig.tight_layout()
    return fig


def fig_no_document_scores() -> plt.Figure | None:
    """Score of the first passage for questions with an answer and for no_document questions, per version.

    :return: matplotlib figure, or None without index search results
    """
    if ("index", "baseline") not in RESULTS:
        return None
    fig, axes = plt.subplots(1, 2, figsize=(10, 3.6), sharey=True)
    for ax, v in zip(axes, VERSIONS):
        pos = [r["top_score"] for r in RESULTS[("index", v)]["positives"] if r["top_score"] is not None]
        neg = [r["top_score"] for r in RESULTS[("index", v)]["no_document"] if r["top_score"] is not None]
        data = [pos] + ([neg] if neg else [])
        rng = np.random.default_rng(SEED)
        for i, values in enumerate(data, start=1):
            q1, med, q3 = np.percentile(values, [25, 50, 75])
            ax.hlines(i, q1, q3, color=COLOR[v], linewidth=6, alpha=0.35, zorder=1)
            ax.vlines(med, i - 0.28, i + 0.28, color=COLOR["text"], linewidth=2, zorder=4)
            ax.scatter(values, rng.uniform(i - 0.2, i + 0.2, len(values)), s=16, color=COLOR[v], edgecolor="white",
                       linewidth=0.6, zorder=3)
            ax.annotate(f"median {med:.3f}", (med, i + 0.3), ha="center", fontsize=8, color=COLOR["muted"])
        ax.set_yticks(range(1, len(data) + 1))
        ax.set_yticklabels([f"positive (n={len(pos)})"] + ([f"no_document (n={len(neg)})"] if neg else []))
        ax.set_xlabel("score of the first passage")
        ax.set_title(LABEL[v], loc="left", fontsize=10)
    fig.suptitle("Can a score threshold tell 'no document' questions apart?", x=0.01, ha="left", fontsize=11)
    fig.tight_layout()
    return fig


FIGURES = {}
for s in SEARCHES:
    FIGURES[f"{s}_recall_hit_curves.png"] = fig_recall_curves(s)
    FIGURES[f"{s}_mrr_by_slice.png"] = fig_slices(s)
    FIGURES[f"{s}_rank_scatter.png"] = fig_rank_scatter(s)
    FIGURES[f"{s}_changed_questions.png"] = fig_changed_questions(s)
FIGURES["index_no_document_scores.png"] = fig_no_document_scores()
FIGURES = {k: f for k, f in FIGURES.items() if f is not None}
for fig in FIGURES.values():
    display(fig)

# COMMAND ----------

# Optional, second step: LLM-as-a-judge pairwise. Each version answers from its own top passages (index search);
# the judge sees both answers with their passages, twice in swapped order, and a version wins only when both readings
# agree. Off by default.
ANSWER_PROMPT = """You answer questions about Latecoere quality documents using ONLY the passages below.
Answer in the language of the question. Cite passages as [n]. If the passages do not contain the answer, say so.

{passages}

Question: {question}"""

JUDGE_PROMPT = """You compare two answers to the same question. Each answer was written from its own retrieved passages.
Judge which answer better serves the user: correct and faithful to ITS passages, complete, cites the right documents.
An answer that invents facts absent from its passages loses. Expected documents (ground truth): {expected}.

Question: {question}

=== Answer A ===
{answer_a}
--- Passages of A ---
{passages_a}

=== Answer B ===
{answer_b}
--- Passages of B ---
{passages_b}

Reply with JSON only: {{"winner": "A" | "B" | "tie", "faithfulness_a": 0-3, "faithfulness_b": 0-3,
"completeness_a": 0-3, "completeness_b": 0-3, "reason": "<one sentence>"}}"""


def llm(endpoint: str, prompt: str, max_tokens: int = 2000) -> str:
    """One chat completion on a serving endpoint, retried on throttling.

    :param endpoint: serving endpoint name
    :param prompt: user message
    :param max_tokens: completion cap (reasoning tokens included)
    :return: the text of the answer
    """
    for wait in (5, 20, 60, None):
        resp = HTTP.post(f"{HOST}/serving-endpoints/{endpoint}/invocations", headers=auth_headers(), timeout=300,
                         json={"messages": [{"role": "user", "content": prompt}], "max_tokens": max_tokens})
        if resp.status_code == 200:
            content = resp.json()["choices"][0]["message"]["content"]
            if isinstance(content, list):
                content = "".join(p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text")
            return content or ""
        if wait is None or resp.status_code not in (429, 500, 502, 503, 504):
            resp.raise_for_status()
        time.sleep(wait)
    return ""


def passages_block(passages: list) -> str:
    """Numbered passages as the answer model and the judge read them.

    :param passages: dicts with ``REF`` and ``chunk_text``
    :return: text block, one ``[n] (REF)`` paragraph per passage
    """
    return "\n\n".join(f"[{i + 1}] ({p['REF']})\n{(p['chunk_text'] or '')[:3000]}" for i, p in enumerate(passages))


def judge_once(question: str, expected: list, a: dict, b: dict) -> dict:
    """One judge reading.

    :param question: question
    :param expected: expected documents
    :param a: answer A with ``answer`` and ``passages``
    :param b: answer B with ``answer`` and ``passages``
    :return: the judge's JSON verdict, ``{"winner": "tie"}`` when it cannot be read
    """
    text = llm(JUDGE_ENDPOINT, JUDGE_PROMPT.format(expected=", ".join(expected), question=question,
                                                   answer_a=a["answer"], passages_a=passages_block(a["passages"]),
                                                   answer_b=b["answer"], passages_b=passages_block(b["passages"])))
    m = re.search(r"\{.*\}", text, re.S)
    try:
        return json.loads(m.group(0)) if m else {"winner": "tie", "reason": "unreadable verdict"}
    except json.JSONDecodeError:
        return {"winner": "tie", "reason": "unreadable verdict"}


def judge_question(i: int) -> dict:
    """Answers of both versions for one question and the order-swapped verdict.

    :param i: row number in ``positives``
    :return: answers, both readings and the final winner (baseline / code_norm / tie)
    """
    g = positives.iloc[i]
    res = {v: RESULTS[("index", v)]["positives"][i] for v in VERSIONS}
    ans = {v: {"passages": res[v]["passages"],
               "answer": llm(ANSWER_ENDPOINT, ANSWER_PROMPT.format(passages=passages_block(res[v]["passages"]),
                                                                    question=g["question"]))} for v in VERSIONS}
    first = judge_once(g["question"], g["expected"], ans["baseline"], ans["code_norm"])
    second = judge_once(g["question"], g["expected"], ans["code_norm"], ans["baseline"])
    w1 = {"A": "baseline", "B": "code_norm"}.get(first.get("winner"), "tie")
    w2 = {"A": "code_norm", "B": "baseline"}.get(second.get("winner"), "tie")
    return {"question_id": g["question_id"], "question": g["question"], "winner": w1 if w1 == w2 else "tie",
            "reading_1": json.dumps(first, ensure_ascii=False), "reading_2": json.dumps(second, ensure_ascii=False),
            "answer_baseline": ans["baseline"]["answer"], "answer_code_norm": ans["code_norm"]["answer"]}


PAIRWISE = None
if RUN_PAIRWISE_JUDGE:
    assert RUN_INDEX_SEARCH, "the pairwise judge reads the passages of the index search"
    candidates = [i for i in range(len(positives))
                  if not JUDGE_ONLY_CHANGED
                  or [p["REF"] for p in RESULTS[("index", "baseline")]["positives"][i]["passages"]]
                  != [p["REF"] for p in RESULTS[("index", "code_norm")]["positives"][i]["passages"]]]
    candidates = candidates[:JUDGE_MAX_QUESTIONS]
    logger.info(f"pairwise judge: {len(candidates)} questions with different passages")
    with ThreadPoolExecutor(max_workers=MAX_PARALLEL) as pool:
        PAIRWISE = pd.DataFrame(list(pool.map(judge_question, candidates)))
    display(PAIRWISE["winner"].value_counts().rename("questions").reset_index())
    display(PAIRWISE)

# COMMAND ----------

# MLflow: one parent run (the comparison) and one child run per (search, version). Child metric names never change, so
# runs of different days line up in the MLflow compare view; recall_curve / hit_curve use the step as k.


def loggable(frame: pd.DataFrame) -> pd.DataFrame:
    """A copy whose list columns are JSON strings, which mlflow.log_table accepts.

    :param frame: any frame
    :return: the copy
    """
    out = frame.copy()
    for col in out.columns:
        if out[col].map(lambda x: isinstance(x, (list, tuple, np.ndarray))).any():
            out[col] = out[col].map(lambda x: json.dumps(list(x), ensure_ascii=False)
                                    if isinstance(x, (list, tuple, np.ndarray)) else x)
    return out


def metric_key(name: str) -> str:
    """A metric name MLflow accepts (letters, digits, ``_ - . /``).

    :param name: raw name
    :return: sanitized name
    """
    return re.sub(r"[^A-Za-z0-9_\-./]", "_", name)


w.workspace.mkdirs(EXPERIMENT_PATH.rsplit("/", 1)[0])
mlflow.set_experiment(EXPERIMENT_PATH)
RUN_TS = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
dataset = mlflow.data.from_pandas(loggable(golden_raw), source=GOLDEN_TABLE, name="eval_golden_retrieval",
                                  digest=DATASET_DIGEST)
COMMON_PARAMS = {"golden_table": GOLDEN_TABLE, "dataset_digest": DATASET_DIGEST, "dataset_updated_at": DATASET_UPDATED,
                 "questions_scored": len(SCORED[(SEARCHES[0], "baseline")]), "questions_dropped_errors": len(_errored),
                 "no_document_questions": len(no_document), "skipped_questions": len(skipped),
                 "drop_unknown_refs": DROP_UNKNOWN_REFS, "index_rows": INDEX_ROWS, "hit_level": "document, language variants merged",
                 "app_code_path": APP_CODE_PATH, **CODE_FINGERPRINT}
TAGS = {"suite": "golden_retrieval", "env": "dev", "dataset_digest": DATASET_DIGEST}

with mlflow.start_run(run_name=f"retrieval baseline vs code_norm {RUN_TS}", tags={**TAGS, "role": "comparison"}) as parent:
    mlflow.log_input(dataset, context="evaluation")
    mlflow.log_params({**COMMON_PARAMS, "searches": ",".join(SEARCHES), "baseline_index": BASELINE_INDEX,
                       "new_index": NEW_INDEX, "pairwise_judge": RUN_PAIRWISE_JUDGE})
    for _, d in DELTAS.iterrows():
        key = metric_key(f"{d['search']}.{d['questions']}.delta_{d['metric']}")
        mlflow.log_metrics({key: d["delta"], f"{key}.ci95_low": d["ci95_low"], f"{key}.ci95_high": d["ci95_high"]})
    for s in SEARCHES:
        counts = COMPARE[s]["change"].value_counts()
        mlflow.log_metrics({f"{s}.improved": int(counts.get("improved", 0)),
                            f"{s}.regressed": int(counts.get("regressed", 0)), f"{s}.same": int(counts.get("same", 0))})
        mlflow.log_table(loggable(COMPARE[s]), f"tables/{s}_comparison.json")
        mlflow.log_table(loggable(COMPARE[s][COMPARE[s]["change"] == "regressed"]), f"tables/{s}_regressed.json")
        mlflow.log_table(loggable(COMPARE[s][COMPARE[s]["change"] == "improved"]), f"tables/{s}_improved.json")
        mlflow.log_table(SLICE_TABLES[s], f"tables/{s}_slices.json")
    mlflow.log_table(SUMMARY, "tables/summary.json")
    mlflow.log_table(DELTAS, "tables/deltas_bootstrap.json")
    for name, fig in FIGURES.items():
        mlflow.log_figure(fig, f"figures/{name}")
    if PAIRWISE is not None:
        n = len(PAIRWISE)
        mlflow.log_metrics({f"pairwise.{k}_rate": float((PAIRWISE["winner"] == k).mean()) if n else 0.0
                            for k in ("code_norm", "baseline", "tie")} | {"pairwise.questions": n})
        mlflow.log_params({"answer_endpoint": ANSWER_ENDPOINT, "judge_endpoint": JUDGE_ENDPOINT,
                           "answer_top_passages": ANSWER_TOP_PASSAGES, "judge_only_changed": JUDGE_ONLY_CHANGED})
        mlflow.log_table(PAIRWISE, "tables/pairwise_judge.json")

    for s, v in SCORED:
        f = SCORED[(s, v)]
        with mlflow.start_run(run_name=f"{s} / {v}", nested=True,
                              tags={**TAGS, "role": "version", "search": s, "version": v}):
            mlflow.log_input(dataset, context="evaluation")
            params = {**COMMON_PARAMS, "search": s, "version": v, "index": INDEX_OF[v],
                      "query_code_normalization": v == "code_norm",
                      "embedded_column": "chunk_preprocessed" if v == "code_norm" else "chunk_text",
                      "keyword_columns": "all synced, chunk_preprocessed and cited_codes included" if v == "code_norm"
                      else "chunk_text, semantic_headers, REF, url"}
            if s == "index":
                params.update({"num_results": INDEX_NUM_RESULTS, "query_type": "HYBRID",
                               "reranker": json.dumps(RERANKER)})
            else:
                params.update({k.lower(): (val or "")[:500] for k, val in APP_SETTINGS.items()})
                params["app_code_normalization_steps"] = ("query, citation lookup, language variant choice"
                                                          if v == "code_norm" else "all off in memory")
            mlflow.log_params(params)
            mlflow.log_metrics({metric_key(k): val for k, val in summary(f).items() if not np.isnan(val)})
            for k in range(1, K_MAX + 1):
                mlflow.log_metric("recall_curve", float(f[f"recall_at_{k}"].mean()), step=k)
                mlflow.log_metric("hit_curve", float(f[f"hit_at_{k}"].mean()), step=k)
            for sl in SLICES:
                for value, g in f.groupby(sl):
                    mlflow.log_metrics({metric_key(f"slice.{sl}.{value}.mrr"): float(g["rr"].mean()),
                                        metric_key(f"slice.{sl}.{value}.recall_at_10"): float(g["recall_at_10"].mean()),
                                        metric_key(f"slice.{sl}.{value}.n"): len(g)})
            if s == "index":
                scores = [r["top_score"] for r in RESULTS[(s, v)]["no_document"] if r["top_score"] is not None]
                if scores:
                    mlflow.log_metric("no_document.top_score_median", float(np.median(scores)))
                    mlflow.log_metric("positive.top_score_median", float(f["top_score"].median()))
            cols = ["question_id", "question", "lang", "division", "has_code", "expected_bucket", "expected", "unknown",
                    "first_rank", "rr", "recall_at_5", "recall_at_10", "recall_at_50", "hit_at_10", "ndcg_at_10",
                    "retrieved_top10", "n_docs", "query", "top_score", "latency_s"]
            mlflow.log_table(loggable(f[cols]), "tables/per_question.json")

logger.info(f"MLflow: experiment {EXPERIMENT_PATH}, parent run {parent.info.run_id} "
            f"({HOST}/ml/experiments/{parent.info.experiment_id}/runs/{parent.info.run_id})")
