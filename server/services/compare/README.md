# services/compare

The services of the document comparison: the LLM calls of the diff and of the impact judge, the chunked analysis of large
diffs, the file processors, the impact query builder, the summaries, the export helpers, and the conversion of Office
files.

Parent: [services](../README.md). The routes that call these services are in [routers/compare](../../routers/compare/README.md).

## Files

| Module | Content |
|---|---|
| `llm.py` | The compare LLM chain. `stream_analysis` streams the diff, `complete_chain` runs the short calls (impact judge, summaries). `compare_chain` is the primary endpoint followed by `COMPARE_LLM_FALLBACK_ENDPOINTS`. Without that setting the fallback is GPT-5.6 Luna, or GPT-6 Luna when the primary is GPT-5.6 Luna. `CompareLlmUnavailable` when every attempt failed |
| `chunked_analysis.py` | Large diffs: split at section boundaries, one analysis call per part (parts run in parallel, `COMPARE_CHUNK_PARALLELISM`), merged into one stream |
| `impact_queries.py` | Reads the change list (structured JSON, Markdown report or free text), numbers the changes `C1`, `C2`..., and builds one search query per group of changes (`changes_to_queries`) |
| `config.py` | Settings of the compare services (the timeout of a summary call) |
| `summarize.py` | Summary of one document: text through the summary endpoint, or an image through the vision endpoint |
| `export_helpers.py` | Excel workbooks of a structured diff, the impact action list, the side-by-side image thumbnails, and the parsing of the model's JSON |
| `conversion.py` | `.doc` text (antiword, then olefile), `.docx` to HTML (mammoth), `.pptx` slides to PNG (LibreOffice) |
| `soffice.py` | Portable LibreOffice for the exact-layout PDF preview: discovery, one extraction per process, `convert_docx_to_pdf` |
| `processors/` | The file processors: text, images and the messages sent to the model (see its README) |

## Rules

- The analysis method (`standard`, `structured` or `comparative`) comes from `processors/versions.yml`. The default for
  each file type is set by `COMPARE_METHOD_<TYPE>`, and a request can override it.
- The compare LLM chain uses the endpoint health of the chat chain: the same cooldowns, and a 404 keeps an endpoint out for
  ten minutes.
- The processors and the conversions are synchronous. Callers run them in a thread.
- `soffice.py` looks for LibreOffice in this order: `SOFFICE_PATH`, a tree already extracted, the archive named by
  `SOFFICE_ARCHIVE_VOLUME_PATH`, then the well-known system paths.
- A structured analysis is parsed back from its JSON for the Excel export. The parser tries several fallbacks before it
  gives up.
- A failing analysis endpoint is never ignored: the failure is logged, the endpoint is rested, the next candidate of the chain is tried, and `CompareLlmUnavailable` is raised only when none answers.

## See also

- [processors](processors/README.md): the file processors and their diff engines.
- [chat](../chat/README.md): the chat LLM chain that this package reuses.
- [infra](../infra/README.md): Vector Search, which runs the impact search.
