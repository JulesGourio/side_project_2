# Dev journal — KA → VSI integration

Branch `ka_to_vsi`. Plan: `docs/plan/ka-to-vsi-integration.md`. Every step is logged with what was done and its proof.

## 2026-10-06 — start of implementation

- **Scope:** plan steps 1 → 6.
- **Rule:** one action, one test; no step moves on without its proof.
- **Commits:** none unless asked. All work stays uncommitted on `ka_to_vsi`.

## Step 1 — Division instructions in the repo ✅

- **Done:** `server/config/chat_vsi/instructions_{all,as,is}.md`, written from the live KA instructions (`databricks knowledge-assistants get-knowledge-assistant`). The stray AS authoring note ("▎ Note : j'ai intégré l'ordre de citation QP→MI…", 218 chars at the end) was not copied.
- **Proof:** `python3 scripts/check_vsi_instructions.py` (new, re-runnable) → exit 0.
  - `all`: IDENTICAL (6429/6429 chars);
  - `as`: IDENTICAL (6504 chars, live 6724 with the note excluded);
  - `is`: IDENTICAL (6212/6212).
  - `grep "▎ Note"` = 0 in the 3 files.

## Step 2 — Module `server/services/chat_vsi.py` ✅

**Done:**
- `server/services/chat_vsi.py` — port of the measured `vsi_predict`:
  - French search-query rewrite (falls back to the question alone if the rewrite fails);
  - HYBRID search with question + French query, merged by best rank;
  - passages grouped and numbered by document;
  - division instructions + citation rule;
  - `claude-sonnet-4-6` streamed through the existing `stream_analysis`;
  - `CitationStreamParser` (streaming `[n]` parser with a hold-back buffer);
  - emits the `stream_chat()` contract: deltas, `sources` + `citations`, one `metadata` (`trace_id` = `vsi-<uuid>`), `error`, `[DONE]`.
- **Inputs the v0 never saw, handled explicitly:**
  - the `[Date: …]` prefix chat.py puts on the last turn is kept for the answer but left out of the search and the rewrite;
  - `⟦n⟧` markers stored in earlier answers are stripped from the history.
- **Settings**, read at call time: `CHAT_VSI_INDEX_ALL/AS/IS` (defaults: dev `_v1` indexes), `CHAT_VSI_LLM_ENDPOINT` (default `databricks-claude-sonnet-4-6`), `CHAT_VSI_NUM_RESULTS` (default 10).
- `server/services/streaming.py::stream_analysis`: new optional `operation` argument for the label in user-facing errors. Default `'Document comparison'`, so Compare is unchanged; Chat VSI passes `'Chat'`. Without it, a VSI failure said "Document comparison failed".
- `scripts/capture_vsi_raw_answers.py` → `tests/fixtures/chat_vsi_raw_answers.json`: the module's real raw answers (with `[n]` and the real stream chunking) on the 21 golden cases.
  - This also exercised rewrite + Vector Search + generation end to end against the real services: 21/21 OK.
  - Real data: 148 markers, **100 split across two deltas**, 25 grouped — the hold-back buffer is required.

**Proof (`tests/test_chat_vsi.py`, 52 tests, all passing):**
- parser: split marker, never a partial marker emitted, grouped `[2][5]`, numbering by first appearance, unknown number dropped, `[` that is not a marker (Markdown link, `[Date:`, `[^1]`, `[]`), no citation, incomplete marker at end;
- equivalence on the 21 real answers: streaming == whole-text parse with the real chunking and 25 random re-chunkings each;
- contract: happy path, exactly one `metadata` before `[DONE]` with a `vsi-` trace id, Vector Search 403 → `error` + `[DONE]`, Vector Search timeout → same, LLM error before the stream → same, LLM error mid-stream → no partial marker emitted;
- settings: division → index (ALL / AS / IS / unknown / None), env override, the routing actually uses the IS index, unconfigured index → explicit `ConfigError`, instructions per division;
- inputs: `⟦n⟧` stripped from prompt and rewrite, date out of the search but given to the answer, documents numbered in the prompt, rewrite failure → question-only search.

