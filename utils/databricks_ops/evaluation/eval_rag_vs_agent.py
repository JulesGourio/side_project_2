# Databricks notebook source
# MAGIC %md
# MAGIC # RAG comparison -- Local RAG (Index + LLM) vs Agent (All_v2)
# MAGIC
# MAGIC Compare a small local RAG (Vector Search + `databricks-gemini-3-1-flash-lite`) against
# MAGIC the existing Knowledge Assistant agent (`ka-7679a56e-endpoint`). Edit the params below, run all.

# COMMAND ----------

# -- Params --
VECTOR_SEARCH_INDEX = "uat_landingzone.qualibot.chunks_index_v2"
NUM_CHUNKS = 8
CHUNK_EXCERPT_CHARS = 1500
LOCAL_RAG_LLM_ENDPOINT = "databricks-gemini-3-1-flash-lite"
LOCAL_RAG_MAX_OUTPUT_TOKENS = 2000  # thinking model -- needs headroom for reasoning_tokens
LOCAL_RAG_TEMPERATURE = 0.0
AGENT_ENDPOINT = "ka-7679a56e-endpoint"
AGENT_TIMEOUT_S = 90.0
TEST_QUESTIONS = [
    "comment dois-je faire pour réparer une carte relais sur un testeur électrique LATE4000 ?",
]
OUTPUT_HTML_PATH = r"C:\Users\L0041770\Desktop\GenAI\Qualibot\latec-compare\output_rag\respose.html"  # None to skip saving


# COMMAND ----------

# -- Auth --
# Bypasses databricks-sdk's Config() auto-detection entirely (it kept picking up
# stray/leaked env vars depending on the kernel). Instead: read the host straight
# from ~/.databrickscfg's [UAT] profile, and mint a token via the `databricks` CLI
# directly (`databricks auth token --profile UAT`) -- this is what the CLI itself
# uses and is unaffected by any Python-side env pollution.
import configparser
import json
import os
import shutil
import subprocess

PROFILE = "UAT"

if not shutil.which("databricks"):
    raise RuntimeError("The `databricks` CLI is not on PATH. Install it: https://docs.databricks.com/en/dev-tools/cli/install.html")

_cfg_path = os.path.expanduser("~/.databrickscfg")
_parser = configparser.ConfigParser()
_parser.read(_cfg_path)
if PROFILE not in _parser:
    raise RuntimeError(f"No [{PROFILE}] profile in {_cfg_path}. Run: databricks auth login --profile {PROFILE}")
HOST = _parser[PROFILE]["host"].rstrip("/")

_result = subprocess.run(
    ["databricks", "auth", "token", "--host", HOST, "--profile", PROFILE],
    capture_output=True, text=True,
)
if _result.returncode != 0:
    raise RuntimeError(
        f"`databricks auth token --profile {PROFILE}` failed:\n{_result.stderr}\n"
        f"Try running it yourself in a terminal, or re-login: databricks auth login --host {HOST} --profile {PROFILE}"
    )
TOKEN = json.loads(_result.stdout)["access_token"]
HEADERS = {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}
print(f"Profile: {PROFILE}")
print(f"Host: {HOST}")
print(f"Token acquired: {len(TOKEN)} chars")


# COMMAND ----------

# -- Run queries, build HTML, display --
import html as _html
import re
import time
import urllib.parse

import httpx
from IPython.display import HTML, display

SYSTEM_PROMPT = ("Answer the question using ONLY the provided excerpts. Cite excerpts by "
                 "their ref, like (ref: XXX). If the excerpts don't answer it, say so.")


def _message_text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
    return ""


def run_local_rag(question):
    start = time.monotonic()
    resp = httpx.post(f"{HOST}/api/2.0/vector-search/indexes/{VECTOR_SEARCH_INDEX}/query", headers=HEADERS, timeout=30,
                       json={"query_text": question, "query_type": "HYBRID", "num_results": NUM_CHUNKS,
                             "columns": ["chunk_id", "IDDOC", "REF", "division", "url", "chunk_text", "semantic_headers"]})
    resp.raise_for_status()
    data = resp.json()
    cols = [c["name"] for c in data["manifest"]["columns"]]
    chunks = [dict(zip(cols, row)) for row in data["result"]["data_array"]]

    by_ref = {}
    for c in chunks:
        ref = c.get("REF") or c.get("IDDOC", "")
        if ref not in by_ref or (c.get("score") or 0) > by_ref[ref].get("score", 0):
            by_ref[ref] = c
    candidates = sorted(by_ref.values(), key=lambda c: c.get("score") or 0, reverse=True)

    excerpts = "\n".join(f"- ref: {c.get('REF', '')}\n  excerpt: {(c.get('chunk_text') or '')[:CHUNK_EXCERPT_CHARS]}" for c in candidates)
    sources = [{"ref": c.get("REF", ""), "url": c.get("url", ""), "division": c.get("division", ""), "score": c.get("score")} for c in candidates]

    resp = httpx.post(f"{HOST}/serving-endpoints/{LOCAL_RAG_LLM_ENDPOINT}/invocations", headers=HEADERS, timeout=60,
                       json={"messages": [{"role": "system", "content": SYSTEM_PROMPT},
                                          {"role": "user", "content": f"QUESTION:\n{question}\n\nEXCERPTS:\n{excerpts}"}],
                             "max_tokens": LOCAL_RAG_MAX_OUTPUT_TOKENS, "temperature": LOCAL_RAG_TEMPERATURE})
    resp.raise_for_status()
    completion = resp.json()
    answer = _message_text(completion["choices"][0]["message"]["content"])
    return {"answer": answer, "sources": sources, "usage": completion.get("usage") or {}, "duration_s": round(time.monotonic() - start, 2)}


