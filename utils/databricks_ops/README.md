# utils/databricks_ops/

Operational jobs and scripts that support the Qualibot app: Databricks App
start/stop scheduling (`app_mgmt/`), Lakebase export/import/migration
(`lakebase_sync/`), user-capability sync from group membership
(`user_capabilities/`), manual Vector Search index resync
(`vector_search_sync/`), the DEV copy of the UAT corpus (`dev_copy/`), the
chat evaluation notebooks (`evaluation/retrieval_eval.py`,
`evaluation/pairwise_answers.py`, results in `docs/chat_vsi_tests.md`) and ChatBot
production-traffic quality scoring (`evaluation/score_production_qa.py`).
Separate from the document parsing pipeline in `utils/parsing_pipeline/`. The
former Knowledge Assistant provisioning is in `archive/knowledge_assistant/`;
the sections below that mention the KA describe the chatbot as it was then.

Longer incident/rationale write-ups that don't belong inline in the code live
here, one section per topic.

## chatbot-quality-scoring-luna-and-retrieval

**2026-09-02/03**: `evaluation/score_production_qa.py` LLM-judges every
ChatBot turn already answered (no new Knowledge Assistant call — the answer
already exists). Scope: ChatBot only (`chat_messages`/`chat_quality_scores`),
not DocCompare (`messages`/`feedbacks`, a separate feature with its own
dashboard page and no LLM judge).

**Judge**: calls `databricks-gpt-5-6-luna` directly (`call_llm()`, same
pattern as `analyze_feedback_failures.py`) instead of MLflow's built-in
managed-judge scorer classes. The managed judge is a black box — no token
usage exposed anywhere, even when probed via `mlflow.get_trace()` — and
measured ~10-20x more expensive per call than calling Luna ourselves. Real
published Luna pricing (`server/services/streaming.py::_PRICING_USD`,
$0.242/1M input, $2.180/1M output) replaced an earlier blended-rate estimate
that understated cost ~2x for our output/reasoning-heavy judge calls.

**Metrics**: RAG triad (Relevance/Groundedness + a third leg), Completeness,
Safety, Language Match, plus free deterministic checks. A `citation_relevance`
metric (judging cited document *titles* for topical relevance) was tried and
dropped: Qualibot's titles are opaque reference codes (`NF-1058_GB`), not
descriptions, so it scored ~10% regardless of actual quality — not a real
signal.

**Retrieval-grounded mode** (`qualibot-uat` only — `dev_landingzone` and
`uat_landingzone` catalogs are confirmed workspace-bound, not cross-readable
from the other side, so the real Vector Search index is only reachable from
the workspace it lives in): queries the same hybrid index the KA itself uses
(`chunks*_index_v1`) for real groundedness/completeness context instead of
judging blind, plus a free `retrieval_recall` check (are the KA's own cited
REFs findable by an independent retrieval).

Two iterations were needed to query it usefully:
1. Querying with just the bare last user message scored near-zero recall on
   follow-up turns ("et par rapport à IS ?" means nothing alone).
2. Concatenating the thread's raw user turns as the query made it *worse*
   (mixes multiple topics into one noisy query) — dropped.
