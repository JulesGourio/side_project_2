# parsing_pipeline

Daily chain that turns the documents of the Intraqual document management system into indexed passages.
The chatbot and the impact search read these passages.

The chain is the job `D_1_Qualibot_Parsing_Pipeline`, defined in
[`resources/parsing_pipeline.job.yml`](../../resources/parsing_pipeline.job.yml). It runs six tasks in order.
Each notebook is a task; the modules next to them hold the logic. The notebooks follow the section layout
`Technical debt`, `Configuration`, `Inputs`, `Data Preparation`, `Data Transformations`, `Quality Checks`, `Outputs`.

## Files

| File | Role |
|---|---|
| `create_proj_category_reference.py` | Task `1_categories`: freshness guard of the Intraqual sources, table `category_reference` |
| `create_proj_parse_manifest.py` | Task `2_manifest`: scope, pruning of stale content, classification of the volume folders, table `parse_manifest` |
| `create_proj_parsing_checkpoint.py` | Task `3_parse`: file selection, parsing of the non-PDF formats and of a few PDFs, checkpoint |
| `create_proj_parsing_checkpoint_gpu.py` | Task `3b_parse_pdf`: Docling on the PDFs queued by `3_parse` (GPU) |
| `create_proj_chunks.py` | Task `3c_chunks`: retry, passages, writes of the tables |
| `create_proj_image_metadata.py` | Task `4_describe_images`: image descriptions by a vision model, image passages |
| `update_proj_vector_indexes.py` | Task `5_sync_index`: creates missing indexes, triggers their sync, refreshes `chunks_full` |
| `create_proj_doc_catalog.py` | Task `6_update_kb_metadata`: document catalog and knowledge-base date in Lakebase |
| `config.py` | Parameters (`PARSING_*` environment variables with their defaults), thresholds, model prompts, document and image statuses |
| `categories.py` | Category hierarchy, division of each document, scope gate (Spark) |
| `manifest_steps.py` | Phases of `2_manifest` |
| `selection.py` | Choice of the best file per IDDOC (format priorities) |
| `index_state.py` | Whether a Vector Search index is still syncing, from its detailed state |
| `retention.py` | Which checkpoint snapshots a full run drops (older than `CHECKPOINT_SNAPSHOT_KEEP_DAYS`, the latest is kept) |
| `parse_steps.py` | Phases of `3_parse`: run scope, selection, batched parsing, resume, retry, empty folders |
| `build_steps.py` | Tables of `3_parse` built from the checkpoint: `image_metadata`, `parsed_documents`, passages, archive notices |
| `output_steps.py` | Writes of `3_parse`: tables, run health, change log, archive notices, ghost-document guard |
| `describe_steps.py` | Phases of `4_describe_images`: model calls, image passages, promotion of documents |
| `llm_describe.py` | Asynchronous vision calls: rate limiter, endpoint chain, circuit breaker |
| `format_parsers.py` | Bytes to text, one parser per format (Docling and the fallback parsers) |
| `image_utils.py` | Parsing UDF of `3_parse`: Docling or fallback, image extraction, volume save |
| `image_fallbacks.py` | Text and images of the files Docling does not read: DOCX and PPTX (XML), binary PPT (OLE), scanned PDF pages; writes of the images to the volume (path check) |
| `chunking.py` | Splits a Markdown document into passages (sections, tables, provenance prefix) |
| `tables.py` | The only Delta write layer (`save_table`, `merge_into`, from `leap_utils`), and the quality checks of the tables: RED and AMBER levels, Great Expectations, REF consistency of the passages with `parse_manifest` (AMBER) |
| `worker.py` | Runtime shared by the driver and the workers: configuration broadcast, token counting, chunking UDFs |

The modules listed in `config.WORKER_MODULES` (`chunking`, `worker`, `format_parsers`, `image_fallbacks`, `image_utils`, `config`) are shipped to the Spark workers; the others run on the driver.

Unit tests in `tests/` cover `chunking.py`, `tables.py`, the statuses of `config.py`, `selection.py`,
`index_state.py`, `retention.py` and the image description in `llm_describe.py`.

