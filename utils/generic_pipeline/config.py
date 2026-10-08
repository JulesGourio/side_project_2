"""config.py — Single source of truth for all generic pipeline parameters.

Mirrors the structure of parsing_pipeline/config.py, stripped of every
Intraqual-specific variable (gd_doc, gd_cat, scope gate, division
hierarchy, etc.).

Priority for every parameter: env var > explicit override in notebook > this default.
"""

import os as _os

def _env(key, default):
    """Return env var if set, otherwise default."""
    return _os.environ.get(key, default)

def _require_env(key):
    """Return env var; raise if unset — no environment-specific default."""
    val = _os.environ.get(key)
    if not val:
        raise RuntimeError(f"{key} must be set (see job YAML or notebook widgets)")
    return val


# =============================================================================
# Catalog / Schema / Paths
# =============================================================================
CATALOG_SCHEMA = _require_env("GENERIC_CATALOG_SCHEMA")

# Appended to every table the pipeline writes.
TABLE_SUFFIX = _env("GENERIC_TABLE_SUFFIX", "")

# Volumes
VOLUME_ROOT_PATH  = _require_env("GENERIC_VOLUME_ROOT_PATH")
VOLUME_BASE_PATH  = _require_env("GENERIC_VOLUME_BASE_PATH")
OFFLINE_MODELS_DIR = _require_env("GENERIC_OFFLINE_MODELS")

# Relative to REPO_DIR (same vendor tree as parsing_pipeline).
ANTIWORD_BIN_RELATIVE   = "../parsing_pipeline/../../data/vendor/antiword/antiword_local/usr/bin/antiword"
ANTIWORD_SHARE_RELATIVE = "../parsing_pipeline/../../data/vendor/antiword/antiword_local/usr/share/antiword"

# =============================================================================
# Target Delta tables
# =============================================================================
TARGET_PROCESSED_FILES_TABLE = f"{CATALOG_SCHEMA}.processed_files{TABLE_SUFFIX}"
TARGET_CHUNK_TABLE           = f"{CATALOG_SCHEMA}.chunks{TABLE_SUFFIX}"
TARGET_IMAGE_METADATA_TABLE  = f"{CATALOG_SCHEMA}.image_metadata{TABLE_SUFFIX}"

# =============================================================================
# Run mode
# =============================================================================
RUN_MODE = _env("GENERIC_RUN_MODE", "incremental")  # "incremental" | "full"

# Overridable via GENERIC_PARSE_FILTER="file1.pdf,file2.docx" for a targeted run.
PARSE_FILTER = [x.strip() for x in _env("GENERIC_PARSE_FILTER", "").split(",") if x.strip()] or None

# =============================================================================
# Feature flags
# =============================================================================
ENABLE_AUDIT       = True
ENABLE_RETRY       = True
ENABLE_TIMING_TEST = False

# =============================================================================
# Resilience
# =============================================================================
CHECKPOINT_BATCH_SIZE   = 100
PARSE_TIMEOUT_SECONDS   = 200
MAX_CHUNKS_SPREADSHEET  = 100

# =============================================================================
# Chunk cleaning flags
# =============================================================================
CLEAN_IMAGE_PLACEHOLDERS = True
CLEAN_FORMULA_ARTIFACTS  = True
DEDUPLICATE_CHUNKS       = True

# =============================================================================
# Empty + small file auto-exclusion
# =============================================================================
EMPTY_SMALL_FILE_SIZE_BYTES = int(_env("GENERIC_EMPTY_SMALL_FILE_SIZE_BYTES", "51200"))

# =============================================================================
# Docling engine settings
# =============================================================================
def _detect_gpu():
    import shutil as _sh, subprocess as _sp
    if _sh.which("nvidia-smi") is None:
        return False
    try:
        _sp.check_output(["nvidia-smi"]); return True
    except Exception:
        return False
_use_gpu_env = _os.environ.get("GENERIC_USE_GPU")
USE_GPU = (_use_gpu_env.strip().lower() in ("1", "true", "yes")) if _use_gpu_env else _detect_gpu()
DO_OCR                  = False
GPU_OCR_FALLBACK        = False
TABLE_STRUCTURE_MODE    = "accurate"
GENERATE_PICTURE_IMAGES = True
IMAGE_SCALE             = 3.0
MIN_AREA_RATIO          = 0.05
MAX_REPEAT              = 2

# =============================================================================
# Image save settings
# =============================================================================
IMAGE_MAX_DIMENSION = 4096
IMAGE_JPEG_QUALITY  = 95
IMAGE_RESAMPLING    = "LANCZOS"
IMAGE_FORMAT        = "PNG"

