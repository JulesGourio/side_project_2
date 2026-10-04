# DocCompare + Impact Search

The document-comparison tab: upload two versions of a document, get a diff
report, then optionally search the knowledge base for which other documents
that change might affect.

## How the Compare page works

Uploading two documents produces up to three independent diff results, each
with its own button, cache, and history row — there is no selector anywhere
in this flow; whichever cards you've generated are shown side by side. On
top of that, each document can be summarized on its own (see "Document
summary" below), independent of any diff.

### 1–2. Main analysis: Change Summary vs Change Table

| | Change Summary (`standard`) | Change Table (`structured`) |
|---|---|---|
| Button | "Generate Change Summary" | "Generate Change Table" |
| Output | Narrative Markdown, one bullet per change, grouped by section | Live JSON array parsed into a table (section, type, criticality, before/after, rationale) |
| Rendered by | `ResultCard` | `StructuredResultCard` / `JsonDiffTable` |
| Export | PDF (`POST /compare/export-pdf`) | Excel (`POST /compare/export-excel`) |

Both call the same `POST /compare/analyze`, differing only in the
`processor_version` form field (`standard` vs `structured`) — the backend
processor factory (`server/services/processors/`) picks a different prompt/
output format per file type accordingly. "Run Both" fires them concurrently
(`Promise.allSettled`); each keeps its own `isAnalyzing*` flag, so one
finishing doesn't block or affect the other.

Each is cached server-side keyed on `(old_file_hash, new_file_hash,
app_version, processor_version)` — re-running the same file pair + version
replays the cached DB row instantly unless you click "Re-run" (force
refresh, bypasses the cache). A history entry loaded from the "History"
panel is routed back into whichever track its `processing_method` matches
(`structured` → Change Table card, anything else → Change Summary card);
it does not touch the other track.

**Excel export** (Change Table only): parses the raw JSON diff text
(`_parse_json_response` — tries direct `json.loads`, then a ` ```json `
fenced block, then a bracket-extraction regex, in that order) into rows,
then builds a colour-coded `.xlsx` via `openpyxl` — one row per change,
background colour driven by criticality (high/medium/low) or change type,
with before/after image thumbnails embedded when the comparison detected
visual diffs. Change Summary exports as a paginated PDF instead
(`_build_pdf_bytes_from_markdown`) since it's narrative text, not tabular.
Separately, if `COMPARE_VOLUME_PATH` is configured, both exports are *also*
silently auto-saved into the comparison's UC Volume session folder right
after the analysis finishes (`autoSaveExcel`/`autoSavePdf`) — that's for
audit/session-restore, not the same request as the interactive download
button.

### 3. Impact search: which documents does this change affect?

**Route**: `POST /compare/impact` (NDJSON stream). **Button**: "Judge Impacted Docs".
**UI**: `ImpactResults.tsx`. Redesigned 2026-10-02 — the earlier single-call design
(all changes packed into ≤8 queries, 12 candidates max, 5 passages × 800 chars each,
one LLM call for every candidate, only verdict + section names shown) silently lost
documents and passages; see git history before that date for its full write-up.

How it works:

1. **Numbered change list** (`impact_queries.py::extract_changes`): every Change
   Table row becomes `C1`, `C2`… in table order (Markdown: one per `##` section;
   manual/raw text: a single `C1`). Editorial/structural rows keep their id but are
   not searched (renumbering never impacts another document).
2. **One Vector Search query per change** (HYBRID, `COMPARE_IMPACT_PER_QUERY_RESULTS`
   hits each, up to `COMPARE_IMPACT_MAX_QUERIES`; past that, the lowest-criticality
   changes share the last query — none is dropped).
3. **Per-document grouping** (`vector_search.py::_aggregate_docs`): a chunk hit by
   several changes is kept once with all their ids; language variants of one
   document (`GO-1316_FR` / `_GB`) are merged; the compared document itself (its REF
   found in either uploaded file name) is left out. Ranking: number of changes that
   found the document, then best similarity.
4. **One judge call per candidate, in parallel** (top `COMPARE_IMPACT_MAX_CANDIDATES`,
   6 at a time): the judge sees the whole change list and up to 10 retrieved passages
   of that document (3,000 chars each, provenance prefix stripped, in document
   order). It returns a verdict, a confidence, a reason, and **every** conflicting
   passage with the change ids it conflicts with, its section, a verbatim quote
   (highlighted in the UI by exact match, `_find_quote`) and a one-line explanation.
   A failed call only marks that document "Judgment failed".
5. **Streaming**: `plan` (change list, candidates count, not-judged documents,
   excluded REF) → one `document` event per judged candidate as soon as it finishes
   → `done` (usage, duration). The UI fills in progressively.

Status shown per document: **Impacted** (impacted, high/medium confidence), **To
check** (low confidence, either way), **Not impacted**, **Judgment failed**.
Documents ranked below the judge cap are dropped from the UI and the export (still
in the `plan` event's `not_judged` for debugging): end users want a short list.

**Kept out of the UI on purpose** (end users, 2026-10-02): cost, durations, query
counts, similarity scores and any current/archive filter — every search covers
all documents, archive ones just carry an "Archive YYYY" badge.

**Two views**: *By document* (verdict, reason, then the passages to update with
the quote highlighted and, under each, the change it conflicts with spelled out —
section and old → new value, never a bare "C37") and *By change* (only the changes
that conflict with a document, in full; the others folded into one "N other
changes with no document to update" line, since a heavily revised document can
carry dozens of changes). Filter: verdict.

**Archive (pre-2018) documents**: the parsing pipeline only feeds the RAG tables
with documents published from `DOC_DATE_CUTOFF` (2018-01-01); older ones go to
`chunks_archive`. `chunks_full` (= `chunks` + `chunks_archive`) and its index
`chunks_full_index` are the impact-search source, so `COMPARE_IMPACT_INDEX` must
point at that index for archive documents to be found. Their publication date is
read from the chunk's provenance prefix; before `COMPARE_IMPACT_ARCHIVE_BEFORE` they
get an "Archive YYYY" badge.

**Link with the Change Table**: the table's `#` column shows the same ids (row 3 =
`C3`). When the result was computed from the Change Table on screen
(`source: structured`), clicking a change id fires `FOCUS_CHANGE_EVENT`;
`JsonDiffTable` un-hides Low rows, scrolls to that row and highlights it.

**More than `COMPARE_IMPACT_MAX_QUERIES` changes** (`_group_by_section`): changes of
the same section share one query (split when it would exceed 2,000 chars);
neighbouring sections are merged only if there are still too many groups. This only
changes retrieval — the number of judge calls, hence the cost, is unchanged.

**Feedback** (`impact_feedbacks`, `POST /compare/impact/feedback`): 👍/👎 on each
document's verdict (one click, `ref` + `verdict_shown` stored — the data to measure
the judge with) and a "Was this impact search helpful?" vote with optional comment
on the whole result (`ref` NULL). Rows link to `impact_requests` through the
`impact_request_id` sent in the `done` event (also replayed from the cache).

