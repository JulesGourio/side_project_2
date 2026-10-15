# services/infra/lakebase

The application's access to Lakebase (autoscaling PostgreSQL): the connection pool with its token refresh, the tables
created at each start, the request logs, the result caches and the shared `users` table.

Parent: [infra](../README.md).

## Files

| Module | Content |
|---|---|
| `connection.py` | `init_lakebase` (pool, schema check, background token refresh), `shutdown_lakebase`, `get_pool`. Creates the database when it is missing. `get_pool` returns `None` when Lakebase is not configured or not reachable |
| `schema.py` | `ensure_schema`: `CREATE TABLE IF NOT EXISTS` for every table, then `_add_columns` for the columns added later |
| `columns.py` | The column lists of the chat tables: `CHAT_TURN_COLUMNS`, `CHAT_CHUNK_COLUMNS`, `CHAT_TURN_TIMINGS`. The schema and the inserts both read them |
| `audit.py` | `store_error` (`app_errors`), `store_chat_turn` (`chat_turns`, `chat_retrieved_chunks` and `app_errors` in one transaction), `store_llm_request` and its updates (`llm_requests`), `store_impact_request` (`impact_requests`, `impact_document_results`) |
| `cache.py` | The result caches of the impact search (`impact_cache`) and of the summaries (`summary_cache`) |
| `users.py` | `upsert_user`: registers an identity in `users` |

The tables are `compare_messages`, `compare_feedbacks`, `llm_requests`, `app_errors`, `impact_requests`,
`impact_document_results`, `impact_feedbacks`, `impact_cache`, `summary_cache`, `users`, `chat_sessions`, `chat_messages`,
`chat_feedbacks`, `chat_turns`, `chat_retrieved_chunks`, `catalog_metadata` and `doc_catalog`. The schema is described in
[docs/lakebase_schema.md](../../../../docs/lakebase_schema.md).

## Rules

- The schema is created at every start and the statements are idempotent. A failing schema step is logged, and the app
  goes on with the tables that exist.
- Every write is best-effort: it logs its own failure and returns. A database error never reaches the user.
- `store_error` is synchronous. It reads the origin from the caller's frame, so call it directly, not inside
  `asyncio.create_task`. It schedules the write itself.
- The columns of `chat_turns` are defined once, in `columns.py`. A value written to the turn log needs its column there.
- The pool is rebuilt before its access token expires, about an hour after it was issued.
- The connect timeout is `LAKEBASE_CONNECT_TIMEOUT_S` (default 5 s), so a run without the database falls back quickly.
- Routes that need the database answer 503, or report the feature as unavailable, when `get_pool()` is `None`.

## See also

- [infra](../README.md): the other shared services.
- [docs/lakebase_schema.md](../../../../docs/lakebase_schema.md): the tables and their columns.