## Tasks

| Task | Notebook | Compute | Writes |
|---|---|---|---|
| `1_categories` | `create_proj_category_reference.py` | single-node CPU | `category_reference` |
| `2_manifest` | `create_proj_parse_manifest.py` | single-node CPU | `parse_manifest`, `parsed_documents` (skip rows), pruning in the passage and image tables |
| `3_parse` | `create_proj_parsing_checkpoint.py` | single-node CPU (S policy, `parse_cpu_cluster`) | `parsing_checkpoint` (non-PDF formats, and the PDFs when at most `parsing_gpu_min_pdfs`), `parsing_run_scope`, `parsing_gpu_queue`, `document_change_log`, task value `pdfs_for_gpu` |
| `3b_needs_gpu` | condition `pdfs_for_gpu > 0` | none | — |
| `3b_parse_pdf` | `create_proj_parsing_checkpoint_gpu.py` | GPU, only when the condition is true | `parsing_checkpoint` (the queued PDFs) |
| `3c_chunks` | `create_proj_chunks.py` | the CPU cluster of `3_parse` (after the `false` branch of the condition or `3b_parse_pdf`, `run_if: AT_LEAST_ONE_SUCCESS`) | `parsing_checkpoint` (retries), `parsed_documents`, `chunks`, `chunks_archive`, `archive_notices`, `image_metadata`, `parsing_run_health` |
| `4_describe_images` | `create_proj_image_metadata.py` | CPU | `image_metadata`, image passages in `chunks` and `chunks_archive`, `parsed_documents` |
| `5_sync_index` | `update_proj_vector_indexes.py` | classic CPU (`meta_cpu_cluster`, GX on `chunks_full`) | `chunks_full`, Vector Search indexes |
| `6_update_kb_metadata` | `create_proj_doc_catalog.py` | serverless | `doc_catalog`, `catalog_metadata` (Lakebase) |

