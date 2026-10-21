"""worker.py — runtime shared by the driver and the executors: logger, worker configuration, token counting, and the
Spark helpers (chunk text, REF url, pandas UDFs).

`configure()` runs on the driver; the workers restore CONFIG with `ensure_config()` (broadcast, then spark.conf JSON,
then a JSON file on the volume) or fall back to `_DEFAULTS`. Format parsing lives in `format_parsers.py`,
passage splitting in `chunking.py`.
"""

import json
import logging
import os
import re
from typing import Any, Dict, List

import pandas as pd

import chunking  # shipped with addPyFile, like this module

from pyspark.sql import Column
from pyspark.sql import functions as F
from pyspark.sql import types as T
from pyspark.sql.functions import pandas_udf

# Only our module logger is configured, never the root logger (root=INFO leaks py4j/pyspark chatter to stdout).
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
if not logger.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S"))
    logger.addHandler(_handler)
    logger.propagate = False

for _noisy in ("py4j", "py4j.clientserver", "py4j.java_gateway", "pyspark", "docling", "transformers"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)


# --- Configuration ---
# _DEFAULTS is the worker fallback; the notebook injects the real config via configure().
_DEFAULT_VOLUME_BASE_PATH = os.environ.get("PARSING_VOLUME_BASE_PATH")

_DEFAULTS: Dict[str, Any] = {
    # None (not a hardcoded path) if unset, so a bad fallback fails loudly downstream.
    "OFFLINE_MODELS_DIR": os.environ.get("PARSING_OFFLINE_MODELS"),
    "ANTIWORD_BIN": None,
    "ANTIWORD_SHARE_DIR": None,
    "VOLUME_BASE_PATH": _DEFAULT_VOLUME_BASE_PATH,
    # Engine knobs (balanced defaults)
    "USE_GPU": False,
    "DO_OCR": False,                      # True only if scanned PDFs are expected
    "GPU_OCR_FALLBACK": False,             # Docling's own do_ocr=True re-parse (EasyOCR, GPU) -- distinct from the LLM-OCR fallback
    "TABLE_STRUCTURE_MODE": "accurate",   # "accurate" | "fast"
    "GENERATE_PICTURE_IMAGES": True,      # set False when not describing images
    "IMAGE_SCALE": 0.75,
    # Image extraction
    "MIN_AREA_RATIO": 0.03,
    "MAX_REPEAT": 3,
    # Tokenizer
    "TOKENIZER_ENCODING": "cl100k_base",
    "USE_TIKTOKEN": False,
    "CHARS_PER_TOKEN": 3.5,
    # Chunking
    "MIN_CHUNK_TOKENS": 250,
    "TARGET_CHUNK_TOKENS": 500,
    "MAX_CHUNK_TOKENS": 1000,
    "MAX_CHUNK_CHARS": 4000,
    "CHUNK_OVERLAP_RATIO": 0.12,
    # Worker-config fallback file (must match VOLUME_BASE_PATH/_parsing_config.json)
    "WORKER_CONFIG_JSON": os.path.join(_DEFAULT_VOLUME_BASE_PATH, "_parsing_config.json"),
}

CONFIG: Dict[str, Any] = {}


def configure(**kwargs) -> None:
    """Set run configuration from the notebook. Call on the driver before parsing.

    :param kwargs: Configuration keys and values, as the notebook sets them.
    """
    CONFIG.update(kwargs)
    logger.info("worker.configure() called with %d keys", len(kwargs))


_CONFIG_BROADCAST = None


def broadcast_config(spark_session) -> None:
    """Broadcast CONFIG to all workers. Call once on the driver after configure().

    :param spark_session: Active Spark session.
    """
    global _CONFIG_BROADCAST
    _CONFIG_BROADCAST = spark_session.sparkContext.broadcast(dict(CONFIG))
    logger.info("CONFIG broadcast (%d keys)", len(CONFIG))


