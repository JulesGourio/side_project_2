# Document comparison

Compare tab (`/compare`, [`ComparePage.tsx`](../../pages/ComparePage.tsx)). The user
drops two versions of a document. The `CompareView` component produces, separately
or together:

- a **change summary** (Markdown, one bullet per change);
- a **change table** (JSON shown as a table);
- an **impact search**: the other documents of the base that these changes touch,
  with a verdict per document and the passages to update;
- a **summary of one document** on its own.

The server provides the analyses (`server/routers/compare/`), the exports
(`server/routers/exports.py`), the history (`server/routers/history.py`), the votes
(`server/routers/feedback.py`) and the previews (`server/routers/preview.py`). This
folder holds only the interface and the browser state.

## Files

| File | Role |
|---|---|
| `CompareView.tsx` | Page: assembles the panels, both analyses, the impact search and the history |
| `DocsPanel.tsx` | Two drop zones and the per-document summaries (one shown at a time) |
| `DocDropZone.tsx` | One drop zone: type and size check, preview, replacement |
| `FilePreviewModal.tsx` | Full-screen preview of a file (PDF, image, DOCX; other formats converted by the server) |
| `AnalysisActions.tsx` | Generate, cancel and re-run buttons for each analysis, and "Run Both" |
| `AnalysisResults.tsx` | Shows the summary or table card; tabs when both exist |
| `ResultCards.tsx` | Cards: Markdown summary, structured table, document summary |
| `JsonDiffTable.tsx` | Change table: columns by file type, hiding of low-criticality changes, images |
| `ImageLightbox.tsx` | Before / after view of a visual change |
| `Feedback.tsx` | Vote and comment on a result card |
| `ImpactSection.tsx` | Impact search section: buttons, state, result card |
| `impact/ImpactResultsCard.tsx` | Impact results, "by document" or "by change", verdict filters |
| `impact/ImpactParts.tsx` | Sub-components: conflicting passages, changes, badges, change list |
| `impact/ImpactFeedback.tsx` | Votes on a document verdict and a general opinion on the search |
| `impact/types.ts` | Types of the impact result (NDJSON stream) |
| `ComparisonHistory.tsx` | Paginated history panel |
| `hooks/useAnalysisTrack.ts` | One analysis over an SSE stream; instantiated twice (table, summary) |
| `hooks/useImpactSearch.ts` | Impact search over an NDJSON stream, cancellation, restored result |
| `hooks/useDocSummaries.ts` | Summaries of one document |
| `hooks/useCompareDocs.ts` | The two documents and their hashes, mirrored to local storage |
| `hooks/useCompareConfig.ts` | Server configuration: volume, impact index, processor versions available for the file type |
| `hooks/useHistoryLoad.ts` | Loads a history entry into the right track and restores the files |
| `api.ts` | HTTP calls, SSE stream reading, automatic saves, history |
| `files.ts` | Accepted file types, maximum size, SHA-256 hash, placeholder files |
| `storage.ts` | Local storage keys, time-limited writes during a stream |
| `types.ts` | Shared types: document, image pair, change, summary |
| `ui.tsx` | Shared components: buttons, tabs, card, modal, scrolling that follows the stream |

## Dropping files

- A file is accepted when its extension is in `SUPPORTED_EXTENSIONS` (`files.ts`):
  PDF, images, Word, PowerPoint, Excel, XML. A file larger than
  `MAX_FILE_SIZE_BYTES` (20 MB) is refused.
- The content is read in the browser (base64 and bytes). It is kept in local storage,
  except for placeholders.
- Changing either file clears the results, the impact search and the hashes in
  progress.

## Analysis: table and summary

`useAnalysisTrack` makes the call. The browser computes the SHA-256 hashes of both
files, then sends a `FormData` to `POST /api/compare/analyze` with `old_file`,
`new_file`, the hashes, `force_refresh` and `processor_version` (`structured` for the
table, `standard` for the summary).

The response is an SSE stream (`data: …` lines). Events read by `readAnalysisStream`:

| Event | Effect |
|---|---|
| `metadata` | File type, method, request id; when `cached` is true, the result comes from the server cache |
| `image_context` | Image pairs sent to the model |
| `response.output_text.delta` | Text appended to the result |
| `warning` | Notification; the report is marked as possibly incomplete |
| `usage` | Consumption, kept with the history entry |
| `error` | Error shown above the results |
| `[DONE]` | Normal end of the stream |

At the end, in this order:

1. If the stream was cut without `[DONE]`, the partial text stays on screen with a
   "retry" message. It is neither added to the history nor cached.
