"""config.py — All pipeline parameters.

Each parameter can be overridden via environment variables (set in the Job task
configuration or via spark.conf) so that dev / uat / prod targets diverge
without touching this file.

Priority for every parameter: env var > explicit override in notebook > this default.
"""

import datetime as _dt
import os as _os


def _env(key, default) -> str:
    """Return env var if set, otherwise default.

    :param key: Name of the environment variable.
    :param default: Value returned when the variable is unset.
    :return: value of the variable, or ``default`` when it is unset.
    """
    return _os.environ.get(key, default)


def _require_env(key) -> str:
    """Return env var; raise if unset — no environment-specific default.

    :param key: Name of the environment variable.
    :return: value of the variable, never empty.
    """
    val = _os.environ.get(key)
    if not val:
        raise RuntimeError(f"{key} must be set (see resources/parsing_pipeline.job.yml)")
    return val


# --- Environment, schema, paths ---
# dev, uat or prod: the tables are written to <PIPELINE_WRITE_ENV>_proj.qualibot.<table>.
PIPELINE_WRITE_ENV = _require_env("PIPELINE_WRITE_ENV")
# Tables another task of the job wrote are read here; empty means the write environment.
PIPELINE_READ_ENV = _os.environ.get("PIPELINE_READ_ENV") or PIPELINE_WRITE_ENV

# Intraqual data asset (quality_system) is a reference table, read in the reference environment like the others.
REFERENCE_READ_ENV = _require_env("REFERENCE_READ_ENV")
INTRAQUAL_GOLD_CATALOG_SCHEMA = f"{REFERENCE_READ_ENV}_gold.quality_system"
_G = INTRAQUAL_GOLD_CATALOG_SCHEMA

# One row per document revision (document_revision_ID is the IDDOC), with its main category, path and document type.
INTRAQUAL_REVISIONS_TABLE = _env(
    "PARSING_INTRAQUAL_REVISIONS_TABLE", f"{_G}.document_revisions_intraqual"
)
# One row per category and ancestor: gives the name of every category of a path.
INTRAQUAL_CATEGORY_ANCESTORS_TABLE = _env(
    "PARSING_INTRAQUAL_CATEGORY_ANCESTORS_TABLE", f"{_G}.category_ancestors_intraqual"
)

# The daily job fails the run if one of these tables was last written longer ago than the threshold, instead of
# silently indexing a partial corpus.
INTRAQUAL_REQUIRED_TABLES = [INTRAQUAL_REVISIONS_TABLE, INTRAQUAL_CATEGORY_ANCESTORS_TABLE]
MAX_SOURCE_STALENESS_HOURS = int(_env("PARSING_MAX_SOURCE_STALENESS_HOURS", "36"))

# --- Qualibot perimeter (scope gate) ---
# Columns of INTRAQUAL_REVISIONS_TABLE. The full-diffusion flag is not part of the gate: the portal shows those documents too.
DOC_SCOPE_FILTER = _env(
    "PARSING_DOC_SCOPE_FILTER",
    "revision_status_code = 1 AND lifecycle_state_code = 7 AND is_hidden = false",
)

# --- Optional fallback (disabled by default) ---
# Empty = disabled.
DIVISION_ARCHIVE_TABLE = _env("PARSING_DIVISION_ARCHIVE_TABLE", "")
DIVISION_REFERENCE_TABLE = _env(
    "PARSING_DIVISION_REFERENCE_TABLE", f"{PIPELINE_WRITE_ENV}_proj.qualibot.category_reference"
)

# Pre-computed by create_proj_parse_manifest — avoids querying the Intraqual tables directly here.
PARSE_MANIFEST_TABLE = _env(
    "PARSING_PARSE_MANIFEST_TABLE", f"{PIPELINE_WRITE_ENV}_proj.qualibot.parse_manifest"
)

# Volumes
VOLUME_ROOT_PATH = _require_env("PARSING_VOLUME_ROOT_PATH")
VOLUME_BASE_PATH = _require_env("PARSING_VOLUME_BASE_PATH")
OFFLINE_MODELS_DIR = _require_env("PARSING_OFFLINE_MODELS")

# Relative to REPO_DIR (utils/parsing_pipeline/, two levels below the repo root that holds third_party/).
ANTIWORD_BIN_RELATIVE = "../../third_party/antiword/bin/antiword"
ANTIWORD_SHARE_RELATIVE = "../../third_party/antiword/share"

