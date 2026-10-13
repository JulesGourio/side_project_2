# KA migration — findings

Collected 2026-10-05 → 2026-10-06. Scope: replacing the Knowledge Assistant (KA) behind the Chat tab with a custom implementation.

Source tags:
- **[verified]** — checked directly by us (workspace CLI, code, live call), with date
- **[doc-public]** — public Databricks page, read first-hand
- **[doc-internal]** — internal Databricks document, read first-hand
- **[Slack]** — internal Slack message, read first-hand, quoted verbatim
- **[Glean]** — Glean AI summary, **not read first-hand — do not cite**
- **[deduction]** — our reasoning, not a sourced fact

Quotes in "…" are verbatim (typos included).

## 0. Before communicating — what can be shared

**Internal only — do not share with customers:**
- Agent Bricks Internal FAQ (go/agentbricks/faq, KA/SA Deprecation tab = go/ka/sa/deprecation/faq). The document carries "Databricks Confidential" markings. Its timeline is labelled "current working plan". The deprecation tab says "Last updated: Sep 4, 2026". [doc-internal]
- FEOP-3421 (internal Jira, status "Initial Draft" when read on 2026-10-05). It is an alert to account teams that describes the customer notification. **We have not seen the customer email itself.** [doc-internal]
- All Slack messages (internal channels).
- Everything tagged [Glean].

**Public — citable:**
- The two Databricks blog posts in §3.
- Public docs pages. As fetched on 2026-10-06 [verified]:
  - The KA page ("Last updated on Sep 11, 2026") contains **no "Legacy" notice**.
  - The Content Search page states "This feature is in Beta". A workspace admin must turn on "Analyze Files in Volumes with Genie Agents" in Previews.

**Customer-specific:** §4–6 describe Latécoère's dev workspace and the Qualibot code.

## 1. KA status and timeline

The two sources below differ in dates and wording. They are kept separate on purpose.

### FEOP-3421 (created 2026-09-29) — what customers are being notified of [doc-internal]

- "Your customer [customer_name] is being notified that Knowledge Assistant (KA) and Supervisor Agent (SA) are moving to Legacy status on September 30, 2026."
- "Existing Knowledge Assistant and Supervisor Agent endpoints will continue to work and remain officially supported as Generally Available products — there is no immediate action required today."
- "New feature development is formally paused."
- "Accounts that have used Knowledge Assistant or Supervisor Agent in the last 90 days can continue to create them; all other accounts can no longer create new endpoints."
- "We will finalize the timeline and publish detailed migration guides by the end of November 2026. At that time, the end-of-life date for Knowledge Assistant and Supervisor Agent will be shared. The end-of-life date will be no earlier than February 2027."
- Guidance to account teams: "Do NOT ask customers to stop active production use."

### Internal FAQ, KA/SA Deprecation tab (last updated 2026-09-04) — internal working plan [doc-internal]

| Phase | Current target (verbatim) | Existing endpoints |
|---|---|---|
| Legacy | "Mid-September 2026, targeting September 30" | Continue to work |
| Deprecated | "Tentatively by the end of 2026, subject to the October review and Genie Agents readiness". "We will make an announcement about this date by Oct 18, 2026" | Continue to work |
| End of Support | "H1 2027 (dependent on the above)" | Stop working |

Also in the FAQ:
- Creation rules tighten when Deprecated: "Existing customers (in workspaces with KA/SA endpoints) can create". Customers in workspaces without KA/SA, and new customers, cannot.
- Migration guide: "Not yet. A detailed guide is planned on or before the deprecation date."
- Automation: "No automation is committed today." Even if a replacement agent is created automatically, "customers will still need to move their application to the new API".
- MLflow: "Existing traces will not be migrated".
- KA/SA APIs and SDK "will not move to GA. They will be supported until the End of Support date".
- Maintenance: no new features. "We will provide required updates to keep endpoints working (like if a model we use is retired, we'll upgrade)".
- Pricing: "Their pricing will remain the same until End of Support".

