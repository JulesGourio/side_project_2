"""config.py — Single source of truth for all pipeline parameters.

This file is the version deployed with the Declarative Automation Bundle.
Each parameter can be overridden via environment variables (set in the Job task
configuration or via spark.conf) so that dev / uat / prod targets diverge
without touching this file.

Priority for every parameter: env var > explicit override in notebook > this default.

Usage:
    from config import *
    print(IMAGE_SCALE)
"""

import os as _os

def _env(key, default):
    """Return env var if set, otherwise default."""
    return _os.environ.get(key, default)

def _require_env(key):
    """Return env var; raise if unset — no environment-specific default."""
    val = _os.environ.get(key)
    if not val:
        raise RuntimeError(f"{key} must be set (see resources/parsing_pipeline.job.yml)")
    return val


# =============================================================================
# Catalog / Schema / Paths
# =============================================================================
CATALOG_SCHEMA        = _require_env("PARSING_CATALOG_SCHEMA")

# Appended to every table the pipeline writes — PARSING_TABLE_SUFFIX=_test isolates a whole validation run.
TABLE_SUFFIX = _env("PARSING_TABLE_SUFFIX", "")

INTRAQUAL_SOURCE_CATALOG_SCHEMA = _require_env("PARSING_INTRAQUAL_SOURCE")

INTRAQUAL_BRONZE_CATALOG_SCHEMA = _require_env("PARSING_INTRAQUAL_BRONZE")
_B = INTRAQUAL_BRONZE_CATALOG_SCHEMA

GD_DOC_LATEST         = _env("PARSING_GD_DOC_LATEST",          f"{_B}.gd_doc_latest")
GD_DOC_CAT_LATEST     = _env("PARSING_GD_DOC_CAT_LATEST",      f"{_B}.gd_doc_cat_latest")
GD_CAT_LATEST         = _env("PARSING_GD_CAT_LATEST",          f"{_B}.gd_cat_latest")
GD_TYPDOC_LATEST      = _env("PARSING_GD_TYPDOC_LATEST",       f"{_B}.gd_typdoc_latest")
GD_UTILISATEUR_LATEST = _env("PARSING_GD_UTILISATEUR_LATEST",  f"{_B}.gd_utilisateur_latest")

# The daily job fails the run if this table shows a stale source, instead of silently indexing a partial corpus.
INGESTION_FRESHNESS_TABLE = _env("PARSING_INGESTION_FRESHNESS_TABLE",
                                 f"{_B}.intraqual_ingestion_freshness_all")
MAX_SOURCE_STALENESS_HOURS = int(_env("PARSING_MAX_SOURCE_STALENESS_HOURS", "36"))

# =============================================================================
# Qualibot perimeter (scope gate)
# =============================================================================
# Do not add difftotale=1 back (excludes legitimate docs); v_qualibot_latest's MV returns only the latest delta, not full scope.
DOC_SCOPE_FILTER = _env(
    "PARSING_DOC_SCOPE_FILTER",
    "courant = 1 AND etat = 7 AND nonvisible = 0",
)

# =============================================================================
# Legacy fallbacks (disabled by default)
# =============================================================================
# Empty = disabled. Kept as variables to re-enable via --var if a gap shows up.
GD_DOC_FALLBACK          = _env("PARSING_GD_DOC_FALLBACK",          "")
DIVISION_ARCHIVE_TABLE   = _env("PARSING_DIVISION_ARCHIVE_TABLE",   "")
DIVISION_REFERENCE_TABLE = _env("PARSING_DIVISION_REFERENCE_TABLE",
                                f"{CATALOG_SCHEMA}.category_reference{TABLE_SUFFIX}")

# Pre-computed by 2_Cleanup_Volume — avoids querying gd_doc/gd_cat/gd_typdoc directly here.
PARSE_MANIFEST_TABLE  = _env("PARSING_PARSE_MANIFEST_TABLE",
                             f"{CATALOG_SCHEMA}.parse_manifest{TABLE_SUFFIX}")

# Volumes
VOLUME_ROOT_PATH      = _require_env("PARSING_VOLUME_ROOT_PATH")
VOLUME_BASE_PATH      = _require_env("PARSING_VOLUME_BASE_PATH")
OFFLINE_MODELS_DIR    = _require_env("PARSING_OFFLINE_MODELS")

# Relative to REPO_DIR (utils/parsing_pipeline/, two levels below the repo root that vendors antiword).
ANTIWORD_BIN_RELATIVE   = "../../data/vendor/antiword/antiword_local/usr/bin/antiword"
ANTIWORD_SHARE_RELATIVE = "../../data/vendor/antiword/antiword_local/usr/share/antiword"

