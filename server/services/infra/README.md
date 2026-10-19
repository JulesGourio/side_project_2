# services/infra

The services shared by the chatbot and the document comparison: access rights, credentials, Vector Search, the document
catalog, the streaming prices, the origin of errors, Lakebase and the PDF layout.

Parent: [services](../README.md).

## Files

| Module | Content |
|---|---|
| `user.py` | The identity from the request headers (`get_user_identity`). The capabilities (`get_capabilities`): a live group check on the visitor's token, cached per visitor. The FastAPI dependencies `require_chat` and `require_compare`. The workspace URL |
| `credentials.py` | `get_credentials`: host and token from the environment, then from the SDK's own authentication, then the forwarded token of the request |
| `vector_search.py` | The impact search (`run_impact_search`): one query per change, grouping per document, one judge call per candidate, the event stream, the display order. Also the constants and the JSON parsing used by the chat search |
| `vs_gate.py` | `post`: every Vector Search query of the app goes through it, with a cap on the queries in flight and retries |
| `doc_catalog.py` | The document catalog, read from the Lakebase table `doc_catalog` and reloaded every `DOC_CATALOG_REFRESH_S` seconds: REF lookup, titles, revisions, dates, language variants, `augment_sources`, `with_document_info` |
| `streaming.py` | The prices of the endpoints and the cost in EUR, the parsing of the text deltas, the endpoints that refuse a temperature |
| `error_origin.py` | The origin of an error (`path:line in function`: the deepest frame of this repository) and the fingerprint that groups identical errors |
| [`lakebase/`](lakebase/README.md) | Lakebase: pool, schema, request logs, caches, users |
| [`pdf_report/`](pdf_report/README.md) | The PDF layout of the answers, conversations and summaries |
| `__init__.py` | Empty |

## Rules

- A check that shows no allowed group denies. A check that cannot be made (no token, SCIM and the warehouse both
  failing) grants everything and is not cached: the Databricks app permission already decides who reaches the app. The
  groups come from SCIM `Me` with the visitor's token (no warehouse permission needed); `is_account_group_member()` on
  `CAPS_WAREHOUSE_ID` is only the second opinion when SCIM fails or shows no allowed group. No table holds the groups.
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

## See also

- [lakebase](lakebase/README.md) and [pdf_report](pdf_report/README.md).
- [chat](../chat/README.md) and [compare](../compare/README.md): the callers.