### Slack, 2026-08-31 — deprecation timeline thread [Slack]

- "The *retirement/end of life* date for the service will be further into H1 2027, so customers will have a sufficient time to migrate."

**Terminology:** FEOP-3421 says "end-of-life", the FAQ says "End of Support". The sources don't confirm they are the same date.

## 2. Migration paths offered by Databricks

### A. Genie Agents + Content Search over a UC Volume (recommended low-code path)

- "Content Search over Volumes is now available in beta. This uses the same parsing, chunking, and embedding technology from Knowledge Assistant and creates an index over a volume." [doc-internal, FAQ]
- **Availability: the sources disagree.**
  - FAQ (2026-09-04): "GA will be planned for FY27Q3 (by November 2026)." [doc-internal]
  - Slack (2026-09-30): "we're planning for PuPr in oct/nov and then GA will folow." [Slack]
  - Public docs (2026-10-06): "This feature is in Beta". [doc-public]
- **Vector Search / AI Search indexes are not supported by Genie Agents:**
  - "at this time, that is correct. we may expand genie agent capabilities in the future, but it's not planned to support VS today" [Slack, 2026-09-01]
  - "we aren't looking to support plugging in underlying AI/Lakebase Search indexes directly to the genie agent yet" [Slack, 2026-09-01]
  - "AI Search indexes and custom agents (apps or legacy endpoints) are not planned as a sub-agent in Genie Agents at this time" [doc-internal, FAQ]
- **API change:** "the API will likely change, as noted in the FAQ around migration (we may be able to help setup the genie agent in a similar form, but we cannot migrate the API for them and the agent mode api from genie agents has some differences)." [Slack, 2026-08-31]
- **MLflow:** "we do not have plans to integrate mlflow tracing into genie agents at this time." [Slack, 2026-08-31]
- **Content Search auto-enabled when a Volume is attached to a Genie Agent:** "launching next week!" [Slack, 2026-10-02]

### B. Custom agent

- "For use cases where customers need to combine Genie Agents and other custom agents or AI Search indexes, we recommend building a new custom agent to integrate these together." [doc-internal, FAQ]
- "We will provide content search as a tool [insert some level of detail]" — placeholder in the FAQ, not written yet. [doc-internal]
- Content Search CRUD + query API for custom agents is being drafted: "But we don’t have timeline to launch the API publicly available." [Slack, 2026-09-15]
- "we do have ai_parse_document and ai_prep_search both available as AI functions that can help do some of what KA has done. however, the rest of the KA processing pipeline and then retrieval system are specific to databricks and specific to KA. i don't think open sourcing them would be helpful for anyone" [Slack, 2026-09-08]

### Request to open-source KA (Mehdi, 2026-09-07) [Slack]

Replies in the thread, 2026-09-08:
- "exposing the code of agent bricks doesn't help in that case; someone would still need to figure out how to run and operate that code itself."
- "have you looked at the content search feature we released recently? that's the intended replacement here."

### What this means for Qualibot [deduction]

- **Path A** starts from documents in a Volume, indexed by Content Search. Genie Agents can't use our Vector Search indexes, so the outputs of our parsing pipeline wouldn't carry over:
  - GPU parsing and LLM image description (see `CLAUDE.md`);
  - chunk headers `[Source: REF | …]`, which the app relies on (§6);
  - the AS/IS index split;
  - the Intraqual scope gate.
- **Path A** also requires changing the app's integration (new API).
- **Path B** keeps our indexes and pipeline. We must then rebuild what KA does on top of retrieval (§3).
- The Content Search tool for custom agents has no public timeline, so it can't be planned on.

## 3. How KA works internally

### Instructed Retriever architecture [doc-public]

Blog *Instructed Retriever: Unlocking System-Level Reasoning in Search Agents*, 2026-01-06, read in full on 2026-10-06.

