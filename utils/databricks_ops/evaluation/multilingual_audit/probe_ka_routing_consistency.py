"""Quantify how consistently a multi-source Knowledge Assistant routes a
division-tagged question to the RIGHT source, across repeated identical
calls (sequential and concurrent).

Written for ka-087d89b6-endpoint (qualibot_test_routing_3idx — a 2026-07-21
rebuild of qualibot_assistant2 with 3 sources: chunks_as_index_v2,
chunks_is_index_v2, chunks_index_v2/ALL), but works against any KA endpoint
that supports the same `[Division: AS]` / `[Division: IS]` tag convention
and returns a trace via `databricks_options.return_trace`.

Run standalone: .venv/Scripts/python.exe probe_ka_routing_consistency.py [ENDPOINT] [PROFILE]

Findings from the 2026-07-21 run against ka-087d89b6-endpoint, 32 calls
(16 sequential + 16 concurrent), are recorded in ../README.md — in short:
for AS-tagged questions the agent queried the WRONG source (pure IS
division docs, zero AS) in 10/16 calls (62.5%), and this rate is *not*
concurrency-driven — it happens just as often sequentially (6/8) as
concurrently (4/8). Source selection between AS/IS/ALL is unreliable
per-call, not a race condition.
"""
import sys
import json
import time
import asyncio
import httpx
from databricks.sdk.core import Config

AGENT_ENDPOINT = sys.argv[1] if len(sys.argv) > 1 else "ka-087d89b6-endpoint"
PROFILE = sys.argv[2] if len(sys.argv) > 2 else "UAT"
N_PER_MODE = 8  # calls per question per mode (sequential / concurrent)

_cfg = Config(profile=PROFILE)
_auth = _cfg.authenticate()
HOST = _cfg.host.rstrip("/")
HEADERS = {"Authorization": _auth["Authorization"], "Content-Type": "application/json"}

QUESTIONS = {
    "AS_explicit": "[Division: AS]\n\nQuelles sont les règles de gestion des entrepôts (stamp/tampon) ?",
    "IS_explicit": "[Division: IS]\n\nQuelles sont les règles de fonctionnement en salle propre ?",
}


def _docs_summary(data):
    spans = data.get("databricks_output", {}).get("trace", {}).get("data", {}).get("spans", [])
    for sp in spans:
        if sp.get("name") == "docs":
            raw = sp.get("attributes", {}).get("mlflow.spanOutputs")
            try:
                docs = json.loads(raw) if raw else []
            except json.JSONDecodeError:
                return []
            return [(d.get("metadata", {}).get("doc_source"), d.get("metadata", {}).get("division")) for d in docs]
    return []


async def call(client, question):
    start = time.monotonic()
    url = f"{HOST}/serving-endpoints/{AGENT_ENDPOINT}/invocations"
    payload = {"input": [{"role": "user", "content": question}], "stream": False,
               "databricks_options": {"return_trace": True}}
    try:
        resp = await client.post(url, json=payload, headers=HEADERS, timeout=90)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        return {"docs": [], "error": f"{type(e).__name__}: {e}", "duration_s": round(time.monotonic() - start, 2)}
    return {"docs": _docs_summary(data), "error": None, "duration_s": round(time.monotonic() - start, 2)}


def classify(expected_div, docs):
    """correct-source / used-ALL (safe fallback) / WRONG-SOURCE (the bug) / EMPTY / mixed."""
    if not docs:
        return "EMPTY"
    sources = set(ds for ds, _div in docs)
    divs = set(div for _ds, div in docs if div)
    # Suffix-agnostic (index suffix has moved _v2 -> _v1 since this script was
    # written; hardcoding it silently miscategorized every correct/ALL call as
    # "mixed" instead, without erroring — caught 2026-09-04 rerunning against
    # a KA rebuilt on _v1 indexes).
    if all(s.startswith(f"chunks_{expected_div.lower()}_index") for s in sources):
        return "correct-source"
    if all(s.startswith("chunks_index") and not s.startswith(("chunks_as_", "chunks_is_")) for s in sources):
        return "used-ALL"
    wrong_div = "AS" if expected_div == "IS" else "IS"
    if divs == {wrong_div}:
        return "WRONG-SOURCE"
    return f"mixed({sorted(sources)})"


async def run_batch(client, mode, n, tally):
    if mode == "sequential":
        results = []
        for label, q in QUESTIONS.items():
            for _ in range(n):
                results.append((label, await call(client, q)))
    else:
        batch = list(QUESTIONS.items()) * n
        calls = await asyncio.gather(*[call(client, q) for _, q in batch])
        results = list(zip([label for label, _ in batch], calls))
    for label, r in results:
        expected_div = "AS" if label == "AS_explicit" else "IS"
        verdict = classify(expected_div, r["docs"]) if not r["error"] else f"ERROR:{r['error']}"
        tally[(mode, label, verdict)] = tally.get((mode, label, verdict), 0) + 1
        print(f"[{mode}/{label}] verdict={verdict} n_docs={len(r['docs'])} duration={r['duration_s']}s")


async def main():
    async with httpx.AsyncClient() as client:
        tally = {}
        print(f"Endpoint: {AGENT_ENDPOINT}\n{'=' * 90}\nSEQUENTIAL x{N_PER_MODE} per question")
        await run_batch(client, "sequential", N_PER_MODE, tally)
        print(f"\n{'=' * 90}\nCONCURRENT x{N_PER_MODE} per question (fired together)")
        await run_batch(client, "concurrent", N_PER_MODE, tally)
        print(f"\n{'=' * 90}\nTALLY")
        for key, count in sorted(tally.items()):
            print(f"{key}: {count}")


if __name__ == "__main__":
    asyncio.run(main())
