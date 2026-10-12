# KA replacement mapping — what the app really uses, and what already replaces it

Principle: replace only what the app actually consumes from the KA, with what the project and the platform already have. Write only the glue. Add a component only when a measured gap against the KA requires it.

## Sources

- I/O ids (I, B, O) refer to `docs/ka-black-box-io-contract.md`.
- **[code]** — this repo.
- **[verified]** — checked live on the dev workspace (2026-10-05/06).

## 1. Used by the app → replaced by

Glue column: **none** = nothing to write.

| # | KA element (contract) | What the app really uses | Already available | Glue to write |
|---|---|---|---|---|
| 1 | Division routing (I1) | One single-source KA per division | The 3 indexes the KAs already read: `chunks_index_v1` / `chunks_as_index_v1` / `chunks_is_index_v1` on Vector Search endpoint `qualibot` [verified]. Division logic in `_endpoint_for_division` [code]. | Map division → index name |
| 2 | Conversation (I3–I5) | Messages `{role, content}`, earlier answers with `⟦n⟧`, error turns | Same list, built by `chat.py` [code] | Strip `⟦n⟧` from history before prompting |
| 3 | Date (I7) | `[Date: …]` prefix on the last question | `_with_today_date` [code] | none |
| 4 | Translation (I8) | Third-language question → EN, answer back | `translation_bridge.py` [code] | none |
| 5 | Instructions (I9) | All behaviour rules live in the KA | Live KA instructions, ALL / AS / IS, 6.2–6.7k chars [verified]. Not stored in the repo. | Store them in the repo as the system prompt (drop the stray AS note) |
| 6 | Retrieval (B1) | Relevant passages from the division index | HYBRID query, `vector_search.py::_fetch_chunks` [code]. Compare already queries `chunks_index_v1` on dev (`COMPARE_IMPACT_INDEX`) [code]. | Call it on the division index with the user's question |
| 7 | Scope (B2) | Answers limited to the division | Per-division indexes (row 1) + instructions | none |
| 8 | Answer language (B3) | Answer in the user's language | Instructions + LLM | none |
| 9 | Metadata (B6) | REF, title, division, category, date read from the passages | Every chunk starts with `[Source: REF \| Title \| Division \| Category \| Date de diffusion]` [verified] | none |
| 10 | Answer rules (B5, B7–B13, B15) | Format, final document list, recency, no links, archived docs, clarification, safety NO, Markdown | Instructions + LLM | none |
| 11 | Citations (B14 → O4) | Citation → document + position in the text | Nothing — the LLM must cite | Citation rule in the prompt + parser of `[n]` markers → `{n, pos}` |
| 12 | Text stream (O3) | Streamed answer text | `streaming.py` already streams `databricks-claude-sonnet-4-6` and parses `chat.completion.chunk` (`stream_analysis`, `_parse_text_delta`) [code] | Reuse it for the answer |
| 13 | Sources (O7 → P1) | `{title = REF, url}` per cited document | Vector Search rows carry `REF` and `url` directly; `url` is the same Intraqual link the KA cites [verified]. Simpler than the KA, where the app had to dig the REF out of the trace. | Build the list from the cited rows |
| 14 | Trace id (O6) | Stored in `chat_messages.trace_id` | `mlflow[databricks]` in `pyproject.toml`, `server/tracing.py` [code] | A request id at first; MLflow trace later if needed |
| 15 | Errors (O10) | Error event, retries | `chat.py` error handling, retries in `streaming.py` [code] | Map Vector Search / LLM failures to the error event |
| 16 | Output format | Events read by `chat.py` | The contract `stream_chat()` already emits [code] | Emit the same events |

## 2. KA internals — not observable, not rebuilt up front

| # | KA element | Status | Decision |
|---|---|---|---|
| 17 | Query rewriting with history | Hidden in the trace | Not in the first version. Add only if follow-up questions measurably fail. |
| 18 | Multi-query plan / metadata filters | Hidden. Filters are supported by Vector Search (`filters_json`, equality and range) [verified]. | Add only on a measured gap (e.g. date-relative questions) |
| 19 | Reranking (`rerank` span) | Hidden | Vector Search score order first. Add a reranker only on a measured gap. |
| 20 | Search in French (B4) | Rule in the instructions | Third-language questions are already translated by the bridge (row 4) |

## 3. Used by the app but not needed

| # | KA element | Why not needed |
|---|---|---|
| 21 | Reasoning summary (O2) | Only saved to the DB, never displayed [code] |
| 22 | Footnoted final text (O5) | Ignored by the app [code] |
| 23 | `tool_name` / `tool_query` / `tool_result` | Only saved; no `function_call` seen in the captured call [verified] |
| 24 | KA SSE wire format (O1) | Only `stream_chat()` reads it; the replacement emits the app's events directly |

## 4. KA features not used by the app — nothing to rebuild

| # | KA feature | Evidence |
|---|---|---|
| 25 | File sources (Volume, Table), managed ingestion, image handling, sync | Qualibot's own pipeline produces the chunks and indexes; KA sources are `source_type: index` [verified] |
| 26 | Several sources per KA (up to 10) | One source per KA [verified] |
| 27 | Examples, guidelines, ALHF, labeling, import/export | 0 examples on the 3 KAs [verified] |
| 28 | Playground, View thoughts / trace / sources, trace labels | KA UI, not used by the app |
| 29 | KA as a tool / Supervisor sub-agent | Not used |
| 30 | KA CRUD API, KA permissions | Replaced by plain grants on the indexes |
| 31 | Dedicated Vector Search endpoint per KA (`ka-*-vs-endpoint`) | Not needed: the indexes live on endpoint `qualibot` [verified] |
| 32 | KA rate limit (≈3–4 questions/min/user) | Constraint disappears; the LLM endpoint's limits apply instead |
| 33 | Per-answer pricing | Becomes LLM tokens + the Vector Search endpoint already in use |

## 5. What actually has to be written

1. Division → index map; Vector Search query with the user's question (reuse `_fetch_chunks`).
2. Prompt = division instructions + numbered passages + conversation (history stripped of `⟦n⟧`) + citation rule.
3. Streamed LLM answer (reuse the existing streaming code).
4. Parser: `[n]` markers → clean text + `{n, pos}`; sources `{REF, url}` from the cited rows.
5. Emit `stream_chat()`'s events, including errors.
6. Switch between Chat KA and Chat VSI (route / engine) and the front `engine` prop.

Existing prototype to start from: `utils/evaluation/eval_rag_vs_agent.py`, a local RAG (Vector Search, 8 passages, LLM) compared against the KA. It still points to `uat_landingzone.qualibot.chunks_index_v2`, an index that was dropped.
