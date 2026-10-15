# QualiBOT

QualiBOT is a Databricks App: a FastAPI backend serving a React frontend, on top of
a shared base of quality and technical documents (aerospace). It provides three features:

| Feature | What it does | Documentation |
|---|---|---|
| **Document comparison** (`/compare`) | Compares two versions of a document and produces a summary or a table of changes. | [client/src/components/compare/README.md](client/src/components/compare/README.md) |
| **Impact search** (Compare tab) | Finds the other documents of the base that the changes touch, with the relevant passages. | [client/src/components/compare/README.md](client/src/components/compare/README.md) |
| **Chat** (`/chat`) | Answers questions from the document base, with numbered citations and a division filter (ALL, AS, IS). | [client/src/components/chat/README.md](client/src/components/chat/README.md) |

The document base is produced by the parsing pipeline, a daily chain of Databricks
jobs (`1_categories` to `6_update_kb_metadata`) described in
[utils/parsing_pipeline/README.md](utils/parsing_pipeline/README.md). The chat and the
impact search query the Vector Search index that this pipeline maintains.

## Repository layout

### Application

- `server/`: FastAPI backend (layout below).
- `client/`: React and TypeScript frontend, built with Vite. `src/` holds the pages
  (`pages/`) and the components (`chat/`, `compare/`, `layout/`, `shared/`) and `lib/`;
  `out/` is the build served by the backend.
- `start.sh`: start command of the app. It installs antiword when it is missing, loads
  `target_config.env` when present, then runs uvicorn.
- `app.yaml`: environment variables of the app, with their default values and their role.

#### Server layout

```
server/
  app.py              entry point; serves the built frontend
  tracing.py          MLflow tracing setup
  routers/            /api routes: chat, compare, config, exports, feedback, health, history, preview
  services/chat/      chat engine (engine/), LLM calls, prompt loading, translation bridge, turn log, MLflow traces
  services/compare/   comparison processors (processors/), analysis, impact queries, summaries, PDF conversion
  services/infra/     Lakebase, access rights, Vector Search, document catalog, streaming, PDF report rendering
  prompts/chat/       chat prompts, one file per prompt
  assets/fonts/       Inter font used by the PDF exports
```

### Deployment and operations

- `databricks.yml`: Databricks Asset Bundle `qualibot`: variables, sync exclusions and targets.
- `resources/`: jobs and the app, declared per target, one file per concern:
  `parsing_pipeline.job.yml` (daily parsing and indexing chain), `chat_mlflow.yml`
  (chat traces and prompt registry), `dev_copy.yml` (copy of UAT data into DEV),
  `grants.yml` (access grants), `lakebase_sync.yml` (Lakebase export and import),
  `app_schedules.yml` (stop and start of the apps).
- `utils/`: tools that run outside the app, one folder per concern (deployment, parsing
  pipeline, evaluation, grants, Lakebase sync, alerts, app management, DEV copy). Map in
  [utils/README.md](utils/README.md).
