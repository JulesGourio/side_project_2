# Databricks notebook source
# MAGIC %md
# MAGIC # Qualibot — Évaluation golden du retrieval, PROD (lecture seule)
# MAGIC
# MAGIC Même calcul que le notebook DEV (`eval_retrieval_mlflow.py`) : chaque question du jeu golden passe par le vrai
# MAGIC `retrieve_for_turn` de l'app PROD. Aucune réponse n'est générée.
# MAGIC
# MAGIC **Différences avec la version DEV**
# MAGIC - Aucune expérience MLflow, aucun run, aucune trace.
# MAGIC - Les questions golden sont dans le notebook (`GOLD_ROWS`), pas lues dans une table.
# MAGIC - Aucune écriture : ni table, ni volume, ni fichier. Les résultats sont seulement affichés.
# MAGIC - Pas de widgets : les paramètres sont codés en dur.
# MAGIC
# MAGIC **Scores** (documents distincts dans l'ordre envoyé au LLM, questions appariées uniquement)
# MAGIC - **Recall@K** : part des documents attendus parmi les K premiers.
# MAGIC - **MRR@K** : 1 / rang du premier document attendu (0 si manqué).
# MAGIC - **AllRefs@K** : part des questions dont tous les documents attendus sont dans les K premiers.
# MAGIC - **@all** : la même chose sur tout le contexte envoyé. Un K au-dessus du nombre moyen de documents envoyés affiche `n/a`.

# COMMAND ----------
# MAGIC %md
# MAGIC ## Dépendances de l'app PROD
# MAGIC Le code de l'app est lu dans `/Workspace/.bundle/qualibot/prod/files` (lecture seule).

# COMMAND ----------
# MAGIC %pip install -q -r /Workspace/.bundle/qualibot/prod/files/requirements.txt fastapi pyyaml "databricks-sdk>=0.102" tqdm

# COMMAND ----------
dbutils.library.restartPython()

# COMMAND ----------
import asyncio
import inspect
import json
import logging
import os
import re
import shlex
import sys
import threading
import time
import traceback
from concurrent.futures import as_completed

import pandas as pd
import yaml
from databricks.sdk import WorkspaceClient
from tqdm.auto import tqdm

log = logging.getLogger("qualibot_retrieval_golden_prod")
log.setLevel(logging.INFO)
if not log.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    log.addHandler(handler)
log.propagate = False

# COMMAND ----------
# MAGIC %md
# MAGIC ## Paramètres (codés en dur, PROD)
# MAGIC | Paramètre | Valeur | Rôle |
# MAGIC |---|---|---|
# MAGIC | `APP` | `/Workspace/.bundle/qualibot/prod/files` | code de l'app PROD, lu seulement |
# MAGIC | `CHUNKS_TABLE` | `prod_proj.qualibot.chunks` | citations exactes (`cited_codes`), lecture seule |
# MAGIC | `INDEXES` | `chat` | l'app PROD telle que déployée (son `CHAT_INDEX`) |
# MAGIC | `KS` | `1,3,5,10,20,50` | K de Recall@K, MRR@K, AllRefs@K |
# MAGIC | `MAX_PARALLEL` | `1` | questions cherchées en même temps |
# MAGIC | `DEBUG` | `False` | journal de chaque tentative échouée |
# MAGIC
# MAGIC Options de `INDEXES` (même syntaxe que DEV) : voir la cellule suivante.

# COMMAND ----------
# Fixed parameters. Nothing is written: no table, no volume, no MLflow experiment.
APP = "/Workspace/.bundle/qualibot/prod/files"
CHUNKS_TABLE = "prod_proj.qualibot.chunks"
INDEXES = "chat"
KS = sorted({1, 3, 5, 10, 20, 50})
KS_ALL = [*KS, "all"]
MAX_PARALLEL = 1
DEBUG = False

if not os.path.isfile(f"{APP}/app.yaml"):
    raise RuntimeError(f"PROD app code not found in {APP}: deploy the PROD bundle first")
log.info("PROD retrieval evaluation | app=%s | indexes=%s | ks=%s", APP, INDEXES, KS)

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


CONFIGS = parse_configs(INDEXES)
RUN = list(CONFIGS)

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
# MAGIC Questions copiées de la table DEV `dev_proj.qualibot.eval_golden_retrieval` dans `GOLD_ROWS`.
# MAGIC Les questions `no_document` / `to_complete` sans document attendu sont ignorées. Les conflits
# MAGIC avec `unknown_refs` sont gardés dans les résultats bruts mais exclus des scores.

# COMMAND ----------
# One dict per golden question, same fields as the DEV table (paste the export of the DEV cell here).
GOLD_ROWS = [
    # {"question_id": "...", "question": "...", "expected_refs": ["..."], "unknown_refs": [],
    #  "division": "...", "lang": "fr"},
]
if not GOLD_ROWS:
    raise ValueError("GOLD_ROWS is empty: paste the golden questions first")