# --- Target Delta tables ---
TARGET_PROCESSED_FILES_TABLE = f"{PIPELINE_WRITE_ENV}_proj.qualibot.parsed_documents"

# All divisions; the chat filters on the `division` column at query time.
TARGET_CHUNK_TABLE = f"{PIPELINE_WRITE_ENV}_proj.qualibot.chunks"
# Chunks of documents published before DOC_DATE_CUTOFF: kept out of `chunks` (chat),
# merged with `chunks` into `chunks_full` by update_proj_vector_indexes for the impact-search index.
TARGET_CHUNK_TABLE_ARCHIVE = f"{PIPELINE_WRITE_ENV}_proj.qualibot.chunks_archive"

TARGET_IMAGE_METADATA_TABLE = f"{PIPELINE_WRITE_ENV}_proj.qualibot.image_metadata"
TARGET_IMAGE_CACHE_TABLE = f"{PIPELINE_WRITE_ENV}_proj.qualibot.image_description_cache"  # one description per (image, request)
TARGET_HEALTH_TABLE = f"{PIPELINE_WRITE_ENV}_proj.qualibot.parsing_run_health"  # one row per pipeline run, for the monitoring dashboard
TARGET_CHANGE_LOG_TABLE = f"{PIPELINE_WRITE_ENV}_proj.qualibot.document_change_log"  # one row per NEW/REVISED document, for the monitoring dashboard

# Working tables of 3_parse: the parse checkpoint, the manual RAG exclusions (IDDOC, reason) and the retry scratch table.
CHECKPOINT_TABLE = f"{PIPELINE_WRITE_ENV}_proj.qualibot.parsing_checkpoint"
RAG_EXCLUSION_TABLE = f"{PIPELINE_WRITE_ENV}_proj.qualibot.rag_exclusions"
RETRY_TEMP_TABLE = f"{PIPELINE_WRITE_ENV}_proj.qualibot.parsing_retry_temp"
# Hand-over between the parse tasks of one run (overwritten by 3_parse): the IDDOCs in scope (3c_chunks rebuilds
# them), and the PDFs left for the GPU task.
RUN_SCOPE_TABLE = f"{PIPELINE_WRITE_ENV}_proj.qualibot.parsing_run_scope"
GPU_QUEUE_TABLE = f"{PIPELINE_WRITE_ENV}_proj.qualibot.parsing_gpu_queue"

# Above this many PDFs left to parse, 3_parse hands them to the GPU task; at or below, it parses them on CPU and the
# GPU cluster never starts. Only PDFs use Docling's GPU models (layout, tables): docx / pptx / html do not.
GPU_MIN_PDFS = int(_env("PARSING_GPU_MIN_PDFS", "").strip() or "30")

# Column groups shared by the steps: the document metadata of parse_manifest, the category hierarchy of
# category_reference, and what a selected file is enriched with.
MANIFEST_METADATA_COLS = [
    "ref",
    "titre",
    "type_document",
    "categorie",
    "langue",
    "auteur",
    "doc_date",
    "indice",
]
HIERARCHY_COLS = ["division"] + [f"niveau_plus_{n}" for n in range(1, 7)]
BUSINESS_METADATA_COLS = MANIFEST_METADATA_COLS + HIERARCHY_COLS
ENRICHMENT_COLS = [
    "ref",
    "titre",
    "type_document",
    "categorie",
    "langue",
    "auteur",
    "document_prefixes",
    "division",
    "doc_date",
] + [f"niveau_plus_{n}" for n in range(1, 7)]

# Modules the executors import (pandas UDFs); shipped with addPyFile by 3_parse and 4_describe_images.
WORKER_MODULES = (
    "chunking.py",
    "worker.py",
    "format_parsers.py",
    "image_fallbacks.py",
    "image_utils.py",
    "config.py",
)

# --- Date cutoff ---
# Docs before this date are parsed like the others but their chunks go to
# TARGET_CHUNK_TABLE_ARCHIVE instead of the RAG chunk tables (parsed_documents:
# is_filtered_by_date=True, is_in_rag=False). An unknown date counts as recent.
DOC_DATE_CUTOFF = _env("PARSING_DOC_DATE_CUTOFF", "2018-01-01")
_dt.date.fromisoformat(
    DOC_DATE_CUTOFF
)  # fails at import on a malformed value: the steps cast it to a date literal