- **Core idea:** propagate "system specifications" to both retrieval and response generation, not just the user query. Specifications are:
  - user instructions;
  - labeled examples of relevant / non-relevant `<query, document>` pairs;
  - index descriptions (the metadata available to filter on).
- It can be called in a static workflow or exposed as a tool to an agent.
- **Three retrieval capabilities:**
  1. **Query Decomposition** — "a full search plan, containing multiple keyword searches and filter instructions".
  2. **Contextual Relevance** — the reranker uses instructions (e.g. recency) to boost documents.
  3. **Metadata Reasoning** — "from last year" becomes `doc_timestamp > TO_TIMESTAMP('2024-11-01')`.
- Response generation is kept "concordant with the retrieved results, system specifications, and any previous user history or feedback".

**Results reported** (Databricks benchmarks, not our corpus):
- **Query generation**, on StaRK-Instruct (e-commerce, 198 queries; inclusion / exclusion / recency instructions):
  - "35–50% higher recall" than the raw query;
  - models tested: GPT5-nano, GPT5.2, Claude4.5-Sonnet, and InstructedRetriever-4B (fine-tuned with TAO / offline RL, recall as reward);
  - InstructedRetriever-4B "almost equals the performance of much larger frontier models, and outperforms the GPT5-nano model";
  - on StaRK-Amazon (no explicit instructions), recall is about 10% above the raw query.
- **End-to-end**, on "a mix of five proprietary and academic benchmarks", each with "a custom quality judge":
  - Instructed Retriever is "more than 70%" better than traditional RAG (RAG on Databricks Vector Search);
  - KA has an "upward of 15% quality gain" over RAG + rerank;
  - in a multi-step agent (Claude Sonnet 4), KA as a tool beats RAG as a tool "by over 30%", with an "average reduction of 8%" in time to completion.

The article attributes KA's gains to persisting the system specifications through every stage. **It does not separate the contribution of the architecture from that of the models.**

Not in the article: the production model per stage, number of queries, top-k, prompts, or the citation mechanism.

### Instructed-Retriever-1 (IR-1) [doc-public]

Blog *3x Faster Search: Parallel Test-Time Scaling with Instructed-Retriever-1*, 2026-06-04, read in full on 2026-10-06.

- "Instructed-Retriever-1 is a single model trained for both retrieval stages: query generation to increase recall and reranking to increase precision, run in parallel to keep latency low."
- **Harness:** user instructions and the index schema are propagated to query and filter generation, reranking, and answer generation.
- **Reranking:** a "multi-pivot groupwise reranker". "Candidates are ranked in parallel groups, each anchored by one or more pivot chunks, and the group rankings are merged into a final ordering."
- **Latency:**
  - "Answer generation time has dropped by 2x, and search time has dropped by more than 3x, bringing Time To First Token (TTFT) to around two seconds";
  - end-to-end "consistently below 10s on our offline eval setup";
  - footnote: averages over offline evaluations, around 256 output tokens; "Actual latency may vary".
- **Quality:**
  - On KARLBench, IR-1 "matches Claude Sonnet 4.5 retrieval quality" (figure, Recall@10; no numbers in the text).
  - On "a large-scale internal dataset representative of Knowledge Assistant usage", with a fixed candidate set and LLM-judge labels on a 0–3 scale: nDCG@10 is **80.1 for Claude Sonnet 4.5 and 81.0 for IR-1**, "gains of +12.8% and +14.1% compared to a setting with no reranking". The article presents both gains as demonstrating its multi-pivot groupwise reranker.
- **Serving:** Mixture-of-Experts, FP8 quantization, speculative decoding.
- **Availability:** "Instructed-Retriever-1 has begun rolling out to all customers" (in the KA context). The article mentions no access outside KA.

**Availability check:**
- No serving endpoint for IR-1 on the dev workspace (46 endpoints listed, 2026-10-06). [verified]
- Glean states IR-1 is internal to KA, and that AI Search has a separate reranker. [Glean]