**Full suite:** 166 passed, 1 skipped, 2 failed. The 2 failures are the pre-existing `test_deploy_config.py` ones (`utils/deploy/target_env.json` absent locally).

**Golden check** (module from the branch synced to `/Workspace/Shared/qualibot-custom`, not deployed):
- new notebook `scripts/qualibot_golden_eval.py` (widgets `engines`, `run_tag`), imported as `/Users/mehdi.lamrani@databricks.com/qualibot-eval/qualibot_golden_eval`;
- criterion: metrics ≥ VSI v0 (15/21, 19/21, 63 %).

| Run (MLflow experiment `qualibot-vsi-vs-ka`) | Correctness | Guidelines | Doc recall |
|---|---|---|---|
| VSI v0, reference (`555745c9`) | 15/21 | 19/21 | 62.7 % |
| Module r1 (`acf89a55`, job `493371470792746`) | 14/21 | 20/21 | 56.0 % |
| Module r2 (`32249ba9`) | 15/21 | 20/21 | 54.3 % |
| Module r3 (`e4a9d743`) | 15/21 | 20/21 | 61.0 % |

- **The criterion is not met literally on recall** (3 runs below 63 %). I did not call that noise without checking. What I checked:
- **Port faithful to the v0**, by reading both codes side by side: same rewrite prompt, `temperature` 0 (`supports_temperature` is true for Claude), 120 / 2000 max tokens, HYBRID, k = 10, merge by best rank, grouping, prompt, instructions. Only differences: streamed answer, and the extra columns requested from Vector Search (projection only).
- **A second v0 run is not usable.** Re-run `d6a64203` (job `931548530516616`, run alongside r2 and r3) has 20 traces in ERROR, all `429 Too Many Requests` on `databricks-claude-sonnet-4-6`; 3 questions never got an answer. The v0 calls `httpx.post` without a retry, while the module goes through `stream_analysis`, which retries a 429 (0 errors in r1–r3).
- **Case by case, the recall gap is two cases:**
  - painting qualification (IF20016);
  - CMP template (NF10065).
  - Sequential probe, 5 module turns + 5 v0-path turns per case (`docs/evidence/step2-variance-probe.py` → `.log`):
    - painting: IF20016 retrieved 5/5 and cited **2/5 by both**, so citation choice varies at generation;
    - CMP: NF10065 cited **2/5 by both**, and only when the French rewrite comes out as « Template du CMP » (E0059MM is retrieved, the answer names NF-10065, `augment_sources` adds it). With « Template du CMP Latécoère » it never is. The rewrite alternates between the two at temperature 0.
    - Given the same rewrite, the module and the v0 path retrieve exactly the same documents.
- **DANAFF** (correctness 0/3 for the module, 1/2 for the v0): the failure mode is an invented expansion of the acronym. Probe `docs/evidence/step2-danaff-probe.py` → `.log`: module 2/5, v0 1/5. It happens only with the rewrite « Définition DANAFF Latécoère », in both paths.
- **Conclusion:** no regression from the port. Same rates as the v0 on every case that differs. The run-to-run spread of one engine is larger than the module/v0 gap: KA recall 58.7 % (`deba4f68`) → 44.4 % (`79229faa`), module 54.3 % → 61.0 %.
- **Observation, not acted on** (out of scope, the plan ports the v0 as measured): the rewrite's nondeterminism (adding « Latécoère ») drives most of the run-to-run variance on CMP and DANAFF.

## Step 3 — Engine switch in `chat.py` ✅

**Done:**
- `chat_ws` body moved into `_run_chat_ws(websocket, engine)`; `/api/chat/ws` → KA (unchanged behaviour), new `/api/chat-vsi/ws` → `stream_chat_vsi`.
- `CHAT_VSI_ENABLED` (default true, needs `CHAT_ENABLED`).
- VSI turns saved with `endpoint_name = vsi-<division>`.
- Errors stored under the right route.
- Translation bridge, citation markers, catalog sources and persistence are shared, not duplicated.

