# QualiBOT

FastAPI + React application deployed on Databricks Apps, built around one
shared knowledge base of aerospace quality/technical documents. Two
features, one app, one deployment.

## Features

| Tab | What it does | Docs |
|---|---|---|
| **Compare** | Diffs two document versions, generates a change report, then searches the knowledge base for other documents that change impacts | [`client/src/components/compare/README.md`](client/src/components/compare/README.md) |
| **Chat** | RAG conversational assistant over the document knowledge base, with division scoping (ALL/AS/IS), inline citations, and the knowledge-base build pipeline (parsing/chunking/embeddings) it retrieves from | [`client/src/components/chat/README.md`](client/src/components/chat/README.md) |

Each doc above covers its feature in depth. This file covers what's shared
across both: the LLMs involved, the app shell, and how to run/deploy
the app.

## LLMs across the app

Which model does what:

| Step | Model / endpoint | Configured via | Role |
|---|---|---|---|
| Compare — diff analysis (Change Summary / Change Table) | `databricks-claude-sonnet-4-6` | `COMPARE_ANALYSIS_ENDPOINT` | Identifies substantive content changes between two document versions |
| Compare — impact search retrieval | Vector Search index `chunks_index_v1` (Databricks-managed embeddings) | `COMPARE_IMPACT_INDEX` | Multi-query retrieval over the full knowledge base, one focused query per derived change |
| Compare — impact search judgment | `databricks-gpt-5-6-luna` | `COMPARE_IMPACT_ENDPOINT` | Judges each retrieved candidate `impacted: true/false` + a 2-4 sentence reason |
| Compare — per-document summary (text) | `databricks-gpt-5-6-luna` | `COMPARE_SUMMARY_ENDPOINT` | Summarizes one uploaded document (old or new) on its own, independent of the diff |
| Compare — per-document summary (image) | `databricks-gpt-5-6-luna` | `COMPARE_SUMMARY_IMAGE_ENDPOINT` | Same feature, routed to a vision model for image files |
| Chat — conversational agent | Databricks Knowledge Assistant (`ka-*-endpoint`, one per division) | `CHAT_ENDPOINT` / `_ALL` / `_AS` / `_IS` | Owns its own retrieval + answer synthesis internally |
| Chat — translation bridge | `databricks-gpt-5-6-luna` | `CHAT_TRANSLATE_ENDPOINT` | Detects + translates non-FR/EN questions and answers (bridge disabled by default) |
| Knowledge base — image description | `databricks-gpt-5-mini` | `PARSING_LLM_ENDPOINT` (`utils/parsing_pipeline/config.py`) | Describes tables/figures/diagrams extracted during parsing; the description is folded into the surrounding chunk text |
| Knowledge base — chunking | No LLM — token-bounded markdown-structure splitter | n/a | See the Chat doc's "Knowledge base pipeline" section |
| Knowledge base — embeddings | Databricks-managed Vector Search embeddings (Delta Sync index) | index-level config, not in this repo | Powers retrieval for both Compare's impact search and Chat |

The offline knowledge-base build (parsing, chunking, image description,
`utils/parsing_pipeline/` notebooks `00`–`06`) is documented in the Chat
doc's "Knowledge base pipeline" section, since Chat is what retrieves from
it — Compare's impact search queries the same index but doesn't own the
pipeline.

## Shared architecture

Both tabs share the same capability-gating pattern (`can_compare`/
`can_chat` on the `users` Lakebase table, synced by
`utils/databricks_ops/user_capabilities/sync_user_capabilities.py`) and the
same app shell (`client/src/App.tsx`, `TopBar.tsx`).

## Local development

Runs the FastAPI backend + Vite dev server on your machine, hitting real UAT
endpoints (LLM, Vector Search, chat KA) under your own identity — no deploy
needed to iterate.

1. Create `.env.local` at the repo root (gitignored, never committed):

   ```
   ENV=development
   DATABRICKS_CONFIG_PROFILE=UAT
   COMPARE_ENABLED=true
   CHAT_ENABLED=true
   CHAT_ENDPOINT=ka-7679a56e-endpoint
   COMPARE_ANALYSIS_ENDPOINT=databricks-claude-sonnet-4-6
   COMPARE_IMPACT_INDEX=uat_landingzone.qualibot.chunks_index_v1
   COMPARE_IMPACT_ENDPOINT=databricks-gpt-5-6-luna
   COMPARE_IMPACT_NUM_RESULTS=30
   COMPARE_IMPACT_MAX_CANDIDATES=8
   COMPARE_IMPACT_MAX_QUERY_CHARS=6000
   COMPARE_IMPACT_MAX_TOKENS=3500
   COMPARE_SUMMARY_ENDPOINT=databricks-gpt-5-6-luna
   COMPARE_SUMMARY_IMAGE_ENDPOINT=databricks-gpt-5-6-luna
   COMPARE_SUMMARY_MAX_CHARS=300000
   COMPARE_SUMMARY_MAX_TOKENS=800
   COMPARE_VOLUME_PATH=
   LAKEBASE_PROJECT_ID=qualibot
   LAKEBASE_DATABASE=doccompare_test
   ```

   (Same values as `app.yaml` — adjust the endpoint names if they change.
   Leaving `COMPARE_VOLUME_PATH` empty disables volume save/restore — fine
   for quick iteration, backend logs a warning and continues.
   `LAKEBASE_DATABASE=doccompare_test` points at the disposable uat-test
   database rather than production history — deliberate for local runs,
   change it if you specifically need to inspect prod/uat data.)

   **Set `LAKEBASE_PROJECT_ID` even locally — don't leave it blank.** Whether
   Lakebase (port 5432) is actually reachable from your machine varies: it's
   blocked on the corporate wifi, but not necessarily on other networks (VPN,
   home). Rather than hardcoding "local never connects" via an empty
   `LAKEBASE_PROJECT_ID`, the connection now has a short timeout
   (`LAKEBASE_CONNECT_TIMEOUT_S`, default 5s — see `server/services/lakebase.py`)
   — so it's safe to always set real values: when the port is reachable you
   get full history/job persistence locally for free; when it's blocked, it
   fails fast (a few seconds at startup, not asyncpg's 60s default) into the
   existing no-history fallback, same as leaving it blank — Compare/Chat
   degrade to no-history and keep working either way.

   **`.env.local` is loaded via a relative path** (`load_dotenv(dotenv_path='.env.local')`
   in `server/app.py`) — it silently does nothing if missing or if uvicorn isn't
   launched from the repo root, leaving every `COMPARE_*`/`CHAT_*`
   var empty (symptom: `"COMPARE_ANALYSIS_ENDPOINT not configured in app.yaml"`
   even though the file exists). Check the first line uvicorn logs at startup —
   it must read `Starting in development mode`; `Starting in production mode`
   means `.env.local` wasn't found.

2. **Unset `DATABRICKS_TOKEN`** if it's set in your shell (`echo $DATABRICKS_TOKEN` /
   `$env:DATABRICKS_TOKEN` to check). If present, the server prefers it over your
   CLI profile — and if it belongs to a different workspace/identity, every
   Databricks call (Vector Search especially) fails with 403 instead of falling
   back to `DATABRICKS_CONFIG_PROFILE=UAT`. Bash: `env -u DATABRICKS_TOKEN python -m uvicorn ...`.
   PowerShell: `Remove-Item Env:\DATABRICKS_TOKEN` for the session, or unset it in
   your profile.