### Other internals [Glean — not read first-hand, do not cite]

- Hybrid semantic + keyword retrieval.
- Dynamic number of chunks under a token budget, with an internal reference default of 50 chunks max.
- Page-level citations; a doc URI column is required on sources.

### Observed trace [verified, 2026-10-05, one call — sample of 1]

Call to `ka-4d15cb32-endpoint` (ALL) with `databricks_options.return_trace: true`, question "Quelles procédures parlent de qualification CND ?".

| Span | Type | Duration | Observed |
|---|---|---|---|
| `Knowledge Assistant` | AGENT | 7.97 s | root span |
| `examples` | EXAMPLES | 0.28 s | content not exposed |
| `rerank` | RERANKER | 0.70 s | no inputs/outputs exposed |
| `Final_response` | CHAIN | 5.76 s | final answer text |
| `docs` | RETRIEVER | — | outputs 10 chunks; carries attribute `mlflow.databricksHideRetriever` |
| `attribution` | CHAIN | — | `n_attributions` = 7; each has `sent_id`, `start_char_id`, `end_char_id`, `citations[{doc_id, doc_title, cited_text}]` |

- No span exposes the search queries, search type, filters, or candidate count before reranking.
- Retrieved chunks start with the header `[Source: QP-1518 | Title: Qualification and certification of NDT personnel | Division: AS | Category: AS - PROCESSES / PROCESSUS | Date de diffusion: 2026-09-03]`.

### What KA source code would and wouldn't give us [deduction]

- **Would give:**
  - the hidden retrieval logic;
  - exact prompts (planning, rerank, generation, how instructions are wrapped);
  - the attribution algorithm;
  - operational settings.
- **Would not give:**
  - the IR-1 model (not exposed, see above);
  - Databricks-internal infrastructure;
  - quality parity on our corpus without evaluation.
- In Databricks' own evaluation (their harness, their internal dataset), Claude Sonnet 4.5 reranks at near-parity with IR-1 (80.1 vs 81.0 nDCG@10). IR-1's documented advantages are latency and serving efficiency. Whether a rebuild with available LLMs reaches KA quality on Qualibot data is **unverified**.

## 4. Current KA setup — dev workspace `dbc-c623749d-731b` [verified, 2026-10-05/06]

| KA | Endpoint | Served entity | KA id | Experiment | Knowledge source |
|---|---|---|---|---|---|
| `qualibot_ALL_v2` | `ka-4d15cb32-endpoint` | `ka-base-model-74f74ffb` | `4d15cb32-1edb-4f86-aa7c-e6c2e91a9002` | `2208897195815112` | `dev_landingzone.qualibot.chunks_index_v1` |
| `qualibot_AS_v2` | `ka-2ef8a9ac-endpoint` | `ka-base-model-afc72253` | `2ef8a9ac-bf46-4b92-97a1-f2f395eab2f4` | `2208897195815113` | `dev_landingzone.qualibot.chunks_as_index_v1` |
| `qualibot_IS_v2` | `ka-710526e7-endpoint` | `ka-base-model-f51b5c2a` | `710526e7-42ef-4e3b-b9cb-9887a6c12aaa` | `2208897195815114` | `dev_landingzone.qualibot.chunks_is_index_v1` |

- Created 2026-10-05, state `ACTIVE`, endpoint task `agent/v1/responses`.
- Knowledge sources are `source_type: index` (our own Vector Search indexes, not KA-parsed files), with `doc_uri_col: url` and `text_col: chunk_text`.
- Instructions are 6,212 (IS), 6,429 (ALL) and 6,724 (AS) characters, all organized into the same section headings:
  - `Scope (IMPORTANT)` for AS/IS, `Divisions` for ALL;
  - `CRITICAL LANGUAGE RULES`;
  - `Reference ordering (AS)` (AS only);
  - `Sources & metadata`, `Answer format`, `Source citations`;
  - `Conflicting facts across documents (recency)`, `No fabricated links`;
  - `Archived documents (published before 2018)`, `Search language (important)`;
  - `When unsure`, `Safety-critical answers (operators)`.
