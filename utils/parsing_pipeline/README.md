# Parsing Pipeline

Daily chain: `1_categories` -> `2_manifest` -> `3_parse` (GPU) -> `4_describe_images` (LLM) -> `5_sync_index` -> `6_update_kb_metadata`.

`3_Parse_Pipeline.py` only orchestrates: each phase (scope, file selection, Docling batches, retry, `image_metadata`,
`processed_files` / `chunks`, writes) is a function in `parse_steps.py`, driver side only.

This README collects the incident/investigation narratives that used to live as
long inline comments across `resources/parsing_pipeline.job.yml` and the
notebooks/modules under this directory. Code keeps a single terse one-line
pointer back to the relevant section here — the full story (root cause,
what was tried and failed, what fixed it) lives only in one place.

## spot-driver-reclaim

### 2026-07-01 — Spot driver reclaim killed a GPU run

`parse_gpu_cluster` (the Docling GPU cluster, `resources/parsing_pipeline.job.yml`)
originally let the driver run on a spot instance like the workers. Run
`1041938121709483` (2026-07-01) died because "driver node is a spot instance
that was terminated by the cloud provider" — a spot reclaim of the driver
kills the whole job, not just one task's partition.

Fix: `first_on_demand: 1` in `aws_attributes` — the driver is pinned
on-demand, workers stay `SPOT_WITH_FALLBACK` to keep GPU cost down. Losing a
spot worker now only retries that task's partition instead of crashing the run.

## gpu-task-executor-contention

### 2026-07-02 — 16 Spark tasks fighting over 1 GPU

`g4dn.4xlarge` has 16 vCPUs but only 1 GPU. Without explicit GPU resource
config, Spark schedules up to 16 concurrent tasks per executor (one per
vCPU), with zero awareness of the single shared GPU. Confirmed live via the
Spark UI during run `480896252037130` (2026-07-02): executors reported
`maxTasks=16` while `resources.gpu` had exactly 1 address. Up to 16
concurrent Docling/PyTorch processes were fighting over one 16 GB-VRAM T4,
contending/thrashing instead of parallelising.

Fix: `spark.executor.resource.gpu.amount=1` and
`spark.task.resource.gpu.amount=1` in `parse_gpu_cluster`'s `spark_conf`.
This caps concurrency to 1 Docling parse per worker (2 total on this
cluster), each running uncontended — expected to be net faster, not slower,
for GPU-bound work. Revisit with fractional GPU sharing (e.g. 0.25) only
after benchmarking VRAM headroom per Docling model.

## pythonpath-worker-daemon-revert

### 2026-07-02 — Static PYTHONPATH broke the pyspark worker-daemon bootstrap