# =============================================================================
# Target Delta tables
# =============================================================================
TABLE_SUFFIX = _env("PARSING_TABLE_SUFFIX", "")

TARGET_PROCESSED_FILES_TABLE = f"{CATALOG_SCHEMA}.processed_files{TABLE_SUFFIX}"

TARGET_CHUNK_TABLE     = f"{CATALOG_SCHEMA}.chunks{TABLE_SUFFIX}"          # ground truth: all divisions
TARGET_CHUNK_TABLE_AS  = f"{CATALOG_SCHEMA}.src_chunks_as{TABLE_SUFFIX}"   # = chunks filtered to division AS
TARGET_CHUNK_TABLE_IS  = f"{CATALOG_SCHEMA}.src_chunks_is{TABLE_SUFFIX}"
# Chunks of documents published before DOC_DATE_CUTOFF: kept out of the RAG tables above,
# merged with `chunks` into `chunks_full` by 5_Sync_Vector_Indexes for the impact-search index.
TARGET_CHUNK_TABLE_ARCHIVE = f"{CATALOG_SCHEMA}.chunks_archive{TABLE_SUFFIX}"   # = chunks filtered to division IS

TARGET_IMAGE_METADATA_TABLE  = f"{CATALOG_SCHEMA}.image_metadata{TABLE_SUFFIX}"
TARGET_AUDIT_TABLE           = f"{CATALOG_SCHEMA}.audit_files_unified{TABLE_SUFFIX}"
TARGET_HEALTH_TABLE          = f"{CATALOG_SCHEMA}.parsing_run_health{TABLE_SUFFIX}"  # one row per pipeline run, for the monitoring dashboard
TARGET_CHANGE_LOG_TABLE      = f"{CATALOG_SCHEMA}.document_change_log{TABLE_SUFFIX}"  # one row per NEW/REVISED document, for the monitoring dashboard

# =============================================================================
# Date cutoff
# =============================================================================
# Docs before this date are parsed like the others but their chunks go to
# TARGET_CHUNK_TABLE_ARCHIVE instead of the RAG chunk tables (processed_files:
# filtered_by_date=True, include_in_rag=False). No DATEDIFF = unknown date = recent.
DOC_DATE_CUTOFF = _env("PARSING_DOC_DATE_CUTOFF", "2018-01-01")

# Total number of pre-cutoff documents allowed to be parsed, most recent first
# (cumulative, not per run): 0 = none, N = the N most recent, -1 = all.
# 2_Cleanup_Volume turns it into parse_manifest.parse_content.
ARCHIVE_MAX_DOCS = int(_env("PARSING_ARCHIVE_MAX_DOCS", "0") or "0")

# One metadata-only "notice" chunk per pre-cutoff document (REF, title, revision,
# date — no content), rebuilt on every run into TARGET_ARCHIVE_NOTICE_TABLE.
# Copied into the RAG chunk tables (chatbot indexes) only when ARCHIVE_NOTICES_IN_RAG is on.
TARGET_ARCHIVE_NOTICE_TABLE = f"{CATALOG_SCHEMA}.chunks_archive_notices{TABLE_SUFFIX}"
ARCHIVE_NOTICES_IN_RAG = _env("PARSING_ARCHIVE_NOTICES_IN_RAG", "false").strip().lower() in ("1", "true", "yes")
# Also hardcoded in 5_Sync_Vector_Indexes.py (serverless, no config.py) and matched
# by ARCHIVE_NOTICE_MARKER in server/services/vector_search.py.
ARCHIVE_NOTICE_CONTENT_TYPE = "archive_notice"
ARCHIVE_NOTICE_MARKER = "ARCHIVED DOCUMENT — CONTENT NOT INDEXED"

# =============================================================================
# Manual REF exclusions — reviewed by hand, add sparingly. Excluded permanently
# as SKIPPED_REF_MANUAL by 2_Cleanup_Volume.py.
# =============================================================================
MANUAL_REF_EXCLUSIONS = set()

# =============================================================================
# Empty + small file auto-exclusion
# =============================================================================
# Zero-text + small file = blank stub (verified by hand) -> auto-skip permanently; zero-text ABOVE this size stays ERROR for review.
EMPTY_SMALL_FILE_SIZE_BYTES = int(_env("PARSING_EMPTY_SMALL_FILE_SIZE_BYTES", "51200"))  # 50 KiB

