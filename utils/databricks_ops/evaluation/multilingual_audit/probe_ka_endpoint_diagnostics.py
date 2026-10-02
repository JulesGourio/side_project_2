"""Diagnose ka-0c98558f-endpoint (the two-source AS/IS Knowledge Assistant
build): the original hypothesis was "routing between the AS source and the
IS source looks random, maybe a concurrency race". This script fires the
identical question repeatedly (sequential AND concurrent) and inspects the
MLflow trace embedded in the response (`databricks_options.return_trace`) to
see whether the 'docs' span (real retrieval) and 'examples' span (KA's
internal few-shot retriever) behave consistently or flip across calls.

Run standalone: .venv/Scripts/python.exe probe_ka_endpoint_diagnostics.py [PROFILE]

Findings from the 2026-07-21 run are recorded in ../README.md — in short: NOT
random. The 'docs' span returned [] on every single call (AS-only, IS-only,
both, division-tagged or not, sequential or concurrent — 20+ trials), and the
'examples' span threw the same internal error every time:
    "AI Search endpoint 5fc54fba-96bb-4de3-9b3b-3d5483d8f4ab not found."
That ID does not match the workspace's only Vector Search endpoint
(`databricks vector-search-endpoints list-endpoints` -> id
2319d024-bb16-4b2c-a9f7-b21e6b6fbd30, name "qualibot") — it's a dangling
reference to a resource that isn't there, not a live race condition.

UPDATE 2026-07-21 (same day): traced the cause — ka-0c98558f-endpoint's two
knowledge sources pointed at `src_chunks_as_index` / `src_chunks_is_index`,
index names that no longer exist (superseded by the `_v2` rebuild). A fresh
Knowledge Assistant was created with the current `_v2` indexes
(ka-087d89b6-endpoint, qualibot_test_routing_3idx — TEMPORARY, see
../README.md) and DOES return real results. Testing that working endpoint
surfaced a different, more serious problem: routing between AS/IS/ALL
sources is unreliable per-call (not concurrency-related) — see
probe_ka_routing_consistency.py.
"""
import sys
import json
import re
import time
import urllib.parse
import asyncio

import httpx
from databricks.sdk.core import Config

PROFILE = sys.argv[1] if len(sys.argv) > 1 else "UAT"
AGENT_ENDPOINT = "ka-0c98558f-endpoint"

_cfg = Config(profile=PROFILE)
_auth = _cfg.authenticate()
HOST = _cfg.host.rstrip("/")
HEADERS = {"Authorization": _auth["Authorization"], "Content-Type": "application/json"}

QUESTIONS = {
    "AS_explicit": "[Division: AS]\n\nQuelles sont les règles de gestion des entrepôts (stamp/tampon) ?",
    "IS_explicit": "[Division: IS]\n\nQuelles sont les règles de fonctionnement en salle propre ?",
}


def _extract_sources(content_items):
    sources, seen = [], set()
    for item in content_items:
        for ann in item.get("annotations", []) or []:
            if ann.get("type") != "url_citation":
                continue
            url = ann.get("url", "")
            parsed = urllib.parse.urlparse(url)
            ref = urllib.parse.parse_qs(parsed.query).get("ref", [""])[0]
            m = re.search(r"\[Source:\s*([^|]+)\|\s*Title:\s*([^|]+)\|\s*Division:\s*([^|]+)\|",
                          urllib.parse.unquote(url))
            division = m.group(3).strip() if m else ""
            key = ref or url
            if key in seen:
                continue
            seen.add(key)
            sources.append({"ref": ref, "division": division})
    return sources


def _span_summary(data):
    spans = data.get("databricks_output", {}).get("trace", {}).get("data", {}).get("spans", [])
    out = []
    for sp in spans:
        exc = None
        for ev in sp.get("events", []) or []:
            if ev.get("name") == "exception":
                exc = ev.get("attributes", {}).get("exception.message")
        out.append((sp.get("name"), sp.get("attributes", {}).get("mlflow.spanOutputs"), exc))
    return out


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
        return {"error": f"{type(e).__name__}: {e}", "duration_s": round(time.monotonic() - start, 2)}
    answer, sources = "", []
    for out in data.get("output", []):
        if out.get("type") != "message":
            continue
        for item in out.get("content", []) or []:
            if item.get("type") == "output_text":
                answer += item.get("text", "")
        sources += _extract_sources(out.get("content", []) or [])
    return {
        "sources_used": data.get("custom_outputs", {}).get("sources_used"),
        "sources": sources,
        "spans": _span_summary(data),
        "duration_s": round(time.monotonic() - start, 2),
        "error": None,
    }


async def main():
    async with httpx.AsyncClient() as client:
        print("=" * 90)
        print("SEQUENTIAL: each question x3")
        for label, q in QUESTIONS.items():
            for i in range(3):
                r = await call(client, q)
                docs_out = next((out for name, out, _exc in r.get("spans", []) if name == "docs"), None)
                examples_exc = next((exc for name, _out, exc in r.get("spans", []) if name == "examples"), None)
                print(f"[{label} #{i+1}] sources_used={r.get('sources_used')} docs_span={docs_out} "
                      f"examples_error={examples_exc} duration={r['duration_s']}s")

        print("\n" + "=" * 90)
        print("CONCURRENT: both questions x3, fired together (asyncio.gather)")
        batch = list(QUESTIONS.items()) * 3
        results = await asyncio.gather(*[call(client, q) for _, q in batch])
        for (label, _q), r in zip(batch, results):
            docs_out = next((out for name, out, _exc in r.get("spans", []) if name == "docs"), None)
            examples_exc = next((exc for name, _out, exc in r.get("spans", []) if name == "examples"), None)
            print(f"[{label}] sources_used={r.get('sources_used')} docs_span={docs_out} "
                  f"examples_error={examples_exc} duration={r['duration_s']}s")


if __name__ == "__main__":
    asyncio.run(main())
