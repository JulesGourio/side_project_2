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

**Route**: `POST /compare/impact`. **Button**: "Judge Impacted Docs (Index + LLM)".

How it works: the changes text is split into focused queries (one per
change/section), each queried against the Vector Search index
(`COMPARE_IMPACT_INDEX`) and deduped into a candidate document list. A
single LLM call (`COMPARE_IMPACT_ENDPOINT`, currently
`databricks-claude-sonnet-4-6`) then judges each candidate `impacted:
true/false` from all of its retrieved passages together and — when
impacted — lists every section that conflicts, alongside a confidence +
reason that opens with a general verdict before any location detail.
Output is JSON: ref, division, url, `max_score`, chunk count, `impacted`,
`sections` (array), `confidence`, `reason` — sorted impacted-first.
Cost/latency: one Vector Search query round-trip + one short LLM call —
typically a few cents and a few seconds.

**Chunk count is deduped, but no longer shown in the card** (2026-07-20,
UI display dropped 2026-07-28): multi-query retrieval runs one Vector
Search query per derived change, so the same chunk_id routinely comes back
under several different queries — `chunk_count` used to increment per hit
instead of per distinct chunk, so a 27-chunk document could show "83 chunks
matched". `_aggregate_docs` still dedupes on `chunk_id` internally (used
for ranking via `_evidence_rank`, and still in the JSON export), but the
"N chunk(s) matched" footer wording was removed from
`ImpactedDocsCard` — end users found "chunk" too implementation-specific to
be useful, unlike "matched by N/M changes" which stayed since it explains
retrieval corroboration in plain terms. Separately, the raw HYBRID
`max_score` ("N% match") is no longer shown in the card at all — it's a
retrieval signal, not a relevance one (a document can score 90%+ on shared
vocabulary alone while being about an unrelated topic — the same caveat
that applied to the old V2 method), and the LLM's
`impacted`/`confidence`/`reason` is the signal that actually matters now.
`max_score` is still computed and used for internal ranking, and still
returned in the JSON export for anyone who wants it.

**How section names are found, and why there can be several** (reworked
2026-07-28): parsed documents embed structural markers directly in the
chunk text itself, e.g.
`[6. DESCRIPTION DETAILLEE > 6.1 GENERER DES IDEES A VALEUR AJOUTEE]` — the
judging LLM is told to read section numbers/titles from *those inline
markers*. Each candidate is judged from up to `_EXCERPTS_PER_CANDIDATE` (5)
retrieved passages together, not just its single highest-scoring one — the
prompt explicitly says a lower-ranked passage can hold the actual
conflicting detail — so `sections` is a JSON array: one entry per distinct
section the conflict actually spans, instead of collapsing everything into
whichever passage scored highest. The `semantic_headers` chunk metadata is
passed along too, but only as a secondary heading hint — it's frequently
missing (verified against the live index 2026-07-20: `semantic_headers =
'{}'` for ~48% of `text`-type chunks and ~22% of `table`-type chunks) and,
when present, isn't as reliable as the inline markers for pinpointing the
exact sub-section. `sections` comes back as an empty array only when the
candidate isn't impacted, or truly no section information (neither inline
marker nor heading hint) exists anywhere relevant.

The prompt also no longer uses the word "excerpt" anywhere — internally the
retrieved chunk text shown to the judge is now called a "passage", and the
"reason" is required to open with a one-sentence general verdict on the
whole document before any passage-specific detail, then mention briefly if
the conflict recurs elsewhere, instead of reading as if only the top-scoring
passage mattered.

~~Known bug (fixed 2026-07-20)~~: the prompt originally showed the judging
LLM a placeholder like `(section: "unlabelled")` for passages with no
heading hint, then told it to "copy the exact label shown" — so it
dutifully echoed the placeholder back as a fake section name on every
passage lacking one. Fixed by phrasing the placeholder as an instruction
rather than a quotable label (`[no heading]`), telling the LLM explicitly
never to echo it, and stripping it server-side as a last resort if it
still does.

