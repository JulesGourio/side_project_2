# Lakebase: tables and columns

Reference of the application's tables in Lakebase (project `qualibot`, branch `production`). One database per environment: `doccompare` (UAT, DEV, PROD).

Source of truth: `server/services/infra/lakebase/schema.py` (creation and changes) and `columns.py` (columns of the chat turn log). The app creates the tables at startup (`ensure_schema`) with `CREATE TABLE IF NOT EXISTS`, then `ALTER TABLE … ADD COLUMN IF NOT EXISTS`. At startup it also drops the obsolete columns and merges duplicate rows of `users`; it never drops a table.

A new column is added in `schema.py`, or in `columns.py` for the chat tables. A new key of a turn's log (`TurnLog.data`) must have its column in `CHAT_TURN_COLUMNS`: `tests/test_lakebase_logging.py` checks it.

## Overview

| Table | One row per | Written by |
|---|---|---|
| `chat_turns` | chat turn (question, then answer), successful or not | `infra/lakebase/audit.py` (`store_chat_turn`) |
| `chat_retrieved_chunks` | passage retrieved for a turn, sent to the model or set aside | `infra/lakebase/audit.py` |
| `chat_sessions` | chat conversation | `routers/chat/persistence.py`, `routers/chat/sessions.py` |
| `chat_messages` | message of a conversation | `routers/chat/persistence.py`, `routers/chat/sessions.py` |
| `chat_feedbacks` | thumbs up / thumbs down on a chat message | `routers/chat/sessions.py` |
| `app_errors` | error, or warning of a step (`severity`) | `infra/lakebase/audit.py` |
| `compare_messages` | comparison analysis, history | `routers/history.py` |
| `compare_feedbacks` | vote on a comparison analysis | `routers/feedback.py` |
| `llm_requests` | `/compare/analyze` analysis call | `infra/lakebase/audit.py` |
| `impact_requests` | impact search (`/compare/impact`) | `infra/lakebase/audit.py` |
| `impact_document_results` | document judged by the model in an impact search | `infra/lakebase/audit.py` |
| `impact_feedbacks` | vote on an impact search: on the whole result, or on one document's verdict | `routers/feedback.py` |
| `impact_cache` | cached impact search result | `infra/lakebase/cache.py` |
| `summary_cache` | cached document summary | `infra/lakebase/cache.py` |
| `users` | user known to the app | `infra/lakebase/users.py` |
| `catalog_metadata` | "documents up to date as of" date (one row, `id = 1`) | entered by hand; read by `routers/config.py` |
| `doc_catalog` | document catalog: reference, title, link, revision, date | pipeline task `6_update_kb_metadata`; read by `infra/doc_catalog.py` |

**Chat join keys**:
- `trace_id` (`chat-` followed by 32 hexadecimal characters): present in `chat_turns`, `chat_retrieved_chunks`, `app_errors` and `chat_messages`.
- `chat_turns.user_message_id` and `chat_turns.assistant_message_id`: `chat_messages.id` of the two messages of the turn.
- `chat_turns.mlflow_trace_id`: MLflow trace id of the turn (empty when `CHAT_MLFLOW_EXPERIMENT` is empty or when sending failed). The thumbs vote is attached to it as an assessment.

## Chat

### `chat_turns`

One row per turn. The columns of the first table are fixed; the others come from `CHAT_TURN_COLUMNS` (`columns.py`).

**Result**

| Column | Type | Meaning |
|---|---|---|
| `id` | SERIAL | primary key |
| `created_at` | TIMESTAMPTZ | date of the turn |
| `trace_id` | TEXT NOT NULL, UNIQUE | turn identifier (`chat-…`) |
| `user_message_id` | INTEGER | `chat_messages.id` of the question (NULL after deletion) |
| `assistant_message_id` | INTEGER | `chat_messages.id` of the answer (NULL after deletion) |
| `status` | TEXT NOT NULL | `ok`, `degraded`, `error` or `aborted` (see below) |
| `error_stage` | TEXT | step that failed |
| `error_type` | TEXT | type of the error |
| `error_msg` | TEXT | error message |
| `warnings` | TEXT[] NOT NULL, default `{}` | warning codes of the turn (see "Codes") |