Each task depends on the previous one. Only PDFs use Docling's GPU models (layout, tables): docx, pptx and html go
through Docling's simple pipeline, and doc / xls / ppt / rtf / odt through format parsers, all on CPU. A daily run
(0 to a few new PDFs) parses everything in `3_parse` and never starts the GPU cluster; a rebuild with more than
`parsing_gpu_min_pdfs` (30) PDFs hands them to `3b_parse_pdf`. The parse tasks hand over through `parsing_run_scope`
(the IDDOCs of the run, refused by `3c_chunks` if another job run wrote it) and `parsing_gpu_queue`, so the job runs
one run at a time. The parse notebooks pin an `opencv-python-headless` older than 4.13 (4.13 aborts on the runtime's OpenSSL) in a `%pip` cell: 4.11.0.86 on the CPU tasks (the standard runtime keeps numpy 1; 4.12 needs numpy 2), 4.12.0.88 on the GPU task. It replaces the
OpenCV build that Docling pulls in, which fails on the GPU runtime and without a display library.

## Settings

The job sets the environment variables read by `config.py`. The main ones, with their defaults in `config.py`:

| Variable | Default | Effect |
|---|---|---|
| `PARSING_RUN_MODE` | `incremental` | `incremental`, `full` (reparse everything, the checkpoint is cloned first) or `rechunk` (rebuild the tables from the checkpoint, no volume scan, no parsing) |
| `PARSING_PARSE_FILTER` | empty | Comma-separated IDDOCs: restricts the run to them |
| `PIPELINE_WRITE_ENV` | job variable `pipeline_write_env` | `dev`, `uat` or `prod`: the tables are written to `<env>_proj.qualibot` |
| `PARSING_MAX_SOURCE_STALENESS_HOURS` | `36` | Freshness guard of `1_categories`; `0` disables it |
| `PARSING_DOC_DATE_CUTOFF` | `2018-01-01` | Date below which a document is archived |
| `PARSING_ARCHIVE_MAX_DOCS` | `0` | Number of archived documents parsed, most recent first; `-1` for all |
| `PARSING_ARCHIVE_NOTICES_IN_RAG` | `false` | Copies the archive notices into `chunks` |
| `PARSING_VECTOR_SEARCH_INDEXES` | empty | Indexes synced by `5_sync_index` (widget `indexes` takes precedence) |
| `PARSING_LLM_ENDPOINT` | `databricks-gpt-6-luna` | Vision model of `4_describe_images` |
| `PARSING_LLM_FALLBACK_ENDPOINTS` | `databricks-gpt-5-6-luna` | Comma-separated endpoints tried after the primary |
| `PARSING_LLM_BATCH_SIZE` | `6000` | Images described per run |
| `PARSING_IMAGE_MAX_ATTEMPTS` | `10` | Attempts before an image is set aside as `FAILED_LIMIT` |

The other variables (volumes, catalog and schema, GPU, rate budgets, chunk sizes, OCR thresholds) are
listed at the top of `config.py`.

## Documents and scope

- **Scope gate** (`DOC_SCOPE_FILTER`): `revision_status_code = 1 AND lifecycle_state_code = 7 AND is_hidden = false` on
  `document_revisions_intraqual` (the Intraqual data asset; `courant`, `etat` and `nonvisible` at source). Only the
  current revision of a validated, visible document enters the chain. `difftotale` is not part of the gate: the
  Intraqual portal shows documents with `difftotale = 0`.
- **Division** (`categories.build_division_reference`): from the root of the main category (`AS -`, `IS -`, PROCESSES,
  DIRECTIVE); under the process tree (`00-LATECOERE PROCESSES_One QMS`) a leaf category named `AS` or `IS` sets it,
  and every other document is `ALL` (shared by the divisions). Only `AS`, `IS`, `DIRECTIVE` and `ALL` exist. Pre-cutoff
  documents go to `chunks_archive` except under the date-exempt roots (`DATE_EXEMPT_ROOT_IDCATS`).
- **Manifest**: `2_manifest` keeps the in-scope documents, with their REF, title, revision (`indice`), date,
  category hierarchy, document type and `parse_content` flag. A category listed in `EXCLUDED_IDCATS` and a REF
  listed in `MANUAL_REF_EXCLUSIONS` are excluded.
- **One revision, one IDDOC**: each revision of a document has its own IDDOC. When an IDDOC leaves the scope,
  `2_manifest` deletes its rows in `chunks`, `chunks_archive`, `image_metadata` and the `parsed_documents` rows
  of statuses `SUCCESS`, `ERROR`, `EMPTY_TEXT` and `IMAGES_PENDING`. When several in-scope IDDOCs share a REF,
  only the newest revision (highest `indice`, then highest IDDOC) keeps its passages. The `parsed_documents`
  rows of the other IDDOCs stay, so they are not parsed again.
- **Pruning and `DRY_RUN`**: the pruning of derived rows runs on every run. The widget `dry_run` (default `true`)
  protects the volume only: out-of-scope folders are deleted when it is `non`.
- **Volume folders** are classified by `2_manifest`: `KEEP` (parsed), `DELETE` (out of scope), `ORPHAN` (IDDOC
  unknown in `document_revisions_intraqual`, kept on the volume, not parsed), `SKIP` (name not `D_<IDDOC>` or `Dm_<IDDOC>`), `MANUAL`,
  `CAT_EXCL`.
- **Archive**: a document published before `PARSING_DOC_DATE_CUTOFF` is parsed like the others, but its passages go
  to `chunks_archive` and never to `chunks`. `parsed_documents.is_filtered_by_date` is `true` for these. A document
  without date counts as recent. `archive_notices` holds one passage per archived document (REF, title, revision,
  type, date and a statement that the content is not indexed), rebuilt on every run; it reaches `chunks` only when
  `PARSING_ARCHIVE_NOTICES_IN_RAG` is `true`.
- **Empty folder**: an in-scope IDDOC whose folder has no parseable file is recorded as `SKIPPED_EMPTY_FOLDER`.

### Statuses of `parsed_documents` (one row per IDDOC)

| `parse_status` | Meaning |
|---|---|
| `SUCCESS` | Parsed, passages written |
| `ERROR` | Parsing failed, or the file exceeded `PARSE_TIMEOUT_SECONDS` (200 s, `parser_strategy` = `timeout`); retried by the next incremental run |
| `EMPTY_TEXT` | No text and no image left to describe |
| `IMAGES_PENDING` | No text, images still to describe; `4_describe_images` moves it to `SUCCESS` or `SKIPPED_EMPTY_IMAGES` |
| `SKIP_TOO_LARGE` | Above the size ceiling of the format |
| `SKIPPED_EMPTY_SMALL_FILE` | No text and at most 50 KiB |
| `SKIPPED_EMPTY_IMAGES` | No usable image |
| `SKIPPED_EMPTY_FOLDER` | No parseable file in the folder |
| `SKIPPED_REF_OUT_OF_SCOPE`, `SKIPPED_IDDOC_NOT_FOUND`, `SKIPPED_SOURCE_REMOVED` | Out of scope, unknown IDDOC, source removed from Intraqual |
| `SKIPPED_REF_MANUAL`, `SKIPPED_CATEGORY_EXCLUDED` | Manual exclusions |
| `MISSING_CHUNKS` | `SUCCESS` without passage, set by the ghost-document guard; parsed again by the next run |

## Selection and parsing (`3_parse`)

- **File choice** (`selection.py`): only the folders of the target IDDOCs are listed. Candidates are ranked by
  folder prefix (`D_` before `Dm_`), then by format priority, then by the newest modification time. Formats
  without priority and temporary files (`~`) are excluded, as are `headers.html`, `thumbs.db`, `.ds_store`,
  `desktop.ini` and `copyright.txt`.

  | Format | Priority |
  |---|---|
  | docx 1, pdf 2, docm 3, doc 4, html 5, pptx 6, ppt 7, txt 8, xml 9 | 1 to 9 |
  | xlsx 10, xls 11, xlsm 12, xlsb 13, rtf 14, odt 15, ods 16 | 10 to 16 |

- **Resume**: a file already `SUCCESS` with the same path and `document_sha256` is not parsed again. An IDDOC
  already in a terminal status is skipped in `incremental` mode, unless its `indice`, `doc_date` or `ref` changed.
- **Retry**: an IDDOC that failed is parsed again with its next-best file (`ENABLE_RETRY` in `config.py`).
- **Size ceilings** (`parse_steps.py`): 15 MB for the formats read by Docling, 35 MB for PDF, 100 MB for the other
  formats and for the OOXML files (`docx`, `docm`, `pptx`, `pptm`). Above 15 MB, an OOXML file goes straight to the
  XML reader. Above the ceiling, the file is recorded as `SKIP_TOO_LARGE` without a parse attempt. Before the
  size check, OOXML files lose their embedded objects and video or audio, and large images are recompressed.
- **Parsers** (`format_parsers.py`, `image_utils.py`): Docling for PDF, DOCX, PPTX, HTML and Markdown or text, with
  the accurate table mode; Docling's own OCR is off. `.doc` through antiword, `.rtf` through striprtf, `.odt` and
  `.ods` through odfpy, `.xls` through xlrd, `.xlsx` and `.xlsm` through openpyxl (raw XML of the zip as fallback),
  `.xlsb` through pyxlsb, `.csv`, `.tsv` and `.json` as text, one bullet per row for CSV and TSV. Binary `.ppt`
  is read from the OLE streams (text runs and embedded JPEG or PNG). DOCX and PPTX are read from their XML when
  Docling fails or finds no image.
- **Spreadsheets** keep at most `MAX_CHUNKS_SPREADSHEET` (100) passages per file.
- **Scanned PDF**: when the extracted text is shorter than `PARSING_LLM_OCR_TEXT_THRESHOLD` (150 characters), the
  first `PARSING_LLM_OCR_MAX_PAGES` (100) pages are rendered and queued for transcription by the vision model
  (`PDF_PAGE_OCR_PROMPT`). Stamps and watermarks are written on a `STAMP/WATERMARK:` line.
- **Images**: extracted at scale 3.0 (about 216 DPI) as PNG to `<VOLUME_BASE_PATH>/<IDDOC>/<IDDOC>_IMG_<NNN>.png`
  (page renders: `_PAGE_<NNN>`), with an `fsync` after the write. An image is set aside as `SKIPPED_DECORATIVE`
  without a model call when its longer side is under 140 px or its shorter side is under 85 px. An image covering
  less than 5 % of its page is not kept, and an image that appears on more than two pages is dropped.
- **Checkpoint**: the parsing writes the checkpoint every 100 files. A full run first clones the existing
  checkpoint to `_pipeline_checkpoint_<YYYYMMDD_HHMM>` (shallow clone).

## Passages (`chunking.py`)

- The Markdown is cut on the headings of levels 1 to 3. A passage never spans two sections of level 1 or 2; a
  section too small to stand alone joins the next one.
- Sizes are bounded in tokens (tiktoken `cl100k_base`: 150 minimum, 300 target, 450 maximum) and in characters
  (1 600), because dot leaders and form underscores use few tokens for many characters.
- Consecutive passages of one section overlap by 12 % of the target size. Tables longer than the target are split
  by rows, with the header repeated.
- `semantic_headers` is the heading path common to the blocks of a passage. The passage starts with the section
  path, written once.
- Each passage starts with a provenance prefix (`EMBED_SOURCE_PREFIX`):

  ```
  [Source: <REF> | Title: <title> | Type: <type> | Division: <division> | Category: <category> | Date de diffusion: <date>]
  ```

- Table of contents, cover and approval blocks, and front matter get their own `chunk_content_type` (`toc`,
  `front_matter`). A body found in at least 20 documents (`BOILERPLATE_MIN_DOCS`) is marked `boilerplate`.
- Duplicate passages of one IDDOC (same text hash) are removed. The text of `<!-- image -->` and
  `<!-- formula-not-decoded -->` placeholders is removed before chunking.
- `chunk_id` is `<IDDOC>-<NNNNNN>` (chunk index plus one, six digits). The archive notice is `<IDDOC>-000000`.
- The language of a document comes from its REF suffix (`_FR`, `_GB`, ...); without a suffix, from stop words.

## Image descriptions (`4_describe_images`)

- A rebuild (`3c_chunks`) keeps the description of an `(IDDOC, image_id)` only when it still describes the image now
  at that id: the document was not parsed again after the description was written, or the image has the same page and
  size. The ids are positions in the parse output, and a re-parse that changes the number or order of the images
  moves them (965 of 2,314 documents re-parsed on UAT on 2026-10-16 changed their image count); any other image goes
  back to `PENDING` and is described again.

- The images with status `PENDING` or `ERROR` are described, at most `PARSING_LLM_BATCH_SIZE` per run. The
  others wait for the next run.
- Each image goes through the endpoint chain: the primary endpoint, then the fallbacks. Each endpoint is tried
  in `PARSING_LLM_ROUNDS` (2) passes, with waits of 5, 15 and 30 seconds between passes. Up to 10 calls run at
  once, under a rate limiter. The limit is the smallest of the input-token, output-token and request budgets,
  divided by the average tokens per image.
- An endpoint that fails `PARSING_LLM_BREAKER_FAILURES` (5) images in a row is skipped for the rest of the run.
  A rate-limit error (429) counts neither as a failure of the endpoint nor as an attempt on the image.
- An image no endpoint described stays `PENDING` with `attempts` and `last_error_kind` set. After
  `PARSING_IMAGE_MAX_ATTEMPTS` attempts it becomes `FAILED_LIMIT` and is not retried.
- Results are merged into `image_metadata` in chunks of `PARSING_LLM_CHECKPOINT_CHUNK_SIZE` (2 000): an interrupted
  run keeps the chunks already merged.
- Statuses: `PENDING`, `DONE` (described), `SKIPPED` (the model answered `SKIP`), `SKIPPED_DECORATIVE`
  (size filter), `EXTRACTION_FAILED` (no file on the volume), `FAILED_LIMIT`.
- The prompt `IMAGE_DESCRIPTION_PROMPT` asks for a category (TABLE, FLOWCHART, DIAGRAM, DATA_CHART, SCHEMATIC,
  PHOTO_TECH, TEXT_DOC), then the extraction, or `SKIP:` for an image that carries no information or repeats the
  text around it.
- An image passage is written only for a `DONE` image whose description has at least 40 characters and is not a
  `SKIP` answer. Its body holds the section and the caption; its prefix adds `Image: page <N>, <label>`.
- The rows are merged on `(IDDOC, image_id)` in `image_metadata` and on `chunk_id` in the passage tables. Only the
  rows that change are rewritten.
- A document waiting for its images (`IMAGES_PENDING`, `EMPTY_TEXT`) is promoted once none of its images is left
  to describe: to `SUCCESS` with the text assembled from the descriptions, or to `SKIPPED_EMPTY_IMAGES`.
- The serving token is read from the secret `qualibot/serving_token`; without the secret, the notebook's own token.
- The widget `rebuild_image_chunks` rebuilds the image passages from the stored descriptions, without model calls.

## Vector Search indexes (`5_sync_index`)

- The indexes come from the widget `indexes`, or from `PARSING_VECTOR_SEARCH_INDEXES` when it is empty. An empty
  list syncs nothing.
- Only `chunks_index` and `chunks_full_index` are created by this task, on the endpoint named by the widget
  `vector_search_endpoint` (default `qualibot`), with the embedding model of the widget `embedding_model`, on
  `chunk_text`, primary key `chunk_id`, pipeline type `TRIGGERED`. Any other index in the list is synced only.
- The task triggers each sync, then waits for it, up to `wait_minutes` (45). A failed or offline index raises an error.
  A sync still running at the end of the wait is reported, not failed.
- `chunks_full` holds `chunks` and `chunks_archive` (every document whatever its date, for the impact search). It
  is refreshed by a MERGE on `chunk_id` when the widget `build_chunks_full` is `true` or when `chunks_full_index`
  is in the list. The MERGE deletes the rows that left the source. Archive notices are not copied.
- The chunk tables keep 60 days of Change Data Feed history (`delta.deletedFileRetentionDuration` and
  `delta.logRetentionDuration`), so that a triggered sync can resume.

## Document catalog (`6_update_kb_metadata`)

- Writes to the Lakebase databases named by the widget `lakebase_database` (comma-separated). An empty value skips
  the task. The task runs serverless, because the Lakebase endpoint is a private endpoint that classic compute
  cannot reach.
- `doc_catalog` is rebuilt in one transaction from `parse_manifest`: REF, title, Intraqual link (built from the
  REF), division, `in_chat` (the REF has passages in `chunks`), current revision and its date. The write is refused
  when the new catalog has fewer than half the rows of the current one.
- The `revision` and `doc_date` columns are added when the notebook's identity owns the table; otherwise the catalog
  is written without them and a warning names the owner.
- `catalog_metadata` (single row, `id = 1`) receives `documents_as_of` = today and `updated_at`. The chat shows
  `documents_as_of` in its toolbar.
- The application reloads `doc_catalog` every 30 minutes (`server/services/infra/doc_catalog.py`).

## Tables, volumes and checks

Tables (schema `<env>_proj.qualibot`, the same names in every environment):

| Table | Content |
|---|---|
| `parse_manifest` | In-scope documents of the day, one row per IDDOC |
| `parsed_documents` | One row per IDDOC: status, file retained, parser, error, `is_in_rag`, `is_filtered_by_date` |
| `parsing_checkpoint` | Result of each file attempted; read by every step of `3_parse` |
| `chunks` | Chat passages (text and images), all divisions |
| `chunks_archive` | Passages of documents published before the cutoff |
| `archive_notices` | One notice per archived document |
| `chunks_full` | `chunks` and `chunks_archive`, for the impact search |
| `image_metadata` | One row per image: location, status, description, tokens |
| `category_reference` | Category hierarchy and division of each IDDOC |
| `parsing_run_health`, `document_change_log` | One row per run; one row per new or revised document (monitoring) |
| `rag_exclusions`, `parsing_retry_temp` | Working tables |

Volumes: the Intraqual documents (`PARSING_VOLUME_ROOT_PATH`, read, one folder per IDDOC named `D_<IDDOC>` or
`Dm_<IDDOC>`; `2_manifest` may delete out-of-scope folders), the images (`PARSING_VOLUME_BASE_PATH`) and the
offline models (`PARSING_OFFLINE_MODELS`).

Quality checks (`tables.py`, Great Expectations through `leap_utils`): a RED failure stops the task before anything is written. An AMBER failure is logged. `6_update_kb_metadata` runs on serverless, where GX is not available: its checks run in Python (`check_rows`). The CPU clusters get GX from their S policy; the GPU cluster of `3_parse` has no policy and declares `great_expectations==0.18.22` as a task library. Every notebook calls `quality.require_gx()` in its import cell: on 2026-10-16 a UAT `3_parse` without GX failed on `ModuleNotFoundError: great_expectations` at its quality checks, after 20 h of parsing.

| Table | Checks |
|---|---|
| `category_reference` | Not empty; `IDDOC` not null and unique (RED); `division` on 99 % of the rows (AMBER) |
| `parse_manifest` | Not empty; `IDDOC` not null and unique (RED); `ref` and `titre` on 99 % (AMBER) |
| `parsed_documents` | `IDDOC` not null and unique; `parse_status` not null (RED); `ref` and `division` on 99 % (AMBER) |
| `chunks` and `chunks_archive` | `chunk_id` not null and unique across both; `IDDOC`, `REF`, `chunk_text` not null (RED); `division` and `titre` on 99 % (AMBER) |
| image passages | `chunk_id` not null and unique; `IDDOC`, `REF`, `chunk_text` not null (RED); `division` on 99 % (AMBER) |
| `image_metadata` | `(IDDOC, image_id)` not null and unique (RED); `volume_path` on 95 % (AMBER) |
| `doc_catalog` | Not empty; `ref` not null and unique (RED); `title` and `division` on 99 % (AMBER) |
| `chunks_full` | Not empty; `chunk_id` not null and unique; `chunk_text` not null (RED) |

`tables.check_ref_mapping` compares the REF of each passage with `parse_manifest` and logs the mismatches (AMBER).

## Links

- [`resources/parsing_pipeline.job.yml`](../../resources/parsing_pipeline.job.yml): the job, its clusters and its parameters.
- [`utils/README.md`](../README.md): the other scripts of `utils/`.

## Behaviours worth knowing

- **Timeouts.** The job stops after 48 hours. `1_categories` and `6_update_kb_metadata` stop after 1 hour, `2_manifest` after 2,
  `5_sync_index` after 3 and `4_describe_images` after 12. `3_parse` stops after 24 hours, `3b_parse_pdf` after 12 and
  `3c_chunks` after 6. The job has one run slot, so a task that stops late holds the next run back: the limits are set
  above the longest full parse seen, to be checked against the next full run.
- **Pruning.** When a delete on a derived table fails, the rows of `parsed_documents` that point to it stay, so the next
  run finds the same IDDOCs and prunes them again; the failure is logged as an error.
- **Index wait.** `5_sync_index` treats `ONLINE_TRIGGERED_UPDATE` and `ONLINE_UPDATING_PIPELINE_RESOURCES` as a sync in progress
  and waits for `ONLINE_NO_PENDING_UPDATE`.
- **Change log.** A repaired run keeps its job run id; `document_change_log` does not repeat the changes it already holds
  for that run.
- **Checkpoint snapshots.** A full run drops the `_pipeline_checkpoint_<timestamp>` snapshots older than 30 days, keeping the
  most recent one.
- **Image descriptions.** The media type sent to the vision model (PNG, JPEG, GIF or WebP) is read from the file.