2. If the server answered from its cache, the result is shown as it is.
3. Otherwise, if the save volume is configured, the session folder receives both
   documents, the text (`analysis.md`), then the Excel file (table) or the PDF file
   (summary).
4. The result is added to the history (`POST /api/history`).

Steps 3 and 4 never block the display: their failures are ignored.

**Cancellation**: the partial result stays on screen with an information message,
and it is not saved.

**Warning**: a report that comes with a `warning` is saved without hashes. The cache
replays only the text, so the warning would be lost.

**Re-run** sends `force_refresh=true` and bypasses the cache.

The two analyses are independent: each has its own state, cancellation and cache.
"Run Both" starts them together; the button appears only when both analyses are
offered for the file type.

**Change table**: rows are numbered in document order. The "#" column shows the
identifiers `C1`, `C2`, … used by the impact search. Low-criticality changes are hidden
by default, not removed. Images are paired with rows by order of appearance, not by
page number, because the page number returned by the model is unreliable.

**Partial stream**: the table is read as the text arrives (`parsePartialJsonItems`),
without waiting for the end.

## Impact search

`useImpactSearch` calls `POST /api/compare/impact` with a JSON body: `changes_text`,
the hashes, the file names, `force_refresh`. The change text comes from the table if
there is one, otherwise from the summary. The search does not start while an analysis
is streaming.

The stream is NDJSON, one event per line:

| Event | Content |
|---|---|
| `plan` | List of changes, number of candidates, queries used or failed |
| `document` | One judged document, shown as soon as it is ready |
| `done` | Consumption, duration, impact request id |
| `error` | Error, which stops the search |

**Verdicts** (`status`): `impacted`, `check` (to check), `not_impacted`, `error`
(judgment failed). The card filter works on these four values.

**Views**: "by document" (verdict, reason, passages to update, conflicting passage
highlighted) or "by change" (only the changes that touch a document). Clicking a
change such as "C12" in the passages scrolls the change table to the matching row,
when that table is displayed.

**"Archive" badge**: shown for a document published before the archive cutoff (the
`archive` field of the server result).

**Saving**: the result is kept in local storage and, when the comparison has a history
entry, attached to it (`PUT /api/history/{id}/impact`). A search without changes is
not attached.

**Votes**: one vote per document verdict, and one general opinion, sent to
`POST /api/compare/impact/feedback` with the request id.

## History

The panel reads `GET /api/history` by pages, then `GET /api/history/{id}` for one
entry. `useHistoryLoad` puts the entry into its track: `structured` goes to the table,
the others to the summary. The other track is kept only if it belongs to the same two
files (same hashes); otherwise it is reset. The saved impact result is restored in the
same operation.

The files are reloaded from the session folder on the volume (`GET /api/compare/load`).
Without an available file, the drop zone shows the file name as a placeholder, with the
message "File not available — drop to replace".

## Summary of one document

`useDocSummaries` sends the file and its hash to `POST /api/compare/summarize`. A
document without extractable text gets a "no content" summary, not an error. Each card
can be exported to PDF.

## Feedback

- `Feedback.tsx`: thumbs up or down with an optional comment, sent to
  `POST /api/feedback`. One send per comparison, remembered in local storage.
- `impact/ImpactFeedback.tsx`: votes on the verdicts and a general opinion (see above).

## Exports and previews

- PDF: `downloadPdf` in [`lib/exportPdf.ts`](../../lib/exportPdf.ts), rendered by the
  server (`POST /api/export/pdf`).
- Excel: `downloadExport` in `api.ts`, for the table (`POST /api/compare/export-excel`)
  and for the impact search (`POST /api/compare/impact/export-excel`).
- Preview: PDF and images are read locally; a DOCX goes through
  [`ExactDocxPreview`](../shared/ExactDocxPreview.tsx) (`POST /api/preview/pdf`); other
  formats are converted to HTML by `POST /api/preview`.

## Local storage

[`storage.ts`](storage.ts) keeps the documents, hashes, texts and results so that the
page survives a reload. Writes are limited to one per second during a stream. A failed
write (quota exceeded) is ignored: the result in memory stays correct.

## Interface configuration

`useCompareConfig` reads `/api/config/app` and `/api/config/processors`:

- automatic saving is enabled only when the volume path is set and is not a
  placeholder marker (`TODO…`);
- the impact search is offered only when the index name is valid;
- the processor versions offered (`standard`, `structured`) depend on the file type.
  The types and their versions are defined in
  `server/services/compare/processors/versions.yml`.

## Access

`ComparePage` shows the access-denied panel when `can_compare` is false. On the
server, the analysis, impact, summary, save and history routes require access to the
comparison (`require_compare`).
