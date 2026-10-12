# KA usage scan — Qualibot

Scan date: 2026-10-06. Inventory of every place the Qualibot project uses or depends on the Knowledge Assistant (KA).

## Scope

- **Local repo** — this folder, imported 2026-10-05 from `/Workspace/Shared/Qualibot` (200 files).
- **Dev bundle** — `/Workspace/Shared/.bundle/qualibot/dev/files` on `dbc-c623749d-731b` (229 files, of which 81 are absent locally). Exported read-only on 2026-10-06.
- **Identical in both** (hash-compared 2026-10-06): the KA-related app code — `server/services/streaming.py`, `server/routers/chat.py`, `server/services/translation_bridge.py`, `server/services/doc_catalog.py`.
- **Frontend:** source exists only locally. The bundle's built JS (`client/out/assets/index-CDrpgZJ6.js`) uses the same WebSocket contract.
- Paths under `utils/knowledge_assistant/`, `utils/ka_legacy/`, `utils/ka_legacy/`, `utils/evaluation/`, `utils/stress_test/`, `utils/deploy/` and `utils/evaluation/multilingual_audit/` exist **only in the dev bundle**.

Tags:
- **[code]** — read in the code
- **[verified]** — checked live on the dev workspace
- **[doc-projet]** — written by the project team in the repo, not verified by us

## 1. App at runtime [code]

1. **`stream_chat()`** — `server/services/streaming.py:381`. The only KA call in the app: `POST {host}/serving-endpoints/{ka}/invocations` with `{"input": messages, "stream": true, "databricks_options": {"return_trace": true}}`; fallback `{"messages": …}`.
2. **WebSocket `/api/chat/ws`** — `chat.py:548`, calls `stream_chat` at `:644`. The route the frontend uses (`ChatView.tsx:30`).
3. **SSE `POST /api/chat/stream`** — `chat.py:367`, calls `stream_chat` at `:445`. Same processing, **not called by the frontend** (local source and bundle JS).
4. **Division routing** — `_endpoint_for_division`, `chat.py:45-56`: ALL / AS / IS → `CHAT_ENDPOINT_*`, fallback `CHAT_ENDPOINT`.
5. **Credentials** — `_get_chat_credentials`, `chat.py:199-219`: `DATABRICKS_TOKEN` env → SDK `Config()` (app service principal) → `x-forwarded-access-token` as last resort.
6. **Text** — `response.output_text.delta`, with mojibake fix (`streaming.py:563-572`).
7. **Citations** — `response.output_text.annotation.added` (`url_citation`) → sources + `{n, pos}` (`:574-615`).
8. **Trace** — `response.output_item.done`: real `trace_id`; `url → REF` map from the RETRIEVER span via `[Source: REF` (`:617-660`).
9. **Tool calls** — `function_call` → `tool_name`, `tool_query` (`ka_query` / `query`) (`:619-625`).
10. **Reasoning** — `response.reasoning_summary_text.delta` → `reasoning_steps`; logs KA-internal retrieval errors ("Vector search failed", "already running") (`:662-673`).
11. **`response.completed`** — TOOL / RETRIEVER span extraction (`:675-755`). Per code comments the KA never sends its trace there; legacy path.
12. **Mid-stream `error`** after HTTP 200, e.g. KA 429 (`:757-777`).
13. **Retries and errors** — 429 and 5xx retries, 400/422 format fallback, timeout (`:409-463`, `:801-810`).
14. **WebSocket ping** during slow KA calls, so proxies don't drop the socket (`chat.py:668-680`).

## 2. Workarounds for KA behaviour [code]

15. **`_with_today_date`** (`chat.py:171`) — prepends `[Date: YYYY-MM-DD]`: "The Knowledge Assistant endpoint has no way to know the current date".
16. **Translation bridge** (`translation_bridge.py`):
    - third-language questions are translated to EN because KA retrieval embeds the raw question;
    - the answer language is checked because the KA answered in the wrong language (session `5bd9382a`, 2026-07-08).