3. Run both servers (two terminals):

   ```
   python -m uvicorn server.app:app --host 127.0.0.1 --port 8000 --reload
   cd client 
   npm run dev   
   # Vite on :3000, proxies /api to :8000
   ```

   Open `http://localhost:3000/compare` (or `/chat`). `client/out`
   (the committed production build) is unaffected — the dev server serves from
   source directly.

4. **Going back to a normal deploy**: nothing to revert. `.env.local` is
   gitignored and never uploaded by `deploy_qualibot.ps1` (it writes its own
   `target_config.env` per environment); just stop the two local servers and
   run the deploy script as usual. If you rebuilt `client/out` for local testing,
   the deploy script rebuilds it again anyway unless you pass `-SkipBuild`.

## Deploying

### Token refresh

Run the appropriate command before deploying if your token has expired:

```powershell
# UAT (also used by the uat-test target — same workspace)
databricks auth login --host https://dbc-3a17bfce-9e88.cloud.databricks.com --profile UAT

# PROD  (set host once workspace is provisioned)
databricks auth login --host <prod-host> --profile PROD
```

### Deploy

```powershell
.\utils\deploy\deploy_qualibot.ps1 -AppEnv uat        # UAT (qualibot-uat)
.\utils\deploy\deploy_qualibot.ps1 -AppEnv uat-test   # UAT — disposable test app (qualibot-uat-test)
.\utils\deploy\deploy_qualibot.ps1 -AppEnv prod       # PROD
```

By default the script builds the React frontend (`bun run build`), writes the per-env `target_config.env`, uploads the source with `databricks sync`, checks the target app's compute state (auto-starting it first if needed — `uat`/`prod` only, see below), then triggers a restart with `databricks apps deploy`. Pass `-SkipBuild` to skip the frontend rebuild (Python-only changes), or `-Infra` to also run `databricks bundle deploy` (re-applies app ACLs / UC volume bindings via terraform — only needed when resources or permissions change, requires MANAGE on the target catalog).

## Environments

| Env      | Databricks target      | Profile | Workspace                                        |
|----------|-------------------------|---------|---------------------------------------------------|
| UAT      | `qualibot-uat`          | `UAT`   | `https://dbc-3a17bfce-9e88.cloud.databricks.com`   |
| UAT-TEST | `qualibot-uat-test`     | `UAT`   | `https://dbc-3a17bfce-9e88.cloud.databricks.com`   |
| PROD     | `qualibot-prod`         | `PROD`  | TBD                                                |

`qualibot-uat-test` is a disposable app in the same UAT workspace, used to try changes (e.g. new KA endpoints, the chat translation bridge) without touching the real `qualibot-uat` app or its Lakebase data — see the comment above its target in `databricks.yml`.

## Maintenance

### Copy Lakebase UAT tables to a local dev catalog

The local machine can't reach Lakebase directly (port 5432 is blocked). Run the export as a Databricks job first, then pull it locally — one-time setup (`psycopg2-binary`, proxy cert flag) and the `--direct` option are documented in [`utils/databricks_ops/lakebase_sync/README.md`](utils/databricks_ops/lakebase_sync/README.md):

```powershell
# 1. Import + run the export notebook as a one-off job on UAT (serverless compute)
databricks workspace import //Users/<you>/export_lakebase_uat_to_volume --profile UAT --file utils\databricks_ops\lakebase_sync\export_lakebase_uat_to_volume.py --format SOURCE --language PYTHON --overwrite
databricks jobs submit --profile UAT --json '{"tasks":[{"task_key":"export","notebook_task":{"notebook_path":"/Users/<you>/export_lakebase_uat_to_volume","source":"WORKSPACE"}}]}'

# 2. Pull the exported JSON and push it into dev_landingzone.qualibot
python utils\databricks_ops\lakebase_sync\copy_Lakebase_tables.py
```

### Compare diff-engine evaluation

Token-free scoring of the diff engine (recall/precision against annotated
reference pairs) — see [`utils/compare_eval/README.md`](utils/compare_eval/README.md).