Tried setting `PYTHONPATH` as a cluster-level env var, as a more robust
alternative to `addPyFile()` for the rare "worker joins late, misses
utils.py" `ModuleNotFoundError` (hit live once, run `480896252037130`,
2026-07-02 — see `3_Parse_Pipeline.py`'s `addPyFile()` call site).
This broke
something more fundamental instead: confirmed live, it made Spark's own
pyspark-worker-daemon bootstrap fail fast and consistently inside the
isolated venv created by the opencv `%pip` fix ("Error while finding module
specification for 'pyspark.daemon': No module named 'pyspark'") — a much
worse, faster failure than the rare one it was meant to fix.

Reverted the same day. Databricks/Spark manages `PYTHONPATH` dynamically per
worker-daemon launch; a static override at the cluster level interferes with
that. `addPyFile()` in the notebook is imperfect (rare late-joining-executor
gap) but far safer than a static `PYTHONPATH`. Do not re-add one without a
clean isolated test.

## opencv-python-headless-crash

### 2026-07-02 — docling-core drags in a broken opencv, SIGABRTs on cv2 import

`docling-core` transitively pulls in the LATEST `opencv-python` /
`opencv-python-headless` (4.13.0.9x), which hard-crashes on the very first
`cv2` import with `Fatal Python error: Aborted`. First theory (tried and
ruled out): a GUI/X11 dependency issue, since `opencv-python`'s default
build needs Qt/X11 which headless clusters don't have — switching to
`opencv-python-headless` did NOT fix it. Actual confirmed root cause: the
crash message is
`crypto/fips/fips.c:154: OpenSSL internal error: FATAL FIPS SELFTEST FAILURE`,
a known regression in that opencv release line on hosts running OpenSSL
3.0.x (Databricks' base image) — see opencv/opencv-python#1184, independent
of GUI vs headless. Confirmed live on run `646389187637539` (2026-07-02):
crashed on the FIRST docling import of a freshly-started worker process.

Also tried: declaring `opencv-python-headless` as a job-cluster library
alongside `docling-core` (docling-project/docling#3201). Did NOT help
(confirmed live 3x) — without a version pin it still resolves to the broken
4.13.x release, and having both `opencv-python` and `opencv-python-headless`
installed is documented upstream as producing a broken install regardless of
declaration order.

Fix: pin an opencv-python-headless release from BEFORE the 4.13.x line via a
`%pip install -q --force-reinstall --no-deps opencv-python-headless==4.12.0.88`
cell inside `3_Parse_Pipeline.py` (not a job-cluster library — the
`--force-reinstall` must run AFTER docling-core's own install, which already
installed the broken version at cluster-library-install time). A `%pip`
cell (not a job-cluster library) is required so the pin applies afterwards;
Databricks syncs notebook-scoped libraries to every executor, which matters
since the crash happens inside a `pandas_udf` on the workers, not the
driver. Also set as belt-and-suspenders at the cluster/OS-process level
(`USE_TF`, `TRANSFORMERS_NO_TF`, `TF_CPP_MIN_LOG_LEVEL`, `CUDA_MODULE_LOADING`,
`HF_HUB_OFFLINE`, `TRANSFORMERS_OFFLINE`, `HF_HUB_DISABLE_TELEMETRY` in
`parse_gpu_cluster`'s `spark_env_vars`) since `utils._patch_worker_env()`
setting these as the first line of the parsing UDF is still "too late" if
anything else touches CUDA/TF first on a freshly-started worker process.

Do not re-add `opencv-python-headless` as a plain job-cluster library
without removing the `%pip` cell, and do not remove the `%pip` cell without
re-verifying the crash is actually gone.

## oversized-file-oom-gate

### 2026-07-06 — A flat file-size gate conflated OOM risk with content bloat

Investigation in `debug/trop_volumineux_analysis.md` on 77 `SKIP_TOO_LARGE`
files: a single 15 MB gate on raw file size was applied to every format
alike, conflating two different things.

1. Docling's real OOM risk (page/slide rasterization) only applies to the
   formats it actually renders: docx/docm/pptx/pptm/pdf. xls/xlsx/xlsm/xlsb
   (custom openpyxl parser) and doc/rtf/odt/ods (antiword/text-based
   parsers) never rasterize anything, so they can use a much higher
   ceiling.
2. For docx/docm/pptx/pptm specifically, 26/77 oversized files were bloated
   by embedded OLE/Office objects or embedded video — dead weight for text
   extraction, not real content (e.g. a 34 MB docx carrying a 30 MB
   embedded .doc nobody needs).

Fix, two parts:
- `utils.strip_ooxml_bloat()` pre-cleans OOXML zips (docx/docm/pptx/pptm)
  before the size gate: strips embedded media junk, recompresses real
  embedded images (kept — still needed for the image extraction/description
  pipeline) since full resolution isn't needed for Docling's page rendering
  or the vision-LLM step downstream.
- Per-format ceilings instead of one flat gate: `MAX_SIZE_DOCLING` (15 MB,
  docx/docm/pptx/pptm post-strip and any other Docling-rasterized format),
  `MAX_SIZE_PDF` (35 MB — PDFs still rasterize, but two known scanned PDFs
  at 17/28 MB need the LLM-OCR fallback a chance to run instead of being
  pre-excluded), `MAX_SIZE_OTHER` (100 MB — antiword/openpyxl/etc., no
  page-rasterization OOM risk). docx/docm/pptx/pptm above the Docling-safe
  threshold are no longer excluded at all: `image_utils._fallback_parse_docx`/
  `_pptx` reads the zip/XML directly (no rasterization, no OOM risk) and
  gets real text + images out without Docling. Legacy binary `.ppt` (never a
  Docling format — always errors immediately, never rasterizes) belongs in
  the high-ceiling bucket for the same reason, handled by
  `image_utils._fallback_parse_ppt_legacy`.

## uc-not-enabled-single-user

### 2026-07-27 — Meta CPU cluster started without Unity Catalog

Run `754481470867538`: `meta_cpu_cluster` (00b/00 tasks) started without
Unity Catalog enabled, and every table read failed with `[UC_NOT_ENABLED]`.
`parse_gpu_cluster` and `describe_cpu_cluster` inherit `data_security_mode`
from a server-side default and were unaffected; `meta_cpu_cluster` didn't.
Fix: explicit `data_security_mode: SINGLE_USER` on `meta_cpu_cluster` in
`resources/parsing_pipeline.job.yml`.

## llm-batch-size-kernel-crash

### 2026-07-03 — CPU driver kernel dies partway through an image-description run

The CPU single-node cluster (`m5d.xlarge`) running `4_Describe_Images_LLM.py`
crashes with "Fatal error: Python kernel is unresponsive" after a
surprisingly stable number of images within the same session — confirmed
twice in real conditions (2026-07-03): 8013 then 8066 images before crash,
on two different clusters/kernels (so a cumulative per-session memory leak,
not tied to one specific run).

Fix: `LLM_BATCH_SIZE` caps the total number of images processed per notebook
invocation at 6000 (~25% margin below the observed crash point) instead of
letting it crash. The task ends in SUCCESS with images left `PENDING`,
ready to be picked up by the next invocation (repair-run) for the next
batch.

## image-filter-thresholds

### Geometric pre-filter thresholds (audit piste C)

Deterministic pre-filter applied when building `image_metadata`: an image
that matches is marked `SKIPPED_DECORATIVE` (no LLM call, no indexing).
Deliberately conservative — does NOT filter on a large width/height ratio,
since a real flowchart/timeline can legitimately be wide; only cuts what is
physically illegible. Audit (piste C) found `IMAGE_SCALE=3.0` is correctly
applied, but ~22% of figures are physically small on the page (~1 inch ->
~244 px at 216 DPI). The short side of an image is effectively a text/table
line height: below ~85 px, text/table lines stop being legible (visually
verified: a 353x64 table was illegible). Thresholds validated on a 60-image
sample: 0 false positives on flowchart/table/chart/diagram.

Chosen values: `IMG_SKIP_MAX_DIM = 140` px (longer side), `IMG_SKIP_MIN_SIDE = 85` px
(shorter side) — see `config.py`.

## ocr-fallback-error-swallowed

### 2026-07-31 — OCR fallback failures were invisible in parser_error

Same error-swallowing class as the antiword incident above. When Docling's
initial conversion yields near-empty text on a `.pdf`, an OCR fallback
pass is attempted (`GPU_OCR_FALLBACK=True`); if that OCR pass itself raised
(confirmed live: 2 cases of "CUDA is not available in the system" from
the OCR engine), the exception only reached `logger.warning` (executor
stderr, not queryable) and the caller silently fell through to the
original near-empty text, reporting the generic "Empty after Docling
conversion" — indistinguishable from a genuinely blank scan.

Fix: the OCR exception's detail is now carried through and appended to
`parser_error` ("...; OCR fallback also failed: ..."), and general
exception messages elsewhere (`_exc_detail()`) now walk `__cause__`/
`__context__` instead of using bare `str(e)`, since Docling/PyTorch
exceptions frequently wrap a deeper root cause this way (e.g.
"Pipeline StandardPdfPipeline failed" alone gave no indication of what
actually failed underneath).

## image-write-fsync-bug

### 2026-07-04 — PIL.save() reported success but the file never reached durable storage

Confirmed live: `PIL.save(path)` reports success, and the file is visible
(`os.path.exists` + `getsize`) from the SAME cluster right after writing —
but is later found completely absent (the folder doesn't even exist) from a
totally fresh cluster/session reading the same UC Volume path. Not a
spot-reclaim issue (reproduced on an ON_DEMAND cluster) and not FUSE
metadata-cache staleness (a brand-new cluster saw nothing at all) — the
bytes genuinely never reached durable storage. `PIL.save()` closes its file
handle without an `fsync`, and the UC Volumes FUSE layer apparently doesn't
guarantee a plain `close()` flushes to the backend.

Fix: write through an explicit file handle, then `fh.flush()` +
`os.fsync(fh.fileno())` before considering the write successful, instead of
`pil_img.save(path)` directly — see `image_utils.py`'s image-save helper.

## division-processes-bug

### 2026-07-06 — Every "PROCESSES" document fell through to division AUTRE

`IDCAT_PROCESSES`'s raw category name is `"1 - PROCESSES / PROCESSUS"` (no
`"AS -"` prefix), even though it belongs to the AS division and
`niveau_plus_1` already displays it as `"AS - PROCESSES / PROCESSUS"`. The
division-derivation logic (`selection.build_category_hierarchy`) was missing
this special case, so every document rooted under PROCESSES fell through to
`"AUTRE"`. Confirmed live: 2291/2296 "AUTRE" documents (99.8%) were this
single root category, vs. only 4 in the pre-rewrite system
(`dev_landingzone.qualibot` / `uat_landingzone.qualibot` archives).

Fix: an explicit `root_idcat == IDCAT_PROCESSES -> "AS"` branch before the
generic `"AS -"` / `"IS -"` prefix checks. `category_reference` (built by
`1_Build_Category_Reference.py`) must be rebuilt after this fix for
`parse_manifest` (which only copies it at `2_Cleanup_Volume.py` run time,
not kept in sync afterwards) to pick up the corrected division — confirmed
live: after fixing this and rebuilding `category_reference`, `parse_manifest`
for 11/20 test IDDOCs still held the stale `"AUTRE"` value because
`2_Cleanup_Volume.py` hadn't been re-run.

## image-description-wipe-bug

### 2026-07-03, 2026-07-06, 2026-07-07 — Rebuilds kept discarding finished LLM descriptions

`image_status_col()` (parse-time only) can only ever return
`EXTRACTION_FAILED` / `SKIPPED_DECORATIVE` / `PENDING` — `DONE` / `SKIPPED`
are assigned later by `4_Describe_Images_LLM.py` and live only in the
current `image_metadata` table. Any rebuild-from-checkpoint of
`image_metadata` that doesn't explicitly carry those statuses forward resets
already-described images back to `PENDING`, discarding real (paid-for) LLM
output. Hit live three times:

- 2026-07-03: 19,866 descriptions wiped, recovered from Delta history.
- 2026-07-06: 38,587 descriptions wiped, recovered from Delta history version 99.
- 2026-07-07: a Python-side "preserve" join (joining prior DONE/SKIPPED rows
  back in before writing) silently failed to restore status for a large
  share of IDDOCs on a 4870-IDDOC rebuild (e.g. IDDOC 23041: 298 DONE -> 299
  PENDING) — the preserve step itself didn't fire, the third occurrence of
  this bug despite two prior "fixes".

Fix: a real Delta `MERGE` (keyed on `IDDOC, image_id`, the same key
`4_Describe_Images_LLM.py`'s own MERGE uses to write `DONE`/`SKIPPED`
back) replaced the delete-then-append + Python preserve-join pattern in
`3_Parse_Pipeline.py`'s final-write cell. `WHEN MATCHED`, parse-time
fields always take the fresh value, but `status`/`description`/tokens/
`described_at` only take the fresh (`PENDING`) value when the existing row
isn't already `DONE`/`SKIPPED`. This removes the dependency on a fragile
join running correctly first — the preserve decision is re-derived at write
time, atomically, from the table's own current state.

## iddoc-19247-tie-break-collision

### 2026-07-06 — Same-mtime files produced non-deterministic chunk collisions

`rank_candidates()` in `selection.py` picks one file per IDDOC by extension
priority and modification time, but ties (same extension, same mtime — e.g.
a main document plus several `priloha_N` attachments synced together) had no
final tie-break. Confirmed live: IDDOC 19247 had 4 same-mtime `.docx` files;
different runs picked a different "rank 1" winner each time. Since
`chunk_id` is derived from `IDDOC + chunk_index` only (not `source_path`),
each winning file's chunks collided under the same `chunk_id` with divergent
content — and separately, `3_Parse_Pipeline.py`'s checkpoint dedup
(`_read_checkpoint_deduped`) partitioned by `source_path` produced 4
surviving checkpoint rows for this one IDDOC, each with different
`chunk_sha256`.

Fix: added a final tie-break on `source_path` (ascending) in
`rank_candidates()`'s window ordering, and switched
`_read_checkpoint_deduped()`'s dedup partition key from `source_path` to
`IDDOC` — enforcing "one parsed representative per IDDOC" end to end,
matching what every downstream table already assumes.

## mv-self-join-blocked

### 2026-07-03 — Self-joins blocked on remotely-filtered materialized views

`gd_cat_latest` / `gd_doc_cat_latest` / `gd_doc_latest` (the `*_latest` MV
bronze tables) are remotely-filtered materialized views. Spark blocks
self-joins by default on this MV type ("Self-joins are blocked on remotely
evaluated MVs"). Hit twice independently:

- `selection.build_category_hierarchy()`'s `IDPERE` walk-up self-joins
  `gd_cat_latest` against itself — confirmed live, run `592030761035648`.
- `2_Cleanup_Volume.py` reads `gd_doc_latest` independently in several
  cells (REF/TITRE mapping, doc_date, IDDOC->IDTYPDOC) and later joins them
  together when assembling `df_manifest_full` — Spark's optimizer treats
  that as a self-join too. Confirmed live, run `89333676857892` (2026-07-03).

Fix: `spark.conf.set("spark.databricks.remoteFiltering.blockSelfJoins", "false")`
at the top of both notebooks/functions, before the joins run.

## image-metadata-duplicate-merge-crash

### 2026-07-04 — MERGE failed on pre-existing duplicate (IDDOC, image_id) pairs

`image_metadata` has a small number of duplicate `(IDDOC, image_id)` pairs
(pre-existing, ~114 as of 2026-07-03). If a description batch happens to
include both copies of a pair, the `MERGE` in
`4_Describe_Images_LLM.py` raises `DELTA_MULTIPLE_SOURCE_ROW_MATCHING` —
confirmed live, run `1089418553818115` (2026-07-04). Fix: dedup the source
batch on `(IDDOC, image_id)` before writing — both copies were described
identically in the same run, so either is fine to keep.

## division-chunks-drift

### 2026-07-04 — Image chunks silently missing from the division-split tables

`src_chunks_as` / `src_chunks_is` only ever received text chunks (written by
`3_parse`) and silently drifted out of sync with `chunks` (the "all
divisions" table) every time images got described, since
`4_Describe_Images_LLM.py` only mirrored image chunks into `chunks`.
Confirmed live: ~20k image chunks missing from `src_chunks_as`/`src_chunks_is`
after a full description run. Fix: mirror the same delete-then-append into
`src_chunks_as`/`src_chunks_is` (filtered by `division`) right after writing
to `chunks`.

## llm-checkpoint-chunking-near-miss

### 2026-07-03 — A long description run had no incremental checkpoint

Before `LLM_CHECKPOINT_CHUNK_SIZE`, a run over tens of thousands of images
(hours at the rate-limited throughput) only wrote its one `MERGE` at the
very end — a crash near the end would lose everything, money already spent
on LLM calls AND the results, with nothing saved. A ~39k-image / ~11h run
was cancelled early specifically because of this gap before it could bite
(2026-07-03). Fix: split processing into fixed-size chunks
(`LLM_CHECKPOINT_CHUNK_SIZE`), each persisted via `MERGE` immediately —
bounds the loss to at most one chunk.

## spark-default-parallelism-bug

### 2026-07-02 — Cluster size was silently ignored, capping parsing at 8-way

`spark.conf.get("spark.default.parallelism")` only reads the explicit config
MAP — that key is never actually set there, so this call always raised and
silently fell back to a hardcoded value of 8, regardless of actual cluster
size. Confirmed live on run `480896252037130`: a 2-worker x 16-core (32-core)
GPU cluster was still repartitioning every 100-file batch into only 8
partitions — each task then parsed ~12 files SEQUENTIALLY inside one Python
process (observed task durations of 100-270s, one task at a time per GPU,
matching ~12 files x 10-20s/file). Fix:
`spark.sparkContext.defaultParallelism` — a computed property, not a
config-map lookup, and always populated correctly.

## resume-exclusion-history

### 2026-07-03, 2026-07-08, 2026-07-27 — Three fixes to the "already parsed, skip it" logic

`3_Parse_Pipeline.py`'s resume/exclusion logic (which files to skip
because they're already handled) went through three separate bug fixes:

- **2026-07-03**: matching excluded files by `source_path` alone regardless
  of `parse_status` meant a file that ever got `ERROR`/`EMPTY_TEXT`/`TIMEOUT`
  in `_pipeline_checkpoint` (never dropped/cleaned — accumulates forever)
  was excluded from re-parsing PERMANENTLY, even after fixing the actual
  cause. Confirmed live: 394 `.doc` files that failed with "antiword
  conversion failed" (root cause: antiword was never actually deployed to
  the workspace, fixed separately) stayed invisible to every retry attempt
  because their `source_path` already had an `ERROR` row. Fix: only
  `SUCCESS` is treated as permanent for a given `source_path`.

  Separately, `utils._convert_doc_antiword()` used to swallow every antiword
  failure mode (missing binary, non-zero exit, exception) into an identical
  generic "antiword conversion failed" string, routed only to
  `logger.warning` (executor stderr, not queryable) while the caller
  persisted that same generic string to `error_trace` regardless of cause.
  Confirmed live 2026-07-03: even after the deployment fix above, 259/398
  retried `.doc` files still failed with the identical generic message, and
  all 20 sampled files converted fine in isolation — so the real cause
  (timeout / resource contention under concurrent pipeline load, most
  likely) stayed unqueryable. Fix: the actual reason is now returned and
  lands in `error_trace`.
- **2026-07-08**: `SKIP_TOO_LARGE` was removed from the "permanent" set once
  `image_utils`'s fallback parsers gave oversized docx/pptx/ppt a real
  non-Docling path — a `SKIP_TOO_LARGE` row became retryable exactly like
  `ERROR`. Confirmed live: without this, 24 docx/pptx files stayed
  permanently stuck even after unblocking them in `processed_files`, because
  the join still matched their stale `SKIP_TOO_LARGE` row by `source_path`.
- **2026-07-27**: exclusion switched from `(source_path)` alone to
  `(source_path, document_sha256)` — a revision rewrites the file in the
  SAME `D_<IDDOC>` folder under the same name, so a `source_path`-only
  filter reported "already parsed" on genuinely changed content and the
  daily job would never re-index an updated document. `sha256` is computed
  on the actual bytes read, so the comparison is exact.

## image-cleanup-ordering

### Pre-existing image folders must be cleaned AFTER the resume-exclusion join

`3_Parse_Pipeline.py` removes pre-existing image folders for the IDDOCs
it's about to re-parse, to ensure a clean slate. This must run AFTER the
resume-exclusion join, not on the pre-resume file list: an IDDOC already
present in the checkpoint (e.g. from a prior attempt that crashed mid-batch)
is skipped by the resume join and will never be re-parsed in that run.
Cleaning its folder before checking resume used to delete its already-saved
images without ever regenerating them, leaving a checkpoint row pointing at
a dangling `volume_path` (image lost, LLM description later fails with
"failed to read image"). Scoping the cleanup to the post-resume file list
avoids this.

## rtf-odt-ods-never-selected

### 2026-07-31 — rtf/odt/ods files were silently excluded before ever reaching a parser

`selection.FORMAT_PRIORITIES` (the dict that assigns each file extension a
priority for `rank_candidates`/`select_best_files`) never listed `rtf`,
`odt`, or `ods` — despite `utils.py` having real parsers for them
(`_parse_rtf`, `_parse_odf`) and `3_Parse_Pipeline.py`'s `NON_DOCLING_EXTS`
size gate treating them as a legitimate category alongside `doc`/`xls`/`ppt`.
Any extension missing from `FORMAT_PRIORITIES` falls back to
`ext_priority=99` (the "unsupported format" marker), and
`select_best_files()` filters out every row with `ext_priority >= 99`. An
IDDOC whose only file was `.rtf`/`.odt`/`.ods` therefore never appeared in
`df_selected` — no error, no `SKIPPED_*`/`ERROR` row, nothing: the document
silently never entered the pipeline at all.

Fix: added `rtf`/`odt`/`ods` to `FORMAT_PRIORITIES` (priorities 14-16, below
every Docling-native format). This makes the existing `_parse_rtf`/`_parse_odf`
error-detail fix (see the error-swallowing pattern discussion elsewhere in
this README) actually reachable — before this fix, correctly reporting the
real failure reason for a format that could never be selected in the first
place had no effect in production.

## env-aware-worker-fallbacks

### 2026-07-30 — Hardcoded DEV path in the worker fallback broke UAT

`utils.py`'s `_DEFAULTS` dict and `ensure_config()` are the last-resort tiers a
worker falls back to when it can't restore `CONFIG` from a broadcast or
`spark.conf` (shared/USER_ISOLATION clusters can't read `spark.conf` inside a
pandas UDF). Both tiers used to point at a hardcoded DEV volume path. Confirmed
live 2026-07-30: on UAT this tier silently failed (path unreachable), so
`CONFIG` never restored and `ANTIWORD_BIN` fell through to its `None` default
on every worker.

Fix: both tiers now read `PARSING_VOLUME_BASE_PATH` / `PARSING_OFFLINE_MODELS`
— cluster env vars (`spark_env_vars`), visible in every process including a
pandas UDF worker subprocess, unlike `CONFIG` itself — instead of a hardcoded
DEV path.

## pandas-udf-optional-param-rejected

### 2026-07-07 — pandas_udf rejects a default-valued / Optional parameter

`pandas_udf` validates the wrapped function's signature at decoration time
(`pyspark.sql.pandas.typehints.infer_eval_type`) and rejects a default value /
`Optional[...]` parameter outright: confirmed live,
`PySparkNotImplementedError: UNSUPPORTED_SIGNATURE`.

Fix: `parse_and_extract_images_udf`'s `xml_only_series` stays a required
positional column — every call site passes it explicitly (`F.lit(False)`
where the caller doesn't need the large-OOXML fallback path) instead of
relying on a default.

## volume-scan-targeted-listing

### Targeted volume scan instead of a full recursive listing every run

`selection.scan_volume_files()` does a full `recursiveFileLookup` over the
ENTIRE volume on every run: it re-lists and re-ranks every file for every
IDDOC ever ingested, even though a normal incremental run only has a handful
of new or retryable IDDOCs — suspected to be the "very long startup" at the
start of the pipeline.

`3_Parse_Pipeline.py` instead: (1) works out which IDDOCs still need a
physical file from Delta reads only (`parse_manifest` + `processed_files`, no
volume I/O at all), (2) lists the volume ROOT once, non-recursively, to map
folder name -> IDDOC, (3) scans (recursively, via `selection.scan_volume_paths()`)
ONLY the folders for those target IDDOCs. Timed explicitly (`[TIMING]` print)
rather than guessed — compare across runs against cluster-creation timing
(Databricks Jobs/Clusters API: `CREATING`->`RUNNING`->`DRIVER_HEALTHY`) to see
how much of the old "long startup" was actually this scan versus cluster/
library startup time.

## ppt-legacy-ole-heuristic

### Why legacy .ppt uses a byte-scan heuristic instead of a real parser

Docling doesn't support legacy binary `.ppt` (pre-2007 OLE compound file
format) at all — `utils._get_input_format` returns `None` for `.ppt`, an
immediate "Unsupported format" error, no attempt made — and unlike docx/pptx
there's no zip/XML to read.

A byte-accurate MS-PPT record parser needs the full record-type table to tell
atoms from containers. `image_utils._fallback_parse_ppt_legacy` instead scans
the raw "PowerPoint Document" OLE stream for runs of printable UTF-16LE/ASCII
text (the classic `strings`-style trick) and the "Pictures" stream for
JPEG/PNG file signatures. Ordering is approximate and some non-content strings
(font/style names) leak through, but it costs nothing (no OOM risk) and beats
a hard skip — extracted images flow into `image_metadata` like any other
parser's, so they still get a vision-LLM description in task `4_describe_images`.

## null-iddoc-in-list-guard

### 2026-07-30 — A null IDDOC broke the chunk-deletion SQL

`4_Describe_Images_LLM.py` builds a `DELETE ... WHERE IDDOC IN (...)`
statement from a Python list of affected IDDOCs. A null IDDOC in that list,
`str()`-joined into the `IN` list, resolves as an unquoted bare identifier in
the generated SQL rather than a value — breaking the statement. Fix: filter
out null IDDOCs (`.isNotNull()`) before building the list — a null IDDOC has
no valid `chunk_id`/DELETE target anyway.

## empty-small-file-exclusion

### Zero-text small files are excluded permanently, not retried forever

A file that parses to zero text AND is small (`<= EMPTY_SMALL_FILE_SIZE_BYTES`)
is marked `SKIPPED_EMPTY_SMALL_FILE` instead of `ERROR`/`EMPTY_TEXT` (both of
which are retried every run). Checked by hand on a batch of these (2026-07-06):
all were blank template stubs, never worth trying a different parser on.
Zero-text ABOVE that size stays `ERROR`/`EMPTY_TEXT` — could be a scanned PDF
(OCR candidate) or a real bug, worth a human look rather than silent exclusion.

## target-iddocs-vs-num-files

### HAS_TARGET_IDDOCS vs num_files track two different things

`num_files` is "how many files still need a FRESH Docling parse" — it drops to
0 once every target IDDOC already has a terminal row in the checkpoint.
`HAS_TARGET_IDDOCS` is "is there anything in scope for this run's OUTPUT
tables" (`processed_files`/`chunks`/`image_metadata`), independent of whether
new parsing happened. Confirmed live 2026-07-06: gating the processed_files/
chunks/image_metadata build cells on `num_files == 0` meant a deliberate
no-Docling rebuild (delete some output rows, then rerun to re-derive fresh
business metadata only) silently wrote nothing, since `num_files` was 0.

## chunk-content-type-null-filter-bug

### chunk_content_type must be set on every chunk row, not just image chunks

`chunk_token_count`/`chunk_content_type`/`chunk_sha256` are kept on every
chunk row (not computed then dropped) so `4_Describe_Images_LLM.py` can
tell text and image chunks apart on the same table and compute the next free
`chunk_index`. These used to be set only on image chunks: `04`'s filter
`chunk_content_type != 'image'` then silently excluded every text chunk too,
since `NULL != 'image'` evaluates to `NULL` in SQL, not `TRUE`.

## checkpoint-snapshot-pruning

### Manual command to prune old `_pipeline_checkpoint` snapshots

`3_Parse_Pipeline.py` creates a named shallow-clone snapshot
(`_pipeline_checkpoint_YYYYMMDD_HHMM`) before every FULL run. These are cheap
(metadata pointers, no data copy) so pruning is a deliberate, occasional
action, not something the notebook does automatically. To prune, keeping only
the most recent `N`:

```python
keep = 3
snaps = [r.tableName for r in spark.sql(
    f"SHOW TABLES IN {CATALOG_SCHEMA} LIKE '_pipeline_checkpoint_*'").collect()]
for old in sorted(snaps)[:-keep]:
    spark.sql(f"DROP TABLE IF EXISTS {CATALOG_SCHEMA}.{old}")
    print(f"Pruned old snapshot: {old}")
```

## processed-files-reconciliation

### 2026-08-20 — Stale ERROR rows sat forever next to a fresh SKIPPED_* row for the same IDDOC

`2_Cleanup_Volume.py` logs permanent exclusions (`SKIPPED_REF_OUT_OF_SCOPE`,
`SKIPPED_IDDOC_NOT_FOUND`, `SKIPPED_REF_MANUAL`, `FILTERED_BY_DATE`) into
`processed_files` via a Delta MERGE keyed on `(IDDOC, parse_status)`. Because
the status was part of the match key, an IDDOC's old `ERROR`/`EMPTY_TEXT` row
(from whenever it last had a real file to parse) was never seen as "the same
row" as a new `SKIPPED_*` row once the document left scope — the MERGE just
inserted the new row alongside the old one, forever. `processed_files` is
documented (and assumed by every downstream reader) to hold one row per
IDDOC; in practice, any IDDOC that failed once and later left scope carried
two contradictory rows with no way to tell, without cross-referencing
`gd_doc_latest` by hand, that the `ERROR` was stale. Confirmed live
2026-08-20 on 8 IDDOCs (14609, 19557, 19961, 20042, 20044, 20047, 20061,
20062) reported as "still failing" that had actually already left scope.

Worse: an IDDOC whose volume folder AND `gd_doc_latest` row both disappear
entirely (the document was deleted at the source, not just superseded by a
newer revision) is invisible to `2_Cleanup_Volume.py`'s classification loop —
it only iterates over folders physically present on the volume this run.
Nothing ever detected or recorded that this happened; the stale row (often
`ERROR`) just sat there permanently, indistinguishable from a live failure.
Confirmed live: IDDOCs 19884, 19965, 20023, 20030 — folder and `gd_doc_latest`
row both gone, `ERROR` row still sitting in `processed_files`.

Fix, two parts:
- Both `processed_files` writes in `2_Cleanup_Volume.py` (the
  `SKIPPED_*`/`FILTERED_BY_DATE` blocks) switched from MERGE-keyed-on-status to
  `DELETE FROM ... WHERE IDDOC IN (...)` followed by a plain append — the same
  idiom `3_Parse_Pipeline.py`'s own incremental `write_outputs()` already
  used correctly. Whatever the IDDOC's previous status was, exactly one fresh
  row survives.
- A new step diffs `processed_files`' existing IDDOCs against this run's
  volume-folder listing and `gd_doc_latest` (+ fallback) lookup. Any IDDOC in
  neither is reclassified `SKIPPED_SOURCE_REMOVED` — a status distinct from
  `SKIPPED_REF_OUT_OF_SCOPE` specifically because "revision superseded by a
  new IDDOC" is normal/expected, while "gone from the source entirely" is
  worth being able to tell apart when auditing failures.

Missed on the first pass, caught testing the LLM-OCR fallback below live:
the mirror direction. Removing IDDOC 15920 from `MANUAL_REF_EXCLUSIONS` did
NOT get it re-parsed, because its old `SKIPPED_REF_MANUAL` row was never
cleaned up — that status (like `SKIPPED_REF_OUT_OF_SCOPE`/
`SKIPPED_IDDOC_NOT_FOUND`/`SKIPPED_SOURCE_REMOVED`) is in
`3_Parse_Pipeline.py`'s `_TERMINAL_STATUSES`, so `resolve_parse_scope()`
treated the IDDOC as already permanently handled even after it became
eligible again. Added a second reconciliation step: any IDDOC classified
`KEEP` this run that still carries one of those 4 statuses in
`processed_files` gets that stale row deleted, so it's picked up as pending
again. `FILTERED_BY_DATE` is deliberately excluded from this check — it
legitimately coexists with `KEEP` (a separate, orthogonal date-cutoff
exclusion), it isn't a stale scope/exclusion status.

## llm-ocr-scanned-pdf

### 2026-08-20 — Genuinely bad PDF scans get a last-resort LLM-OCR pass instead of staying ERROR/EMPTY_TEXT forever

Some PDFs are real scans of poor enough quality (or Docling's own OCR
pipeline errors outright on them) that `GPU_OCR_FALLBACK` inside
`_parse_via_docling()` still can't recover usable text. Previously these just
stayed `ERROR`/`EMPTY_TEXT` — IDDOC 15920 (`Dm_15920.pdf`) was even hand-added
to `MANUAL_REF_EXCLUSIONS` for this reason on 2026-07-06.

Rather than a point-fix for that one document, `parse_and_extract_images_udf`
(`image_utils.py`) now has a generic last resort: when a PDF's extracted text
is still under `LLM_OCR_TEXT_THRESHOLD` chars after everything else has been
tried, its first `LLM_OCR_MAX_PAGES` pages are rendered to JPEG
(`_render_pdf_pages_for_llm_ocr`, via `pypdfium2` — already a direct `docling`
dependency, no new library needed) and appended to the document's `images`
list with `label="scanned_page"`, exactly like any other extracted figure.
This means:
- `image_count > 0` so the doc flows into `image_metadata` through the
  existing path (`build_image_metadata()`), no changes needed there.
- `parser_error` is cleared and `parser_strategy` becomes
  `pending_llm_ocr:pdf` — the document lands on `EMPTY_TEXT`, not `ERROR`
  (there's no body text, but the content is being handled via image-OCR
  chunks instead).
- Step 4 (`4_Describe_Images_LLM.py`) describes each `scanned_page` image
  through the SAME async/rate-limited Luna engine as any other image, but
  `build_llm_prompt()` routes `label="scanned_page"` to `PDF_PAGE_OCR_PROMPT`
  instead of `IMAGE_DESCRIPTION_PROMPT` — full verbatim transcription, never
  `SKIP`, with any stamp/watermark/annotation called out explicitly on its
  own `STAMP/WATERMARK:` line (these often carry status info — cancelled,
  superseded, do-not-use — that isn't in the body text and must never be
  silently dropped; this is exactly what IDDOC 15920 turned out to be: a
  single-page "Proces zrušen / Process not performed" cancellation stamp, not
  the real multi-page document). The transcription is then indexed as a
  normal image chunk, same as any other described figure.

## image-chunk-full-rebuild-churn

### 2026-08-20 — Every incremental run rewrote all ~38,700 image chunks, not just the new ones

`4_Describe_Images_LLM.py`'s "build image chunks" step read `image_metadata`
filtered to `status == 'DONE'` with no scoping to what this run actually
processed — that's every image ever described, across the pipeline's whole
history. The write step then did `DELETE FROM chunks WHERE chunk_content_type
= 'image' AND IDDOC IN (<every one of those IDDOCs>)` followed by a full
re-insert of that same entire set. So a run that described 3 new images still
deleted and reinserted all ~38,700 existing image chunks — ~77k Change Data
Feed events the vector index then had to reprocess (re-embed) on every single
run, regardless of how little actually changed. Likely a real contributor to
the CDF-retention staleness this table's search index hit before (see
[[project_qualibot_uat_vector_search_retention_2026-08-18]] project memory).

Fix, two parts:
- `df_described` is now joined against the exact `(IDDOC, image_id)` pairs in
  `rows_to_process` (the images actually just fed to the vision LLM this run)
  — everything already `DONE` from a prior run is left alone.
- The write itself switched from delete-all-for-IDDOC + reinsert-all to a
  Delta `MERGE ... ON chunk_id` (`whenMatchedUpdateAll` /
  `whenNotMatchedInsertAll`) — even for an IDDOC with several images where
  only one was just (re)described, only that one row's CDF entry changes; the
  sibling images already indexed are untouched.

## orphaned-image-chunks-after-a-mid-run-crash

### 2026-08-27 — The rows_to_process scoping above left DONE images permanently un-chunked if the run crashed first

The fix directly above traded one bug for another. Scoping `df_described` to
`rows_to_process` (this run's own in-memory batch) stopped the CDF churn, but
it also meant: if a run crashed *after* the description loop had already
`MERGE`d some images to `status = 'DONE'` in `image_metadata`, but *before*
reaching this chunk-injection phase, those images' chunks were never written
— and never would be. `DONE` images are never re-selected as `PENDING`, and
no later run's `rows_to_process` includes them either (it only holds what
*that* run itself just described), so they're stuck forever: described, but
unsearchable.

Hit for real in PROD 2026-08-27: two `4_describe_images` runs crashed
mid-batch (`Fatal error: The Python kernel is unresponsive` — root cause of
the kernel crash itself still open, unrelated to this fix) after merging
~12,000 images to DONE. Comparing PROD vs UAT image-chunk counts surfaced a
72.7% vs ~100% DONE-to-chunk ratio — UAT has never hit a mid-run crash here,
so its 1:1 ratio was the tell that PROD's shortfall was a real bug, not
"UAT just has more history" (the wrong initial explanation).

Fix: scope `df_described` by an anti-join on `chunk_id` against `chunks`
(`chunk_content_type = 'image'`) instead of `rows_to_process`. This keeps
the original fix's property — a run only ever computes/merges chunks for
images that don't already have one, never touching the ~38k+ already-indexed
— while also being self-healing: any DONE image orphaned by a prior crash
gets picked up by the very next run, from any source, without needing a
manual backfill. Costs one extra full scan of DONE images + existing image
chunk_ids per run (cheap at tens-of-thousands-of-rows volumes) in exchange
for never silently losing a description's searchability again.

## chunking-2026-10

### 2026-10-08 — Passage splitting rewritten (`chunking.py`)

The audit `docs/chat_vsi_tests.md` (§ 5) found, on a local run of the former splitter:
merged passages carried an impossible heading lineage (`_merge_meta` mixed the headers of two
sections), two sections could share one passage, the `[Title > Section]` line was repeated in
front of every paragraph, and there was no overlap between passages — `CHUNK_OVERLAP_RATIO` only
applied inside the character splitter, and was never passed to the workers anyway
(`configure()` lacked it, so it was 0). The Docling `HybridChunker` path never ran: the pipeline
keeps markdown only. `chunking.py` replaces all of it (pure Python, `tests/test_parsing_chunking.py`),
adds a character ceiling, marks tables of contents / front matter / repeated text in
`chunk_content_type`, and builds image passages with their section and caption. Measured first on
DEV test indexes (`archive/evaluation/rechunk_experiment.py`), applied to UAT by one
`full` run (`OPERATIONS.md` D5).

## uat-table-versions

### 2026-07-31 → 2026-10-08 — Why UAT tables carried _v1/_v2/_v3 suffixes, and why they no longer do

- **_v2 → _v3 (2026-07-31)**: `chunks_v2`/`src_chunks_as_v2`/`src_chunks_is_v2` and their 3 indexes had every column typed as string (schema-less JSON import in the former DEV→UAT copy script, live since 2026-07-07). Retyping in place would recreate the physical table and break the index's CDF sync (`DIFFERENT_DELTA_TABLE_READ_BY_STREAMING_SOURCE`, confirmed 2026-07-07), so `_v3` tables + indexes were built in parallel with correct types (full re-embed).
- **_v3 → _v1 (2026-08-18)**: the `_v3` indexes' TRIGGERED sync went silently stale from 2026-07-31 (tasks `5_sync_index`/`6_update_kb_metadata` were new and unvalidated) until the CDF history it needed aged out of the tables' default 7-day `delta.deletedFileRetentionDuration` — `VECTOR_SEARCH_SOURCE_HISTORY_OUT_OF_RETENTION`, which a TRIGGERED incremental sync never recovers from. Fixed by deep-cloning into `_v1` tables with `delta.deletedFileRetentionDuration`/`delta.logRetentionDuration` = `interval 60 days` and fresh `_v1` indexes. `_pipeline_checkpoint_v1`/`image_metadata_v1`/`processed_files_v1` were seeded from `_v3` so the daily job resumed incrementally instead of reparsing on GPU.
- **No suffix (2026-10-08)**: one `chunks` table and one `chunks_index` (chat with a `division` filter, impact search); `src_chunks_as`/`src_chunks_is` and their indexes are gone with the Knowledge Assistant. `parsing_table_suffix` is empty on DEV/UAT/prod; it stays only as the `_test` isolation switch of a validation run. Renames: `operations_dev.md` (DEV) and `OPERATIONS.md` D5 (UAT). Keep the 60-day retention on every table an index reads.