17. **`augment_sources`** (`doc_catalog.py`) + `utils/deploy/build_doc_catalog.py` — the KA only annotates a subset of the documents it names in prose.
18. **`_apply_citation_markers` / `_number_sources`** (`chat.py:91-157`) — `⟦n⟧` markers from KA citation positions.
19. **`_trim_history`** (`chat.py:160`) — max `CHAT_MAX_HISTORY` (10) messages sent to the KA.
20. **Frontend `stripFootnotes`** (`MarkdownRenderer.tsx:74-104`) — removes footnotes and HTML "that Knowledge Assistants inject". **`linkifyCitations`** (`:113-136`) is the fallback when no `⟦n⟧` markers are present.

## 3. Persistence of KA outputs [code]

21. **Lakebase `chat_messages`** — columns `trace_id`, `tool_name`, `tool_query`, `tool_result`, `reasoning_steps`, `endpoint_name`, `sources_json` (`lakebase.py:483-506`; written by `_save_turn`, `chat.py:265`).
22. **KA errors** → `errors` table (`chat.py:511`, `:706`).

## 4. Configuration and deployment [code]

23. **`app.yaml`** — `CHAT_ENABLED`, `CHAT_ENDPOINT="ka-1a2a9a4f-endpoint"` (default; **absent on the dev workspace** [verified]), `CHAT_ENDPOINT_ALL/AS/IS` empty, `CHAT_TRANSLATE_*`, `CHAT_MAX_HISTORY`.
24. **`utils/deploy/target_env.json`** (bundle) — single source of truth for the KA endpoints per target (§10).
25. **`utils/deploy/render_target_config_env.py`** (bundle) — renders `target_config.env`, which `start.sh:62-69` sources. It fixes a real bug: a CRLF-written `\r` in `CHAT_ENDPOINT` broke the KA request URL.
26. **`utils/deploy/deploy_qualibot.ps1`** and **`bitbucket-pipelines.yml`** (bundle) — render `target_config.env` for uat, uat-test, prod.
27. **Local `target_config.env`** (gitignored) — copied from the bundle: dev endpoints, translation bridge on.

## 5. KA provisioning [code]

28. **`utils/knowledge_assistant/ka_profiles.py`** (bundle) — display name, description, instructions and source description for ALL / AS / IS. **The 3 live dev KAs match it exactly** — instructions, description, source description [verified 2026-10-06].
29. **`provision_knowledge_assistant_job.py`** (bundle):
    - create by `display_name`, or update description / instructions;
    - attach the index source (`text_col=chunk_text`, `doc_uri_col=url`);
    - merge KA permissions: CoreAdmin / CoreDev CAN_MANAGE, extra users CAN_MANAGE, app SP CAN_QUERY;
    - optional `GRANT_ON_ENDPOINTS` grants directly on the serving endpoint.
30. **Jobs** `provision_knowledge_assistant_dev` / `_uat_test` / `_prod` — bundle `databricks.yml` l.667 / 1538 / 1732, manual trigger only. The dev job runs as SP `fde6ff28-739f-4a41-b61e-604a298c8478`, which is the creator of the 3 dev KAs [verified].

## 6. Observability and quality on KA traces [code]

31. **Job D_2 — `utils/ka_legacy/migrate_traces_to_uc.py`** (`resources/traces_migration.yml`, qualibot-uat):
    - copies KA MLflow traces into UC tables;
    - source experiments: ALL `4171178917767011` (ka-7679a56e), IS `2748374992560665` (ka-1560aded), AS `2748374992560664` (ka-3a7e9255), test `3375946803618197` (ka-99026e27);
    - `to_migrate: "trace_test"` in the YAML.