- Instructions and sources are readable with `databricks knowledge-assistants get-knowledge-assistant` / `list-knowledge-sources`. The serving endpoint config doesn't expose them.
- The older `ka-df2b7829-endpoint` (created 2026-03-25) fails on every call with "Failed to initialize KBQA agent: AI Search endpoint dac68087-9a21-4c16-a64f-10c427e5599d not found." It is not used by Qualibot.

## 5. Index and source table [verified, 2026-10-05/06]

- `dev_landingzone.qualibot.chunks_index_v1`:
  - Delta Sync index on Vector Search endpoint `qualibot`, primary key `chunk_id`;
  - 75,086 indexed rows, `TRIGGERED` pipeline;
  - source table `dev_landingzone.qualibot.chunks_v1`;
  - Databricks-managed embeddings, `databricks-qwen3-embedding-0-6b` on `chunk_text`.
- Source table columns: `IDDOC` (long), `REF`, `division`, `chunk_id`, `chunk_index` (int), `chunk_text`, `chunk_token_count` (int), `chunk_content_type`, `semantic_headers`, `chunk_sha256`, `url`, `doc_date` (date).
- `columns_to_sync` isn't set on the index spec. Whether `division`, `REF`, `doc_date` and `chunk_content_type` can be filtered at query time is **not verified**.

## 6. App ↔ KA contract [verified, code]

**Code version.** This repo was imported on 2026-10-05 from `/Workspace/Shared/Qualibot`. Compared with the dev bundle `/Workspace/Shared/.bundle/qualibot/dev/files` on 2026-10-06:
- `server/services/streaming.py`, `server/routers/chat.py` and `server/services/doc_catalog.py` are identical (hash-compared).
- The bundle has no frontend source. Its built JS (`client/out/assets/index-CDrpgZJ6.js`) uses the same WebSocket contract described below.

**Replacement seam: `stream_chat()`, `server/services/streaming.py:381`.** `server/routers/chat.py` and the frontend don't depend on the KA directly.

### Request sent by `stream_chat()`

`{"input": messages, "stream": true, "databricks_options": {"return_trace": true}}`, with a fallback to the chat format `{"messages": messages, "stream": true}`.

### KA events consumed

| KA event | Use |
|---|---|
| `response.output_text.delta` | Answer text |
| `response.output_text.annotation.added` (`url_citation`) | One source per URL, numbered by first appearance. Marker position is `end_index` / `start_index` if present, else the streamed text length at that moment. A code comment says the KA sends no offsets; not observed first-hand. `streaming.py:605-615` |
| `response.output_item.done` | Real `trace_id`; a `url → REF` map from the RETRIEVER span via `[Source: REF` in `page_content`. `streaming.py:632-660` |
| `response.reasoning_summary_text.delta` | `reasoning_steps`; logs KA-internal retrieval errors |
| `error` (mid-stream, after HTTP 200) | Error message |

### Events emitted by `stream_chat()` — the contract a replacement must keep

```
{"type":"response.output_text.delta","delta":str}               × N
{"type":"sources","sources":[{title,url,doc_uri}],"citations":[{n,pos}]}
{"type":"metadata","trace_id","tool_name","tool_query","tool_result","reasoning_steps"}
{"type":"error","error","error_type","http_status"}              (on error)
[DONE]
```

### Post-processing in `chat.py` (`chat_ws`, KA-independent)