CASES, SKIPPED = [], []
for r in GOLD_ROWS:
    question = str(r.get("question") or "").strip()
    expected = refs(r.get("expected_refs"))
    if not question or not expected:
        SKIPPED.append(str(r.get("question_id")))
        continue
    overlap = sorted(set(expected) & set(refs(r.get("unknown_refs"))))
    CASES.append({
        "case_id": str(r["question_id"]),
        "question": question,
        "division": str(r.get("division") or "ALL").upper(),
        "expected": expected,
        "unknown_overlap": overlap,
        "score_eligible": not bool(overlap),
    })

if len({c["case_id"] for c in CASES}) != len(CASES):
    raise ValueError("Duplicate question_id in GOLD_ROWS")
if not CASES:
    raise ValueError("No golden questions with expected_refs")
ELIGIBLE = [c for c in CASES if c["score_eligible"]]
if not ELIGIBLE:
    raise ValueError("No eligible golden questions: review unknown_refs/expected_refs")
log.info("Golden: %d total | %d with expected refs | %d eligible | %d unknown conflicts",
         len(GOLD_ROWS), len(CASES), len(ELIGIBLE), len(CASES) - len(ELIGIBLE))

# COMMAND ----------
# MAGIC %md
# MAGIC ## Retrieval with a persistent asyncio loop
# MAGIC Ce bloc ne change pas : même boucle, mêmes reprises, même traduction que l'app.

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
# MAGIC ## Scores at each K

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



# COMMAND ----------
# MAGIC %md
# MAGIC ## Execute each configuration (no write)
# MAGIC Résultats gardés en mémoire seulement. Une erreur reste une ligne en erreur, jamais un zéro.

# COMMAND ----------
ALL_ROWS = []
retrieval_loop = get_retrieval_loop()
with tqdm(total=len(RUN) * len(CASES), desc="Golden retrieval", unit="question") as progress:
    for name in RUN:
        apply_config(CONFIGS[name])
        log.info("[%s] index=%s | cases=%d | concurrency=%d", name,
                 CONFIGS[name]["index"], len(CASES), MAX_PARALLEL)
        progress.set_description(f"Golden | {name}")
        semaphore = asyncio.run_coroutine_threadsafe(
            make_semaphore(MAX_PARALLEL), retrieval_loop
        ).result()
        futures = {
            asyncio.run_coroutine_threadsafe(run_case(case, semaphore), retrieval_loop): i
            for i, case in enumerate(CASES)
        }
        outputs = [None] * len(CASES)
        for future in as_completed(futures):
            i = futures[future]
            try:
                outputs[i] = future.result()
            except Exception as exc:
                outputs[i] = {"error": error_description(exc)}
                log.exception("Unexpected runner error case=%s", CASES[i]["case_id"])
            progress.update(1)

        for case, result in zip(CASES, outputs):
            error = result.get("error")
            retrieved = refs(result.get("retrieved") or [])
            ALL_ROWS.append({
                "config": name,
                "config_spec": CONFIGS[name]["spec"],
                "case_id": case["case_id"],
                "question": case["question"],
                "division": case["division"],
                "expected": case["expected"],
                "unknown_overlap": case["unknown_overlap"],
                "score_eligible": case["score_eligible"],
                "retrieved": retrieved,
                "n_docs": len(retrieved),
                "n_passages": result.get("n_passages"),
                "chars": result.get("chars"),
                "latency_s": result.get("latency_s"),
                "error": error,
            })
        log.info("[%s] questions=%d | errors=%d", name, len(CASES),
                 sum(bool(r["error"]) for r in ALL_ROWS if r["config"] == name))

# COMMAND ----------
# MAGIC %md
# MAGIC ## Paired relevance scores + technical metrics
# MAGIC - `n_questions` : questions appariées réussies, utilisées pour Recall, MRR et AllRefs.
# MAGIC - `success_fraction` : questions éligibles réussies / questions éligibles.
# MAGIC - `search_p95_s` : latence P95 de bout en bout (traduction, recherche, reprises incluses).
# MAGIC - `n_docs`, `n_passages`, `chars` : moyennes arithmétiques sur les questions réussies.

# COMMAND ----------
EVAL_ROWS = ALL_ROWS

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
# MAGIC ## Expected documents missing from the top 10, and errors

# COMMAND ----------
K_MISS = 10 if 10 in KS else max(KS)
misses = paired[paired[f"recall_at_{K_MISS}"] < 1].copy()
if not misses.empty:
    k = K_MISS
    misses["missing_expected"] = misses.apply(
        lambda r: [ref for ref in r["expected"] if ref not in set(refs(r["retrieved"])[:k])], axis=1
    )
    display(misses[["config", "question", "expected", "missing_expected", f"recall_at_{k}"]]
            .sort_values(["config", f"recall_at_{k}"]))

error_rows = pdf[pdf["error"].notna()]
if not error_rows.empty:
    display(error_rows[["config", "question", "error"]])

log.info("Golden retrieval completed | rows=%d | paired=%d | skipped=%d",
         len(ALL_ROWS), len(common_ids), len(SKIPPED))

# COMMAND ----------
# MAGIC %md
# MAGIC ## Comparison of every configuration
# MAGIC Sur les mêmes questions appariées que ci-dessus. La première configuration est la référence.

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