32. **Job D_3 — `utils/ka_legacy/score_production_qa.py`** (`resources/quality_scoring.yml`):
    - scores stored turns with MLflow judges, without calling the KA ("The assistants are not called");
    - reads the RETRIEVER step of the KA's own trace (`assistant_retrieval`);
    - optional `feedback_to_agent_traces` also attaches verdicts to the KA trace.
33. **`utils/evaluation/score_production_qa.py`** (bundle, earlier version) — direct Luna judge calls; `fetch_real_trace_hits()` reads the KA trace via `mlflow.get_trace`.
34. **`mlflow_genai_eval_qualibot_uat.py`** (bundle) — registers continuous-monitoring scorers on the KA's 3 native experiments with `sample_rate=1.0`, and `ENABLE_CONTINUOUS_MONITORING = True`. Per its own comment, this is an ongoing cost.
35. **`sync_mlflow_scorer_assessments.py`** (bundle) — copies those assessments into `ka_mlflow_scorer_assessments`; feeds job `sync_mlflow_scorer_assessments_uat`.
36. **`backfill_trace_ids.py`** — recovers real `trace_id`s from the KA experiments, without calling the KA.

## 7. Evaluation scripts that call the KA [code]

37. **`utils/evaluation/Evaluate_Knowledge_Assistant.py`** (bundle) — golden dataset + MLflow judges; default endpoint `ka-7679a56e`; `call_assistant` at l.886.
38. **`utils/evaluation/Build_Golden_Dataset.py`** (bundle) — queries `ka-7679a56e` with `return_trace` (l.643-648).
39. **`mlflow_genai_eval_qualibot_uat.py`** (bundle) — calls the 3 UAT KAs with `real_usage`, `synthetic_retrieval` and `trap` question groups (l.159-160).
40. **`generate_synthetic_retrieval_questions.py`** (bundle) — reads the KA retriever top-K from the trace.
41. **`analyze_feedback_failures.py`** — replays negative-feedback questions on the KAs (l.27-31, 94-95).
42. **`eval_rag_vs_agent.py`** — local RAG vs KA `ka-7679a56e`. Local and bundle versions differ.
43. **`utils/stress_test/Load_Test_Knowledge_Assistant.py`** (bundle) — concurrent load test of `ka-7679a56e`; detects 429s and silent retrieval failures.

## 8. Investigation probes [code]

All in `utils/evaluation/multilingual_audit/` (bundle).

44. **`probe_ka_request_collision.py`** — `ka-7679a56e`.
45. **`probe_ka_routing_consistency.py`** — `ka-087d89b6` (test KA with 3 sources).
46. **`probe_ka_endpoint_diagnostics.py`** — `ka-0c98558f` (2-source AS/IS KA).

## 9. KA behaviours observed by the team [doc-projet]

Sources: `multilingual_audit/README.md`, code comments, script headers. Not verified by us.

