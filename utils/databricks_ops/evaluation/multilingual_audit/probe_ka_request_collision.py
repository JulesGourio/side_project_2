"""Reproduce (or attempt to) the KA-internal Vector Search failures already
tracked in server/services/streaming.py's production log line:

    if 'Vector search failed' in delta or 'already running' in delta:
        logger.warning('stream_chat KA retrieval error on %s: %s', ...)

Two related but distinct symptoms have been observed against this same
string match, both surfaced via SSE `response.reasoning_summary_text.delta`
events, both on ka-7679a56e-endpoint (the real production ALL_v2 endpoint):

  - "Request id <uuid>-0 already running" — diagnosed 2026-06-25 (see memory
    is-source-vector-search-race): the KA decomposes a complex question into
    parallel internal sub-queries for a SINGLE turn and reuses the same
    request id across them, so they self-collide. "Resolved" at the time by
    moving to per-division single-source KA endpoints — but a fresh
    production log from 2026-07-16 shows it recurring on today's
    single-source ka-7679a56e-endpoint, so the fix did not fully hold.
  - "Request is rejected due to heavy load. Please retry with backoff." —
    a genuine throughput/rate-limit response from the underlying Vector
    Search backend, reproduced below under concurrent load.

Run standalone: .venv/Scripts/python.exe probe_ka_request_collision.py [PROFILE]

Findings from the 2026-07-21 runs: SEQUENTIAL calls (one at a time, no
concurrency from this script) never triggered either error, across two
rounds of 6 calls with multi-part questions designed to force internal
decomposition. The SAME questions fired 6-way CONCURRENT triggered "Vector
search failed" (heavy-load flavor) on 4/6 calls in one run — then 0/6 in an
immediate re-run. That inconsistency is itself informative: it's consistent
with genuine, load-dependent throttling on the shared production endpoint
(present when something else is also busy, absent otherwise), not a
deterministic bug reproducible on demand. This does not prove the two
flavors ("heavy load" vs. the "-0 already running" self-collision from
2026-06-25 / memory is-source-vector-search-race) share a root cause — it
shows both are real, both get caught by the same log match in production,
and neither is something a customer-side script can reliably force. Worth
asking Databricks directly rather than guessing further.
"""
import sys
import json
import asyncio
import httpx
from databricks.sdk.core import Config

PROFILE = sys.argv[1] if len(sys.argv) > 1 else "UAT"
AGENT_ENDPOINT = "ka-7679a56e-endpoint"  # real production endpoint — keep concurrency modest
N_CONCURRENT = 6

_cfg = Config(profile=PROFILE)
_auth = _cfg.authenticate()
HOST = _cfg.host.rstrip("/")
HEADERS = {"Authorization": _auth["Authorization"], "Content-Type": "application/json"}

# Multi-part questions, likely to make the KA decompose into several
# internal parallel sub-queries (the condition the 2026-06-25 diagnosis
# ties to the request-id self-collision).
MULTI_PART_QUESTIONS = [
    "Quels documents référencent à la fois les exigences de traçabilité des lots, "
    "les procédures de non-conformité produit, et les qualifications d'opérateurs en "
    "contrôle non destructif ?",
    "What is the approval process for engineering deviations, which procedures cover "
    "supplier process changes, and what are the cleanroom PPE requirements?",
    "Compare les règles de gestion des entrepôts, les règles de qualification qualité, "
    "et les règles de traçabilité des non-conformités entre AS et IS.",
]


async def probe(client, question):
    url = f"{HOST}/serving-endpoints/{AGENT_ENDPOINT}/invocations"
    payload = {"input": [{"role": "user", "content": question}], "stream": True,
               "databricks_options": {"return_trace": True}}
    hits = []
    try:
        async with client.stream("POST", url, json=payload, headers=HEADERS, timeout=90) as resp:
            if resp.status_code != 200:
                body = await resp.aread()
                return {"error": f"HTTP {resp.status_code}: {body[:300]}", "hits": []}
            async for line in resp.aiter_lines():
                if not line.startswith("data: "):
                    continue
                raw = line[len("data: "):]
                if raw.strip() == "[DONE]":
                    break
                try:
                    chunk = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if chunk.get("type") == "response.reasoning_summary_text.delta":
                    delta = chunk.get("delta", "")
                    if "Vector search failed" in delta or "already running" in delta:
                        hits.append(delta.strip()[:300])
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}", "hits": []}
    return {"error": None, "hits": hits}


async def run_sequential(client, n):
    print(f"--- SEQUENTIAL x{n} (no concurrency from this script) ---")
    for i in range(n):
        q = MULTI_PART_QUESTIONS[i % len(MULTI_PART_QUESTIONS)]
        r = await probe(client, q)
        label = f"COLLISION: {r['hits']}" if r["hits"] else (r["error"] or "clean")
        print(f"[seq {i + 1}] {label}")


async def run_concurrent(client, n):
    print(f"\n--- CONCURRENT x{n} (fired together) ---")
    batch = (MULTI_PART_QUESTIONS * ((n // len(MULTI_PART_QUESTIONS)) + 1))[:n]
    results = await asyncio.gather(*[probe(client, q) for q in batch])
    collisions = 0
    for i, r in enumerate(results):
        if r["error"]:
            print(f"[conc {i + 1}] ERROR: {r['error']}")
        elif r["hits"]:
            collisions += 1
            print(f"[conc {i + 1}] COLLISION: {r['hits']}")
        else:
            print(f"[conc {i + 1}] clean")
    print(f"\n{collisions}/{len(results)} concurrent calls showed a Vector Search failure.")


async def main():
    async with httpx.AsyncClient() as client:
        await run_sequential(client, N_CONCURRENT)
        await run_concurrent(client, N_CONCURRENT)


if __name__ == "__main__":
    asyncio.run(main())