# Total number of pre-cutoff documents allowed to be parsed, most recent first
# (cumulative, not per run): 0 = none, N = the N most recent, -1 = all.
# create_proj_parse_manifest turns it into parse_manifest.parse_content.
ARCHIVE_MAX_DOCS = int(_env("PARSING_ARCHIVE_MAX_DOCS", "0") or "0")

# One metadata-only "notice" chunk per pre-cutoff document (REF, title, revision,
# date — no content), rebuilt on every run into TARGET_ARCHIVE_NOTICE_TABLE.
# Copied into the RAG chunk tables (chatbot indexes) only when ARCHIVE_NOTICES_IN_RAG is on.
TARGET_ARCHIVE_NOTICE_TABLE = f"{PIPELINE_WRITE_ENV}_proj.qualibot.archive_notices"
ARCHIVE_NOTICES_IN_RAG = _env("PARSING_ARCHIVE_NOTICES_IN_RAG", "false").strip().lower() in (
    "1",
    "true",
    "yes",
)
# Also hardcoded in update_proj_vector_indexes.py (which does not import config.py) and matched
# by ARCHIVE_NOTICE_MARKER in server/services/infra/vector_search.py.
ARCHIVE_NOTICE_CONTENT_TYPE = "archive_notice"
ARCHIVE_NOTICE_MARKER = "ARCHIVED DOCUMENT — CONTENT NOT INDEXED"

# =============================================================================
# Manual REF exclusions, recorded as SKIPPED_REF_MANUAL (permanent) by create_proj_parse_manifest.py.
# =============================================================================
MANUAL_REF_EXCLUSIONS = set()

# --- Category exclusion ---
# Documents whose principal category, or one of its ancestors, is listed are skipped as SKIPPED_CATEGORY_EXCLUDED by create_proj_parse_manifest.py.
EXCLUDED_IDCATS = {3196}  # ONE_QMS-5S > Obsolete Documents

# Documents under these root categories are indexed whatever their date (DOC_DATE_CUTOFF does not apply to them).
DATE_EXEMPT_ROOT_IDCATS = {3870}  # 00-LATECOERE PROCESSES_One QMS

# --- Empty + small file auto-exclusion ---
# Zero-text + small file = blank stub -> skipped permanently; zero-text above this size stays ERROR for review.
EMPTY_SMALL_FILE_SIZE_BYTES = int(_env("PARSING_EMPTY_SMALL_FILE_SIZE_BYTES", "51200"))  # 50 KiB

# --- Run mode ---
RUN_MODE = _env("PARSING_RUN_MODE", "incremental")  # "incremental" | "full" | "rechunk"
# "full": scan every in-scope document, parse only the files whose (IDDOC, content hash) has no reusable checkpoint
# row, then rebuild every table from the checkpoint. "rechunk": the same rebuild, without scanning or parsing.

# Ceiling on the files one run may parse: above it, 3_parse stops before the GPU parse. 0 = no ceiling (a deliberate
# full re-parse). A daily run parses tens of files; thousands mean the checkpoint was not recognised.
MAX_PARSE_FILES = int(_env("PARSING_MAX_PARSE_FILES", "").strip() or "500")

# =============================================================================
# Scope filter (None = all IDDOCs, list = specific)
# Overridable via PARSING_PARSE_FILTER="123,456" for a targeted (test) run.
# =============================================================================
PARSE_FILTER = [
    int(x) for x in _env("PARSING_PARSE_FILTER", "").split(",") if x.strip().isdigit()
] or None

# --- Feature flags ---
ENABLE_RETRY = True
# Per-step durations of each file in the checkpoint `timings` column: a few perf_counter calls, read to find where a slow run spent its hours.
ENABLE_TIMING_TEST = True

# --- Resilience -- batch checkpointing & per-file timeout ---
CHECKPOINT_BATCH_SIZE = 100  # Write to Delta every N files (crash-safe)
CHECKPOINT_SNAPSHOT_KEEP_DAYS = 30  # a full run keeps its checkpoint snapshot this long
CHECKPOINT_ORPHAN_KEEP_DAYS = (
    180  # a document out of scope keeps its parse result this long, in case it comes back
)
PARSE_TIMEOUT_SECONDS = 200  # Max seconds per file -- beyond this, mark as TIMEOUT
MAX_CHUNKS_SPREADSHEET = 100  # Limit chunks for xlsx/xls/xlsm (None = no limit)

