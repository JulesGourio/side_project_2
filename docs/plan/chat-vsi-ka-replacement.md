# Plan: Chat VSI — custom replacement for the Knowledge Assistant

> Status: PLANNED

## Goal

Build, in this repo, a module that replaces the Knowledge Assistant (KA) behind the Chat. It must behave and return the same things in broad lines, per `docs/ka-black-box-io-contract.md`. It ships behind the **Chat VSI** tab while **Chat KA** keeps running, so the two can be compared before cutover.

## Decisions (user, 2026-10-06)

| Topic | Decision |
|---|---|
| Integration | Chat VSI tab, same output contract as `stream_chat()`; Chat KA kept in parallel |
| v1 scope | Full Instructed Retriever: multi-query search plan, metadata filters, LLM reranking, generation with citations |
| Answer model | `databricks-claude-sonnet-4-6` |

## Reference documents

- `docs/ka-black-box-io-contract.md` — inputs (I), behaviours (B), outputs (O), app processing (P), display (D).
- `docs/ka-usage-scan.md`, `docs/ka-migration.md`.

## Verified facts this plan relies on (2026-10-06)

- **Vector Search query.**
  - `POST /api/2.0/vector-search/indexes/{index}/query` with `query_type: "HYBRID"`.
  - `filters_json` works on `chunks_index_v1` for equality and range on metadata. Tested with `{"division": "AS", "doc_date >=": "2026-01-01"}`: returned only AS chunks dated ≥ 2026-01-01.
  - Requested columns come back, plus `score`.
- **Indexes.** `dev_landingzone.qualibot.chunks_index_v1` / `chunks_as_index_v1` / `chunks_is_index_v1`, on Vector Search endpoint `qualibot`. They are the KAs' current knowledge sources.
- **Columns:** `chunk_id`, `IDDOC`, `REF`, `division`, `chunk_index`, `chunk_text`, `chunk_token_count`, `chunk_content_type`, `semantic_headers`, `chunk_sha256`, `url`, `doc_date`.
- **Values in `chunks_v1`:**
  - `division`: AS 60,498 · IS 13,711 · DIRECTIVE 359 · AUTRE 284 · NULL 234.
  - `chunk_content_type`: image 38,813 · text 33,387 · table 1,842 · mixed 1,044.
  - 75,086 chunks, 4,920 REFs, `doc_date` from 2018-01-02 to 2026-10-02.
  - 0 chunks contain "ARCHIVED DOCUMENT" in dev.
- **Chunk content.** `chunk_text` starts with `[Source: REF | Title | Division | Category | Date de diffusion]`. Document version and status are not columns: they appear in the document content (validation circuit, revision tables).
- **Reusable code:**
  - `server/services/vector_search.py::_fetch_chunks` — HYBRID REST query; parallel + retry in `_fetch_chunks_multi`.
  - `server/services/streaming.py::stream_analysis` — already streams `databricks-claude-sonnet-4-6` for Compare.
  - `server/routers/chat.py` — post-processing and persistence, unchanged by contract.
- **Instructions.** The live KA instructions (ALL 6,429 · AS 6,724 · IS 6,212 chars) were dumped on 2026-10-05; AS ends with a stray authoring note.
- **App service principal of `qualibot-custom`:**
  - SELECT on `chunks_index_v1` only — nothing on `chunks_as_index_v1` / `chunks_is_index_v1`.
  - USE_CATALOG on `dev_landingzone` couldn't be granted by us (needs MANAGE).
  - A Vector Search query from the app has not been tested yet.
- **MLflow.** `mlflow[databricks]` is in `pyproject.toml`; `server/tracing.py` only sets the tracking URI.
- **Lakebase.** `qualibot-custom` currently shares database `doccompare` with the dev app; its schema bootstrap fails with "must be owner of table messages".

## Architecture

New package `server/services/chat_vsi/`. Entry point:

```python
async def stream_chat_vsi(host, token, division, messages) -> AsyncGenerator[str, None]
```

It yields exactly the events `stream_chat()` yields today (contract §6):

```
{"type":"response.output_text.delta","delta":str}               × N
{"type":"sources","sources":[{title,url,doc_uri}],"citations":[{n,pos}]}
{"type":"metadata","trace_id",...}
{"type":"error",...}                                             (on error)
[DONE]
```

So `chat.py` post-processing (P4–P9) and the whole display (D1–D10) are reused unchanged.

### Pipeline per turn

| Stage | What | Model / service | Notes |
|---|---|---|---|
| 1. Plan | From the last messages, the division instructions, the index description (columns, allowed values, date range) and today's date: produce a standalone question + 1–N sub-queries. Each sub-query has search text in FR/EN and optional filters (`doc_date` range, `REF` list, `chunk_content_type`). | LLM, JSON output | Filters validated against the allowed columns/values; invalid ones dropped. Several formulations in parallel (configurable). Covers B4, B6. |
| 2. Retrieve | Each sub-query → HYBRID query on the division's index (same 3 indexes as the KA), with its filters | Vector Search | Parallel, bounded concurrency, retries (extend `_fetch_chunks` with `filters_json`). Covers B1, B2. |
| 3. Merge | Dedup by `chunk_id`, keep provenance (sub-query, score) | — | |
| 4. Rerank | Instruction-aware relevance scoring of candidates in groups; select the top passages within a token budget | LLM | Covers B9 (recency preference) together with the instructions. |
| 5. Generate | System prompt = division instructions (copied from the live KA, stray AS note removed) + citation protocol. User content = conversation + numbered passages. Streamed. | `databricks-claude-sonnet-4-6` | Covers B3, B5, B7, B8, B10–B13, B15. |
| 6. Adapt | Parse `[n]` markers out of the streamed text (stateful, handles markers split across chunks). Emit clean deltas, compute `{n, pos}`, map passage → document (REF from the header, URL), number sources by first appearance. | — | Covers B14 and contract O3/O4/O7. |
| 7. Trace | MLflow spans for each stage; `trace_id` in `metadata` | MLflow | Covers O6. |

### Files

| File | Content |
|---|---|
| `server/config/chat_vsi/instructions_{all,as,is}.md` | Live KA instructions, verbatim (AS stray note removed) |
| `server/config/chat_vsi/citation_protocol.md` | How to cite passages with `[n]` |
| `server/services/chat_vsi/planner.py` | Stage 1 |
| `server/services/chat_vsi/retriever.py` | Stages 2–3 |
| `server/services/chat_vsi/reranker.py` | Stage 4 |
| `server/services/chat_vsi/generator.py` | Stage 5, streaming |
| `server/services/chat_vsi/citations.py` | Stage 6 |
| `server/services/chat_vsi/__init__.py` | `stream_chat_vsi()` orchestration, tracing, error mapping |
| `server/routers/chat.py` | Shared turn handler with an `engine` parameter (`ka` / `vsi`); new WebSocket `/api/chat-vsi/ws` |
| `client/src/components/chat/ChatView.tsx` | `engine` prop → WebSocket path, title |
| `client/src/pages/ChatVsiPage.tsx` | Renders `ChatView engine="vsi"` (replaces the placeholder) |
| `app.yaml` | `CHAT_VSI_*` settings (below) |
| `tests/test_chat_vsi_*.py` | Unit tests per stage + route tests mirroring `tests/test_chat.py` |

### Settings (`app.yaml`)

```
CHAT_VSI_ENABLED
CHAT_VSI_INDEX_ALL / _AS / _IS
CHAT_VSI_GEN_ENDPOINT        = databricks-claude-sonnet-4-6
CHAT_VSI_PLAN_ENDPOINT       (default: same as GEN)
CHAT_VSI_RERANK_ENDPOINT     (default: same as GEN)
CHAT_VSI_NUM_QUERIES         (max sub-queries)
CHAT_VSI_PER_QUERY_RESULTS   (Vector Search num_results per sub-query)
CHAT_VSI_RERANK_TOP_K        (passages kept for generation)
CHAT_VSI_CONTEXT_TOKENS      (token budget for passages)
CHAT_VSI_MLFLOW_EXPERIMENT
```