- `bitbucket-pipelines.yml`: CI pipeline that deploys the bundle and the app. See [Deployment](#deployment).
- `pyproject.toml`: Python project, dependencies and pytest settings. `requirements.txt`:
  flat list of runtime packages.
- Root files: `.databricksignore` (sync exclusions for the workspace), `.gitignore`,
  `.gitattributes`, `.gitleaksignore` (secret-scan exceptions), `.vscode/` (editor tasks).

### Tests and data

- `tests/`: pytest suite. `golden/` and `fixtures/` hold expected outputs and recorded
  model answers; `diff_corpus.py` and `prompt_noise.py` are test data and detectors used by the tests.
- `third_party/antiword/`: the antiword binary, its character tables and the Debian package used by `start.sh`
  (converts `.doc` files). `third_party/fasttext/lid.176.ftz`: the fastText language-identification model.
- `leap_utils/`: copy of the shared library (table writes and merges, quality checks).
  Do not edit it in this repository.

### Documentation

- `archive/`: superseded documentation (`docs/`, `readmes/`). Never deployed.

`archive/` is excluded from the bundle sync in `databricks.yml`:
nothing reads them at run time.

## Models and indexes

| Step | Model or index | Setting |
|---|---|---|
| Comparison: analysis (summary and table of changes) | `databricks-claude-sonnet-5-5` (default) | `COMPARE_ANALYSIS_ENDPOINT` |
| Comparison: impact search | Vector Search index | `COMPARE_IMPACT_INDEX` |
| Comparison: judgement of each candidate document | `databricks-gpt-5-6-luna` | `COMPARE_IMPACT_ENDPOINT` |
| Comparison: single-document summary (text and images) | `databricks-gpt-5-6-luna` | `COMPARE_SUMMARY_ENDPOINT`, `COMPARE_SUMMARY_IMAGE_ENDPOINT` |
| Chat: answer | `databricks-gpt-6-luna`, fallback `databricks-gpt-5-6-luna` | `CHAT_LLM_ENDPOINT`, `CHAT_LLM_FALLBACK_ENDPOINTS` |
| Chat: query rewrite | `databricks-gpt-6-luna`, fallback `databricks-gpt-5-6-luna`; `databricks-gemini-3-1-flash-lite` on every deployed target | `CHAT_REWRITE_ENDPOINT`, `CHAT_REWRITE_FALLBACK_ENDPOINTS` |
| Chat: translation bridge (disabled) | `databricks-gpt-5-6-luna` | `CHAT_TRANSLATE_ENDPOINT` |
| Parsing: image description (vision) | `databricks-gpt-5-6-luna` | `PARSING_LLM_ENDPOINT` |
| Parsing: passage embeddings | `databricks-qwen3-embedding-0-6b` | `embedding_model` of task `5_sync_index` |

The exact values per target are in `app.yaml` (defaults) and `utils/deploy/target_env.json`
(per-target overrides).

## Startup and access

At startup the app opens the Lakebase connection and loads the document catalog, which is
reloaded every 30 minutes. If Lakebase cannot be reached, the app runs without history.

The visitor's identity comes from the `x-forwarded-user` header set by Databricks Apps.
Access is checked live, without an intermediate table: the app asks Databricks, on behalf of
the visitor (on-behalf-of-user token), whether the visitor belongs to one of these account groups:

| Feature | Account groups |
|---|---|
| Comparison | `Role-Project-LEAP-End-users-Qualibot-DocCompare`, `Role-Project-LEAP-CoreDev`, `Role-Project-LEAP-CoreAdmin` |
| Chat | `Role-Project-LEAP-End-users-Qualibot-ChatBot`, `Role-Project-LEAP-CoreDev`, `Role-Project-LEAP-CoreAdmin` |

The check runs a query on the SQL warehouse set in `CAPS_WAREHOUSE_ID`. Its result is cached
for `CAPS_TTL_S` seconds (600 by default). Without a token, without a warehouse, or when the
check fails, access is denied, and the failure is not cached. `CAPS_BYPASS=true` opens both
features to every visitor; it is set on the DEV target only. In development (`ENV=development`),
a request without the identity header is accepted.

## Configuration

Three sources, in this order of priority:

1. `utils/deploy/target_env.json`: per-target overrides. The deploy script renders them into
   `target_config.env`, which `start.sh` loads at startup.
2. `app.yaml`: default value of each variable, with a comment on its role.
3. The code: fallback value when a variable is absent.

`app.yaml` is the default that is deployed; code defaults can differ.

## Running locally

Local runs call the real Databricks endpoints under your own identity; no deployment is needed.

1. Install the Python project: `pip install -e ".[dev]"`.
2. Create `.env.local` at the repository root (git-ignored) with:
   - `ENV=development`
   - `DATABRICKS_CONFIG_PROFILE`: the CLI profile of the environment you test against
   - `COMPARE_ENABLED=true` and `CHAT_ENABLED=true`
   - `CHAT_INDEX`, `COMPARE_IMPACT_INDEX` and `LAKEBASE_DATABASE`: the values of the
     `dev` target in `utils/deploy/target_env.json`
   - `LAKEBASE_PROJECT_ID=qualibot`

   `LAKEBASE_PROJECT_ID` is needed even locally. If port 5432 is closed, the connection fails
   quickly (`LAKEBASE_CONNECT_TIMEOUT_S`, 5 seconds by default) and the app runs without history.
3. Remove `DATABRICKS_TOKEN` from the environment if it is set: it takes precedence over the CLI
   profile. In PowerShell: `Remove-Item Env:\DATABRICKS_TOKEN`.
4. Start the backend and the frontend, each in its own terminal:

   ```
   python -m uvicorn server.app:app --host 127.0.0.1 --port 8000 --reload
   cd client
   bun install
   bun run dev
   ```

   The Vite dev server runs on `http://localhost:3000` and forwards `/api` (WebSockets included)
   to port 8000. Open `/compare` or `/chat`. The backend logs `Starting in development mode`
   when `.env.local` is loaded.

## Tests

```
python -m pytest
```

The suite is in `tests/` (`testpaths` in `pyproject.toml`). Calls to Databricks and to model
endpoints are replaced by fakes. Spark-based tests are skipped when pyspark or a JVM is missing,
and tests that need a local sample (`test_prompt/`, git-ignored) are skipped when it is absent.

The frontend is checked by lint and build:

```
cd client
bun run lint
bun run build
```

## Deployment

| Deploy script `-AppEnv` | Bundle target | Databricks app | CLI profile | Workspace |
|---|---|---|---|---|
| `dev` | `dev` | `qualibot` | `DEV` | `dbc-c623749d-731b` |
| `uat` | `qualibot-uat` | `qualibot` | `UAT` | `dbc-3a17bfce-9e88` |
| `prod` | `qualibot-prod` | `qualibot` | `qualibot-prod` | `dbc-77b87d5a-6bcb` |

The full host names are in `databricks.yml`. The CLI profile of the target must be authenticated.

The application is deployed from the repository root with `utils/deploy/deploy_qualibot.ps1`:

```powershell
.\utils\deploy\deploy_qualibot.ps1 -AppEnv dev
```

The script:

1. builds the frontend (`bun install`, `bun run build`);
2. renders `target_config.env` from `utils/deploy/target_env.json`, then uploads the code to the
   workspace (with `-Infra`, it runs `databricks bundle deploy` instead, for resources, app
   permissions and volume bindings; this needs MANAGE on the catalog);
3. checks the compute state of the app and starts it when it is stopped;
4. deploys the app from the uploaded code.

Options: `-SkipBuild` skips the frontend build; `-SyncOnly` uploads the code only, without
rebuilding the frontend and without restarting the app.

`bitbucket-pipelines.yml` runs the same bundle and app deployments in CI. Its custom pipelines
are triggered by hand. A push to `main` deploys UAT, and PROD is a manual step after it.

### Resources per environment

| Environment | Tables (`<env>_proj.qualibot`) | App volume | Chat index | Lakebase database |
|---|---|---|---|---|
| DEV | `dev_proj.qualibot` | `dev_proj.qualibot.doc_compare` | `dev_proj.qualibot.chunks_index` | `doccompare` |
| UAT | `uat_proj.qualibot` | `uat_proj.qualibot.doc_compare` | `uat_proj.qualibot.chunks_index` | `doccompare` |
| PROD | `prod_proj.qualibot` | `prod_proj.qualibot.doc_compare` | `prod_proj.qualibot.chunks_index` | `doccompare` |

Delta tables, volumes and Vector Search indexes live in the schema `<env>_proj.qualibot`, with the same names in every environment.

The application's Lakebase tables are `compare_messages`, `compare_feedbacks`, `llm_requests`,
`app_errors`, `impact_requests`, `impact_document_results`, `impact_feedbacks`, `impact_cache`,
`summary_cache`, `chat_sessions`, `chat_messages`, `chat_feedbacks`, `chat_turns`,
`chat_retrieved_chunks`, `doc_catalog`, `catalog_metadata` and `users`. The application creates
and completes this schema at startup. Column reference: [server/services/infra/lakebase/schema.md](server/services/infra/lakebase/schema.md).

## Documentation

| Document | Content |
|---|---|
| [client/src/components/chat/README.md](client/src/components/chat/README.md) | Chat feature |
| [client/src/components/compare/README.md](client/src/components/compare/README.md) | Comparison and impact search |
| [utils/parsing_pipeline/README.md](utils/parsing_pipeline/README.md) | Parsing and indexing pipeline |
| [utils/README.md](utils/README.md) | Map of `utils/` |
| [utils/lakebase_sync/README.md](utils/lakebase_sync/README.md) | Lakebase export, import and schema maintenance |
| [utils/alerts/README.md](utils/alerts/README.md) | SQL alert queries |
| [server/services/infra/lakebase/schema.md](server/services/infra/lakebase/schema.md) | Column reference of the Lakebase tables |