**Kept with the comparison**: once a search finishes, the result JSON is written to
`messages.impact_text` of the analysis it was run on (`PUT /history/{id}/impact`);
loading that history entry shows it again.

**Manual mode** ("describe a change by hand", no files): deliberately disabled
(`IMPACT_MANUAL_MODE_ENABLED = false` in `CompareView.tsx`, decision 2026-10-05). It
still works behind the flag.

**Export Excel** (`POST /compare/impact/export-excel`, `_build_impact_excel_bytes`):
sheet *Passages* = one row per conflicting passage (the action list), *Documents* =
every judged document, *Changes* = the change list.

**Cost/latency**: up to 15 small calls on `databricks-gpt-5-6-luna`, ~4-8k input tokens
each — a few cents; 15-40 s, but the first results appear after a few seconds.

**Caching**: the final result is cached in `impact_cache` keyed on the two file
hashes plus a fingerprint of the change text, the index, the judge endpoint and the
limits (`_impact_cache_version`, stored in the `app_version` column — the same pair
searched from the Change Summary, from the Change Table or against another index is
three different results, 2026-10-04) and replayed as the same event stream
(`cached: true`). Results cached by the previous design (no `changes` key) are
ignored. Not cached when a judge call failed. "Re-run" sends `force_refresh: true`.