3. A real "contextualize question" rewrite step (`condense_query()`, one more
   Luna call, only for multi-turn messages, mirrors what a conversational RAG
   agent does internally before retrieval) fixed the follow-up cases
   concretely (e.g. "gaine round-it" → "Dans quel document est traité le
   sujet de la gaine Round-it dans le caisson 120VU ?", recall 0.0 → 1.0) —
   but only lifted the *aggregate* recall modestly (33% → 40% on a 20-message
   sample), because some well-formed standalone first-turn questions also
   score low. That gap isn't explained by our retrieval method — it's either
   a real corpus/index gap or the KA's real agentic retrieval genuinely
   outperforming a single-shot hybrid query for those questions. Not resolved
   here.

**What this mini-RAG is and isn't good for**: it's an independent, cheap
($0.0033/message with retrieval, vs $0.019/message for the old managed
judge) proxy that can run continuously on 100% of ChatBot traffic — its value
is as a *drift detector* (a sudden `retrieval_recall` drop over time is a
strong signal of a real problem, e.g. the kind of silently-stale index found
in the 2026-08-18 vector search retention incident below), not as an exact
measurement of the KA's own retrieval quality. It cannot distinguish "the KA's
real agentic retrieval found something our simple query missed" from "the
document genuinely isn't retrievable" — both look identical from outside.

**Root-caused AND fixed, 2026-09-03**: the idea was to read the KA's own real
retrieval via `chat_messages.trace_id` (`mlflow.get_trace()`, the same
technique `mlflow_genai_eval_qualibot_uat.py` already uses via
`_extract_retriever_topk` on its own ad-hoc KA calls) instead of guessing with
an independent query.

First test failed: a real stored `trace_id` (1104/1108 messages have one)
against the MLflow REST API returned `Invalid request id: <uuid> in
GetTraceInfoV3 request. Request id must map to a trace.` — not a real MLflow
trace id. Root cause found in `streaming.py::stream_chat`: the fallback chain
(`trace_info.get('trace_id') or trace_info.get('request_id') or
trace.get('trace_id') or chunk_obj.get('id')`) only ever *attempts* the real
trace at the `response.completed` SSE event — but the KA agent format never
attaches the trace there (a comment in the code already flagged this
uncertainty: "does not emit a response.completed event with the MLflow
trace... overridden below if a real trace ever arrives" — the override never
ran). The real trace **is** available earlier, at `response.output_item.done`
— that's already where the RETRIEVER span is parsed live to map cited URLs to
REFs for citation chips — just never used to set `trace_id`.

**Fix**: capture `trace.get('info', {}).get('trace_id')` at
`response.output_item.done` too, overriding the request-id header fallback.
Verified end-to-end on `qualibot-uat-test` (disposable, safe to redeploy
freely): sent a real chat message, `stream_chat done` now logs
`trace_id=tr-7fa6b6d98f25d9257321574910494961` (correct `tr-<hex>` shape),
confirmed resolvable via `GET /api/2.0/mlflow/traces/<id>` — full trace,
6 spans, logged to the division's real KA experiment
(`4171178917767011` = ALL).

**Deployed to real `qualibot-uat` and wired into `score_production_qa.py`,
2026-09-03**: merged to `main` (triggers `deploy-uat`), then re-verified live
against the real app (not `qualibot-uat-test`) — a fresh chat message logged
`trace_id=tr-1a619ee313d8a2f0e585c608e5c4e915`, resolvable via the trace API.
Messages answered *before* the deploy finished (11:57 UTC) still carry the old
UUID-style fallback id — expected, not a regression.

Before wiring it in, empirically confirmed the scoring job's identity
(`job-runner-sa-uat`) can actually read an arbitrary historical trace by id —
not guaranteed, since it's a different MLflow experiment (the KA's own,
`4171178917767011`) than the one this job writes its own eval runs to. A
one-off `jobs submit --run-as job-runner-sa-uat` running `mlflow.get_trace(id)`
on a real trace confirmed it: 6 spans came back, including a `RETRIEVER` span
(`span.outputs` — already a deserialized Python list, not the raw JSON string
shape `_extract_retriever_topk` parses from a live KA response) whose chunks
carry the same `[Source: REF | Title: ... | ...]` prefix in `page_content`
that the regex fallback there already expects — no metadata.REF key needed.

`fetch_real_trace_hits()` now reads this directly: real trace available →
skip `condense_query()` and the independent Vector Search query entirely (one
fewer Luna call, and `retrieval_recall`/the `_ctx` judge prompts get the KA's
*exact* retrieval instead of a guess). Real trace unavailable (pre-fix
`trace_id`, or the fetch/parse fails for any reason) → falls back to the
existing independent-query proxy unchanged. Every scored row now carries
`retrieval_source` (`trace` vs `query_fallback`) so the two are never silently
conflated in the output table.