`status`: `ok` = answer given, nothing to report; `degraded` = answer given, but at least one step did not run as configured (`warnings` says which); `error` = no answer; `aborted` = the browser left before the end.

**Expected settings**

| Column | Type | Meaning |
|---|---|---|
| `index_name` | TEXT | search index used |
| `config` | JSONB | engine settings at the time of the turn: index, search sizes, answer models and their fallbacks, rewrite model, token limits |

**Question and identity**

| Column | Type | Meaning |
|---|---|---|
| `session_id` | TEXT | conversation (no foreign key: a turn whose messages could not be written is still logged) |
| `user_id` | TEXT | user |
| `workspace_id` | TEXT | Databricks workspace |
| `app_version` | TEXT | app version |
| `division` | TEXT | requested scope: `ALL`, `AS` or `IS` |
| `question` | TEXT | question asked |
| `question_lang` | TEXT | detected language of the question |
| `question_en` | TEXT | question sent to the search when it was translated |
| `history_messages` | INTEGER | messages of the conversation passed to the engine |

**Query rewrite**

| Column | Type | Meaning |
|---|---|---|
| `fr_query` | TEXT | search query in French |
| `en_query` | TEXT | search query in English |
| `rewrite_ok` | BOOLEAN | the rewrite (or translation) succeeded |
| `rewrite_mode` | TEXT | `translate`: opening message, only translated into the other language; `rewrite`: bilingual rewrite |
| `rewrite_endpoint` | TEXT | model that rewrote the query (NULL: every model failed) |
| `rewrite_attempts` | JSONB | every try of the rewrite or translation, retries included |
| `rewrite_input_tokens` | INTEGER | input tokens of the rewrite |
| `rewrite_output_tokens` | INTEGER | output tokens of the rewrite |
| `rewrite_cost_eur` | DOUBLE PRECISION | cost of the rewrite |

**Search**

| Column | Type | Meaning |
|---|---|---|
| `named_refs` | TEXT[] | references (REF) named in the conversation, looked up in addition |
| `vs_calls_expected` | INTEGER | Vector Search queries planned |
| `vs_calls_ok` | INTEGER | Vector Search queries that succeeded |
| `rerank_ok` | BOOLEAN | the reranker answered |
| `passages_retrieved` | INTEGER | passages found (rows of `chat_retrieved_chunks`) |
| `passages_sent` | INTEGER | passages passed to the model (`kept`) |
| `documents_sent` | INTEGER | distinct documents passed to the model |
| `prompt_chars` | INTEGER | size of the prompt, in characters |
| `instructions_sha` | TEXT | fingerprint (16 characters) of the system instructions used; changes when `server/prompts/chat/system/*.md` changes |
| `mlflow_trace_id` | TEXT | MLflow trace of the turn |

**Answer**

| Column | Type | Meaning |
|---|---|---|
| `answer_endpoint` | TEXT | model that answered (NULL: none) |
| `llm_fallback` | BOOLEAN | answer given by the fallback model |
| `llm_attempts` | JSONB | every call to a model: endpoint, outcome, HTTP status, seconds, seconds to the first text (`first_text_s`) |
| `truncated` | BOOLEAN | output token limit reached |
| `continuations` | INTEGER | number of resumptions after a cut |
| `input_tokens` | INTEGER | input tokens of the answer |
| `output_tokens` | INTEGER | output tokens of the answer |
| `thinking_tokens` | INTEGER | reasoning tokens |
| `cost_eur` | DOUBLE PRECISION | cost of the answer (the rewrite cost is in `rewrite_cost_eur`) |
| `answer_chars` | INTEGER | size of the answer, in characters |
| `citations_count` | INTEGER | number of citations in the answer |
| `cited_refs` | TEXT[] | references cited in the answer |
| `sources_count` | INTEGER | source chips shown (cited or named in the text) |
| `answer_translated` | BOOLEAN | answer translated into the language of the question |
| `user_message` | TEXT | message shown to the user on failure (NULL: nothing) |