**Which text is used as input** (`changesTextForImpact` in `CompareView.tsx`):
`analysis || analysisStd` — the Change Table (JSON) when it exists, else the Change
Summary (Markdown). A "Using: …" label says which.

**Hard API limits** (verified 2026-07-10): `query_text` errors past ~29,000 chars
(each change query is capped at 2,000), HYBRID `num_results` ≤ 200.

**Boilerplate collapse** (2026-07-17): the text diff collapses per-page
repeats of the same change (revision stamps, classification fields — same
text on ≥`COMPARE_BOILERPLATE_MIN_PAGES` distinct pages, default 3) and
dot-leader table-of-contents rows into single annotated entries. Measured on
`utils/compare_eval` (recall unchanged): 15-53% fewer diff entries and up to
51% smaller diff text — fewer LLM tokens per analysis and fewer noise changes
to search.

### 4. Document summary: quick per-file summary, no diff needed

**Route**: `POST /compare/summarize`. **Buttons**: "Summarize Document A" /
"Summarize Document B" — shown as soon as the respective file is uploaded,
no need to run any diff first.

How it works: for text-bearing files, the uploaded file's text is extracted
the same way each processor already extracts it for diffing
(`extract_document_text` in `server/services/processors/factory.py` —
reuses each processor module's own single-file extraction helper, just
without pairing it against a second document), then a single cheap LLM call
(`COMPARE_SUMMARY_ENDPOINT`, default `databricks-gpt-5-6-luna`) produces a
~150-300 word Markdown summary.

**Images**: routed to a separate vision-capable endpoint
(`COMPARE_SUMMARY_IMAGE_ENDPOINT`, default `databricks-gpt-5-6-luna`) via
`prepare_image_content` (reuses
`ImageProcessor`'s own normalisation — resize/JPEG-recompress oversized or
unsupported formats — just for one image instead of a pair), which
describes the image (~100-200 words) instead of summarizing document text.
Both paths share the same route, cache, and result card — the server picks
the model and prompt based on the file's extension.

**Caching**: keyed on the file's own content hash + `app_version`
(`summary_cache` Lakebase table) — deliberately *not* paired with the other
file's hash like impact/analyze, since a document's summary doesn't depend
on what it's being compared against. This means the same document is only
ever summarized once even if it later shows up as "Document B" in a
different comparison. A "Re-run" button appears once a summary exists,
sending `force_refresh: true` to bypass the cache.

**Display: one card, full width, tab-switched** (2026-07-20) — showing A and
B side by side halved each card's width and still read as cramped even
after widening the page (see below), so only one summary is ever rendered
at a time, always at full width. When both A and B are active a small
"Document A / Document B" tab switcher appears above the card; triggering
either summarize button also switches to that tab. Picked over a stacked
(A above B) layout specifically because it avoids extra scrolling.

**"No extractable text" is a distinct state, not silence**: a document
that yields empty text (`no_content: true`, e.g. a scanned page with no
text layer) used to make the card vanish entirely — `oldActive`/`newActive`
only checked `text || loading || error`, missing this case, so the only
sign anything happened was an easy-to-miss toast. Fixed by tracking
`noContent` explicitly in `DocSummaryState` and rendering it as a normal
card state with an explanatory message.

**Truncation**: extracted text is capped to `COMPARE_SUMMARY_MAX_CHARS`
(300,000 chars, raised 2026-07-20 from an initial 40,000 that was silently
truncating most real multi-page documents) before being sent to the LLM; if
that cut anything the card shows a ⚠️ truncation banner.

**No cost/token/duration display**: unlike the server-side logging (still
full detail in Lakebase), the UI deliberately shows none of that — end
users don't need it and it reads as noise/an error. This applies to the
impact search card too (2026-07-20).