# --- Chunk cleaning flags ---
CLEAN_IMAGE_PLACEHOLDERS = True
CLEAN_FORMULA_ARTIFACTS = True
DEDUPLICATE_CHUNKS = True


# --- Docling engine settings ---
# USE_GPU: explicit override via PARSING_USE_GPU, else auto-detected (nvidia-smi).
def _detect_gpu() -> bool:
    """True when nvidia-smi exists and runs."""
    import shutil as _sh, subprocess as _sp

    if _sh.which("nvidia-smi") is None:
        return False
    try:
        _sp.check_output(["nvidia-smi"])
        return True
    except Exception:
        return False


_use_gpu_env = _os.environ.get("PARSING_USE_GPU")
USE_GPU = (_use_gpu_env.strip().lower() in ("1", "true", "yes")) if _use_gpu_env else _detect_gpu()
DO_OCR = False
# Docling's own EasyOCR re-parse is off; scanned pages go through the LLM-OCR fallback below.
GPU_OCR_FALLBACK = False
TABLE_STRUCTURE_MODE = "accurate"  # "accurate" | "fast"
GENERATE_PICTURE_IMAGES = True
IMAGE_SCALE = 3.0  # ~216 DPI -- captures fine text in diagrams
MIN_AREA_RATIO = 0.05
MAX_REPEAT = 2

# --- Image save settings ---
IMAGE_MAX_DIMENSION = 4096  # preserves full detail for large schematics
IMAGE_JPEG_QUALITY = 95  # near-lossless (only used if IMAGE_FORMAT=JPEG)
IMAGE_RESAMPLING = "LANCZOS"  # sharpest downscale algorithm
IMAGE_FORMAT = "PNG"  # PNG = lossless, no compression artifacts on text/diagrams

# --- Tokenizer / Chunking ---
# tiktoken (cl100k) is more accurate than CHARS_PER_TOKEN for technical French; falls back to it if unavailable offline.
USE_TIKTOKEN = True
CHARS_PER_TOKEN = 3.5
# Passage sizes (chunking.py), overridable per run; utils/evaluation/retrieval_eval.py measures their effect on retrieval.
MIN_CHUNK_TOKENS = int(_env("PARSING_MIN_CHUNK_TOKENS", "150"))
TARGET_CHUNK_TOKENS = int(_env("PARSING_TARGET_CHUNK_TOKENS", "300"))
MAX_CHUNK_TOKENS = int(_env("PARSING_MAX_CHUNK_TOKENS", "450"))
# Character ceiling on top of tokens: dot leaders / form underscores count few tokens for many
# characters, and the Vector Search reranker reads only the first 2,000 characters.
MAX_CHUNK_CHARS = int(_env("PARSING_MAX_CHUNK_CHARS", "1600"))
# Overlap between consecutive passages of one section (fraction of TARGET).
CHUNK_OVERLAP_RATIO = float(_env("PARSING_CHUNK_OVERLAP_RATIO", "0.12"))
# A passage body found in at least this many documents is marked "boilerplate".
BOILERPLATE_MIN_DOCS = int(_env("PARSING_BOILERPLATE_MIN_DOCS", "20"))

# --- LLM settings (vision model for image description) ---
# Describe every image again (a prompt or model change), whatever the parsing run mode: tens of thousands of LLM calls.
REDESCRIBE_ALL_IMAGES = _env("PARSING_REDESCRIBE_ALL_IMAGES", "false").strip().lower() in (
    "1",
    "true",
    "yes",
)
LLM_MODEL_ENDPOINT = _env("PARSING_LLM_ENDPOINT", "databricks-gpt-6-luna")
LLM_MAX_TOKENS = 5000  # GPT-5 reasoning tokens need headroom beyond the visible output
LLM_TEMPERATURE = 1.0  # gpt-5 family: temperature=1 only
LLM_MAX_RETRIES = 5  # per endpoint and per pass
# Tried in order after the primary, comma-separated. An endpoint that rejects images is skipped after a few failures in a row.
LLM_FALLBACK_ENDPOINTS = _env("PARSING_LLM_FALLBACK_ENDPOINTS", "databricks-gpt-5-6-luna")
# Passes over the endpoint chain; the waits are slept before the second and the following passes.
LLM_ROUNDS = int(_env("PARSING_LLM_ROUNDS", "2"))
LLM_ROUND_WAITS_S = (5.0, 15.0, 30.0)
# Images in a row that fail on one endpoint before it is skipped for the rest of the run.
LLM_BREAKER_FAILURES = int(_env("PARSING_LLM_BREAKER_FAILURES", "5"))
# Runs in which an image is attempted before it is set aside as FAILED_LIMIT (a full run resets the count).
IMAGE_MAX_ATTEMPTS = int(_env("PARSING_IMAGE_MAX_ATTEMPTS", "10"))
LLM_MAX_CONCURRENT = 10  # Memory of the single-node m5d.xlarge caps the concurrency
# Kept below the batch size at which the kernel becomes unstable.
LLM_BATCH_SIZE = int(_env("PARSING_LLM_BATCH_SIZE", "6000"))