### Sessions

Turns are tagged through the existing `endpoint_name` column (`vsi-all`, `vsi-as`, `vsi-is`). Each tab lists only its own sessions, filtered on that column. No schema change.

## Contract coverage

| Contract | How it's covered |
|---|---|
| I1 division target | Division → index map (same 3 indexes as the KA) |
| I3–I5 conversation, `⟦n⟧` in history, error turns | Accepted as-is; planner and generator strip `⟦n⟧` before use |
| I6–I8 window, date, translation | Unchanged (app side) |
| I9 instructions | Stored in repo, injected as system prompt |
| B1–B15 | See the pipeline table |
| O3, O4, O6, O7, O8, O10 | Produced by stages 5–7 in `stream_chat()`'s event format |
| O2, O5 | Not needed |
| P1–P9, D1–D10 | Unchanged |

## Prerequisites (infra, before deploy)

1. Grant `qualibot-custom`'s SP SELECT on `chunks_as_index_v1` and `chunks_is_index_v1`. Get USE_CATALOG on `dev_landingzone` from an owner/admin (Jules). Then test a Vector Search query from the app.
2. MLflow experiment for Chat VSI traces + CAN_EDIT for the app SP.
3. **Decision needed:** keep sharing Lakebase `doccompare` with the dev app, or isolate `qualibot-custom` in its own database (e.g. `LAKEBASE_DATABASE=doccompare_custom`; the app role has `createdb`).

## Implementation steps

1. Prerequisites above.
2. Instruction files + division profiles.
3. Retriever with filters + tests.
4. Planner (prompt, JSON schema, filter validation) + tests.
5. Reranker + tests.
6. Generator + citation adapter + tests: markers split across chunks, unknown `n`, no citations, markers inside links.
7. `stream_chat_vsi()` orchestration, MLflow spans, error mapping + tests.
8. Backend route (shared handler, `engine`) + route tests.
9. Frontend: `ChatView` `engine` prop, `ChatVsiPage`, per-tab sessions.
10. Deploy `qualibot-custom` on dev, smoke test on the 3 divisions.
11. Evaluation against the KA, then tuning.

## Evaluation (parity with the KA)

- **Question set:** real questions from chat history, plus the KA's own answers for the same questions.
- **Run:** both engines on the same questions and divisions.
- **LLM judge:**
  - relevance;
  - groundedness in the cited passages;
  - citation correctness (the cited document supports the statement);
  - language match;
  - completeness of the final document list;
  - B11 / B12 / B13 behaviours on dedicated questions.
- **Latency:** time to first token and total time.
- Starting point: `utils/databricks_ops/evaluation/eval_rag_vs_agent.py` (local RAG vs KA).
- Acceptance thresholds: to agree with the user before step 11.

## Risks and open questions

- **Latency:** two LLM stages (plan, rerank) run before the first token. Mitigation: parallel calls, configurable lighter models for plan/rerank, streamed generation. The models for plan/rerank are not decided yet (default: same as generation).
- **Cost and rate limits** of `claude-sonnet-4-6`: up to 3 LLM calls per turn.
- **Final document list:** version and status must be read from the passages (no structured column). Risk of omissions.
- **Shared directives:** which `division` values count as shared (DIRECTIVE, AUTRE, NULL) is encoded in the KA's per-division indexes. Reusing those indexes keeps the same scoping.
- **Lakebase shared with the dev app** (see prerequisites).

## Out of scope for v1

- Replacing the translation bridge or removing other KA workarounds (I7, I8, P5, D5).
- Compare.
- Switching Chat KA off.
