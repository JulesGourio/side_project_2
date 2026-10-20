# routers/compare

The document comparison API: the streamed diff analysis, the streamed impact search, the single-document summary, and
the saving and loading of a comparison folder on the volume.

Parent: [routers](../README.md). The logic is in [services/compare](../../services/compare/README.md), and the search,
the audit rows and the access checks are in [services/infra](../../services/infra/README.md).

## Files

| Module | Routes | Content |
|---|---|---|
| `__init__.py` | none | Includes the four modules below |
| `config.py` | none | `get_config()`: every `COMPARE_*` setting, read per request, with its default as a named constant. `APP_VERSION`, the file size limit, the number of analyses at once. The default analysis prompt, used when `COMPARE_ANALYSIS_SYSTEM_PROMPT` is unset |
| `common.py` | none | File helpers: validation (size, extension), safe file names, `_is_within_volume` |
| `analyze.py` | `POST /compare/analyze` | Diff of two files as server-sent events: cache check, wait for a slot, processor, audit row (`llm_requests`), one call or a chunked analysis |
| `impact.py` | `POST /compare/impact` | Impact search as NDJSON: `plan`, one `document` event per judged candidate, `done`. Result cache, audit row (`impact_requests`) |
| `summarize.py` | `POST /compare/summarize` | Summary of one document (text, or an image through the vision endpoint), cached by file hash |
| `volume.py` | `POST /compare/save`, `GET /compare/load` | Stores the two files of a comparison in a folder of `COMPARE_VOLUME_PATH`, and reads them back |

## Rules

- Both file hashes are computed from the uploaded bytes. The cache key adds the app version, and for the impact search
  also the change list, the index and the endpoint.
- `analyze` waits for a slot (`COMPARE_MAX_CONCURRENT`) and stops the model call when the browser disconnects. Its
  audit row records the outcome (`ok`, `error` or `aborted`) and keeps the messages with the images replaced by
  placeholders.
- `impact` caches a result only when no document failed. `force_refresh` skips the cache.
- Both streaming routes call the compare LLM chain (`services/compare/llm.py`), which retries across the endpoints.
- A volume path must lie inside `COMPARE_VOLUME_PATH`. Without a configured volume, `/compare/load` reads only `/Volumes/`
  paths.

## Access

Every route of this folder declares `require_compare`. The three `save-*` routes that write to the volume are in
[`../exports.py`](../exports.py), and they declare no access dependency (see [routers](../README.md)).

## See also

- [routers](../README.md) and [services/compare](../../services/compare/README.md).
