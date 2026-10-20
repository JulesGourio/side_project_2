# services

The logic behind the routers, in three packages. The routers call the services; the services never import the routers.

Parent: [server](../README.md).

## Packages

| Folder | Content | README |
|---|---|---|
| `chat/` | The chatbot: the engine that answers a question, the LLM calls with their retries and fallbacks, the prompt loader, the per-turn log, the MLflow traces, the translation bridge | [chat](chat/README.md) |
| `compare/` | The document comparison: LLM calls of the diff and the impact judge, chunked analysis, the file processors, the impact queries, summaries, export helpers, file conversion | [compare](compare/README.md) |
| `infra/` | Shared by both features: Lakebase, access rights, credentials, Vector Search, the document catalog, streaming prices, error origin, the PDF layout | [infra](infra/README.md) |

The `__init__.py` of each package is empty.

## Dependencies

- `chat/` imports from `infra/` only: the streaming prices and parsers, the Vector Search gate and constants, the
  document catalog. Nothing in `chat/` imports `compare/`.
- `compare/llm.py` imports `chat/llm.py`. Both run the same endpoint chain, with the same retries and the same endpoint
  cooldowns, so one serving endpoint and its quota serve both features.
- `infra/vector_search.py` (the impact judge) imports `compare/llm.py`. `compare/impact_queries.py` imports one JSON
  parser from `infra/vector_search.py`.

## Rules

- The chatbot prompts are files in [`server/prompts/chat/`](../prompts/README.md). The compare prompts and the impact
  judge prompt are Python constants: the default analysis prompt in `routers/compare/config.py`, the summary prompts in
  `compare/summarize.py`, the diff prompts in `compare/processors/_diff_engines/prompts.py`, the judge prompt in
  `infra/vector_search.py`.
- Blocking calls (SDK, file parsing, conversions) run in a thread through `asyncio.to_thread`.
- Log writes to Lakebase are best-effort: they log their own failure and never raise into the request.

## See also

- [routers](../routers/README.md): the callers of these services.
- [prompts](../prompts/README.md): the chatbot prompt files.