def scoreboard_table(reference: str) -> pd.DataFrame:
    """One row per configuration: relevance at each K, latency, context size, and the deltas with the reference.

    :param reference: label of the reference configuration
    :return: the scoreboard, configurations in the order of ``indexes``
    """
    rows = []
    for name in RUN:
        s = summary[summary["config"] == name].iloc[0]
        row = {"config": name, "spec": CONFIGS[name]["spec"], "questions": int(s["n_questions"])}
        # A K above the documents the configuration sends would score the whole context again: n/a.
        sent = s["n_docs"]
        row.update({f"Recall@{k}": s[f"recall_at_{k}"] if k <= sent else float("nan") for k in KS})
        row["Recall@all"] = s["recall_at_all"]
        row.update({f"AllRefs@{k}": s[f"all_refs_at_{k}"] if k <= sent else float("nan")
                    for k in KS if k in (5, K_MAIN)})
        row["AllRefs@all"] = s["all_refs_at_all"]
        row["MRR@all"] = s["mrr_at_all"]
        row.update({"p95 latency (s)": s["search_p95_s"], "documents": s["n_docs"], "passages": s["n_passages"],
                    "characters": s["chars"]})
        rows.append(row)
    board = pd.DataFrame(rows)
    ref = board[board["config"] == reference].iloc[0]
    for column in (f"Recall@{min(5, max(KS))}", f"Recall@{K_MAIN}", "Recall@all", "MRR@all"):
        if column in board:
            board[f"Δ {column}"] = board[column] - ref[column]
    return board


def styled(board: pd.DataFrame):
    """Scoreboard with percentages, deltas in green / red and the best value of each metric in bold.

    :param board: output of ``scoreboard_table``
    :return: pandas Styler
    """
    share = [c for c in board.columns if c.startswith(("Recall@", "AllRefs@"))]
    delta = [c for c in board.columns if c.startswith("Δ")]
    fmt = {c: "{:.1%}" for c in share}
    fmt.update({c: "{:+.1%}" if "MRR" not in c else "{:+.3f}" for c in delta})
    fmt.update({"MRR@all": "{:.3f}", "p95 latency (s)": "{:.1f}", "documents": "{:.1f}",
                "passages": "{:.1f}", "characters": "{:,.0f}"})

    def sign(value: float) -> str:
        """CSS color of a delta.

        :param value: the delta
        :return: CSS declaration
        """
        return "" if pd.isna(value) or abs(value) < 1e-9 else f"color: {'#0ca30c' if value > 0 else '#d03b3b'}"

    style = (board.style.format(fmt, na_rep="n/a").hide(axis="index")
             .highlight_max(subset=share + ["MRR@all"], props="font-weight: bold"))
    return (style.map if hasattr(style, "map") else style.applymap)(sign, subset=delta)


def ranks_table() -> pd.DataFrame:
    """Rank of the first expected document per question (rows) and configuration (columns), None = missed.

    :return: rows ``case_id``, ``question``, ``lang``, ``has_code``, ``expected`` and one column per configuration
    """
    golden_by_id = {str(r["question_id"]): r for r in GOLD_ROWS}
    base = paired[paired["config"] == RUN[0]][["case_id", "question", "expected"]].copy()
    base["lang"] = base["case_id"].map(lambda c: str(golden_by_id.get(c, {}).get("lang") or "unknown").lower())
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


def moved_table(ranks: pd.DataFrame, reference: str) -> pd.DataFrame:
    """Questions whose first expected document moved, per configuration against the reference, largest move first.

    :param ranks: output of ``ranks_table``
    :param reference: label of the reference configuration
    :return: rows ``config``, ``change``, ``question``, ``expected``, reference rank, configuration rank
    """
    rows = []
    for name in RUN:
        if name == reference:
            continue
        for _, r in ranks.iterrows():
            delta = reciprocal(r[name]) - reciprocal(r[reference])
            if abs(delta) > 1e-12:
                rows.append({"config": name, "change": "improved" if delta > 0 else "regressed",
                             "question": r["question"], "lang": r["lang"], "expected": ", ".join(r["expected"]),
                             f"rank {reference}": r[reference], "rank": r[name], "delta_rr": delta})
    moved = pd.DataFrame(rows)
    if moved.empty:
        return moved
    return moved.sort_values(["config", "delta_rr"], key=lambda s: s if s.name == "config" else -s.abs())


if not common_ids:
    log.info("Comparison skipped: no paired question")
else:
    REFERENCE = RUN[0]
    board = scoreboard_table(REFERENCE)
    ranks = ranks_table()
    slice_fig, slices = slice_figure(ranks)
    moved = moved_table(ranks, REFERENCE)
    counts = (moved.groupby(["config", "change"]).size().unstack(fill_value=0).reindex(RUN[1:]).fillna(0).astype(int)
              if not moved.empty else pd.DataFrame())

    display(styled(board))
    for fig in (curve_figure(), rank_figure(ranks), slice_fig):
        display(fig)
    if not counts.empty:
        display(counts.reset_index())
        display(moved.drop(columns="delta_rr"))
# Figures still open at the end of a cell are drawn a second time by Databricks.
plt.close("all")