47. **Language bias** of `HYBRID` retrieval for non-fr/en questions.
48. **Non-deterministic source selection with several knowledge sources**: 10 of 16 AS-tagged calls (62.5%) queried the wrong source, with no error. This is the stated reason for one single-source KA per division.
49. **Incident 2026-07-16 on the prod KA `ka-7679a56e`**: "Vector search failed … Request id …-0 already running" (parallel sub-queries colliding). "heavy load" errors also appeared under concurrency. In both cases the answer still arrives, missing some sources.
50. **No clock** (the KA can't know the date), and mid-conversation language switches.
51. **Trace location**: the trace arrives at `output_item.done`, not `response.completed` (app fix 2026-09-03).
52. **Untraced failures**: a 429 is rejected before the KA runs, so there is no trace in its experiment. A silent retrieval failure is logged with all steps OK (empty RETRIEVER, no RERANKER).
53. **KA ACLs don't reach the endpoint**: KA-level permissions don't reach the serving endpoint (DEV 2026-10-05), hence `GRANT_ON_ENDPOINTS`.

## 10. KA endpoints referenced in the project

| Target | ALL | AS | IS | Source |
|---|---|---|---|---|
| dev | `ka-4d15cb32` | `ka-2ef8a9ac` | `ka-710526e7` | `target_env.json` [verified live] |
| uat | `ka-7679a56e` | `ka-3a7e9255` | `ka-1560aded` | `target_env.json` [code] |
| uat-test | `ka-7679a56e` | `ka-3a7e9255` | `ka-99026e27` | `target_env.json` [code] |
| prod | `ka-5d35f4d5` | `ka-c9dbc4db` | `ka-3315943a` | `target_env.json` [code] |

Test / legacy endpoints in scripts and config: `ka-087d89b6`, `ka-0c98558f`, `ka-1a2a9a4f` (app.yaml default), `ka-71794b8e` (app.yaml comment).

## 11. Inconsistencies found

1. **Stray authoring note in the live AS KA instructions.** They end with "▎ Note : j'ai intégré l'ordre de citation QP→MI directement dans le prompt de KA-AS …". Present in both `ka_profiles.py` and the live `qualibot_AS_v2` [verified].
2. **uat-test IS endpoint.** It is `ka-99026e27`, which `Migrate_KA_Traces_To_UC.py` describes as a "disposable assistant used to validate the job". To check.
3. **Token comment vs code.** `provision_knowledge_assistant_job.py` l.29-32 says the app calls the KA "with the end user's own forwarded token". `_get_chat_credentials` uses the app SP first.
4. **Prod KAs.** `tests/test_deploy_config.py` says prod "has no workspace/KA agents yet"; `target_env.json` defines 3 prod KA endpoints.
5. **Compare impact attributed to the KA.** `AboutView.tsx:116, 143, 151` and `client/public/content/home-content.json:41` say the knowledge assistant identifies impacted documents. Compare impact search uses Vector Search + an LLM (`vector_search.py`); the KA variant was dropped 2026-07-20 (`client/src/components/compare/README.md`).
6. **Outdated prompt doc.** `client/src/components/chat/chat_system_prompt.md` describes a `[Division: …]` prefix that is now a no-op (`chat.py:291-293`). It also lacks 5 sections present in the live KAs: `Source citations`, `Conflicting facts across documents (recency)`, `No fabricated links`, `Archived documents (published before 2018)`, `Search language (important)` [verified].
7. **Local test can't run.** `tests/test_deploy_config.py` reads `utils/deploy/target_env.json`, which is absent from the local repo.

## 12. Tests [code]

- `tests/test_chat.py` — mocks `stream_chat` and `CHAT_ENDPOINT`; no real KA call.
- `tests/test_citation_markers.py` — `_apply_citation_markers`.
- `tests/test_deploy_config.py` — per-target overrides in `target_env.json` (see §11.7).

## 13. Documentation mentioning the KA

- `client/src/components/chat/README.md`, `client/src/components/chat/chat_system_prompt.md` (§11.6).
- `README.md`.
- `utils/README.md`, `utils/evaluation/chat_citation_feedback_loop.md`.
- `utils/evaluation/multilingual_audit/README.md` (bundle).
- `docs/architecture.html` (bundle version describes one KA per scope).

## 14. Checked — not KA

- **Compare** — `compare.py`, `vector_search.py`, `summarize.py`, `impact_queries.py` call Model Serving LLMs and Vector Search. The `impact_queries.py` docstring still mentions the old KA "V1" flow.
- **Chat feedback** (`chat.py:745-784`), **sessions and sharing** — Lakebase only; nothing goes to the KA or MLflow.
- **Bundle `lakebase.py` diff** — adds `impact_feedbacks` (Compare); not KA.
- **`dev_copy/`, `lakebase_sync/`**, and `probe_index_language_bias.py` / `probe_embedding_geometry.py` — no KA call. The two probes target Vector Search and embeddings.
- **Parsing pipeline** — feeds the indexes the KA reads, without calling the KA. Per the provisioning job comment, the KA picks up index refreshes on its own.
