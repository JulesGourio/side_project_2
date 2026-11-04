# server

The FastAPI backend of QualiBOT. It serves the `/api` routes, runs the chatbot and the document comparison, and serves the
built frontend from `client/out`. The package holds code, the prompt texts, one YAML file and the PDF font. It holds no
data: documents, the catalog and the saved comparisons live in Lakebase and in the Vector Search index.

Project overview: [root README](../README.md).

## Files

| Path | Content |
|---|---|
| `logging_setup.py` | Logging setup: level from `LOG_LEVEL`, request id on every line, quiet libraries |
| `app.py` | Entry point (`server.app:app`): logging, lifespan (Lakebase pool, document catalog refresh), CORS, request log, the routers under `/api`, static files and the single-page fallback |
| `tracing.py` | Sets the MLflow tracking URI to `databricks`. `app.py` imports it before the routers |
| [`routers/`](routers/README.md) | HTTP routes under `/api`: chat, compare, exports, preview, history, feedback, config, health |
| [`services/`](services/README.md) | The logic behind the routes: `chat/`, `compare/`, `infra/` |
| [`prompts/`](prompts/README.md) | The chatbot prompt texts, one file per prompt |
| `assets/fonts/` | Inter font files of the PDF exports. The folder has no README |

## How the app starts

`app.yaml` runs `bash start.sh`. The script puts `antiword` in `$HOME/.local/bin` when it is missing, sources
`target_config.env` when that file exists, and runs `uvicorn server.app:app`.

At startup `app.py` opens the Lakebase pool, creates or checks its tables, and starts the document catalog refresher.
At shutdown it stops the refresher and closes Lakebase and the translation HTTP client. When `client/out` exists,
`app.py` mounts its `assets`, `images`, `logos`, `videos` and `content` folders, and answers every other path outside
`/api` with the matching file or with `index.html`.

## Configuration

Two sources; the second one wins:

1. `app.yaml`, the `env:` list: the default of every setting, for every target.
2. `utils/deploy/target_env.json`: one object per target. It is rendered to `target_config.env` (LF line endings) and
   sourced by `start.sh`. A setting missing for a target keeps its `app.yaml` default.

Locally, `app.py` loads `.env.local` when it exists. Each module reads the settings it uses:
`routers/compare/config.py`, `services/chat/config.py`, `services/chat/engine/config.py`, `services/chat/llm.py`, `services/compare/llm.py`,
`services/infra/lakebase/connection.py`, `services/infra/user.py`.

`ENV` is set nowhere in the repository. `app.py` then takes `production` (no CORS origin), while
`services/infra/user.py` takes `development` (its default). The consequence for access is described in
[services/infra](services/infra/README.md).

## Rules

- Every route is under `/api`. The single-page fallback is registered after the routers, so an `/api` path never
  returns `index.html`.
- Each request has an 8-character id, returned in the `X-Request-ID` header of normal responses (see Logs below).
- An unhandled exception is stored in `app_errors` (stage `unhandled`) and answered with a generic 500.
- The user never sees a technical failure. A failed chat turn or stream sends one fixed message; the cause goes to the
  logs and to `app_errors` (see [services/infra/lakebase](services/infra/lakebase/README.md)).
- Most settings are read at call time. A few are read once at import: `CHAT_ENABLED` in `routers/chat/ws.py`, the
  `CAPS_*` settings in `services/infra/user.py`, the file size limit in `routers/compare/config.py`, the translation bridge and MLflow settings in `services/chat/config.py`.

## Logs

- One setup, `logging_setup.py`. `LOG_LEVEL` (env, default `INFO`) sets the level; `DEBUG` shows the per-call details. httpx, mlflow, uvicorn.access and the Databricks SDK stay at WARNING unless the app runs at DEBUG.
- Every line carries `[id]`: the request id, or the end of the MLflow trace id for a chat turn, so one request or one turn is followed with one search.
- INFO: one line per request after it ends (`GET /api/x -> 200 (0.12s) user=<id>`, none for health and static files unless they fail) and one line per chat turn (`chat turn: status=... division=... user=<id> issues=... <step>=<ms> trace_id=...`).
- WARNING: something degraded worth reading. ERROR: always with the stack trace. A normal fallback or retry is DEBUG.
- No question, prompt or token in a log: the question is in the MLflow trace. The user is the `x-forwarded-user` id (`<user id>@<workspace id>`).
- The SCIM read of the visitor's groups (`services/infra/user.py`) is best effort and never logged: its error text carries the app's auth config. The `Capabilities` line (DEBUG) gives the groups found.
- In the Databricks Apps Logs tab: Source `App` for these lines, `HTTP` for the platform's raw access lines, then the text search: `user=`, `[id]`, `chat turn:`, `status=error`, `status=degraded`, ` - ERROR - `.

## See also

- [routers](routers/README.md): the HTTP routes.
- [services](services/README.md): the logic behind them.
- [prompts](prompts/README.md): the chatbot prompt files.

The interactive API documentation (`/docs`, `/redoc`, `/openapi.json`) is not served unless the environment variable `ENABLE_API_DOCS` is `true`.