**Durations** (milliseconds, INTEGER)

| Column | Measures |
|---|---|
| `total_ms` | from the message received to the end of the turn (answer sent, messages saved) |
| `first_token_ms` | from the message received to the first word of the answer: what the user waits for |
| `translate_in_ms` | language detection and translation of the question |
| `retrieval_ms` | the whole search: rewrite, search and reference lookup |
| `rewrite_ms` | rewrite |
| `search_ms` | Vector Search |
| `ref_lookup_ms` | lookup of the named references |
| `queue_wait_ms` | wait for a generation slot (`CHAT_MAX_CONCURRENT_ANSWERS`) |
| `generation_ms` | answer generation, retries included |
| `translate_back_ms` | translation of the answer |
| `persist_ms` | write to `chat_messages` |

**Codes** (`warnings`, one code per problem; the detail is in `app_errors`, same `trace_id`):

| Code | What happened |
|---|---|
| `translate_in_failed` | language detection or translation of the question failed: search run with the original question |
| `rewrite_failed` | no model rewrote the query: search run with the question alone |
| `rewrite_fallback` | rewrite done by the fallback model |
| `vs_partial` | some Vector Search queries failed (`vs_calls_ok` < `vs_calls_expected`) |
| `user_upsert_failed` | the `users` row could not be updated; the turn was saved anyway |
| `session_not_owned` | the session id belongs to another user; nothing was saved |
| `query_truncated` | a query longer than the Vector Search limit was cut before the call (`vs_calls[].truncated_from` gives its length) |
| `rerank_refused` | the index refused the reranker: raw search only |
| `no_passages` | no passage found |
| `llm_fallback` | answer given by the fallback model |
| `llm_retried` | failed calls (429, timeout, ...) retried before the answer |
| `answer_continued` | answer cut, then resumed |
| `answer_interrupted` | answer cut, no model could finish it |
| `translate_back_failed` | translation of the answer failed: answer left in the model's language |
| `persist_failed` | the messages could not be written to `chat_messages` |
| `client_disconnected`, `send_failed` | information about the browser connection |

### `chat_retrieved_chunks`

One row per passage retrieved for a turn, with its text. The table keeps the passage text, so an answer can be checked against what it was built on after the search index has changed.

| Column | Type | Meaning |
|---|---|---|
| `id` | BIGSERIAL | primary key |
| `created_at` | TIMESTAMPTZ | write date |
| `turn_id` | INTEGER NOT NULL | `chat_turns.id` (deleted with the turn) |
| `trace_id` | TEXT | `trace_id` of the turn |
| `position` | INTEGER | order of retrieval (merged by best rank) |
| `kept` | BOOLEAN NOT NULL, default `TRUE` | passage passed to the model |
| `drop_reason` | TEXT | when set aside: `other_language` (another language of the same document was preferred) or `over_cap` (beyond `CHAT_MAX_SEARCH_PASSAGES`) |
| `prompt_rank` | INTEGER | order in the prompt (passages passed only) |
| `doc_number` | INTEGER | `[n]` number of its document in the prompt |
| `cited` | BOOLEAN | its document is cited by the answer |
| `source` | TEXT | `search` (found by the search) or `ref_lookup` (brought by the reference lookup) |
| `hits` | JSONB | every query that found the passage: `q`, `via`, `rank`, `score` |
| `best_score` | DOUBLE PRECISION | best score |
| `chunk_id` | TEXT | passage identifier in the index |
| `iddoc` | TEXT | Intraqual identifier of the document revision |
| `ref` | TEXT | document reference |
| `division` | TEXT | document division |
| `url` | TEXT | document link |
| `semantic_headers` | TEXT | semantic headers of the passage |
| `chunk_text` | TEXT | passage text |