def run_agent(question):
    start = time.monotonic()
    resp = httpx.post(f"{HOST}/serving-endpoints/{AGENT_ENDPOINT}/invocations", headers=HEADERS, timeout=AGENT_TIMEOUT_S,
                       json={"input": [{"role": "user", "content": question}], "stream": False, "databricks_options": {"return_trace": True}})
    resp.raise_for_status()
    data = resp.json()

    answer_parts, sources, seen = [], [], set()
    for out in data.get("output", []):
        if out.get("type") != "message":
            continue
        for item in out.get("content", []):
            if item.get("type") == "output_text":
                answer_parts.append(item.get("text", ""))
            for ann in item.get("annotations", []) or []:
                if ann.get("type") != "url_citation":
                    continue
                url = ann.get("url", "")
                parsed = urllib.parse.urlparse(url)
                ref = urllib.parse.parse_qs(parsed.query).get("ref", [""])[0]
                m = re.search(r"\[Source:\s*([^|]+)\|", urllib.parse.unquote(url))
                ref = ref or (m.group(1).strip() if m else "")
                base_url = f"{parsed.scheme}://{parsed.netloc}{parsed.path}?{parsed.query}"
                if ref in seen:
                    continue
                seen.add(ref)
                sources.append({"ref": ref, "url": base_url})
    return {"answer": "".join(answer_parts), "sources": sources, "usage": data.get("usage"), "duration_s": round(time.monotonic() - start, 2)}


def chips_html(sources):
    if not sources:
        return "<p class=none>No sources</p>"
    return "<div class=chips>" + "".join(
        f'<a class=chip href="{_html.escape(s["url"])}" target=_blank>{_html.escape(s["ref"])}'
        + (f" - {round(s['score'] * 100)}%" if isinstance(s.get("score"), (int, float)) else "") + "</a>"
        for s in sources) + "</div>"


def usage_html(usage, duration_s):
    parts = [f"{duration_s}s"]
    if usage:
        parts.append(f"{usage.get('prompt_tokens', '?')}+{usage.get('completion_tokens', '?')} tokens")
        if usage.get("reasoning_tokens"):
            parts.append(f"{usage['reasoning_tokens']} reasoning")
    return f"<p class=meta>{' - '.join(parts)}</p>"


rows = []
for i, q in enumerate(TEST_QUESTIONS, 1):
    print(f"[{i}/{len(TEST_QUESTIONS)}] {q}")
    try:
        rag = run_local_rag(q)
    except Exception as e:
        rag = {"answer": f"ERROR: {e}", "sources": [], "usage": None, "duration_s": 0}
    try:
        agent = run_agent(q)
    except Exception as e:
        agent = {"answer": f"ERROR: {e}", "sources": [], "usage": None, "duration_s": 0}
    rows.append(f"""
    <div class=row><h2>{_html.escape(q)}</h2><div class=cols>
      <div class=col><h3>Agent (All_v2)</h3>{usage_html(agent['usage'], agent['duration_s'])}
        <div class=answer>{_html.escape(agent['answer']).replace(chr(10), '<br>')}</div>{chips_html(agent['sources'])}</div>
      <div class=col><h3>Local RAG</h3>{usage_html(rag['usage'], rag['duration_s'])}
        <div class=answer>{_html.escape(rag['answer']).replace(chr(10), '<br>')}</div>{chips_html(rag['sources'])}</div>
    </div></div>""")

report = f"""<html><head><meta charset=utf-8><style>
body{{font-family:-apple-system,Segoe UI,sans-serif;background:#f5f5f7;margin:0;padding:24px;color:#1a1a1a}}
h1{{font-size:20px}} .row{{background:#fff;border-radius:12px;padding:20px;margin-bottom:20px;box-shadow:0 1px 3px rgba(0,0,0,.08)}}
.row h2{{font-size:15px;margin:0 0 14px;color:#0055a4}} .cols{{display:flex;gap:20px}} .col{{flex:1;min-width:0}}
.col h3{{font-size:12px;text-transform:uppercase;letter-spacing:.04em;color:#666;margin:0 0 4px}}
.meta{{font-size:11px;color:#999;margin:0 0 8px}} .answer{{font-size:13px;line-height:1.5;margin-bottom:10px;white-space:pre-wrap}}
.chips{{display:flex;flex-wrap:wrap;gap:6px}} .chip{{font-size:11px;padding:3px 8px;border-radius:6px;background:#eef2f7;color:#0055a4;text-decoration:none}}
.none{{font-size:11px;color:#aaa;font-style:italic}}
</style></head><body><h1>RAG comparison -- Agent (All_v2) vs Local RAG ({LOCAL_RAG_LLM_ENDPOINT})</h1>{"".join(rows)}</body></html>"""

dh = globals().get("displayHTML")
(dh if callable(dh) and dh is not HTML else lambda h: display(HTML(h)))(report)

if OUTPUT_HTML_PATH:
    try:
        with open(OUTPUT_HTML_PATH, "w", encoding="utf-8") as f:
            f.write(report)
        print(f"Saved to {OUTPUT_HTML_PATH}")
    except Exception as e:
        print(f"Could not save: {e}")