## Logging and token/cost visibility

Every impact-search call — success or failure — is logged to the
`impact_requests` Lakebase table (who ran it, duration, chunk/doc counts,
truncation flag). Errors also still go to the existing `errors` table as
before.

Token/cost tracking (`prompt_tokens`/`completion_tokens` from the LLM
response, cost computed via the same pricing table as the main analysis) is
returned in the API response and logged to `impact_requests` — deliberately
**not** rendered in the UI (removed 2026-07-20): end users don't need it and
it reads as noise. Query Lakebase directly for cost visibility.

The client sends the comparison's `old_file_hash`/`new_file_hash` (already
computed for `/compare/analyze` caching) with the impact call, and the
endpoint logs them into `impact_requests` — joining back to the originating
`messages` row is a plain `JOIN ON (old_file_hash, new_file_hash)`. Rows
logged before this was added (or replayed without hashes) keep NULL hashes.

## Configuration (`app.yaml`)

| Var | Purpose |
|---|---|
| `COMPARE_ENABLED` | Feature flag — tab shows "coming soon" when false |
| `COMPARE_ANALYSIS_ENDPOINT` | LLM endpoint for the main analysis (Change Summary / Change Table) |
| `COMPARE_ANALYSIS_SYSTEM_PROMPT` | System prompt for the main analysis |
| `COMPARE_IMPACT_INDEX` | Vector Search index queried for impact search (target: `chunks_full_index`, archive included) |
| `COMPARE_IMPACT_ENDPOINT` | LLM endpoint for the per-document judge calls |
| `COMPARE_IMPACT_MAX_QUERIES` / `_PER_QUERY_RESULTS` | One Vector Search query per change, hits per query |
| `COMPARE_IMPACT_MAX_CANDIDATES` / `_MAX_TOKENS` / `_MAX_QUERY_CHARS` | Documents judged, tokens per judge call, change-list cap per call |
| `COMPARE_IMPACT_ARCHIVE_BEFORE` | Publication date under which a document gets the "Archive" badge (default `2018-01-01`) |
| `COMPARE_SUMMARY_ENDPOINT` | LLM endpoint for the per-document text summary (default `databricks-gpt-5-6-luna`) |
| `COMPARE_SUMMARY_IMAGE_ENDPOINT` | Vision LLM endpoint for image files (default `databricks-gpt-5-6-luna`) |
| `COMPARE_SUMMARY_MAX_CHARS` / `_MAX_TOKENS` | Safety knobs, see truncation note above |
| `COMPARE_MAX_CONCURRENT` | Semaphore bounding concurrent `/compare/analyze` requests |
| `MAX_COMPARE_FILE_MB` / `MAX_COMPARE_PDF_MB` | Upload size cap (`compare.py`, default 20MB). `app.yaml` declares the second, historical name; the first wins when both are set |
| `COMPARE_PDF_MERGE_PAGE_SPLITS` / `COMPARE_PDF_TABLE_LABELS` | `false` restores the pre-2026-10-04 PDF extraction (paragraphs cut by a page break left as two blocks; table rows without column labels) |
| `COMPARE_WORD_LEVEL_CHECK` | `false` restores the pre-2026-10-04 diff filter (drops one-word changes in long paragraphs) — for A/B runs on `compare_eval` only |
| `COMPARE_ANALYSIS_TIMEOUT_S` / `_CONNECT_TIMEOUT_S` / `_RETRIES` | HTTP timeout/retry knobs for the LLM call |
| `COMPARE_MAX_TOKENS` / `_THINKING_BUDGET` / `_TEMPERATURE` | LLM call parameters |
| `COMPARE_METHOD_{PDF,IMAGE,DOCX,PPTX,EXCEL}` | Default processor version per file type |
| `APP_VERSION` | Cache-busting version — bump to invalidate all cached analyses |
| `COMPARE_VOLUME_PATH` | UC Volume path for save/restore + export auto-save; absence gracefully disables it |