Indexes: `idx_chat_retrieved_chunks_turn_id` (`turn_id`), `idx_chat_retrieved_chunks_ref` (`ref`).

### `chat_sessions`

| Column | Type | Meaning |
|---|---|---|
| `id` | TEXT PK | conversation identifier |
| `created_at`, `updated_at` | TIMESTAMPTZ | dates |
| `user_id`, `workspace_id`, `workspace_url` | TEXT | owner |
| `name` | TEXT NOT NULL, default `New conversation` | title |
| `deleted` | BOOLEAN NOT NULL, default `FALSE` | soft delete: rows are never physically removed |
| `deleted_at` | TIMESTAMPTZ | date of the soft delete |
| `share_token` | TEXT | read-only sharing token (NULL: not shared) |

Unique index: `chat_sessions_share_token_idx` (`share_token`, where it is not NULL).

### `chat_messages`

| Column | Type | Meaning |
|---|---|---|
| `id` | SERIAL PK | message identifier |
| `created_at` | TIMESTAMPTZ | date |
| `session_id` | TEXT | `chat_sessions.id` (deleted with the conversation) |
| `user_id`, `workspace_id`, `workspace_url` | TEXT | author |
| `role` | TEXT NOT NULL | `user` or `assistant` |
| `content` | TEXT NOT NULL | message text |
| `trace_id` | TEXT | `trace_id` of the turn (`chat_turns`) |
| `endpoint_name` | TEXT | engine label: `chat-all`, `chat-as` or `chat-is` (NULL on the copies of a shared conversation). The model that answered is `chat_turns.answer_endpoint` |
| `sources_json` | TEXT | sources shown with the answer (reference, title, revision, date) |
| `deleted`, `deleted_at` | BOOLEAN, TIMESTAMPTZ | soft delete |
| `status` | TEXT NOT NULL, default `ok` | `ok` or `error`; failed turns are kept, with the reason |
| `error_msg` | TEXT | reason of the failure |
| `division` | TEXT NOT NULL, default `ALL` | scope of the question: `ALL`, `AS` or `IS` |
| `question_lang` | TEXT | language of the question, as detected by the translation bridge |

Index: `chat_messages_session_idx` (`session_id`).

### `chat_feedbacks`

| Column | Type | Meaning |
|---|---|---|
| `id` | SERIAL PK | identifier |
| `created_at` | TIMESTAMPTZ | date |
| `message_id` | INTEGER | `chat_messages.id` (NULL after deletion) |
| `session_id` | TEXT | `chat_sessions.id` (NULL after deletion) |
| `user_id`, `workspace_id`, `workspace_url` | TEXT | author of the vote |
| `vote` | TEXT NOT NULL | `up` or `down` |
| `comment` | TEXT | optional comment |
| `resolved` | BOOLEAN NOT NULL, default `FALSE` | vote handled (dashboard triage) |
| `resolution_reason` | TEXT | reason it was handled |

## Comparison and history

### `compare_messages`

One row per comparison analysis, for the history.

| Column | Type | Meaning |
|---|---|---|
| `id` | SERIAL PK | identifier |
| `created_at` | TIMESTAMPTZ | date |
| `user_id`, `workspace_id`, `workspace_url` | TEXT | user |
| `old_filename`, `new_filename` | TEXT NOT NULL | names of the compared files |
| `old_file_hash`, `new_file_hash` | TEXT | file fingerprints |
| `analysis_text` | TEXT | analysis result |
| `impact_text` | TEXT | impact search result attached to the comparison |
| `volume_session_path` | TEXT | path of the files on the session volume |
| `file_type` | TEXT | file type |
| `processing_method` | TEXT | extraction method |
| `processor_version` | TEXT | processor version |
| `ttft_s`, `generation_s` | DOUBLE PRECISION | seconds before the first word, and generation seconds |
| `input_tokens`, `output_tokens`, `thinking_tokens`, `total_tokens` | INTEGER | tokens |
| `cost_eur` | DOUBLE PRECISION | cost |
| `app_version` | TEXT | app version |
| `llm_request_id` | INTEGER | `llm_requests.id` of the analysis |
| `endpoint_name` | TEXT | model used (copy of `llm_requests.endpoint_name`) |