# =============================================================================
# Run mode
# =============================================================================
RUN_MODE = _env("PARSING_RUN_MODE", "incremental")  # "incremental" | "full"

# =============================================================================
# Scope filter (None = all IDDOCs, list = specific)
# Overridable via PARSING_PARSE_FILTER="123,456" for a targeted (test) run.
# =============================================================================
PARSE_FILTER = [int(x) for x in _env("PARSING_PARSE_FILTER", "").split(",") if x.strip().isdigit()] or None

# =============================================================================
# Feature flags
# =============================================================================
ENABLE_AUDIT       = True
ENABLE_RETRY       = True
ENABLE_TIMING_TEST = False  # gates per-step timing instrumentation in the parse UDF (utils.py/image_utils.py)

# =============================================================================
# Resilience -- batch checkpointing & per-file timeout
# =============================================================================
CHECKPOINT_BATCH_SIZE   = 100   # Write to Delta every N files (crash-safe)
PARSE_TIMEOUT_SECONDS   = 200   # Max seconds per file -- beyond this, mark as TIMEOUT
MAX_CHUNKS_SPREADSHEET  = 100   # Limit chunks for xlsx/xls/xlsm (None = no limit)

# =============================================================================
# Chunk cleaning flags
# =============================================================================
CLEAN_IMAGE_PLACEHOLDERS = True
CLEAN_FORMULA_ARTIFACTS  = True
DEDUPLICATE_CHUNKS       = True

# =============================================================================
# Docling engine settings
# =============================================================================
# USE_GPU: explicit override via PARSING_USE_GPU, else auto-detected (nvidia-smi).
def _detect_gpu():
    import shutil as _sh, subprocess as _sp
    if _sh.which("nvidia-smi") is None:
        return False
    try:
        _sp.check_output(["nvidia-smi"]); return True
    except Exception:
        return False
_use_gpu_env = _os.environ.get("PARSING_USE_GPU")
USE_GPU = (_use_gpu_env.strip().lower() in ("1", "true", "yes")) if _use_gpu_env else _detect_gpu()
DO_OCR                  = False
# Docling's own do_ocr=True re-parse (EasyOCR, GPU) -- distinct from LLM_OCR_*
# below (the LLM-vision transcription fallback, which IS used). Disabled:
# checked corpus-wide, this GPU OCR path's "docling+ocr:pdf" strategy has
# never once won out over the plain parse (0 of ~18k docs) -- pure GPU cost for
# zero benefit. The scanned_page render + LLM-vision fallback (image_utils.py)
# covers this case instead, off-GPU.
GPU_OCR_FALLBACK        = False
TABLE_STRUCTURE_MODE    = "accurate"   # "accurate" | "fast"
GENERATE_PICTURE_IMAGES = True
IMAGE_SCALE             = 3.0          # ~216 DPI -- captures fine text in diagrams
MIN_AREA_RATIO          = 0.05
MAX_REPEAT              = 2

# =============================================================================
# Image save settings
# =============================================================================
IMAGE_MAX_DIMENSION = 4096      # preserves full detail for large schematics
IMAGE_JPEG_QUALITY  = 95        # near-lossless (only used if IMAGE_FORMAT=JPEG)
IMAGE_RESAMPLING    = "LANCZOS" # sharpest downscale algorithm
IMAGE_FORMAT        = "PNG"     # PNG = lossless, no compression artifacts on text/diagrams

# =============================================================================
# Tokenizer / Chunking
# =============================================================================
# tiktoken (cl100k) is more accurate than CHARS_PER_TOKEN for technical French; falls back to it if unavailable offline.
USE_TIKTOKEN        = True
CHARS_PER_TOKEN     = 3.5
# Passage sizes (chunking.py). Overridable per run to test other sizes (DEV notebook
# utils/databricks_ops/evaluation/rechunk_experiment.py) without editing this file.
# 150 / 300 / 450 tokens, 1,600 characters = DEV variant v2b, chosen 2026-10-08: 80.9 % of the
# expected documents found vs 75.8 % for the former 250 / 500 / 1000 / 4000, with half the context
# (retrieval_eval, u-all, 65 questions; docs/chat_vsi_audit_2026-10.md § 5.6).
MIN_CHUNK_TOKENS    = int(_env("PARSING_MIN_CHUNK_TOKENS", "150"))
TARGET_CHUNK_TOKENS = int(_env("PARSING_TARGET_CHUNK_TOKENS", "300"))
MAX_CHUNK_TOKENS    = int(_env("PARSING_MAX_CHUNK_TOKENS", "450"))
# Character ceiling on top of tokens: dot leaders / form underscores count few tokens for many
# characters, and the Vector Search reranker reads only the first 2,000 characters.
MAX_CHUNK_CHARS     = int(_env("PARSING_MAX_CHUNK_CHARS", "1600"))
# Overlap between consecutive passages of one section (fraction of TARGET).
CHUNK_OVERLAP_RATIO = float(_env("PARSING_CHUNK_OVERLAP_RATIO", "0.12"))
# A passage body found in at least this many documents is marked "boilerplate".
BOILERPLATE_MIN_DOCS = int(_env("PARSING_BOILERPLATE_MIN_DOCS", "20"))