# Persists progress periodically instead of one MERGE at the very end of a multi-hour run.
LLM_CHECKPOINT_CHUNK_SIZE = int(_env("PARSING_LLM_CHECKPOINT_CHUNK_SIZE", "2000"))

# Endpoint rate limits, budgeted below the real ceiling since other app features share it.
LLM_ITPM_BUDGET = int(_env("PARSING_LLM_ITPM_BUDGET", "1000000"))  # real limit: 2,000,000
LLM_OTPM_BUDGET = int(
    _env("PARSING_LLM_OTPM_BUDGET", "100000")
)  # real limit: 200,000 <- bottleneck
LLM_QPH_BUDGET = int(_env("PARSING_LLM_QPH_BUDGET", "324000"))  # real limit: 360,000
# Calibrated avg tokens/image, drives RPM_safe = min(budget/avg) across all three axes.
LLM_AVG_INPUT_TOKENS = int(_env("PARSING_LLM_AVG_IN", "1050"))
LLM_AVG_OUTPUT_TOKENS = int(_env("PARSING_LLM_AVG_OUT", "300"))

# --- Image filtering (deterministic — applied WITHOUT any LLM call) ---
# Deliberately conservative — no filtering on a large width/height ratio.
IMG_SKIP_MAX_DIM = 140  # longer side (px) < threshold -> illegible thumbnail -> skip
IMG_SKIP_MIN_SIDE = 85  # shorter side (px) < threshold -> band/table too short -> skip

# Post-filter: minimum useful length of a description to be injected as a chunk.
MIN_INDEXABLE_DESC_CHARS = 40

# True = the provenance prefix is embedded in chunk_text; False = body only (ref/division/title stay filterable columns).
EMBED_SOURCE_PREFIX = _env("PARSING_EMBED_SOURCE_PREFIX", "true").strip().lower() in (
    "1",
    "true",
    "yes",
)

# --- LLM Prompt template (vision model) ---
# Expected placeholders: {division}, {category}, {context}
IMAGE_DESCRIPTION_PROMPT = """You are a vision extraction engine for an industrial RAG index. You are given an IMAGE plus some surrounding document text (CONTEXT). The context is ALREADY indexed separately, so do not repeat it.

DOMAIN HINTS (use only to resolve acronyms/jargon, never echo back):
- Division: {division}
- Category: {category}
- Nearby text: {context}

Your entire answer MUST begin with either "SKIP:" or "#". Write nothing before it (no letter, no preamble).

If the image is decorative or has no extractable information — logo, letterhead/banner, signature, generic photo of people or buildings, separator line, blank, or a screenshot of a generic software UI — answer with exactly one line and stop:
SKIP: then a reason in 6 words max

Otherwise answer in the document's language (French if the context is French), with no other text, using this exact structure:
- first line: "# " then the CATEGORY in square brackets, then one sentence saying what the image shows AND what it is about — name the process, part, form or subject, using the nearby text if needed (e.g. "Logigramme du traitement d'une non-conformité en réception"). This sentence is the only place where words of the context may be reused;
- second line: **Content:**
- then the extraction following the rules below.
Do not output any literal angle brackets or placeholder text.

CATEGORY is one of: TABLE, FLOWCHART, DIAGRAM, DATA_CHART, SCHEMATIC, PHOTO_TECH, TEXT_DOC.
Extraction rules by category:
- TABLE: reproduce the data as a markdown table.
- FLOWCHART: list the boxes/steps and the arrows between them as "A -> B", and every decision branch (Oui/Non).
- DIAGRAM / SCHEMATIC: name the components, labels, callout numbers, dimensions, axes and the relations actually drawn.
- DATA_CHART: axes + units + the overall trend + the key values.
- PHOTO_TECH: the physical parts, markings and references visible.
- TEXT_DOC: transcribe only the text that is part of the image itself.

Do NOT copy or paraphrase the CONTEXT text: if everything you could write is already in the context, answer SKIP instead. No notes about the document, no mention of division or category."""

