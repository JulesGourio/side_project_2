# tests

Automated tests of the server, of the job code under `utils/`, and of the bundle and configuration files. The tests need no
Databricks workspace and no model endpoint: calls are replaced by fakes, and the expected outputs are stored in this folder.

Parent: [root README](../README.md).

## How the suite runs

- From the repository root: `python -m pytest -q tests`. `pyproject.toml` sets `testpaths = ["tests"]`. There is no
  `conftest.py`, `pytest.ini` or `setup.cfg`. `pytest` is in the `dev` extra of `pyproject.toml`.
- The tests import `server` and `tests` from the root, so run them from there.
- The client has its own tests in `client/tests/`, run with `bun test tests` from `client/` (no dependency to install): the safe-URL rules, the chat history sent to the server and the 7-day expiry of the browser storage. The test step of the CI runs them after pytest.
- Some modules skip as a whole when an optional package is missing: `pytest.importorskip` for `mlflow`, `sqlalchemy`,
  `alembic` (`test_chat_scorers.py`, `test_register_chat_prompts.py`), `mlflow` (`test_mlflow_turns.py`), `tenacity`
  (`test_parsing_describe.py`) and `pyspark` (`test_parsing_selection.py`, and one test of `test_parsing_quality.py`).
- `test_parsing_imports.py` runs a child Python process with the parsing folder on its path, and sets the `PARSING_*`
  variables to defaults when they are not set.

## Folders

| Folder | Content | Read by |
|---|---|---|
| `golden/` | `lakebase_schema.json`: the bootstrap SQL, whitespace-normalised | `test_lakebase_schema.py` |

No test writes a file of `golden/`. A changed expected output is an edit of the JSON in the same change. The other tests build
their inputs inline (generated documents, images and answers) and state the expected values in the test itself.
`prompt_noise.py` holds the heuristics of `test_prompt_noise.py`; it is not a test.

## File groups

| Group | Files | What they protect |
|---|---|---|
| Chat | `test_chat.py`, `test_chat_route.py`, `test_chat_engine.py`, `test_chat_llm.py`, `test_citation_markers.py`, `test_doc_catalog.py` | Session and feedback routes, the WebSocket route, the engine (search, citations, stream contract), the LLM fallbacks, and the document catalog |
| Chat traces | `test_mlflow_turns.py`, `test_chat_scorers.py`, `test_lakebase_logging.py`, `test_register_chat_prompts.py` | The MLflow trace of a turn, the scorers, what is written to Lakebase per turn and per error, and the prompt registration |
| Compare and impact | `test_compare_audit.py`, `test_compare_documents.py`, `test_diff_engines.py`, `test_processors.py`, `test_pdf_layout.py`, `test_compare_llm.py`, `test_compare_pipeline.py`, `test_compare_round3.py`, `test_impact_queries.py`, `test_visual_diff.py`, `test_pdf_report.py` | Extraction and diff, analysis calls, streams, cache, the visual diff, impact-search queries, and the PDF export |
| Parsing pipeline | `test_parsing_chunking.py`, `test_parsing_describe.py`, `test_parsing_doc_status.py`, `test_parsing_quality.py`, `test_parsing_selection.py`, `test_parsing_tables.py` | Passage splitting, image description, document statuses, quality checks, file selection, and the write and cast conventions |
| Deploy and evaluation | `test_deploy_config.py`, `test_check_vision_endpoints.py` | `target_env.json` and its guards; the vision check of `utils/evaluation/` |
| Vector Search gate | `test_vs_gate.py` | The concurrency cap and the retries of the Vector Search queries |

## Guard tests

- `test_bundle_paths.py`: every `notebook_path` and `python_file` of the bundle exists; every job name follows the
  naming convention; the parsing modules import only names that `config.py` defines.
- `test_code_conventions.py`: no `print()`, no `# DBTITLE`, no commented-out import or print under `server/`, `utils/`
  and `scripts/`; each `create_proj_*` and `update_proj_*` notebook has the seven sections, and a markdown cell
  before each code cell.
- `test_deploy_config.py`: each live target sets its two index keys, on its own environment's schema; index names carry
  no version suffix; no `CHAT_ENDPOINT*` key; `CAPS_BYPASS` is `true` on `dev` only.
- `test_parsing_imports.py`: importing the parsing helpers does not start Spark (`leap_utils.config` is not loaded).
- `test_lakebase_schema.py`: the bootstrap emits the same statements in the same order, and every index targets a table
  the schema creates.
- `test_lakebase_logging.py`: every key the engine writes to the turn log, and every timed step, is a column of
  `chat_turns`; every passage key is a column of `chat_retrieved_chunks`.
- `test_chat_llm.py`: the chatbot's configured models (answer, fallback, rewrite, translation) are the Luna models only.
- `test_streaming_temperature.py`: which endpoints accept a `temperature` value.
- `test_standard_prompt.py`, `test_structured_prompt.py`, `test_summarize_prompt.py`, `test_impact_judge_prompt.py`: the
  language and rationale contracts of the comparison, summary and judge prompts.
- `test_prompt_noise.py`: an imperative rationale is flagged as noise, a descriptive one is not.