**Caching**: like the main analysis, the result is cached server-side
(`impact_cache` Lakebase table) keyed on `(old_file_hash, new_file_hash,
app_version)` — clicking "Judge Impacted Docs" again for the same file pair
replays the cached JSON instantly (`cached: true` in the response, "Result
retrieved from cache" toast) instead of re-querying the index and paying for
another LLM call. A "Re-run" button appears next to the primary button once
a result exists; it sends `force_refresh: true`, which skips the cache lookup
and always does a fresh judgment (useful if the knowledge base was updated
since the last run). The cache is skipped entirely when either file hash is
missing (e.g. an impact search replayed from a history entry that predates
hash tracking).

**History (2026-07-20)**: this used to be three independent methods built to
compare against each other — V1 routed the full changes text through the
existing Knowledge Assistant agent (its own retrieval + reasoning); V2 queried
the index directly with no LLM, showing raw similarity scores only. Both were
dropped in favour of the method above ("V3" at the time):
- **V1 (agent)** was the slowest and, in testing, the least reliable — it
  tended to be conservative, often asking a clarifying question instead of
  listing documents, or reporting "no evidence of this exact change" even when
  a related document existed. Its per-call cost was also opaque (the agent's
  internal token usage isn't exposed in its streamed response), unlike the
  single bounded LLM call used now.
- **V2 (index only)** was free and instant, but a raw similarity score is not
  the same as "actually impacted" — a doc can score 90%+ purely on shared
  vocabulary (e.g. "torque", "autoclave") while describing an unrelated
  program/process. It retrieved the same candidates the LLM call already
  retrieves internally, so once the LLM judgment is in place it added no
  information a user could act on — only an unjudged version of the same list.
- The remaining method had the best signal-to-noise of the three in testing:
  it correctly demoted high-scoring-but-irrelevant candidates to
  `impacted: false` with a specific reason, and correctly flagged the
  genuinely conflicting document as `impacted: true` — at roughly
  €0.01–0.02 per call (see the token/cost line under the result card).

**Which text is used as input** (`changesTextForImpact` in `CompareView.tsx`):
`analysis || analysisStd` — it prefers the Change Table (JSON) text, and
only falls back to the Change Summary (Markdown) text if Change Table
wasn't generated for this file pair. This is a plain fallback, not a merge —
if you want the Change Summary text used instead, don't generate a Change
Table for that pair. The impact button only appears once at least one of
the two main analyses has completed, and a small "Using: Change Table /
Change Summary" label above it makes explicit which one will actually be
sent.

**Truncation**: the input is truncated to `COMPARE_IMPACT_MAX_QUERY_CHARS`
(20,000 characters, keeping the *first* 20,000) before being split into
per-change queries and sent to the Vector Search index — if that cut
anything, the response sets `truncated: true` and the card shows a ⚠️ banner
("Query was truncated…"). Each passage shown to the judging LLM is separately
capped to 800 characters (`_CHUNK_TEXT_EXCERPT_CHARS` in `vector_search.py`).

These are cost/latency safety nets, not hard requirements — verified
empirically against the live index on 2026-07-10:
- `query_text` errors past **~29,000 characters** ("Search has too many
  filter clauses or long query text") — 20,000 leaves comfortable headroom
  while cutting truncation frequency on large diffs versus the original
  6,000.
- `num_results` is capped at **200** for `query_type: HYBRID` (requests above
  200 error with `"exceeds the maximum allowed for hybrid search 200"`) —
  `COMPARE_IMPACT_NUM_RESULTS` is currently 30, well under that ceiling.

Adjust `COMPARE_IMPACT_MAX_QUERY_CHARS` / `_NUM_RESULTS` / `_MAX_CANDIDATES`
in `app.yaml` within those hard limits as needed.

**Boilerplate collapse** (2026-07-17): the text diff collapses per-page
repeats of the same change (revision stamps, classification fields — same
text on ≥`COMPARE_BOILERPLATE_MIN_PAGES` distinct pages, default 3) and
dot-leader table-of-contents rows into single annotated entries. Measured on
`utils/compare_eval` (recall unchanged): 15-53% fewer diff entries and up to
51% smaller diff text — fewer LLM tokens per analysis, and the first 20,000
chars sent to retrieval carry more real signal.

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
| `COMPARE_IMPACT_INDEX` | Vector Search index queried for impact search |
| `COMPARE_IMPACT_ENDPOINT` | LLM endpoint for the impact judgment call |
| `COMPARE_IMPACT_NUM_RESULTS` / `_MAX_CANDIDATES` / `_MAX_QUERY_CHARS` / `_MAX_TOKENS` | Safety knobs, see truncation section above |
| `COMPARE_IMPACT_MAX_QUERIES` / `_PER_QUERY_RESULTS` | Multi-query retrieval — one focused Vector Search query per derived change |
| `COMPARE_SUMMARY_ENDPOINT` | LLM endpoint for the per-document text summary (default `databricks-gpt-5-6-luna`) |
| `COMPARE_SUMMARY_IMAGE_ENDPOINT` | Vision LLM endpoint for image files (default `databricks-gpt-5-6-luna`) |
| `COMPARE_SUMMARY_MAX_CHARS` / `_MAX_TOKENS` | Safety knobs, see truncation note above |
| `COMPARE_MAX_CONCURRENT` | Semaphore bounding concurrent `/compare/analyze` requests |
| `MAX_COMPARE_PDF_MB` | Declared here, but **not read anywhere in the code** — see Technical reference below |
| `MAX_COMPARE_FILE_MB` | The var actually read for the upload size cap (`compare.py`, default 20MB) — not declared in `app.yaml` at all; only same-by-coincidence defaults keep this harmless today |
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
| `POST /api/compare/impact` | `compare.py::find_impacted_documents` | `Depends(require_compare)` | JSON `ImpactRequest{changes_text, old_file_hash='', new_file_hash='', force_refresh:bool=False}` | Plain JSON, not multipart — the only compare route where `force_refresh` is a typed bool rather than a string form field. Retries once with the caller's own `x-forwarded-access-token` if the service-principal token gets a 403 from the index. |
| `POST /api/compare/summarize` | `compare.py::summarize_document` | `Depends(require_compare)` | multipart: `file`, `file_hash`, `force_refresh` | Branches image vs. text purely on extension (`EXTENSION_MAP`); "no extractable text" is a normal 200 response (`no_content:true`), not an error. |
| `POST /api/compare/save` | `compare.py::save_to_volume` | `Depends(require_compare)` | multipart: `old_file`, `new_file`, `analysis_text`, `impact_text` | Creates the timestamped UC Volume session folder (`{volume_path}/{YYYY-MM-DD_HHMMSS}`). |
| `GET /api/compare/load` | `compare.py::load_session_files` | `Depends(require_compare)` | query: `session_path`, `old_filename`, `new_filename` | Rejects any `session_path` that doesn't start with the configured `COMPARE_VOLUME_PATH` (or `/Volumes/` as a fallback prefix check) — a directory-traversal guard, returns `403` on mismatch. |
| `POST /api/history` | `history.py::save_comparison` | `Depends(require_compare)` | `SaveComparisonRequest` (18 fields: filenames, hashes, texts, token/cost telemetry, `volume_session_path`) | Writes the row `/compare/analyze` itself never persists — the client calls this explicitly right after a fresh (non-cached) analysis finishes. |
| `GET /api/history` | `history.py::list_comparisons` | `Depends(require_compare)` | query: `limit=20`, `offset=0` | No DB pool → **200** `{comparisons:[], available:false}`, not an error — same in-band-unavailability pattern used everywhere in this app. |
| `GET /api/history/{id}` | `history.py::get_comparison` | `Depends(require_compare)` | | 404 if missing or not owned by the caller. |
| `POST /api/compare/export-excel` | `exports.py::export_excel` | **none** — see gap below | multipart: `json_text`, `filename`, `file_type`, `image_pairs_json` | |
| `POST /api/compare/export-pdf` | `exports.py::export_pdf` | **none** | multipart: `markdown_text`, `filename`, `title` | |
| `POST /api/compare/save-result` / `save-excel` / `save-pdf` | `exports.py` | **none** | | Auto-save-to-volume variants triggered by the client right after analysis; same `COMPARE_VOLUME_PATH` prefix guard as `/compare/load`. |

**Capability-gating gap**: `exports.py` (all five export/auto-save routes),
`preview.py`, and `feedback.py` carry **no** `Depends(require_compare)` at all, unlike
`compare.py` and `history.py`. In practice this means the export/preview surface for
compare-produced content is reachable without `can_compare`, even though the primary
analyze/impact/summarize/history routes are properly gated — worth closing if this
ever needs to be airtight rather than best-effort.

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

| Processor | Extraction | Diffing quirk |
|---|---|---|
| PDF | PyMuPDF, block-sorted `(y,x)`, page-tagged, `TEXT_DEHYPHENATE` | Images deduped by perceptual hash (dhash), 1024px/JPEG-q65 |
| Image | No text extraction — both images sent as 4 content blocks | 1600px/q85 normalization, distinct constants from PDF's embedded-image path |
| DOCX | Walks paragraphs/tables/textboxes/VML shapes; estimates page numbers from break counts | Largest single extraction function in the codebase (~220 lines); VML connector/arrow geometry is extracted as diffable text specifically so the prompt doesn't misclassify it as a "Visual change" |
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

**Query derivation** (`impact_queries.py::changes_to_queries`) is pure regex/heuristic
— no LLM call. It prefers structured JSON diff rows (drops `editorial`/`structural`
type rows unless nothing would remain, sorts by criticality `high`→`medium`→`low`),
falls back to splitting Markdown on `##` headings, and falls back again to the raw
text as one query. A `"no significant changes detected"` short blob returns an empty
query list, which short-circuits the whole retrieval+LLM call (zero cost, `no_changes:true`).

**The Vector Search call itself** (`vector_search.py::_fetch_chunks`) is a plain
`httpx` POST to `/api/2.0/vector-search/indexes/{index}/query` — there's no
Databricks Vector Search SDK client involved. `query_type` is hardcoded `'HYBRID'`.
Fan-out is deliberately narrow: `asyncio.Semaphore(3)`, retried up to twice per query
with linear backoff — a comment in the code explains this directly: the embedding
endpoint behind `query_text` has been observed rejecting concurrent requests with
`"Request id already running"` under load (the same class of race documented in the
Chat README's Vector Search known issue).

**`_aggregate_docs`** is where the "chunk_count can't exceed the real total" fix
actually lives: documents are keyed by `IDDOC|REF`, and within each document, chunks
are deduped by `chunk_id` before incrementing `chunk_count` — this is what makes the
count accurate even though the same chunk legitimately surfaces under several
different per-change queries. Final ranking is `(query_hits, max_score)` — a
document corroborated by more independent derived changes outranks one with a merely
higher single score.

**The judging LLM call** runs at `temperature=0.0` (hardcoded, not configurable —
unlike the main analysis prompt) and receives per-candidate passages each tagged
either `[heading: "..."]` or `[no heading]`; the prompt explicitly warns the model
that a passage's own inline structural markers (`[6. DESCRIPTION > 6.1 ...]`) are
more reliable than the heading hint, and that all of a candidate's passages must be
judged together rather than just the top-scoring one. The now-fixed placeholder-echo
bug is guarded server-side too: any returned `sections` entry that normalizes to
`"no heading"` or `"no section label captured"` is dropped before it reaches the
client, as a last-resort safety net on top of the prompt fix.

**Config drift worth knowing about**: `app.yaml` currently overrides several code
defaults — `COMPARE_IMPACT_MAX_CANDIDATES` is `12` in `app.yaml` vs. a `8` code
default, `COMPARE_IMPACT_MAX_QUERY_CHARS` is `20000` vs. a `6000` code default,
`COMPARE_MAX_CONCURRENT` is `10` vs. a `5` code default, `COMPARE_MAX_TOKENS` is
`16000` vs. an `8192` code default. None of these are bugs — `app.yaml` is what's
actually deployed — but don't trust a code comment that cites the *code* default as
if it were the live value without checking `app.yaml` too.

**`MAX_COMPARE_PDF_MB` vs `MAX_COMPARE_FILE_MB`**: `app.yaml` declares
`MAX_COMPARE_PDF_MB`, but no Python code reads that name anywhere — the actual upload
cap is `MAX_COMPARE_FILE_MB` (`compare.py`, default 20MB), which isn't declared in
`app.yaml` at all. Harmless today only because both default to the same value; if
either default ever needs to change, edit `MAX_COMPARE_FILE_MB`, and consider fixing
the `app.yaml` name to match.

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

`APP_VERSION`'s code-level default is inconsistent across files (`'2'` in
`compare.py`, `'1'` in `history.py`/`app.py`) — currently masked by an explicit
`app.yaml` value, but worth setting explicitly rather than relying on either default
if that env var is ever removed.

### Export mechanics

`_parse_json_response` (`export_helpers.py`) tries, in order: direct `json.loads`,
then a ` ```json ` fenced block, then a bracket-extraction regex — returning `None`
(never raising) if all three fail. Note a real inconsistency between callers:
`export_excel` checks `is None`, while `save_excel_to_session` checks truthiness
(`not rows`) — the latter treats a genuinely valid-but-empty `[]` diff as a failure
too, the former doesn't.

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

**Hashing is 100% client-side**: `computeFileHash` uses the browser's Web Crypto
`crypto.subtle.digest('SHA-256', ...)`, computed independently (not memoized) at each
of the three call sites (Change Table, Change Summary, Summarize). The server never
recomputes or verifies these hashes — they're pure client-trusted cache keys. This is
also why `old_file_hash`/`new_file_hash` in the `messages`/`impact_requests` tables
can legitimately be `NULL`: any request replayed without going through the normal
upload flow (e.g. an old history entry) simply has none to send.

**"Run Both"** (`handleAnalyzeBoth`) is `Promise.allSettled([handleAnalyzeStructured(...), handleAnalyzeStandard(...)])`
— each call manages its own `AbortController` and its own `isAnalyzing*` flag
end-to-end; `allSettled` here is just a defensive wrapper against a stray unhandled
rejection, not a real coordination point between the two tracks.

**History routing** is an exact string match, not a fuzzy fallback:
`processing_method === 'structured'` routes to the Change Table card; **any other
value** — including `'standard'`, a legacy empty string, or something unrecognized —
routes to the Change Summary card by default, with no distinct error path for a
truly-unknown method value.

**No custom hooks exist** in this component — no `usePolling`/`useFileHash`/etc.
Everything "hook-like" (hash computation, localStorage read/write, auto-save,
history persistence) is a plain module-scope function or closure, not a `use*` hook.

**Cancellation is track-specific and partial**: Change Table and Change Summary each
have their own `AbortController` and Cancel button (aborting keeps whatever partial
text had already streamed in, doesn't roll it back). Impact search and Document
summary have no abort wiring at all — once fired, they run to completion.

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