### `compare_feedbacks`

Votes on a comparison analysis.

| Column | Type | Meaning |
|---|---|---|
| `id` | SERIAL PK | identifier |
| `created_at` | TIMESTAMPTZ | date |
| `message_id` | INTEGER | `compare_messages.id` (NULL after deletion) |
| `user_id`, `workspace_id`, `workspace_url` | TEXT | author |
| `vote` | TEXT NOT NULL | `up` or `down` |
| `comment` | TEXT | comment |
| `resolved` | BOOLEAN NOT NULL, default `FALSE` | vote handled |
| `resolution_reason` | TEXT | reason it was handled |

### `llm_requests`

One row per `/compare/analyze` analysis.

| Column | Type | Meaning |
|---|---|---|
| `id` | SERIAL PK | identifier |
| `created_at` | TIMESTAMPTZ | date |
| `old_file_hash`, `new_file_hash` | TEXT | fingerprints |
| `file_type` | TEXT | file type |
| `endpoint_name` | TEXT | model |
| `messages_json` | TEXT | messages sent to the model |
| `input_tokens`, `output_tokens`, `thinking_tokens`, `total_tokens` | INTEGER | tokens |
| `cost_eur` | DOUBLE PRECISION | cost |
| `http_status` | INTEGER | HTTP status of the call |
| `error_type`, `error_msg` | TEXT | error, if any |
| `status` | TEXT | `ok`, `error` or `aborted` (the browser left before the end) |
| `queue_wait_ms` | INTEGER | wait for an analysis slot |
| `build_ms` | INTEGER | file extraction and prompt |
| `first_token_ms` | INTEGER | from the call to the first word |
| `generation_ms` | INTEGER | from the call to the end |
| `total_ms` | INTEGER | total duration |
| `chunked_parts` | INTEGER | number of parts (split into several calls; 0: one call) |

## Impact search

### `impact_requests`

One row per call to `/compare/impact`, successful or not.

| Column | Type | Meaning |
|---|---|---|
| `id` | SERIAL PK | identifier |
| `created_at` | TIMESTAMPTZ | date |
| `user_id`, `workspace_id` | TEXT | user |
| `method` | TEXT NOT NULL | impact search method (e.g. `index_llm_per_doc`) |
| `old_file_hash`, `new_file_hash` | TEXT | fingerprints |
| `changes_chars` | INTEGER | size of the change list, in characters |
| `truncated` | BOOLEAN | change list truncated |
| `chunks_returned` | INTEGER | passages returned by the search |
| `num_documents` | INTEGER | documents found |
| `duration_s` | DOUBLE PRECISION | total duration, in seconds |
| `endpoint_name` | TEXT | judge model |
| `input_tokens`, `output_tokens`, `total_tokens` | INTEGER | tokens |
| `cost_eur` | DOUBLE PRECISION | cost |
| `http_status` | INTEGER | HTTP status |
| `error_type`, `error_msg` | TEXT | error, if any |
| `status` | TEXT | `ok`, `error` or `aborted` |
| `app_version` | TEXT | app version |
| `index_name` | TEXT | search index |
| `queries_used` | INTEGER | Vector Search queries (one per change) |
| `queries_failed` | INTEGER | queries that failed |
| `candidates` | INTEGER | documents sent to the judge |
| `not_judged` | INTEGER | documents found beyond `impact_max_candidates` (not judged) |
| `judge_failed` | INTEGER | judge calls that failed after retries |
| `extract_ms` | INTEGER | change list to queries |
| `search_ms` | INTEGER | search |
| `judge_ms` | INTEGER | all judge calls (run in parallel) |