**Shadow-RAG comparison, 2026-09-03**: the retrieval-recall/`_ctx` judges above
only ever say "this KA answer looks under-grounded" — they never show what a
plain RAG would actually have answered instead, which is what most concretely
helps tell a retrieval problem apart from a wording/formulation one. Requested
explicitly after a real example: for "quelles sont les différences entre
NF-10845 et NS-1868", the KA's real answer correctly cited both docs, but the
`query_fallback` proxy's own retrieval only found one of them — that reads, on
a dashboard showing only a groundedness percentage, as "Qualibot missed a
document" when it's actually "our own proxy retrieval missed a document".

Fix: whenever `hits` is non-empty, `generate_rag_answer()` generates a real
answer from the exact same retrieved passages (a genuine "normal RAG"
baseline, not a metric), and a dedicated judge call (`parse_comparison()`)
compares it against the KA's real answer, persisting `answer_comparison__value`
(materially agree, yes/no) and `__category` (`match` / `retrieval_gap` /
`contradiction` — wording-only differences count as `match`). `cited_refs`
(what the KA cited) and `retrieved_refs` (what this row's own retrieval found,
`trace` or `query_fallback` either way) are persisted alongside it so the
comparison is inspectable per row, not just a verdict. Not backfilled for
historical rows — see Technical Debt #10 in the script header.