## Technical reference

Deep-dive material for anyone modifying this feature — exact routes, schemas, and
internals, file:line accurate as of 2026-07-22. The sections above explain the *why*;
this is the precise *what*.

### Route inventory

| Route | Router:function | Auth | Request shape | Notes |
|---|---|---|---|---|
| `POST /api/compare/analyze` | `compare.py::analyze_documents` | `Depends(require_compare)` | multipart: `old_file`, `new_file`, `old_file_hash`/`new_file_hash` (client-computed, trusted verbatim), `force_refresh`, `processor_version` | Always returns `200 StreamingResponse` — even config/credential errors ride inside the SSE body as an `error` event, since a browser can't cleanly read a non-2xx streaming body. Bounded by `_analyze_semaphore = asyncio.Semaphore(COMPARE_MAX_CONCURRENT)`. |
| `POST /api/compare/impact` | `compare.py::find_impacted_documents` | `Depends(require_compare)` | JSON `ImpactRequest{changes_text, old_file_hash='', new_file_hash='', old_file_name='', new_file_name='', force_refresh:bool=False}` | NDJSON stream (`plan` → `document`… → `done`, or `error`). Config errors are a plain 400 JSON before the stream starts. Retries with the caller's `x-forwarded-access-token` if the service-principal token gets a 403 from the index. |
| `POST /api/compare/impact/export-excel` | `exports.py::export_impact_excel` | **none** | multipart: `result_json`, `filename` | |
| `POST /api/compare/summarize` | `compare.py::summarize_document` | `Depends(require_compare)` | multipart: `file`, `file_hash`, `force_refresh` | Branches image vs. text purely on extension (`EXTENSION_MAP`); "no extractable text" is a normal 200 response (`no_content:true`), not an error. |
| `POST /api/compare/save` | `compare.py::save_to_volume` | `Depends(require_compare)` | multipart: `old_file`, `new_file`, `analysis_text`, `impact_text` | Creates the timestamped UC Volume session folder (`{volume_path}/{YYYY-MM-DD_HHMMSS}`). |
| `GET /api/compare/load` | `compare.py::load_session_files` | `Depends(require_compare)` | query: `session_path`, `old_filename`, `new_filename` | Rejects any `session_path` that doesn't start with the configured `COMPARE_VOLUME_PATH` (or `/Volumes/` as a fallback prefix check) — a directory-traversal guard, returns `403` on mismatch. |
| `POST /api/history` | `history.py::save_comparison` | `Depends(require_compare)` | `SaveComparisonRequest` (18 fields: filenames, hashes, texts, token/cost telemetry, `volume_session_path`) | Writes the row `/compare/analyze` itself never persists — the client calls this explicitly right after a fresh (non-cached) analysis finishes. |
| `GET /api/history` | `history.py::list_comparisons` | `Depends(require_compare)` | query: `limit=20`, `offset=0` | No DB pool → **200** `{comparisons:[], available:false}`, not an error — same in-band-unavailability pattern used everywhere in this app. |
| `GET /api/history/{id}` | `history.py::get_comparison` | `Depends(require_compare)` | | 404 if missing or not owned by the caller. |
| `POST /api/compare/export-excel` | `exports.py::export_excel` | **none** — see gap below | multipart: `json_text`, `filename`, `file_type`, `image_pairs_json` | |
| `POST /api/compare/export-pdf` | `exports.py::export_pdf` | **none** | multipart: `markdown_text`, `filename`, `title` | |
| `POST /api/compare/save-result` / `save-excel` / `save-pdf` | `exports.py` | **none** | | Auto-save-to-volume variants triggered by the client right after analysis; same `COMPARE_VOLUME_PATH` prefix guard as `/compare/load`. |

**Capability gating**: `exports.py`, `preview.py` and `feedback.py` carry no
`Depends(require_compare)` — left open on purpose (decided 2026-10-04).
Every `session_path` is checked with `_is_within_volume` (normalised, `..` refused).

### Processor factory (`server/services/processors/`)