### `impact_document_results`

One row per document judged by the model.

| Column | Type | Meaning |
|---|---|---|
| `id` | SERIAL PK | identifier |
| `created_at` | TIMESTAMPTZ | date |
| `request_id` | INTEGER NOT NULL | `impact_requests.id` (deleted with the request) |
| `ref` | TEXT | document reference |
| `division` | TEXT | division |
| `url` | TEXT | link |
| `impacted` | BOOLEAN | the document is impacted |
| `confidence` | TEXT | confidence of the verdict |
| `section` | TEXT | section concerned |
| `reason` | TEXT | justification |
| `other_documents_json` | TEXT | other documents cited by the verdict |
| `status` | TEXT | `impacted`, `check` (to be checked), `not_impacted` or `error` |
| `judge_ms` | INTEGER | duration of the judge call |
| `judge_attempts` | INTEGER | attempts of the call |
| `input_tokens`, `output_tokens` | INTEGER | tokens of the call |

Index: `idx_impact_document_results_request_id` (`request_id`).

### `impact_feedbacks`

Votes on an impact search: on the whole result (`ref` NULL, optional comment) or on one document's verdict.

| Column | Type | Meaning |
|---|---|---|
| `id` | SERIAL PK | identifier |
| `created_at` | TIMESTAMPTZ | date |
| `impact_request_id` | INTEGER | `impact_requests.id` (NULL after deletion) |
| `message_id` | INTEGER | `compare_messages.id` of the comparison (NULL after deletion) |
| `user_id`, `workspace_id`, `workspace_url` | TEXT | author |
| `old_file_hash`, `new_file_hash` | TEXT | file fingerprints |
| `ref` | TEXT | document voted on (NULL: vote on the whole result) |
| `verdict_shown` | TEXT | verdict shown at the time of the vote |
| `vote` | TEXT NOT NULL | `up` or `down` |
| `comment` | TEXT | comment |
| `resolved` | BOOLEAN NOT NULL, default `FALSE` | vote handled |
| `resolution_reason` | TEXT | reason it was handled |

Index: `idx_impact_feedbacks_request_id` (`impact_request_id`).

### `impact_cache`

Result of an impact search, reused for the same files (the "Re-run" action bypasses it).

| Column | Type | Meaning |
|---|---|---|
| `id` | SERIAL PK | identifier |
| `created_at` | TIMESTAMPTZ | date |
| `old_file_hash`, `new_file_hash` | TEXT NOT NULL | fingerprints of the two files |
| `app_version` | TEXT NOT NULL | app version |
| `result_json` | TEXT NOT NULL | full result |

Index: `idx_impact_cache_hashes` (`old_file_hash`, `new_file_hash`, `app_version`).

### `analysis_cache`

Result of a comparison analysis, reused for the same two files. Written by the server only, never from a request body.

| Column | Type | Meaning |
|---|---|---|
| `id` | SERIAL PK | identifier |
| `created_at` | TIMESTAMPTZ | date |
| `old_file_hash`, `new_file_hash` | TEXT NOT NULL | fingerprints of the two files |
| `cache_version` | TEXT NOT NULL | app version, processor version and prompt version |
| `analysis_text` | TEXT NOT NULL | analysis |
| `file_type`, `processing_method`, `processor_version` | TEXT | how the analysis was produced |

Index: `idx_analysis_cache_hashes` (`old_file_hash`, `new_file_hash`, `cache_version`).

### `summary_cache`

Summary of a document, reused for the same file.

| Column | Type | Meaning |
|---|---|---|
| `id` | SERIAL PK | identifier |
| `created_at` | TIMESTAMPTZ | date |
| `file_hash` | TEXT NOT NULL | fingerprint of the document |
| `app_version` | TEXT NOT NULL | app version |
| `result_json` | TEXT NOT NULL | summary |
| `endpoint_name` | TEXT | model that produced the summary |