# =============================================================================
# LLM settings (vision model for image description)
# =============================================================================
LLM_MODEL_ENDPOINT  = _env("PARSING_LLM_ENDPOINT", "databricks-gpt-5-6-luna")
LLM_MAX_TOKENS      = 5000    # GPT-5 reasoning tokens need headroom beyond the visible output
LLM_TEMPERATURE     = 1.0   # gpt-5 family: temperature=1 only
LLM_MAX_RETRIES     = 5
LLM_MAX_CONCURRENT  = 10   # reduced from 20 to avoid OOM on m5d.xlarge single-node (Bug3 fix)
# Kept below the batch size that destabilizes the kernel.
LLM_BATCH_SIZE      = int(_env("PARSING_LLM_BATCH_SIZE", "6000"))

# Persists progress periodically instead of one MERGE at the very end of a multi-hour run.
LLM_CHECKPOINT_CHUNK_SIZE = int(_env("PARSING_LLM_CHECKPOINT_CHUNK_SIZE", "2000"))

# Endpoint rate limits, budgeted below the real ceiling since other app features share it.
LLM_ITPM_BUDGET     = int(_env("PARSING_LLM_ITPM_BUDGET", "1000000"))  # real limit: 2,000,000
LLM_OTPM_BUDGET     = int(_env("PARSING_LLM_OTPM_BUDGET", "100000"))   # real limit: 200,000 <- bottleneck
LLM_QPH_BUDGET      = int(_env("PARSING_LLM_QPH_BUDGET",  "324000"))   # real limit: 360,000
# Calibrated avg tokens/image, drives RPM_safe = min(budget/avg) across all three axes.
LLM_AVG_INPUT_TOKENS  = int(_env("PARSING_LLM_AVG_IN",  "1050"))
LLM_AVG_OUTPUT_TOKENS = int(_env("PARSING_LLM_AVG_OUT", "300"))

# =============================================================================
# Image filtering (deterministic — applied WITHOUT any LLM call)
# =============================================================================
# Deliberately conservative — no filtering on a large width/height ratio.
IMG_SKIP_MAX_DIM   = 140   # longer side (px) < threshold -> illegible thumbnail -> skip
IMG_SKIP_MIN_SIDE  = 85    # shorter side (px) < threshold -> band/table too short -> skip

# Post-filter: minimum useful length of a description to be injected as a chunk.
MIN_INDEXABLE_DESC_CHARS = 40

# True = provenance prefix embedded in chunk_text; False = body only (ref/division/title stay as filterable columns) — measured to hurt retrieval precision.
EMBED_SOURCE_PREFIX = _env("PARSING_EMBED_SOURCE_PREFIX", "true").strip().lower() in ("1", "true", "yes")

# =============================================================================
# LLM Prompt template (vision model)
# =============================================================================
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

# =============================================================================
# LLM-OCR fallback for scanned PDFs
# =============================================================================
# Triggers when Docling still yields near-empty text: pages are rendered to images and queued through the same PENDING-image pipeline, using the prompt below (full transcription, never SKIP — even a stamp-only page is worth indexing).
# 20 was too low: real digital PDFs where Docling barely parses anything (a
# stub of 14-46 chars, 0 images) clear that floor and never reach this
# fallback, staying invisible SUCCESS rows with effectively no indexed content.
LLM_OCR_TEXT_THRESHOLD = int(_env("PARSING_LLM_OCR_TEXT_THRESHOLD", "150"))
LLM_OCR_MAX_PAGES      = int(_env("PARSING_LLM_OCR_MAX_PAGES", "100"))
# Full-page transcription runs longer than a short image description and was
# hitting the shared LLM_MAX_TOKENS cap (2048) on dense pages, truncating
# mid-transcription -- give it its own, more generous budget.
LLM_OCR_MAX_TOKENS     = int(_env("PARSING_LLM_OCR_MAX_TOKENS", "8192"))

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