`get_processor(filename, method_override=None)` (`factory.py`) maps a file extension
to a `file_type` (`pdf`/`image`/`docx`/`pptx`/`excel`/`xml`) via a flat
`EXTENSION_MAP`, then dispatches to a concrete processor class through a plain
if/elif chain (not a registry). `method_override` — the request's `processor_version`
— wins over the per-type `COMPARE_METHOD_*` env default when non-empty.

The important subtlety: **`'standard'` and `'structured'` call the exact same diff
engine** (`paragraph_semantic_diff`) for PDF/DOCX/PPTX/XML — they differ only in
which system prompt gets attached (free-form Markdown vs. a strict JSON-array
schema). Only `'comparative'` actually swaps the diff engine itself
(`section_canonical_diff`). `ImageProcessor` and `ExcelProcessor` are the exception —
for these two, `processor_version` only ever changes the prompt; there's no
alternate diff routine to switch to.

Tables (PDF and DOCX): a first row that looks like a header (`looks_like_table_header`:
3+ columns, all filled, short, mostly without digits) labels the rows below it;
otherwise rows are positional with empty cells kept (`M8 |  | 22 Nm`).

| Processor | Extraction | Diffing quirk |
|---|---|---|
| PDF | PyMuPDF, block-sorted by `y`, page-tagged, `TEXT_DEHYPHENATE`, ligatures expanded; a paragraph cut by a page break is re-joined; ruled-table rows are labelled with their column header (`Activity: … \| Inspector: X`) when the first row looks like one | Images deduped by perceptual hash (dhash), 1024px/JPEG-q65 |
| Image | No text extraction — both images sent as 4 content blocks | 1600px/q85 normalization, distinct constants from PDF's embedded-image path |
| DOCX | Reads every `w:t` of a paragraph (tracked insertions, content controls, fields — not tracked deletions), block-level `w:sdt`, nested tables, footnotes; skips TOC-styled lines; table cells labelled by their true column. Walks paragraphs/tables/textboxes/VML shapes; estimates page numbers from break counts | Largest single extraction function in the codebase (~220 lines); VML connector/arrow geometry is extracted as diffable text specifically so the prompt doesn't misclassify it as a "Visual change" |
| PPTX | Recursive shape walk, `page_label='Slide'` | 1024px/q80 |
| Excel | Key-based row matching first (rejects auto-increment-looking key columns), falls back to `difflib.SequenceMatcher` unified diff if rows aren't table-shaped | Never produces `image_pairs` |
| XML | Depth-first element walk, `page_label='Item'` | No images at all — no "Visual changes" section in the prompt |

`extract_document_text` (used by `/compare/summarize`) and `prepare_image_content`
genuinely **reuse** each processor's own single-file extraction helper (a
function-scoped import of the same private function the pairwise path calls) rather
than reimplementing extraction — confirmed by direct comparison, not just by
docstring claim.

A dormant module, `_prompt_variants.py`, carries an explicit disclaimer that
production code does not import it — it's only exercised by a debug notebook and a
prompt-comparison test script. Don't assume its prompts are live.

### Impact search internals (`server/services/vector_search.py`, `impact_queries.py`)

Flow and rationale are in section 3 above. Implementation notes:

- The Vector Search call is a plain `httpx` POST to
  `/api/2.0/vector-search/indexes/{index}/query` (no SDK), `query_type: HYBRID`.
  Fan-out is `asyncio.Semaphore(3)` with 2 retries: the embedding endpoint behind
  `query_text` rejects concurrent requests with `"Request id already running"` under
  load. Judge calls use their own `Semaphore(6)`, 1 retry, `temperature=0.0`.
- Only the 7 columns of `_COLUMNS` are requested, so the old and new indexes are
  interchangeable; title and publication date come from the chunk's
  `[Source: … | Title: … | Date de diffusion: …]` prefix (`_split_prefix`).
- A judged passage number outside the candidate's list is dropped; a `section` equal
  to the `[no heading]` placeholder is blanked.