Index: `idx_summary_cache_hash` (`file_hash`, `app_version`).

## Error log

### `app_errors`

One row per error or warning. The warnings of a chat turn are written with the turn (one row per code in `warnings`).

| Column | Type | Meaning |
|---|---|---|
| `id` | SERIAL PK | identifier |
| `created_at` | TIMESTAMPTZ | date |
| `user_id`, `workspace_id` | TEXT | user |
| `endpoint` | TEXT | app route (e.g. `/api/chat/ws`) |
| `error_type` | TEXT | type of the error |
| `error_msg` | TEXT | error message |
| `old_filename`, `new_filename`, `file_type` | TEXT | files concerned (comparison) |
| `stack_trace` | TEXT | call stack (4,000 characters at most) |
| `llm_request_id` | INTEGER | `llm_requests.id` concerned |
| `severity` | TEXT NOT NULL, default `error` | `error` (the request failed) or `warning` (it went on, degraded) |
| `stage` | TEXT | step that failed. Chat: `translate_in`, `rewrite`, `search`, `ref_lookup`, `llm`, `translate_back`, `persist`, `ws`. Comparison: `build`, `llm`, `stream` (analysis); `search`, `judge` (impact). Any other route: `unhandled` |
| `http_status` | INTEGER | HTTP status |
| `upstream` | TEXT | Vector Search index or model endpoint called |
| `trace_id` | TEXT | `trace_id` of the chat turn |
| `session_id` | TEXT | conversation |
| `context` | JSONB | expected and actual values: planned model chain, attempts, queries sent or failed, warning code |
| `app_version` | TEXT | app version |
| `origin` | TEXT | place in the code, `path:line in function` (the deepest frame of the stack in this repository, else the code that reported the error) |
| `fingerprint` | TEXT | 12 characters grouping the same error across requests (same route, step, type and `origin`; line number ignored) |
| `user_message` | TEXT | what the user saw; NULL: nothing was shown |

In the chat, `user_message` is always the same text, sent only when every retry has failed. No technical message is sent to the chat browser: the cause is only in `app_errors`, `chat_turns.error_*` and `chat_messages.error_msg`.

Indexes: `idx_errors_created_at` (`created_at`), `idx_errors_trace_id` (`trace_id`), `idx_errors_fingerprint` (`fingerprint`).

## Miscellaneous

### `users`

| Column | Type | Meaning |
|---|---|---|
| `id` | SERIAL PK | identifier |
| `created_at`, `updated_at` | TIMESTAMPTZ | dates |
| `user_id` | TEXT UNIQUE NOT NULL | Databricks identifier of the user |
| `workspace_id` | TEXT | workspace |
| `email` | TEXT | email (without spaces; unique when not NULL) |
| `display_name` | TEXT | display name |
| `workspace_url` | TEXT | workspace address |
| `role` | TEXT NOT NULL, default `user` | role |
| `is_active` | BOOLEAN NOT NULL, default `TRUE` | active account |
| `groups` | TEXT[] NOT NULL, default `{}` | allowed groups of the user, written by the job task `sync_user_capabilities` from `system.access.audit` |
| `can_chat`, `can_compare` | BOOLEAN NOT NULL, default `TRUE` | capabilities derived from `groups` by the same task (for reading; the app derives them itself) |
| `groups_synced_at` | TIMESTAMPTZ | last write of `groups` by the task; NULL = never computed, the access check ignores the row |

Unique index: `users_email_idx` (`email`, where it is not NULL).

### `catalog_metadata`

One row only (`id = 1`, constraint `single_row`): the "documents up to date as of" date, shown in the interface.

| Column | Type | Meaning |
|---|---|---|
| `id` | INTEGER PK, default `1` | always `1` |
| `documents_as_of` | DATE | date shown |
| `updated_at` | TIMESTAMPTZ | last update |

### `doc_catalog`