def write_worker_config(spark_session) -> str:
    """Persist CONFIG to a JSON file on the volume and to spark.conf, for the workers of shared clusters.
    Returns the JSON path written.

    :param spark_session: Active Spark session; its ``spark.conf`` receives the JSON as well.
    """
    path = _cfg("WORKER_CONFIG_JSON", fallback=os.path.join(_cfg("VOLUME_BASE_PATH"), "_parsing_config.json"))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(CONFIG, f)
    try:
        spark_session.conf.set("app.parsing.config_json", json.dumps(CONFIG))
    except Exception:
        pass
    logger.info("Worker config written to %s (%d keys)", path, len(CONFIG))
    return path


def ensure_config() -> None:
    """Restore CONFIG on a worker (idempotent): broadcast → spark.conf → Volume JSON."""
    if CONFIG:
        return
    if _CONFIG_BROADCAST is not None:
        CONFIG.update(_CONFIG_BROADCAST.value)
        return
    try:
        from pyspark.sql import SparkSession
        _spark = SparkSession.getActiveSession()
        if _spark:
            cfg_json = _spark.conf.get("app.parsing.config_json", "")
            if cfg_json:
                CONFIG.update(json.loads(cfg_json))
                return
    except Exception:
        pass
    try:
        # A cluster env var, visible in every process unlike CONFIG.
        volume_base = os.environ.get("PARSING_VOLUME_BASE_PATH")
        vol_cfg = os.path.join(volume_base, "_parsing_config.json") if volume_base \
            else _DEFAULTS["WORKER_CONFIG_JSON"]
        if os.path.isfile(vol_cfg):
            with open(vol_cfg) as f:
                CONFIG.update(json.loads(f.read()))
    except Exception:
        pass  # _cfg() falls back to _DEFAULTS


def _cfg(key: str, fallback=None) -> Any:
    """Read a config value: CONFIG → _DEFAULTS → fallback → error.

    :param key: Configuration key.
    :param fallback: Value returned when neither CONFIG nor _DEFAULTS holds the key; None means no fallback.
    """
    if key in CONFIG:
        return CONFIG[key]
    if key in _DEFAULTS and _DEFAULTS[key] is not None:
        return _DEFAULTS[key]
    if fallback is not None:
        return fallback
    if key in _DEFAULTS:           # default is legitimately None
        return None
    raise RuntimeError(f"Configuration key '{key}' not set and no default exists.")


