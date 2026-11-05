# Dashboard "Qualibot Usage Tracking" (Lakeview)

Files: `original/Qualibot_Usage_Tracking.lvdash.json` (export as received 2026-11-03), `build_dashboard.py` (applies every fix below), the results `../../resources/dashboards/qualibot_usage_tracking_{dev,uat,prod}.lvdash.json`, deployed by the bundle (`resources/dashboards.yml`, one dashboard per target). Regenerate all three with `python3 build_dashboard.py dev && python3 build_dashboard.py uat && python3 build_dashboard.py prod`.
Dashboard id (from the query names of the export): `01f13d51cb401021adbe602186380270`. Everything is hardcoded to UAT (`ENV` in the script).

## Structure (2026-11-04)

7 pages on one 12-column grid (the former Adoption page was laid out on 24 columns, the others on 12): Usage, ChatBot (Overview + Satisfaction), ChatBot reliability, DocCompare (Overview + Satisfaction + Performance), Parsing (Health Check + Intraqual Changes), Document explorer, Costs (summary, detail, estimates, then a Legacy KA section to delete with the `ka-*` endpoints). Each page: header, KPI row, charts, tables. Pies became bars, rates are percentages, titles are plain English, chart colors are the validated 8-slot categorical palette (`uiSettings.theme.visualizationColors`), status colors (votes, run outcomes) are the reserved status steps. The `users_access_overview` table (duplicate of the inactive-users table) and the ChatBot members table were dropped.

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
| Filters | Period filter also applies to chat, conversations and turns; user filter titled; 3 mojibake titles repaired; `debug_recent_users` and `hc_index_freshness` removed. |
| New page | "ChatBot - Reliability": `chat_turns` (turns by status, error and degraded rate, first-token and total latency p50/p95, degraded/failed turns) and `app_errors` by fingerprint. |

## Not changed, needs a decision or a measurement

- **ChatBot - Quality** (LLM judge, MLflow) removed 2026-11-04, with the datasets `chat_quality_data`, `mlflow_scorer_data`, `scoring_runs_*`. Trace links (UAT): turns since 2026-10-19 link to experiment `258804869957724` (`qualibot_chat_traces`) through `chat_turns.mlflow_trace_id` (`trace:/uat_proj.qualibot.<exp>/<hex>`, URL-encoded in `selectedEvaluationId`: to confirm in the UI); `tr-<hex>` messages (2026-09-03 to 2026-10-16) link to the KA experiment of their division (AS `2748374992560664`, IS `…665`, other `4171178917767011`); older UUID traces (June to 2026-09-03) have no link. A page on `<env>_proj.qualibot.chat_trace_scores` (long format: `chat_trace_id, scored_at, scorer, value, rationale, division, status, vote, judge`) comes once that table exists in UAT; 
- DocCompare links (`lien_ancien/nouveau/analyse`) still point to the volume `uat_landingzone/qualibot/doc_compare`; check the volume location.
- **KA cost block** (AGENT_BRICKS) is empty 30 days after the last KA endpoint is gone; remove it then.
- **Costs**: the three Lakebase projects (UAT `d9fd81bc`, DEV `e9185a28`, PROD `deb195f2`) are counted; the old KA vector endpoints `ka-*-vs-endpoint` (DEV 5, UAT 1, still billed) appear as the category "Vector Search (old KA endpoints)".
- **Every cost dataset scans `system.billing.usage`** (5 datasets). A view in `uat_proj.qualibot` read by the five would divide the scans by five; needs a grant run as the SP.
- **PROD**: workspace host, app URL (`qualibot-3155144190025826.aws.databricksapps.com`), MLflow experiment ids in `trace_link`, volume links are UAT values; a PROD dashboard needs the script parametrised with those.
- **One dashboard per environment** (2026-11-04): the script is built on the UAT names and retargeted at the end (catalog, workspace id, host, app URL, volume links). Per env in `CONFIGS`: workspace, host, MLflow experiment id (trace links are empty on dev and prod until the id is filled in), Lakebase project. Costs read the account-wide billing tables, so the three dashboards show the same cost pages. Open: the PROD "PROD only / PROD+UAT" button with the 2026-11-04 date filter.

## Update 2026-11-05

- **Global filters** `Origin` (PROD: `PROD live` after 2026-10-30 17:00, `Imported from UAT` before; constant `Live` on dev/uat) and `Language`, on the chat, conversations, turns, sessions and DocCompare datasets.
- **Language**: `question_lang` was NULL on every recent row because the translation bridge is off (`CHAT_TRANSLATE_BRIDGE_ENABLED=false`); the chart and its `IS NOT NULL` filter dropped them (same filter on the site chart). The `Language` column falls back to a keyword guess (es/fr/en) on the question; the app now stores the local fastText guess (`ws.py`), so new rows carry a code.
- **Trace links**: the experiment id is read from `mlflow_trace_id` (no per-env config); NULL before only because `experiment_id` was None on dev/prod.
- **Feedback per week**: grey `none` bars for answers without feedback.
- **Import job** `D_1_Qualibot_Lakebase_Export_To_Volume` hourly (`0 0 * * * ?`, was 02:00/14:00).

## Document explorer: diagnostic

Dataset `doc_diagnostic`: one row per document, first matching case wins (01 in chatbot, 02 not in Intraqual, 03 not current revision, 04 not published, 05 hidden, 06 category out of scope, (no date rule: 07 removed) 08 manual exclusion, 09 replaced by newer revision, 10 parsed result not built, 11 not processed yet, 12 parse error, 13 needs OCR, 14 images to describe, 15 file too large, 16 empty document, 17 no file on volume, 18 parsed but no passage, 19 other). "Parsed text" reads `parsing_checkpoint` (all historical rows), hence more refs than `parsed_documents`/`chunks`.
