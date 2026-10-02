# Databricks notebook source
# DBTITLE 1,Header
# MAGIC %md
# MAGIC # RAG comparison — small local RAG (Index + LLM) vs Knowledge Assistant agent (All_v2)
# MAGIC
# MAGIC Standalone evaluation notebook. For a fixed list of test questions, runs **two**
# MAGIC independent answering methods against the same QualiBOT knowledge base and
# MAGIC renders a side-by-side HTML comparison (answer + sources) for each:
# MAGIC
# MAGIC 1. **Local RAG** — direct Vector Search query (`chunks_index_v1`, same index used
# MAGIC    by the Compare app's Impact Search) + a single call to a small/cheap LLM
# MAGIC    (default `databricks-gemini-3-1-flash-lite`) to synthesize an answer from the
# MAGIC    retrieved excerpts. This is the same shape as Impact Search V3
# MAGIC    (`server/services/vector_search.py::synthesize_impact_with_llm`), applied to
# MAGIC    open questions instead of "which docs are impacted".
# MAGIC 2. **Agent (All_v2)** — the existing Knowledge Assistant endpoint
# MAGIC    (`ka-7679a56e-endpoint`, the "ALL division" agent already used by the QualiBOT
# MAGIC    Chat feature) — does its own retrieval + reasoning, called non-streaming here.
# MAGIC
# MAGIC **All tunables are in the Parameters cell below.** Re-run from there to try a
# MAGIC different LLM, chunk count, question set, etc. — nothing else needs editing.
# MAGIC
# MAGIC Read-only: no tables are written. Only writes the comparison HTML file, and only
# MAGIC if `OUTPUT_HTML_PATH` is set.

# COMMAND ----------

# DBTITLE 1,Dependencies
# MAGIC %pip install -q httpx
# MAGIC %restart_python

# COMMAND ----------

# DBTITLE 1,Parameters — edit these, then Run All
# ── Vector Search (shared by the local RAG) ─────────────────────────────────
VECTOR_SEARCH_INDEX = "uat_landingzone.qualibot.chunks_index_v1"  # "ALL" full-corpus index
NUM_CHUNKS = 8                # candidates retrieved per question (HYBRID search, cap is 200)
CHUNK_EXCERPT_CHARS = 1500    # chars of chunk_text fed to the LLM per candidate

# ── Local RAG LLM ────────────────────────────────────────────────────────────
LOCAL_RAG_LLM_ENDPOINT = "databricks-gemini-3-1-flash-lite"  # small/cheap model to test
# Gemini 3.1 flash-lite is a "thinking" model — it spends a chunk of max_tokens on
# hidden reasoning_tokens before writing the visible answer. Verified empirically
# (2026-07-10): 800 tokens left ~0 room for the actual answer (all consumed by
# reasoning); 2000 comfortably covers reasoning (~1000-1100 tokens seen) + a full
# answer. If you swap in a non-thinking model, 800 is plenty — lower it back.
LOCAL_RAG_MAX_OUTPUT_TOKENS = 2000
LOCAL_RAG_TEMPERATURE = 0.0

# ── Agent (existing Knowledge Assistant, "All_v2" division) ─────────────────
AGENT_ENDPOINT = "ka-7679a56e-endpoint"
AGENT_TIMEOUT_S = 90.0

# ── Test questions — edit freely, any length list ───────────────────────────
TEST_QUESTIONS = [
    "What documents reference the NDT/NDI qualification requirements?",
    "Which procedures must be updated when a supplier changes their process?",
    "List the key quality standards applicable to composite part manufacturing.",
    "What is the approval process for deviations from engineering specifications?",
]