# =============================================================================
# Tokenizer / Chunking
# =============================================================================
USE_TIKTOKEN        = True
CHARS_PER_TOKEN     = 3.5
MIN_CHUNK_TOKENS    = 250
TARGET_CHUNK_TOKENS = 500
MAX_CHUNK_TOKENS    = 1000
CHUNK_OVERLAP_RATIO = 0.12

# =============================================================================
# LLM settings (vision model for image description)
# =============================================================================
LLM_MODEL_ENDPOINT  = _env("GENERIC_LLM_ENDPOINT", "databricks-gpt-5-6-luna")
LLM_MAX_TOKENS      = 5000
LLM_TEMPERATURE     = 1.0
LLM_MAX_RETRIES     = 5
LLM_MAX_CONCURRENT  = 10
LLM_BATCH_SIZE      = int(_env("GENERIC_LLM_BATCH_SIZE", "6000"))
LLM_CHECKPOINT_CHUNK_SIZE = int(_env("GENERIC_LLM_CHECKPOINT_CHUNK_SIZE", "2000"))

# Endpoint rate limits.
LLM_ITPM_BUDGET     = int(_env("GENERIC_LLM_ITPM_BUDGET", "1000000"))
LLM_OTPM_BUDGET     = int(_env("GENERIC_LLM_OTPM_BUDGET", "100000"))
LLM_QPH_BUDGET      = int(_env("GENERIC_LLM_QPH_BUDGET",  "324000"))
LLM_AVG_INPUT_TOKENS  = int(_env("GENERIC_LLM_AVG_IN",  "1050"))
LLM_AVG_OUTPUT_TOKENS = int(_env("GENERIC_LLM_AVG_OUT", "300"))

# =============================================================================
# Image filtering (deterministic — applied WITHOUT any LLM call)
# =============================================================================
IMG_SKIP_MAX_DIM   = 140
IMG_SKIP_MIN_SIDE  = 85
MIN_INDEXABLE_DESC_CHARS = 40
EMBED_SOURCE_PREFIX = True

# =============================================================================
# LLM-OCR fallback for scanned PDFs
# =============================================================================
LLM_OCR_TEXT_THRESHOLD = int(_env("GENERIC_LLM_OCR_TEXT_THRESHOLD", "150"))
LLM_OCR_MAX_PAGES      = int(_env("GENERIC_LLM_OCR_MAX_PAGES", "100"))
LLM_OCR_MAX_TOKENS     = int(_env("GENERIC_LLM_OCR_MAX_TOKENS", "8192"))

# =============================================================================
# LLM Prompt templates (vision model)
# =============================================================================
# Same prompts as parsing_pipeline — generic enough for any document.
IMAGE_DESCRIPTION_PROMPT = """You are a vision extraction engine for a RAG index. You are given an IMAGE plus some surrounding document text (CONTEXT). The context is ALREADY indexed separately, so do not repeat it.

DOMAIN HINTS (use only to resolve acronyms/jargon, never echo back):
- Document title: {doc_title}
- Nearby text: {context}

Your entire answer MUST begin with either "SKIP:" or "#". Write nothing before it (no letter, no preamble).

If the image is decorative or has no extractable information — logo, letterhead/banner, signature, generic photo of people or buildings, separator line, blank, or a screenshot of a generic software UI — answer with exactly one line and stop:
SKIP: then a reason in 6 words max

Otherwise answer in the document's language (French if the context is French), with no other text, using this exact structure:
- first line: "# " then the CATEGORY in square brackets, then one sentence describing what the image shows;
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

Do NOT copy or paraphrase the CONTEXT text: if everything you could write is already in the context, answer SKIP instead. No notes about the document, no mention of document title."""

PDF_PAGE_OCR_PROMPT = """You are a vision transcription engine for a RAG index. You are given a page IMAGE from a document that could not be text-extracted directly (e.g. a scan). Transcribe it as faithfully and completely as possible — do not summarize, do not skip any part of the page.

DOMAIN HINTS (use only to resolve acronyms/jargon, never echo back):
- Document title: {doc_title}
- Nearby text: {context}

Answer in the document's language (French if the context is French), with no preamble, using this exact structure:
- first line: "# " then one short sentence describing what kind of page this is (form, table, technical drawing, letter, etc.);
- second line: **Content:**
- then the full transcription: body text, table contents (as markdown tables), headers/footers, handwritten notes if legible.

Critical: transcribe any stamp, watermark, hand-written annotation, or overlay text VERBATIM and call it out explicitly on its own line prefixed "STAMP/WATERMARK:".

Never answer SKIP: even a page containing only a stamp or a mostly-blank form still carries real information and must be transcribed."""