# --- LLM-OCR fallback for scanned PDFs ---
# Triggers when Docling still yields near-empty text: pages are rendered to images and queued through the same
# PENDING-image pipeline with the prompt below (full transcription, never SKIP).
# Below this length the parse counts as a scan: a digital PDF with almost no text would otherwise stay SUCCESS with nothing indexed.
LLM_OCR_TEXT_THRESHOLD = int(_env("PARSING_LLM_OCR_TEXT_THRESHOLD", "150"))
LLM_OCR_MAX_PAGES = int(_env("PARSING_LLM_OCR_MAX_PAGES", "100"))
# Full-page transcription needs more than the shared LLM_MAX_TOKENS.
LLM_OCR_MAX_TOKENS = int(_env("PARSING_LLM_OCR_MAX_TOKENS", "8192"))

# Expected placeholders: {division}, {category}, {context}
PDF_PAGE_OCR_PROMPT = """You are a vision transcription engine for an industrial RAG index. You are given a page IMAGE from a document Docling could not extract text from directly (e.g. a scan). Transcribe it as faithfully and completely as possible — do not summarize, do not skip any part of the page.

DOMAIN HINTS (use only to resolve acronyms/jargon, never echo back):
- Division: {division}
- Category: {category}
- Nearby text: {context}

Answer in the document's language (French if the context is French), with no preamble, using this exact structure:
- first line: "# " then one short sentence describing what kind of page this is (form, table, technical drawing, letter, etc.);
- second line: **Content:**
- then the full transcription: body text, table contents (as markdown tables), headers/footers, handwritten notes if legible.

Critical: transcribe any stamp, watermark, hand-written annotation, or overlay text VERBATIM and call it out explicitly on its own line prefixed "STAMP/WATERMARK:" — these often carry status information (e.g. cancelled, superseded, draft, do not use) that is not in the body text and must never be silently dropped.

Never answer SKIP: even a page containing only a stamp or a mostly-blank form still carries real information and must be transcribed."""


# --- Document and image statuses (parsed_documents, image_metadata) ---
# 3_parse leaves a document in a terminal status, which the next incremental run does not parse again. A document
# with no text but images is IMAGES_PENDING until its images are described: 4_describe_images moves it on.
IMAGES_PENDING = "IMAGES_PENDING"
EMPTY_TEXT = "EMPTY_TEXT"

# A revision replaced by a newer one of the same REF: its chunks are pruned and it is never parsed again.
SUPERSEDED_REVISION = "SKIPPED_SUPERSEDED_REVISION"

TERMINAL_STATUSES = (
    "SUCCESS",
    "SKIPPED_EMPTY_SMALL_FILE",
    "SKIPPED_REF_MANUAL",
    "SKIPPED_SOURCE_REMOVED",
    "SKIPPED_EMPTY_IMAGES",
    "SKIPPED_EMPTY_FOLDER",
    SUPERSEDED_REVISION,
    IMAGES_PENDING,
)

# Statuses 4_describe_images moves on in the same job run.
MOVED_BY_IMAGES = (EMPTY_TEXT, IMAGES_PENDING)

# Image statuses (image_metadata.status). An image still to describe is retried by the next run; the others are final
# for the description step (DONE and SKIPPED are the results, FAILED_LIMIT is reported in the run summary).
IMAGE_RETRIED_STATUSES = ("PENDING", "ERROR")  # rows left with ERROR are retried too
IMAGE_TERMINAL_STATUSES = (
    "DONE",
    "SKIPPED",
    "SKIPPED_DECORATIVE",
    "EXTRACTION_FAILED",
    "FAILED_LIMIT",
)

# Statuses whose derived rows (chunks, image_metadata) are pruned when the document leaves scope.
PRUNED_STATUSES = ("SUCCESS", "ERROR", EMPTY_TEXT, IMAGES_PENDING)