# ── Language parity — fr/en/es phrasings of the same question ──────────────
# Non-regression fixture for the multilingual retrieval bias audit
# (2026-07-21, see utils/databricks_ops/evaluation/multilingual_audit/README.md).
# Each concept is the SAME question asked in fr/en/es. Empirically (audit
# artifact linked in the README), the agent returned 2-3 cited sources for
# fr/en but ZERO for the raw es phrasing of "warehouse_stamp" — this fixture
# exists to catch a regression (or a fix) of that gap, not to exercise new
# ground. Re-run this cell periodically; a concept whose es/other-language
# source count stays at 0 while fr/en don't is the signal to watch.
LANGUAGE_PARITY_QUESTIONS = [
    {
        "concept": "warehouse_stamp",
        "fr": "Un magasinier doit-il porter un tampon personnel pour ses tâches de gestion de stock ?",
        "en": "Does a warehouse operator need a personal stamp for stock management tasks?",
        "es": "¿Un almacenista debe tener un sello personal para sus tareas de gestión de almacén?",
    },
    {
        "concept": "freezer_ppe",
        "fr": "Quel équipement de protection individuelle faut-il porter pour entrer dans une chambre froide ?",
        "en": "What personal protective equipment must be worn to enter a freezer room?",
        "es": "¿Qué equipo de protección personal se debe usar para entrar en un congelador?",
    },
    {
        "concept": "non_conformity",
        "fr": "Comment déclarer une non-conformité produit détectée en contrôle qualité ?",
        "en": "How do you report a product non-conformity found during quality control?",
        "es": "¿Cómo se declara una no conformidad de producto detectada en control de calidad?",
    },
]

# ── Output ───────────────────────────────────────────────────────────────────
# Set to None to skip saving — the report still renders inline via displayHTML below.
OUTPUT_HTML_PATH = "/Volumes/uat_landingzone/qualibot/test/rag_vs_agent_comparison.html"

# COMMAND ----------

# DBTITLE 1,Auth — uses the cluster's attached identity (no token needed)
from databricks.sdk.core import Config

_cfg = Config()
_auth = _cfg.authenticate()
HOST = _cfg.host.rstrip("/")
TOKEN = _auth["Authorization"].removeprefix("Bearer ").strip()
print(f"Host: {HOST}")

# COMMAND ----------

# DBTITLE 1,Local RAG — retrieval + small-LLM synthesis
import html as _html
import json
import re
import time
import urllib.parse
from typing import Any, Dict, List

import httpx

_HEADERS = {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}

_LOCAL_RAG_SYSTEM_PROMPT = """\
You are a technical documentation assistant. Answer the question using ONLY the
provided excerpts from the knowledge base. Cite which excerpt(s) you used by
their ref, like (ref: XXX). If the excerpts don't contain the answer, say so
explicitly instead of guessing.\
"""