**Proof:**
- `tests/test_chat_vsi_route.py`, 11 tests, all passing:
  - KA route never calls VSI;
  - VSI route calls `stream_chat_vsi` with the division and the dated messages;
  - `done` has `⟦1⟧` + numbered sources;
  - `can_chat=False` → refused;
  - VSI disabled → error, and the KA route is unaffected;
  - VSI engine error relayed + saved `status=error`;
  - `endpoint_name` = `vsi-all` / `vsi-as` / `vsi-is` (unknown → `vsi-all`).
- **Full suite: 177 passed** (114 original + 52 + 11), 1 skipped, the same 2 pre-existing failures.

## Step 4 — Front ✅

**Done:**
- `ChatView` takes an `engine` prop (`'ka' | 'vsi'`). It picks the WebSocket path (`/api/chat/ws` or `/api/chat-vsi/ws`), the welcome title and the header.
- `ChatPage` passes it on. `ChatVsiPage` is now `<ChatPage engine="vsi" />` instead of the placeholder.
- `client/vite.config.ts`: `ws: true` on the `/api` proxy, so the chat WebSocket works locally through Vite.
- **Fixed during this step:** a stream that ended with no text and no error left the message on "Thinking" forever. Fixed on both sides:
  - `chat.py`: `[DONE]` with no text and no error → `error` "No answer was produced. Please try again.", turn saved `status=error` (`EmptyAnswer`);
  - `ChatView.streamChat`: a socket that closes without `done` or `error` → an error message.
  - 2 route tests (empty KA stream, empty VSI stream). **Full suite: 179 passed**, 1 skipped, the same 2 pre-existing failures (re-run at the end: same result).
- **Build OK** (`npm run build` via `npm-proxy.cloud.databricks.com`), 18:48, after the last front change (18:46). Bundle `index-CvcpJ4FJ.js` contains `/api/chat-vsi/ws` and the new close message.

**Proof** — `docs/evidence/step4-local-e2e.log`: WebSocket transcripts through the Vite proxy (`scripts/app_chat_turn.py`, new), plus the matching uvicorn lines.
- **Chat VSI, same question on ALL / AS / IS:**
  - streamed answer, 0 `[n]` marker in the deltas;
  - ⟦n⟧ in `done` (12 / 12 / 3), numbered sources with Intraqual URLs;
  - each division answers from its own index (`chunks_index_v1` / `chunks_as_index_v1` / `chunks_is_index_v1`). IS cites IS documents (PRLAT537_EN, Q0298QR_IS_EN, PRLAT558_EN).
- **Invalid index** (`CHAT_VSI_INDEX_ALL=…does_not_exist_index`):
  - the browser gets an `error` "Document search failed (Vector Search returned 404)." after 5.5 s, not a silent close;
  - the server logs the 404 `RESOURCE_DOES_NOT_EXIST`;
  - Chat KA on the same server still answers.
- **Chat KA:** answers, 0 marker in the stream, numbered sources.
- **UI rendering** was checked earlier in a browser: superscripts, source chips, error message instead of "Thinking" (`docs/evidence/chat-vsi-local-ndt.png`, `chat-vsi-local-error.png`). Since then, at the user's request, proofs are terminal logs and transcripts.

## Step 5 — Deployment of `qualibot-custom` ✅

**Done:**
- `databricks sync . /Workspace/Shared/qualibot-custom`:
  - `target_config.env` (gitignored) still in place, and it does not override any `CHAT_VSI_*` setting;
  - `chat_vsi.py` in the workspace is identical to the local one.
- `apps start`, which rebuilt deployment `01f1c1b140921d03a402f07a7299c22e` from the same synced folder.
- `apps deploy` → deployment `01f1c1b1704f189ea57526ff99c400d1` `SUCCEEDED`, app `RUNNING`.

**Proof:**
- `docs/evidence/step5-app-turns.log`: same question over the deployed app's WebSocket (CLI OAuth token).
  - VSI ALL / AS / IS answer: 402 / 387 / 237 deltas, 0 marker in the stream, 12 / 12 / 3 numbered sources, Intraqual URLs.
  - KA ALL answers.