- `run_impact_search` raises before `plan` on retrieval failure (the route turns it
  into an `error` event and an `impact_requests` row with `http_status=502`).

**Config drift worth knowing about**: `app.yaml` currently overrides several code
defaults — `COMPARE_IMPACT_MAX_CANDIDATES` is `12` in `app.yaml` vs. a `8` code
default, `COMPARE_IMPACT_MAX_QUERY_CHARS` is `20000` vs. a `6000` code default,
`COMPARE_MAX_CONCURRENT` is `10` vs. a `5` code default, `COMPARE_MAX_TOKENS` is
`16000` vs. an `8192` code default. None of these are bugs — `app.yaml` is what's
actually deployed — but don't trust a code comment that cites the *code* default as
if it were the live value without checking `app.yaml` too.

### Caching — exact Lakebase schemas

```sql
-- Main analysis cache: keyed on (old_file_hash, new_file_hash, app_version, processor_version)
-- No UNIQUE constraint or index on the hash columns — identity is enforced purely by
-- WHERE + ORDER BY created_at DESC LIMIT 1 (most-recent-wins; rows accumulate unbounded).
-- Row insertion is client-triggered (POST /api/history), not automatic inside /compare/analyze.
messages (id, created_at, user_id, workspace_id, workspace_url,
          old_filename, new_filename, old_file_hash, new_file_hash,
          analysis_text, impact_text, volume_session_path, file_type,
          processing_method, processor_version, ttft_s, generation_s,
          input_tokens, output_tokens, thinking_tokens, total_tokens,
          cost_eur, app_version, llm_request_id)

-- Impact cache: keyed on (old_file_hash, new_file_hash, app_version) — no processor_version dimension
impact_cache (id, created_at, old_file_hash TEXT NOT NULL, new_file_hash TEXT NOT NULL,
              app_version TEXT NOT NULL, result_json TEXT NOT NULL)
CREATE INDEX idx_impact_cache_hashes ON impact_cache (old_file_hash, new_file_hash, app_version)

-- Summary cache: keyed on (file_hash, app_version) only — summaries aren't pairwise
summary_cache (id, created_at, file_hash TEXT NOT NULL, app_version TEXT NOT NULL,
               result_json TEXT NOT NULL)
CREATE INDEX idx_summary_cache_hash ON summary_cache (file_hash, app_version)
```

All three caches insert a new row per computation rather than upserting — reads
always take the most recent matching row. All cache reads/writes are wrapped in a
bare `try/except` gated on `if not pool`, so a Lakebase outage degrades to "always
compute fresh, no cache hit," never a user-visible error.

`APP_VERSION` is defined once, in `compare.py` (`history.py` imports it).

### Export mechanics