# --- Text helpers ---
def normalize_text(text: str) -> str:
    """Unify line endings, drop NUL characters, collapse runs of spaces/tabs and of blank lines.

    :param text: Raw text; an empty value gives an empty string.
    """
    if not text:
        return ""
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "")
    text = re.sub(r"[ \t]+", " ", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def get_tiktoken_encoder() -> Any:
    """The tiktoken encoder, built once per process (cl100k_base unless configured)."""
    global _TIKTOKEN_ENCODER
    if _TIKTOKEN_ENCODER is None:
        import tiktoken
        _TIKTOKEN_ENCODER = tiktoken.get_encoding(_cfg("TOKENIZER_ENCODING", fallback="cl100k_base"))
    return _TIKTOKEN_ENCODER


def count_tokens(text: str) -> int:
    """Token count via tiktoken (USE_TIKTOKEN=True) or a chars/token estimate.

    :param text: Text to measure; an empty text gives 0.
    """
    if not text:
        return 0
    if _cfg("USE_TIKTOKEN", fallback=False):
        try:
            return len(get_tiktoken_encoder().encode(text))
        except Exception:
            pass
    return max(1, int(len(text) / _cfg("CHARS_PER_TOKEN", fallback=3.5)))


# --- Spark helpers ---
# --- Spark schemas ---
CHUNK_SCHEMA = T.ArrayType(T.StructType([
    T.StructField("chunk_index", T.IntegerType(), True),
    T.StructField("chunk_text", T.StringType(), True),
    T.StructField("chunk_char_count", T.IntegerType(), True),
    T.StructField("chunk_token_count", T.IntegerType(), True),
    T.StructField("chunk_content_type", T.StringType(), True),
    T.StructField("metadata", T.MapType(T.StringType(), T.StringType()), True),
]))


_INTRAQUAL_REF_URL_BASE = "https://intraqual.lat.corp/intraqual_prod/identification.aspx?ref="


def intraqual_ref_url(ref_col) -> Column:
    """Document URL built directly from REF, no lookup table needed.

    :param ref_col: Column of REF values.
    :return: column with the Intraqual URL of each REF, null when REF is null.
    """
    return F.when(
        ref_col.isNotNull(),
        F.concat(F.lit(_INTRAQUAL_REF_URL_BASE), F.regexp_replace(ref_col, " ", "%20")),
    )


def source_prefixed_text(body_col, ref_col, titre_col, division_col, category_col,
                          doc_date_col=None, include_prefix=True, type_col=None) -> Column:
    """chunk_text with an optional "[Source: ...]" provenance prefix (include_prefix).
    type_col (document type label, e.g. "05 - Procédure - QP") adds a "Type:" field.

    :param body_col: Column with the passage body.
    :param ref_col: Column of REF values.
    :param titre_col: Column of document titles.
    :param division_col: Column of divisions.
    :param category_col: Column of categories.
    :param doc_date_col: Column of publication dates, or None to omit the date.
    :param include_prefix: False returns the body column unchanged.
    :param type_col: Column of document type labels, or None to omit the type.
    :return: column with the prefix followed by the body.
    """
    if not include_prefix:
        return body_col
    date_part = (
        F.concat(F.lit(" | Date de diffusion: "), F.coalesce(F.date_format(doc_date_col, "yyyy-MM-dd"), F.lit("inconnue")))
        if doc_date_col is not None else F.lit("")
    )
    type_part = (F.concat(F.lit(" | Type: "), F.coalesce(type_col, F.lit("")))
                 if type_col is not None else F.lit(""))
    return F.concat(
        F.lit("[Source: "), F.coalesce(ref_col, F.lit("")),
        F.lit(" | Title: "), F.coalesce(titre_col, F.lit("")),
        type_part,
        F.lit(" | Division: "), F.coalesce(division_col, F.lit("")),
        F.lit(" | Category: "), F.coalesce(category_col, F.lit("")),
        date_part,
        F.lit("]\n\n"), body_col,
    )


def chunk_document(text: str) -> List[Dict[str, Any]]:
    """Passages of an already-parsed markdown document (``chunking.chunk_markdown``).

    :param text: Markdown text of the parsed document.
    """
    text = normalize_text(text)
    if not text:
        return []
    return chunking.chunk_markdown(
        text,
        min_tokens=int(_cfg("MIN_CHUNK_TOKENS", fallback=250)),
        target_tokens=int(_cfg("TARGET_CHUNK_TOKENS", fallback=500)),
        max_tokens=int(_cfg("MAX_CHUNK_TOKENS", fallback=1000)),
        max_chars=int(_cfg("MAX_CHUNK_CHARS", fallback=4000)),
        overlap_ratio=float(_cfg("CHUNK_OVERLAP_RATIO", fallback=0.12)),
        chars_per_token=float(_cfg("CHARS_PER_TOKEN", fallback=3.5)),
        count_tokens=count_tokens,
    )


# --- PySpark pandas UDFs ---
@pandas_udf(CHUNK_SCHEMA)
def build_chunks_udf(text_series: pd.Series) -> pd.Series:
    """Chunk already-parsed markdown text (chunking.chunk_markdown).

    :param text_series: Column of markdown texts, one per row.
    :return: chunk structs per row.
    """
    ensure_config()
    return pd.Series([
        chunk_document(str(t)) if t else [] for t in text_series
    ])


@pandas_udf(T.StringType())
def language_udf(text_series: pd.Series, ref_series: pd.Series) -> pd.Series:
    """Document language (chunking.detect_language): REF suffix, else stop words.

    :param text_series: Column of document texts.
    :param ref_series: Column of REF values.
    :return: language code per row.
    """
    return pd.Series([chunking.detect_language(str(t or ""), str(r or ""))
                      for t, r in zip(text_series, ref_series)])


@pandas_udf(T.IntegerType())
def token_count_udf(text_series: pd.Series) -> pd.Series:
    """Pandas UDF: token count of each text (0 when empty), with the settings restored on the worker.

    :param text_series: Column of texts, one per row.
    :return: token count per row.
    """
    ensure_config()
    return text_series.apply(
        lambda x: count_tokens(str(x)) if pd.notna(x) and str(x).strip() else 0
    ).astype("int32")