- `docs/evidence/step5-app-logs.log` (`databricks apps logs qualibot-custom`):
  - `chat turn done [ws]: engine=vsi division=… endpoint=vsi-all` / `vsi-as` / `vsi-is`, each on its own index;
  - KA turn on `ka-4d15cb32-endpoint`;
  - no Vector Search or LLM error.
- Saved turns read back through `/api/chat/sessions/<id>`: the VSI AS and IS sessions have 2 messages each, with 14 and 3 sources.
- **App SP rights** (⏳ in the plan) are confirmed **by function, not by reading grants** (I have no READ METADATA on `dev_landingzone` and no access to `system.access`):
  - in the app, `_get_chat_credentials` takes the SDK token first, which is the app's SP;
  - the SDK does authenticate as the SP there (Lakebase connects with `WorkspaceClient()`);
  - the 3 indexes answered and the LLM streamed, so the SP has USE_CATALOG `dev_landingzone`, USE_SCHEMA, SELECT on the 3 indexes and access to `databricks-claude-sonnet-4-6`.
- **Startup line** `ERROR … Lakebase schema ensure failed — continuing with existing schema: must be owner of table messages`:
  - not from the branch: `lakebase.py` is unchanged, and the code handles this exact case ("ownership mismatch on one table");
  - the qualibot-custom SP is not the owner of the tables of Jules's `doccompare` database;
  - turns are saved (no "Chat DB save failed").

## Step 6 — Non-regression against the KA ✅

**Done:** golden notebook (identical to `scripts/qualibot_golden_eval.py`), `engines=ka,vsi`, `run_tag="step 6 (deployed code)"`, on the app code as synced and deployed. Job `156661197788493`. 21/21 traces OK on both engines (no 429).

| Run | Correctness | Guidelines | Doc recall |
|---|---|---|---|
| VSI, deployed code (`8f487404`) | **15/21** | **20/21** | **61.0 %** |
| KA, same run (`e75756f6`) | 12/21 | 19/21 | 54.9 % |
| VSI v0, reference (`555745c9`) | 15/21 | 19/21 | 62.7 % |

- Against the KA: better on all three metrics.
- Against the v0: correctness equal, guidelines +1. Recall −1.7 pts, which is −0.25 summed over the 15 cases. The per-case diff shows where that comes from:
  - painting (IF20016 not cited) — measured at 2/5 for both the module and the v0 path in step 2;
  - work centers (GO1594 not cited: 6 documents listed instead of 9–10 in r2/r3) and the inspector case (+1 document) cancel out.

## Final tally (2026-10-06)

| Step | Status | Proof |
|---|---|---|
| 1 — Instructions | ✅ | `scripts/check_vsi_instructions.py` exit 0 |
| 2 — Module | ✅ | 52 unit tests; golden ×3 + variance probes (`docs/evidence/step2-*.log`) |
| 3 — `chat.py` | ✅ | 11 + 2 route tests |
| 4 — Front | ✅ | build; `docs/evidence/step4-local-e2e.log` |
| 5 — Deployment | ✅ | `docs/evidence/step5-app-turns.log`, `step5-app-logs.log` |
| 6 — Non-regression | ✅ | golden on deployed code: VSI 15/20/61 % vs KA 12/19/54.9 % |

**VSI module over its 4 golden runs** (r1–r3 + step 6):
- correctness 14 / 15 / 15 / 15;
- guidelines 20/21 each time;
- recall 56.0 / 54.3 / 61.0 / 61.0 %.

**KA over 3 runs:**
- correctness 13 / 12 / 12;
- guidelines 20 / 21 / 19;
- recall 58.7 / 44.4 / 54.9 %.

**Left open (not blocking, not done):**
- The "≥ v0" criterion is met on correctness and guidelines. On recall it is met only once run-to-run variance is accounted for, as measured in step 2 — not run by run.
- The French rewrite is nondeterministic (« … Latécoère » or not). It drives the CMP and DANAFF variance. Stabilizing it would be a change to the v0 and needs its own measurement.
- Pre-existing, seen during the work: the source title "Q0197QP." keeps a trailing period (`augment_sources`), and the 2 `test_deploy_config.py` failures (`utils/deploy/target_env.json` absent locally).
- Nothing committed. Everything is on `ka_to_vsi`, uncommitted.