Catalog of the documents in the search scope. Rewritten by pipeline task `6_update_kb_metadata` after each daily run (`utils/parsing_pipeline/create_proj_doc_catalog.py`); read by the app, which reloads it every 30 minutes.

| Column | Type | Meaning |
|---|---|---|
| `ref` | TEXT PK | document reference |
| `base_ref` | TEXT | base reference; NULL: computed by the app (`doc_catalog._canon`) |
| `title` | TEXT | title |
| `url` | TEXT | link |
| `division` | TEXT | division |
| `in_chat` | BOOLEAN NOT NULL, default `TRUE` | document present in the chat index |
| `updated_at` | TIMESTAMPTZ NOT NULL | last write |
| `revision` | TEXT | current revision (Intraqual index) |
| `doc_date` | DATE | publication date of the revision |

The columns `revision` and `doc_date` are added by the app when it owns the table; otherwise by pipeline task 6. The app gives them to the model with each document and shows them on the source chips.

## Control queries

Examples for checking the tables, in the Lakebase SQL editor of the database concerned:

```sql
-- Turns of the last 7 days by status, median durations
SELECT status, COUNT(*),
       PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY total_ms)       AS total_p50,
       PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY first_token_ms) AS first_token_p50
FROM chat_turns WHERE created_at > NOW() - INTERVAL '7 days' GROUP BY status;

-- Most frequent warnings
SELECT w AS warning, COUNT(*) FROM chat_turns, UNNEST(warnings) AS w
WHERE created_at > NOW() - INTERVAL '7 days' GROUP BY w ORDER BY 2 DESC;

-- One turn in detail: its passages, in prompt order
SELECT prompt_rank, doc_number, ref, cited, source, best_score, LEFT(chunk_text, 120)
FROM chat_retrieved_chunks WHERE trace_id = 'chat-…' AND kept ORDER BY prompt_rank;

-- Errors by fingerprint, over 7 days
SELECT fingerprint, endpoint, severity, stage, error_type, origin, COUNT(*) AS n,
       MIN(created_at) AS first_seen, MAX(created_at) AS last_seen, MAX(LEFT(error_msg, 150)) AS example
FROM app_errors WHERE created_at > NOW() - INTERVAL '7 days'
GROUP BY 1, 2, 3, 4, 5, 6 ORDER BY n DESC;
```

## Indexes

| Table | Indexes |
|---|---|
| `chat_turns` | `idx_chat_turns_created_at` (`created_at`), `idx_chat_turns_session_id` (`session_id`), `idx_chat_turns_assistant_message_id` (`assistant_message_id`) |
| `chat_retrieved_chunks` | `idx_chat_retrieved_chunks_turn_id` (`turn_id`), `idx_chat_retrieved_chunks_ref` (`ref`) |
| `chat_sessions` | `chat_sessions_share_token_idx` (`share_token`, unique, where it is not NULL) |
| `chat_messages` | `chat_messages_session_idx` (`session_id`) |
| `app_errors` | `idx_errors_created_at`, `idx_errors_trace_id`, `idx_errors_fingerprint` |
| `impact_document_results` | `idx_impact_document_results_request_id` (`request_id`) |
| `impact_feedbacks` | `idx_impact_feedbacks_request_id` (`impact_request_id`) |
| `impact_cache` | `idx_impact_cache_hashes` (`old_file_hash`, `new_file_hash`, `app_version`) |
| `analysis_cache` | `idx_analysis_cache_hashes` (`old_file_hash`, `new_file_hash`, `cache_version`) |
| `compare_messages` | `idx_compare_messages_user` (`user_id`, `created_at` DESC) |
| `chat_sessions` | `idx_chat_sessions_user` (`user_id`, `updated_at` DESC) |
| `chat_feedbacks` | `idx_chat_feedbacks_message` (`message_id`) |
| `summary_cache` | `idx_summary_cache_hash` (`file_hash`, `app_version`) |
| `users` | `users_email_idx` (`email`, unique, where it is not NULL) |