def _extract_message_text(content: Any) -> str:
    """Chat-completion `message.content` is a plain string for most models, but a
    list of typed blocks (e.g. [{"type": "text", "text": "...", "thoughtSignature":
    "..."}]) for "thinking" models like Gemini 3.1 — handle both."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"
        )
    return ""


def fetch_chunks(question: str, num_results: int) -> List[Dict[str, Any]]:
    """Query the Vector Search index directly (HYBRID, Databricks-managed embeddings)."""
    url = f"{HOST}/api/2.0/vector-search/indexes/{VECTOR_SEARCH_INDEX}/query"
    payload = {
        "query_text": question,
        "columns": ["chunk_id", "IDDOC", "REF", "division", "url", "chunk_text", "semantic_headers"],
        "num_results": num_results,
        "query_type": "HYBRID",
    }
    resp = httpx.post(url, json=payload, headers=_HEADERS, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    columns = [c["name"] for c in data.get("manifest", {}).get("columns", [])]
    rows = data.get("result", {}).get("data_array", [])
    return [dict(zip(columns, row)) for row in rows]


def run_local_rag(question: str) -> Dict[str, Any]:
    start = time.monotonic()
    chunks = fetch_chunks(question, NUM_CHUNKS)

    # Dedupe to one excerpt per document (REF), keep the best-scoring chunk per doc.
    by_ref: Dict[str, Dict[str, Any]] = {}
    for c in chunks:
        ref = c.get("REF", "") or c.get("IDDOC", "")
        if ref not in by_ref or (c.get("score", 0) or 0) > by_ref[ref].get("score", 0):
            by_ref[ref] = c
    candidates = sorted(by_ref.values(), key=lambda c: c.get("score", 0) or 0, reverse=True)

    excerpt_blocks = []
    sources = []
    for c in candidates:
        ref = c.get("REF", "") or c.get("IDDOC", "")
        excerpt = (c.get("chunk_text") or "")[:CHUNK_EXCERPT_CHARS]
        excerpt_blocks.append(f"- ref: {ref}\n  division: {c.get('division', '')}\n  excerpt: {excerpt}")
        sources.append({"ref": ref, "url": c.get("url", ""), "division": c.get("division", ""), "score": c.get("score")})

    user_content = f"QUESTION:\n{question}\n\nEXCERPTS:\n" + "\n".join(excerpt_blocks)

    llm_url = f"{HOST}/serving-endpoints/{LOCAL_RAG_LLM_ENDPOINT}/invocations"
    payload = {
        "messages": [
            {"role": "system", "content": _LOCAL_RAG_SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ],
        "max_tokens": LOCAL_RAG_MAX_OUTPUT_TOKENS,
        "temperature": LOCAL_RAG_TEMPERATURE,
    }
    resp = httpx.post(llm_url, json=payload, headers=_HEADERS, timeout=60)
    resp.raise_for_status()
    completion = resp.json()
    answer = _extract_message_text(completion.get("choices", [{}])[0].get("message", {}).get("content", ""))
    usage = completion.get("usage") or {}

    return {
        "answer": answer,
        "sources": sources,
        "usage": usage,
        "duration_s": round(time.monotonic() - start, 2),
    }

# COMMAND ----------

# DBTITLE 1,Agent (All_v2) — non-streaming call + citation-derived sources
def _extract_agent_sources(content_items: List[Dict[str, Any]]) -> List[Dict[str, str]]:
    """Pull {ref, title, division, url} out of each url_citation annotation.

    The KA endpoint embeds a "[Source: REF | Title: ... | Division: ... | ...]"
    tag in the citation URL's #:~:text= fragment (URL-encoded) — decode it and
    also read `ref=` straight off the query string as a robust fallback.
    """
    sources, seen = [], set()
    for item in content_items:
        for ann in item.get("annotations", []) or []:
            if ann.get("type") != "url_citation":
                continue
            url = ann.get("url", "")
            parsed = urllib.parse.urlparse(url)
            ref = urllib.parse.parse_qs(parsed.query).get("ref", [""])[0]
            title = division = ""
            m = re.search(r"\[Source:\s*([^|]+)\|\s*Title:\s*([^|]+)\|\s*Division:\s*([^|]+)\|", urllib.parse.unquote(url))
            if m:
                ref = ref or m.group(1).strip()
                title = m.group(2).strip()
                division = m.group(3).strip()
            base_url = f"{parsed.scheme}://{parsed.netloc}{parsed.path}?{parsed.query}"
            key = ref or base_url
            if key in seen:
                continue
            seen.add(key)
            sources.append({"ref": ref, "title": title, "division": division, "url": base_url})
    return sources


def run_agent(question: str) -> Dict[str, Any]:
    start = time.monotonic()
    url = f"{HOST}/serving-endpoints/{AGENT_ENDPOINT}/invocations"
    payload = {
        "input": [{"role": "user", "content": question}],
        "stream": False,
        "databricks_options": {"return_trace": True},
    }
    resp = httpx.post(url, json=payload, headers=_HEADERS, timeout=AGENT_TIMEOUT_S)
    resp.raise_for_status()
    data = resp.json()

    answer_parts, all_sources = [], []
    for out in data.get("output", []):
        if out.get("type") != "message":
            continue
        content_items = out.get("content", []) or []
        for item in content_items:
            if item.get("type") == "output_text":
                answer_parts.append(item.get("text", ""))
        all_sources.extend(_extract_agent_sources(content_items))

    return {
        "answer": "".join(answer_parts),
        "sources": all_sources,
        "usage": data.get("usage"),  # KA endpoints typically don't report usage — expect None
        "duration_s": round(time.monotonic() - start, 2),
    }

# COMMAND ----------

# DBTITLE 1,Run both methods over every test question
results = []
for i, question in enumerate(TEST_QUESTIONS, 1):
    print(f"[{i}/{len(TEST_QUESTIONS)}] {question}")
    try:
        rag = run_local_rag(question)
    except Exception as e:
        rag = {"answer": f"ERROR: {e}", "sources": [], "usage": None, "duration_s": None}
    try:
        agent = run_agent(question)
    except Exception as e:
        agent = {"answer": f"ERROR: {e}", "sources": [], "usage": None, "duration_s": None}
    results.append({"question": question, "rag": rag, "agent": agent})
    print(f"    local RAG: {rag['duration_s']}s · agent: {agent['duration_s']}s")

# COMMAND ----------

# DBTITLE 1,Language parity — same question, fr/en/es, agent only
# Agent-only (not local RAG): the thing under test is what a real chat user
# gets back, and that's the agent path. Runs each concept's 3 language
# variants and records how many sources the agent cited for each — the
# comparison that matters is WITHIN a concept, across languages.
language_parity_results = []
for concept in LANGUAGE_PARITY_QUESTIONS:
    row = {"concept": concept["concept"], "langs": {}}
    for lang in ("fr", "en", "es"):
        question = concept[lang]
        try:
            agent = run_agent(question)
        except Exception as e:
            agent = {"answer": f"ERROR: {e}", "sources": [], "usage": None, "duration_s": None}
        row["langs"][lang] = agent
        print(f"[{concept['concept']}/{lang}] sources={len(agent['sources'])} duration={agent['duration_s']}s")
    language_parity_results.append(row)

# COMMAND ----------

# DBTITLE 1,Build the HTML comparison report
def _sources_html(sources: List[Dict[str, Any]]) -> str:
    if not sources:
        return '<p class="none">No sources</p>'
    chips = []
    for s in sources:
        label = _html.escape(s.get("ref") or s.get("title") or "?")
        div = _html.escape(s.get("division") or "")
        score = s.get("score")
        score_str = f" · {round(score * 100)}%" if isinstance(score, (int, float)) else ""
        url = s.get("url", "")
        inner = f"{label}{(' · ' + div) if div else ''}{score_str}"
        chips.append(f'<a class="chip" href="{_html.escape(url)}" target="_blank">{inner}</a>' if url else f'<span class="chip">{inner}</span>')
    return '<div class="chips">' + "".join(chips) + "</div>"


def _usage_html(usage, duration_s) -> str:
    parts = []
    if duration_s is not None:
        parts.append(f"{duration_s}s")
    if usage:
        parts.append(f"{usage.get('prompt_tokens', usage.get('input_tokens', '?'))}+{usage.get('completion_tokens', usage.get('output_tokens', '?'))} tokens")
        # "Thinking" models (e.g. Gemini 3.1) spend a chunk of the token budget on
        # hidden reasoning before the visible answer — surface it, it's real cost.
        if usage.get("reasoning_tokens"):
            parts.append(f"{usage['reasoning_tokens']} reasoning tokens")
    return f'<p class="meta">{" · ".join(parts)}</p>' if parts else ""


def _language_parity_html(language_parity_results: List[Dict[str, Any]]) -> str:
    if not language_parity_results:
        return ""
    rows = []
    for row in language_parity_results:
        counts = {lang: len(row["langs"][lang]["sources"]) for lang in ("fr", "en", "es")}
        # Flag when at least one language finds sources and another finds none —
        # that asymmetry is the regression signal, not the absolute counts.
        flagged = max(counts.values()) > 0 and min(counts.values()) == 0
        cells = []
        for lang in ("fr", "en", "es"):
            agent = row["langs"][lang]
            refs = ", ".join(_html.escape(s.get("ref") or "?") for s in agent["sources"]) or "—"
            cls = "zero" if counts[lang] == 0 else ""
            cells.append(f'<td class="{cls}"><strong>{counts[lang]}</strong><br><span class="refs">{refs}</span></td>')
        row_cls = "flagged" if flagged else ""
        rows.append(f'<tr class="{row_cls}"><td>{_html.escape(row["concept"])}</td>{"".join(cells)}</tr>')
    return f"""
    <div class="row">
      <h2>Language parity (fr / en / es) — same question, sources cited by the agent</h2>
      <table class="parity">
        <thead><tr><th>Concept</th><th>fr</th><th>en</th><th>es</th></tr></thead>
        <tbody>{"".join(rows)}</tbody>
      </table>
      <p class="meta">A red row = at least one language got 0 cited sources while another language got some, for the identical question — that's the multilingual retrieval bias this fixture watches for.</p>
    </div>"""


_ROWS = []
for r in results:
    _ROWS.append(f"""
    <div class="row">
      <h2>{_html.escape(r['question'])}</h2>
      <div class="cols">
        <div class="col">
          <h3>Agent (All_v2)</h3>
          {_usage_html(r['agent']['usage'], r['agent']['duration_s'])}
          <div class="answer">{_html.escape(r['agent']['answer']).replace(chr(10), '<br>')}</div>
          {_sources_html(r['agent']['sources'])}
        </div>
        <div class="col">
          <h3>Local RAG ({_html.escape(LOCAL_RAG_LLM_ENDPOINT)})</h3>
          {_usage_html(r['rag']['usage'], r['rag']['duration_s'])}
          <div class="answer">{_html.escape(r['rag']['answer']).replace(chr(10), '<br>')}</div>
          {_sources_html(r['rag']['sources'])}
        </div>
      </div>
    </div>""")

_HTML_REPORT = f"""
<html><head><meta charset="utf-8"><style>
body {{ font-family: -apple-system, Segoe UI, sans-serif; background: #f5f5f7; margin: 0; padding: 24px; color: #1a1a1a; }}
h1 {{ font-size: 20px; }}
.row {{ background: #fff; border-radius: 12px; padding: 20px; margin-bottom: 20px; box-shadow: 0 1px 3px rgba(0,0,0,0.08); }}
.row h2 {{ font-size: 15px; margin: 0 0 14px; color: #0055a4; }}
.cols {{ display: flex; gap: 20px; }}
.col {{ flex: 1; min-width: 0; }}
.col h3 {{ font-size: 12px; text-transform: uppercase; letter-spacing: 0.04em; color: #666; margin: 0 0 4px; }}
.meta {{ font-size: 11px; color: #999; margin: 0 0 8px; }}
.answer {{ font-size: 13px; line-height: 1.5; margin-bottom: 10px; white-space: pre-wrap; }}
.chips {{ display: flex; flex-wrap: wrap; gap: 6px; }}
.chip {{ font-size: 11px; padding: 3px 8px; border-radius: 6px; background: #eef2f7; color: #0055a4; text-decoration: none; }}
.chip:hover {{ background: #dbe7f3; }}
.none {{ font-size: 11px; color: #aaa; font-style: italic; }}
table.parity {{ border-collapse: collapse; width: 100%; font-size: 13px; }}
table.parity th, table.parity td {{ border: 1px solid #e2e5ea; padding: 8px 12px; text-align: left; vertical-align: top; }}
table.parity th {{ background: #f0f2f5; font-size: 11px; text-transform: uppercase; letter-spacing: 0.04em; color: #666; }}
table.parity td.zero {{ color: #b3382c; }}
table.parity tr.flagged {{ background: #fdf1ef; }}
table.parity .refs {{ font-size: 11px; color: #666; }}
</style></head><body>
<h1>RAG comparison — Agent (All_v2) vs Local RAG ({_html.escape(LOCAL_RAG_LLM_ENDPOINT)})</h1>
{_language_parity_html(language_parity_results)}
{"".join(_ROWS)}
</body></html>
"""

displayHTML(_HTML_REPORT)

if OUTPUT_HTML_PATH:
    with open(OUTPUT_HTML_PATH, "w", encoding="utf-8") as f:
        f.write(_HTML_REPORT)
    print(f"Saved to {OUTPUT_HTML_PATH}")
