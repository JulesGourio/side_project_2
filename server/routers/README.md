# routers

The HTTP routes of the application, all mounted under `/api` by [`app.py`](../app.py). A router checks access, reads the
request, calls the services and writes the audit rows. The model and search logic stays in
[`services/`](../services/README.md).

Parent: [server](../README.md).

## Files

| Module | Routes | Content |
|---|---|---|
| `config.py` | `GET /config/app`, `GET /config/processors`, `GET /config/knowledge-base-date`, `GET /me` | Client settings (branding, volume path, impact index, chat switch), the processor version map (`services/compare/processors/versions.yml`), the catalog date, the caller's capabilities |
| `health.py` | `GET /health` | Status and timestamp |
| `feedback.py` | `POST /feedback`, `POST /compare/impact/feedback` | Votes and comments on a comparison or on an impact result (`compare_feedbacks`, `impact_feedbacks`) |
| `history.py` | `POST /history`, `GET /history`, `GET /history/{id}`, `PUT /history/{id}/impact` | The caller's saved comparisons (`compare_messages`) and the impact result attached to one |
| `exports.py` | `POST /compare/export-excel`, `POST /compare/impact/export-excel`, `POST /export/pdf`, `POST /compare/save-result`, `POST /compare/save-excel`, `POST /compare/save-pdf` | Excel and PDF files for download, and their saving into a comparison folder on the volume |
| `preview.py` | `POST /preview`, `POST /preview/pdf` | HTML preview of an Office file; exact-layout PDF through LibreOffice (501 when it is not available) |
| [`compare/`](compare/README.md) | `/compare/analyze`, `/compare/impact`, `/compare/summarize`, `/compare/save`, `/compare/load` | Document comparison |
| [`chat/`](chat/README.md) | `/chat/ws`, `/chat/feedback`, `/chat/sessions...`, `/chat/shared/...` | The chatbot |
| `__init__.py` | none | Empty |

## How the routers fit together

- The routers call `services/`. They import from each other only for shared settings: `history.py` and `exports.py`
  import `APP_VERSION` and `get_config` from `compare/config.py`, and `_is_within_volume` and `_sanitize_filename` from
  `compare/common.py`. The chat
  routes import nothing from the compare routes.
- Three transports: `/compare/analyze` streams server-sent events, `/compare/impact` streams NDJSON lines, and a chat
  turn is a WebSocket (`/chat/ws`), which avoids the buffering of the Databricks Apps proxy.
- Access checks are FastAPI dependencies from `services/infra/user.py`: `require_compare` and `require_chat`. A
  WebSocket cannot return 403, so `chat/ws.py` checks `can_chat` itself.

## Rules

- These routes declare no access dependency: `/me` (it runs the capability check itself and returns the two flags),
  `/config/*`, `/health`, `/preview`, `/preview/pdf`, `/export/pdf`, `/compare/export-excel`,
  `/compare/impact/export-excel`, `/feedback`, `/compare/impact/feedback`, `/compare/save-result`,
  `/compare/save-excel` and `/compare/save-pdf`. The three `save-*` routes write files under `COMPARE_VOLUME_PATH`.
- A save or load path must lie inside `COMPARE_VOLUME_PATH` (`_is_within_volume`). Without a configured volume,
  `GET /compare/load` reads only `/Volumes/` paths.
- Compare results are cached by the SHA-256 of the uploaded bytes. A hash sent by the browser is ignored.
- A pair of files of different types (PDF against DOCX) is refused before any extraction.
- A stream that fails sends one error event with the fixed message, then `[DONE]`. The technical detail goes to
  `app_errors`.
- The saved-comparison routes and the chat session routes answer only the caller's own rows, except a shared chat
  conversation, which is read by its token.
- `history._serialize` returns `dict[str, JsonScalar]`, not a `TypedDict`: it serialises any row of a `SELECT *` and of column lists chosen by the caller, so its keys are not fixed.

## See also

- [server](../README.md), [services](../services/README.md).
- [compare](compare/README.md) and [chat](chat/README.md): the two feature route sets.