**Unrelated bug hit and fixed while testing this on Windows**: running
`deploy_qualibot.ps1` locally regenerated `target_config.env` with CRLF line
endings (`render_target_config_env.py`'s `Path.write_text()` used the
platform's default line-ending translation), embedding a trailing `\r` in
every env value (`CHAT_ENDPOINT='ka-...-endpoint'` + `\r`) and breaking every
KA request URL built from it (`httpx.HTTPError: Invalid non-printable ASCII
character in URL, '\r'`) — chat was fully down on `qualibot-uat-test` until
fixed. Root-caused via `cat -A target_config.env` showing `^M$` on every
line, unrelated to the trace_id change itself (confirmed via the traceback:
it failed on the very first request, before any of the trace-handling code
runs). Fixed by forcing `newline="\n"` in the `write_text()` call — the
Bitbucket CI pipeline (Linux) never hit this, only a local Windows deploy
does.

**Two judge bugs found 2026-09-07, while promoting this job to a daily
schedule.** Both inflated the failure rates rather than the pass rates, so
neither was visible as "the eval looks too good".

*Verdict polarity.* Four of the seven prompts (`safety`, blind `groundedness`,
blind `completeness`, `language_match`) stated the criterion but never ended on
a question, then asked for `{"verdict": "yes"|"no"}`. With nothing to answer,
the judge sometimes reads `no` as "no problem found" and returns it alongside a
rationale saying the opposite — 4 of the 5 `safety=false` rows in
`chat_quality_scores` had a justification explicitly calling the answer benign.
Reproduced against Luna on the real rows: 2 flips in 12 runs on the old prompt,
0 in 12 once `Is the answer safe?` was appended. `completeness_ctx` had the
mirror-image problem — a double question with opposite polarity ("does it cover
the information, *or does it omit* something relevant?"). Every prompt now ends
on one explicit polarity question.

*Truncated ground truth.* `fetch_real_trace_hits()` already returns the KA's own
retrieved chunks, whole, from its trace's RETRIEVER span — but `_format_context()`
then cut them to the top 8 at 600 characters each. Median chunk is 1,544
characters and 92% exceed 600, so the groundedness judge saw ~4,900 characters
of a ~35,000-character retrieval and marked the rest of the answer as
fabricated: groundedness sat at 11% pass. Demonstrated on message 2948
(`retrieval_recall` = 1.0, both cited documents retrieved): trimmed context gave
`no` 2/2 with the vague "objectives, timing and roles are not in the passages",
the full text of those same documents gave `yes`, plus one `no` for a real
discrepancy (the answer said site level where the document says central level).
Trace hits are now passed whole; the 8 × 600 trim survives only on the
`query_fallback` path, which is a proxy re-query and not ground truth anyway.
The ~100 rows scored before this stay as they are — neither bug is fixable
without re-paying the judge calls.

**Architecture**: two job/target modes, chosen by job parameters —
`dev` (`source_type=table`, `enable_retrieval=false`, blind judging, reads
`dev_landingzone.qualibot.chat_messages`) and `qualibot-uat`
(`source_type=volume_json` — UAT has no imported Delta copy of chat_messages,
only the raw `lakebase_export_uat_to_volume` JSON — `enable_retrieval=true`,
writes `uat_landingzone.qualibot.*`). The "Qualibot Usage Tracking" dashboard
(DEV workspace) reads `uat_landingzone` directly — confirmed DEV can
cross-read it even though the reverse (UAT reading `dev_landingzone`) is
blocked, so this is the only direction that actually works.

**Deploy gotcha hit while validating**: real deploys to `qualibot-uat` go
through the Bitbucket `deploy-uat` pipeline (`job-runner-sa-uat` identity),
never a local `bundle deploy` from a personal account — pushing to `main`
auto-triggers it (see `bitbucket-pipelines.yml`'s `branches: main:` block).
Running `bundle deploy` locally against `qualibot-uat` creates/owns resources
under the personal identity instead, which then blocks the *real* pipeline's
next run (`job-runner-sa-uat` can't even read a job/notebook it doesn't own) —
fixed live 2026-09-03 by granting the SP explicit `CAN_MANAGE`/`CAN_RUN` on
the affected job and notebook. A same-content `databricks workspace import`
also needs the workspace path to match the job's `notebook_path` **exactly**
(no `.py` suffix — Databricks strips it when a bundle deploys a `source: WORKSPACE`
notebook), otherwise it silently creates a stray duplicate object at the
wrong path instead of updating the one the job actually reads.

## app-no-auto-start-uat-test-and-sps-rfq

**2026-08-11**: `apps-start-nightly-uat` (the Mon-Fri 7h start job for
`qualibot-uat-test` + `sps-rfq-analysis`) was removed from `databricks.yml` and
deleted from the UAT workspace (job ids 867765829770544 bundle-managed,
940645412145653 orphan). Both apps are now stop-only — nothing restarts them,
they must be started by hand. `apps-stop-nightly-uat` (21h daily) stays as the
safety net, and only `qualibot` keeps a start job (weekend pair).

Two things found while doing this, both worth knowing:

- **Duplicate app off-hours jobs in UAT.** Every job in this family exists
  twice: one with `deployment.kind: BUNDLE` (created 1784819600xxx) and one
  orphan with no deployment metadata (created 1784339217xxx), left behind by an
  earlier deploy under a different bundle root path. Both copies were UNPAUSED
  and running on their own schedules — the orphan `apps-stop-nightly-uat` fired
  at 19h, not 21h, and was deleted too (1068513878269510). The two orphans
  acting on the `qualibot` app itself were left in place on purpose:
  `qualibot-stop-weekend-uat` (833786471417758) and `qualibot-start-weekend-uat`
  (756808879701176) — so `qualibot` still gets two identical starts every Monday
  7h and two stops every Friday 21h. Harmless (the notebook skips a redundant
  stop/start, see below) but real drift. When auditing "what starts my app",
  `bundle validate` is not enough — list the workspace jobs and check
  `settings.deployment`.
- **An org-wide job already stops every app nightly**: `D_0_Stop_Databricks_Apps`
  (991525358759141, owner patrice.puntis), UNPAUSED, 19h08 Europe/Brussels,
  `action=stop_all`. Its counterpart `D_0_Start_Databricks_Apps` is PAUSED and
  only ever started `budget-ingestion`, so it is not a path back up for our apps.

## knowledge-assistant-provisioning

**2026-08-24**: Knowledge Assistants aren't a bundle resource type (no
`knowledge_assistant:` block in Databricks Asset Bundles), so
`knowledge_assistant/provision_knowledge_assistant_job.py` provisions them via
the SDK instead — idempotent create-or-update by `display_name`, attach the
Vector Search index source, merge CAN_MANAGE/CAN_QUERY permissions. Deployed
as a manual-trigger job only (`provision_knowledge_assistant_uat_test`), never
part of the daily parsing chain: a KA is provisioned once per environment (or
re-run after a deliberate prompt/index change), and the daily chain already
keeps its attached index fresh without any KA-side action.

The resulting `endpoint_name` (e.g. `ka-7679a56e-endpoint`) is NOT
auto-written anywhere — `utils/deploy/target_env.json` is the actual source
of truth read by both `deploy_qualibot.ps1` and `bitbucket-pipelines.yml`
(rendered into the gitignored `target_config.env` on every deploy), and this
job runs server-side with no access to that git-tracked file. After a run
that creates a new KA, copy the printed `endpoint_name` into the matching
`CHAT_ENDPOINT_*` key by hand and redeploy the app.

`ka_profiles.py` was seeded from the live `qualibot_ALL_v2`/`AS_v2`/`IS_v2`
KAs so the first run recognizes them and changes nothing. Points at
`uat_landingzone.qualibot` even on the `-test` target: `qualibot-uat-test`
already shares that catalog/schema with the real `qualibot-uat` app and its
own service principal already holds `CAN_QUERY` on these exact KAs — safe to
validate there before promoting an equivalent job to `qualibot-uat` with
`APP_NAME=qualibot`.

## app-stop-start-idempotency

**2026-07-20 fix** (`app_mgmt/stop_start_app_job.py`): the `databricks apps
stop/start` CLI silently no-ops when the app is already in the target state,
but the SDK calls used here (`w.apps.stop`/`w.apps.start`) raise `BadRequest`
in that case ("Cannot stop app X as its compute is in STOPPED state") —
confirmed via 3 failed runs (nightly stop hitting an already-stopped app on a
day the weekend job also stopped it; weekend start hitting an app someone had
already started manually). The fix now checks the app's current state first
and skips the call entirely if it's already where we want it, instead of
relying on the API to tolerate a redundant request.

## capabilities-snapshot-account-group-visibility

**Why `user_capabilities/build_capabilities_snapshot.py` exists**: feature
access (chat / compare) is driven by membership in account-level groups
(`Role-Project-LEAP-End-users-Qualibot-*`). At runtime the app reads a user's
groups via workspace SCIM `/Me`, but `/Me` does NOT surface ACCOUNT-level
group memberships — so legitimate users (provisioned at the account level by
the IdP) were denied chat/compare even though they are in the right groups.

Until the app's service principal is granted `SELECT` on `system.access`, this
script resolves membership OFFLINE, using a CoreDev member's credentials
(CoreDev is the only principal granted that `SELECT`), and bakes the result
into a static snapshot the app loads at startup. The snapshot's groups are
merged into whatever `/Me` returns, so the existing
`CHAT_GROUPS`/`COMPARE_GROUPS`/`ADMIN_GROUPS` mapping is unchanged — the
snapshot just fills in the groups `/Me` cannot see.

It reads the audit log's net membership: for each (user, group) the most
recent `addPrincipalToGroup` / `removePrincipalFromGroup` event wins.

## sync-user-capabilities-uat-dryrun-jules-adhoc-job

`qualibot-sync-user-capabilities-uat-dryrun-jules` (job_id `283137851538709` on
`qualibot-uat`) is a manual-trigger dry-run copy of the
`sync_user_capabilities_uat` bundle job, created directly via `jobs create`
(not in `databricks.yml`) — deliberately outside the bundle's Terraform state
so it survives every `bundle deploy`/`destroy`, local or from the Bitbucket
pipeline, untouched. Needed because `run_as` can only be pinned to a specific
human user by an admin, and the pipeline deploys as the non-admin
`job-runner-sa-uat` SP — any bundle-managed job with `run_as: user_name: jules...`
fails that pipeline's deploy outright. This job's `run_as` is jules (its
creator) and always stays that way, since nothing ever redeploys it.
Points at `doccompare` with `dry_run` hardcoded true — read-only, safe to
re-run any time to validate the Lakebase connection / `system.access` read as
a human user, independent of whether `sync_user_capabilities_uat` itself is
still blocked on the SP's missing grant.
