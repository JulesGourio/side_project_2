# Dashboard "Qualibot Usage Tracking" (Lakeview)

Files: `original/Qualibot_Usage_Tracking.lvdash.json` (export as received 2026-11-03), `build_dashboard.py` (applies every fix below), `Qualibot_Usage_Tracking.lvdash.json` (result, UAT). Regenerate with `python3 build_dashboard.py`.
Dashboard id (from the query names of the export): `01f13d51cb401021adbe602186380270`. Everything is hardcoded to UAT (`ENV` in the script).

## Corrected by the script

| Area | Fix |
|---|---|
| Health Check "Docs in Error" | Debug filter `IDDOC = 20044` removed (the table showed one document). |
| Sources | Parsing pages read `uat_proj.qualibot.{parsed_documents, parsing_checkpoint, chunks, image_metadata, parsing_run_health, document_change_log}` instead of the frozen `uat_landingzone.qualibot.*_v1`; `hc_chunks` groups `chunks` by `division` (no `src_chunks_*`); scope table of the Intraqual data asset (`prod_gold.quality_system.document_revisions_intraqual`) instead of `prod_bronze.intraqual.gd_doc_latest`; DocCompare reads `compare_messages` / `compare_feedbacks` (Lakebase renames, `lakebase_tables_to_skip` leaves the old names out); DEV KA/compare datasets read `dev_proj`. |
| Statuses | `SKIPPED_CATEGORY_EXCLUDED`, `SKIPPED_SUPERSEDED_REVISION`, `SKIPPED_EMPTY_FOLDER`, `SKIPPED_EMPTY_IMAGES` count as Excluded; `SKIP_TOO_LARGE`, `MISSING_CHUNKS` as Error. |
| Billing | No `usage_quantity > 0` / `record_type != 'RETRACTION'` filter: retractions net the originals, restatements no longer count twice. |
| KA cost per request | Messages counted per env and day, once (they were repeated on every endpoint row and on UAT/PROD rows); "KA - Daily Cost" is now a 30-day sum. |
| Groups | `ANY_VALUE(groups)` replaced by the union of the groups of the user_id; `NOT IN (SELECT email ...)` ignores NULL emails (one NULL emptied growth/adoption/access). |
| Chat | Bot/admin email exclusion and `deleted = false` moved into `chat_data`, `chat_sessions_agg`, `chat_conversations_paired` (6 widgets had their own 3-email filter, the others none); KA-only columns dropped from `chat_data`. |
| DocCompare | One feedback per message in `messages_users` (no duplicated analyses); day/week/month totals equal (weekend activity kept, weeks and months zero-filled). |
| Health check | "Needs OCR" detail starts from the current backlog (docs absent from run history were dropped); 30-day trend built from `parsing_run_health`; "Docs in Scope" excludes `SKIPPED_*`; "Last Run Duration" renamed "Avg Parse Time (s/doc)". |
| Document search | `doc_search_text` ranks the checkpoint without its text column, then joins the text. |
| Filters | Period filter also applies to chat, quality, conversations and turns; user filter titled; 3 mojibake titles repaired; `debug_recent_users` and `hc_index_freshness` removed. |
| New page | "ChatBot - Reliability": `chat_turns` (turns by status, error and degraded rate, first-token and total latency p50/p95, degraded/failed turns) and `app_errors` by fingerprint. |

## Not changed, needs a decision or a measurement

- **ChatBot - Quality**: the MLflow section is removed (`uat_proj.qualibot.ka_mlflow_scorer_assessments` does not exist, 2026-11-04 check). The LLM-judge section still reads `uat_landingzone.qualibot.chat_quality_scores` (last row 2026-10-15). The scorers now write `<env>_proj.qualibot.chat_trace_scores` (long format: `chat_trace_id, scored_at, scorer, value, rationale, division, status, vote, judge`), absent in UAT: rebuild the page on it once it exists.
- **KA cost block** (AGENT_BRICKS) is empty 30 days after the last KA endpoint is gone; remove it then.
- **Costs**: the three Lakebase projects (UAT `d9fd81bc`, DEV `e9185a28`, PROD `deb195f2`) are counted; the old KA vector endpoints `ka-*-vs-endpoint` (DEV 5, UAT 1, still billed) appear as the category "Vector Search (old KA endpoints)".
- **Every cost dataset scans `system.billing.usage`** (5 datasets). A view in `uat_proj.qualibot` read by the five would divide the scans by five; needs a grant run as the SP.
- **PROD**: workspace host, app URL (`qualibot-3155144190025826.aws.databricksapps.com`), MLflow experiment ids in `trace_link`, volume links are UAT values; a PROD dashboard needs the script parametrised with those.
- **One dashboard per environment** (decided 2026-11-04): validate UAT first, then parametrise `build_dashboard.py` per env (catalogs, workspace id, host, app URL, experiment ids, Lakebase project, volume links).
