# services/infra

The services shared by the chatbot and the document comparison: access rights, credentials, Vector Search, the document
catalog, the streaming prices, the origin of errors, Lakebase and the PDF layout.

Parent: [services](../README.md).

## Files

| Module | Content |
|---|---|
| `user.py` | The identity from the request headers (`get_user_identity`). The capabilities (`get_capabilities`): a live group check on the visitor's token, cached per visitor. The FastAPI dependencies `require_chat` and `require_compare`. The workspace URL |
| `credentials.py` | `get_credentials`: host and token from the environment, then from the SDK's own authentication, then the forwarded token of the request |
| `config.py` | Settings of the shared services: the Vector Search query timeout and the limits of the impact search judge (fan-out, retries, passages per candidate, token budget) |
| `vector_search.py` | The impact search (`run_impact_search`): one query per change, grouping per document, one judge call per candidate, the event stream, the display order. Also the constants and the JSON parsing used by the chat search |
| `vs_gate.py` | `post`: every Vector Search query of the app goes through it, with a cap on the queries in flight and retries |
| `doc_catalog.py` | The document catalog, read from the Lakebase table `doc_catalog` and reloaded every `DOC_CATALOG_REFRESH_S` seconds: REF lookup, titles, revisions, dates, language variants, `augment_sources`, `with_document_info` |
| `streaming.py` | The prices of the endpoints and the cost in EUR, the parsing of the text deltas, the endpoints that refuse a temperature |
| `error_origin.py` | The origin of an error (`path:line in function`: the deepest frame of this repository) and the fingerprint that groups identical errors |
| [`lakebase/`](lakebase/README.md) | Lakebase: pool, schema, request logs, caches, users |
| [`pdf_report/`](pdf_report/README.md) | The PDF layout of the answers, conversations and summaries |
| `__init__.py` | Empty |

## Rules

- The groups are the union of `users.groups` (rows with `groups_synced_at` set, written by the sync job task
  `utils/lakebase_sync/sync_user_capabilities_job.py` from the account audit log; the only source for the account-level
  groups) and SCIM `Me` on the visitor's token (groups attached to the workspace). No allowed group found grants
  the chat only: the Databricks app permission already decides who reaches the app, and a visitor in a group only
  through another group is in neither source; Compare stays with the groups that name it. The result is cached for
  `CAPS_TTL_S`. When the `users` table cannot be read and SCIM gives no allowed group, the chat is granted and Compare is not, uncached.
  The group names are the constants of `user.py`.
- `CAPS_BYPASS=true` grants both capabilities to every visitor. `tests/test_deploy_config.py` keeps it off outside DEV.
- When `ENV` is unset, `_is_dev()` is true. A request without an `x-forwarded-user` header then gets both capabilities,
  and `get_user_identity` returns the workspace identity instead of none.
- Every Vector Search query goes through `vs_gate`. At most `VS_MAX_CONCURRENT_QUERIES` (default 8) are in flight per app
  instance. Refusals (429, 5xx), timeouts and network errors are retried `VS_QUERY_RETRIES` times (default 5), with
  growing pauses that honour `Retry-After`.
- The catalog stays empty until the first load of the parsing pipeline has written its table. The functions that read it
  then return nothing: no REF is resolved, no source chip is added.
- Capabilities are cached per visitor for `CAPS_TTL_S` (default 600 s).

## Access to indexed documents

Chat and impact search query Vector Search with the service principal of the app, not with the token of the visitor
(which is only a fallback). Unity Catalog grants are therefore not applied per user: every visitor allowed into the chat
reaches every passage of the index, and what a visitor can see is decided by what the parsing pipeline puts in it. The
division filter (AS / IS) narrows the search but is not a permission. The REF links of the catalog cover the whole
parsing scope, including documents that are not in the chat index, so a link opens the document in the Intraqual portal.

## See also

- [lakebase](lakebase/README.md) and [pdf_report](pdf_report/README.md).
- [chat](../chat/README.md) and [compare](../compare/README.md): the callers.