`_parse_json_response` (`export_helpers.py`) tries, in order: direct `json.loads`,
then a ` ```json ` fenced block, then a bracket-extraction regex — returning `None`
(never raising) if all three fail.

The PDF export (`_build_pdf_bytes_from_markdown`) is pure-Python `fpdf2` — no
system-level PDF library dependency. It hand-parses Markdown line by line (no
markdown library): table rows, fenced code, headings, bullet/numbered lists, and
bold-only callout lines each get their own layout branch, with pre-emptive page
breaks so a table or callout is never split across a page boundary. Falls back to
Latin-1 transliteration (a large character-substitution table for em-dashes, curly
quotes, etc.) when no Unicode-capable font is found on the host.

The Excel export embeds before/after image thumbnails as a **composited** side-by-side
JPEG (old + new pasted into one canvas with a 1px divider) rather than two separate
cell images — cheaper to embed and easier to scan per row. Row background color
priority is criticality first, change-type second (`_CRIT_COLORS` wins over
`_TYPE_COLORS` when both are present).

**autoSaveExcel/autoSavePdf are entirely client-triggered**, not a server-side hook —
`CompareView.tsx` calls the save-to-volume endpoints sequentially, after the SSE
stream finishes, only for a fresh (non-cached) result, and treats every failure as
best-effort (`toast.warning`, never blocks the UI).

### Frontend (`CompareView.tsx`) — state model

Three independent result tracks plus a fourth cross-cutting one, each with its own
loading/error/abort state: **Change Table** (structured), **Change Summary**
(standard), **Impact search**, and **Document summary** (`old`/`new`, each an
independent `DocSummaryState{text, error, loading, noContent, meta}` — `noContent` is
tracked as an explicit first-class flag specifically so a scanned-page-with-no-text
document doesn't silently vanish from the UI).

**Hashing**: the client computes SHA-256 (`computeFileHash`, Web Crypto) for its own
state, the history row and the impact call; `/compare/analyze` and `/compare/summarize`
recompute the same hash from the received bytes for their cache lookup and ignore the
form field (2026-10-04). `POST /api/history` still stores the client's hashes — see
`docs/compare_audit_2026-10.md` B1. A report that streamed with a warning, or whose
stream ended without `[DONE]`, is saved without hashes or not at all, so it is never
served from cache.

**"Run Both"** (`handleAnalyzeBoth`) is `Promise.allSettled([handleAnalyzeStructured(...), handleAnalyzeStandard(...)])`
— each call manages its own `AbortController` and its own `isAnalyzing*` flag
end-to-end; `allSettled` here is just a defensive wrapper against a stray unhandled
rejection, not a real coordination point between the two tracks.

**History routing** is an exact string match, not a fuzzy fallback:
loading an entry clears the impact results and, unless it is the same file pair, the
other track. `processing_method === 'structured'` routes to the Change Table card; **any other
value** — including `'standard'`, a legacy empty string, or something unrecognized —
routes to the Change Summary card by default, with no distinct error path for a
truly-unknown method value.

**No custom hooks exist** in this component — no `usePolling`/`useFileHash`/etc.
Everything "hook-like" (hash computation, localStorage read/write, auto-save,
history persistence) is a plain module-scope function or closure, not a `use*` hook.

**Cancellation is track-specific and partial**: Change Table and Change Summary each
have their own `AbortController` and Cancel button (aborting keeps whatever partial
text had already streamed in, doesn't roll it back). Impact search has its own Cancel button and
`AbortController`; the server cancels the remaining judge calls when the client goes
away. Document summary has no abort wiring.

### Logging tables

`impact_requests` logs every `/compare/impact` call (success or failure) — duration,
chunk/doc counts, truncation flag, token/cost, `http_status`. `errors` catches
uncaught exceptions across `/analyze`, `/impact`, `/summarize`, and the export/save
routes, each entry fire-and-forget via `asyncio.create_task` so a logging failure
never affects the response already in flight. `llm_requests` is the per-LLM-call
audit trail for the main analysis specifically — one row inserted right after
`build_messages` succeeds (holding the sanitized, image-stripped prompt), updated
once usage/error data arrives from the SSE stream.

### Tests and the evaluation harness

`tests/test_compare_pipeline.py` covers the diff engine (boilerplate collapse,
MODIFIED/ADDED/REMOVED classification, map-reduce chunking) and
`tests/test_impact_queries.py` covers query derivation + `_aggregate_docs`'s ranking
logic — but **no HTTP-level test exercises `/compare/analyze`, `/impact`,
`/summarize`, `/save`, or `/load` end-to-end** through FastAPI (unlike Chat, which has
a full `TestClient`-based route suite), and there is no `require_compare` 403 test
anywhere (Chat has one; Compare doesn't).

`utils/compare_preview.py` is the actual scoring engine behind the "recall/precision"
claims — it runs the real pipeline against a local file pair and, in `--score` mode,
compares the resulting diff entries against a hand-annotated reference JSON
(`utils/compare_eval/refs/*.json`, real Latécoère documents) to compute recall,
precision (explicitly documented as a lower bound, since references aren't
exhaustive), and a noise-hit count for boilerplate-collapse regressions. It's a
manual CLI tool, not wired into CI — there's no automated regression gate on
detection quality today.

See the root [README.md](../../../../README.md) for deploy/environment/local-testing instructions shared across all three features.