1. `_trim_history` — keeps the last `CHAT_MAX_HISTORY` messages (default 10); the window starts on a user turn.
2. `_with_today_date` — prepends `[Date: YYYY-MM-DD]` to the last user turn.
3. Optional translation bridge (question → EN, answer back).
4. `_apply_citation_markers` — inserts `⟦n⟧` at each `pos`, pushed past links.
5. `augment_sources` (`doc_catalog`) — adds documents named in the prose but never annotated.
6. `_number_sources` — keeps `n` only on inline-cited sources.
7. `_save_turn` — writes to Lakebase `chat_messages`: `sources_json`, `trace_id`, `tool_*`, `reasoning_steps`, `endpoint_name`, `division`, `question_lang`.

Division routing: `_endpoint_for_division` maps ALL / AS / IS to `CHAT_ENDPOINT_*` (`chat.py:45`).

### Frontend

- WebSocket `/api/chat/ws`. Messages handled: `delta`, `done`, `error`; `ping` is ignored.
- `done` carries `content` (with `⟦n⟧` markers) and `sources[{title,url,n}]`. See `client/src/components/chat/ChatView.tsx`.
- `ChatMessage.tsx` turns `⟦n⟧` into links through each source's `n`. Sources without `n` render as plain chips.
- The `/api/chat/stream` (SSE) route exists in the backend but the frontend doesn't call it.

## 7. Open items

- **KA retrieval is a black box.** Parity can only be measured by running a set of real questions through both KA and the custom implementation.
- **Not read:**
  - the deck *Your Options Moving Forward* (Google Slides MCP connection not authorized);
  - "Mini-PRD: ai_search" (Confluence, surfaced by Glean).
- **No read permission** on the KA MLflow experiments (`2208897195815112/13/14`), so no trace history.
- **Unknown:** the content of the KA `examples` step.
- **Not checked:**
  - the AI Search built-in reranker;
  - whether Genie Agents expose a Model Serving endpoint (Glean says no — not first-hand);
  - the KA streaming annotation format (offsets), not observed first-hand.
- **Not seen:** the customer notification email itself (FEOP-3421 only describes it).
- **Unconfirmed:** whether "end-of-life" (FEOP-3421) and "End of Support" (FAQ) are the same date.
- **Not in the repo:** KA instructions were dumped to `/tmp/ka_dump/` only (ephemeral).

## Links

Internal — do not share externally:
- FEOP-3421: https://databricks.atlassian.net/browse/FEOP-3421
- Agent Bricks internal FAQ, KA/SA Deprecation tab: https://docs.google.com/document/d/1OjUrvp7qTtai_AE9OtT4RZuB4rVY7OSI6_fx-e5wmVI/edit?tab=t.5mf2onvcjl3h
- Slack — deprecation timeline thread (2026-08-31): https://databricks.slack.com/archives/C09DKJLT925/p1788191721254709
- Slack — open-sourcing thread (2026-09-07): https://databricks.slack.com/archives/C088VN8U4E5/p1788796253632229
- Slack — "i don't think open sourcing them would be helpful for anyone": https://databricks.slack.com/archives/C088VN8U4E5/p1788883935831339?thread_ts=1788796253.632229&cid=C088VN8U4E5
- Slack — Content Search API / auto-enable thread: https://databricks.slack.com/archives/C077N5FSZDL/p1789461279762909
- Deck — Your Options Moving Forward (not read): https://docs.google.com/presentation/d/1QYbJUGDqzon9NQmK7ZPAA09O26XG-tb3oJse6Bcf-ko/edit?slide=id.g3f84fda0a6b_0_1605#slide=id.g3f84fda0a6b_0_1605

Public:
- Blog — Instructed Retriever: https://www.databricks.com/blog/instructed-retriever-unlocking-system-level-reasoning-search-agents
- Blog — Instructed-Retriever-1: https://www.databricks.com/blog/3x-faster-search-parallel-test-time-scaling-instructed-retriever-1
- Docs — Knowledge Assistant: https://docs.databricks.com/aws/en/agents/agent-bricks/knowledge-assistant
- Docs — Content search: https://docs.databricks.com/aws/en/volumes/content-search
