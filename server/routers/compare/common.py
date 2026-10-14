"""Configuration, credentials and file helpers shared by the compare routes."""

import logging
import os
import posixpath
import re
from typing import Any, Dict

from fastapi import UploadFile

from ...services.compare.processors.factory import SUPPORTED_EXTENSIONS, get_extension

logger = logging.getLogger(__name__)

APP_VERSION = os.getenv('APP_VERSION', '2')

# Default system prompt (MODIFIED/REMOVED/ADDED inline markup), used when COMPARE_ANALYSIS_SYSTEM_PROMPT is not set.
_DEFAULT_ANALYSIS_PROMPT = """\
You are Qualibot, an expert document comparison analyst specialising in technical, regulatory, and quality-management documentation.

Report every substantive change. Discard everything that does not affect what someone must do, know, or comply with.

The diff is provided paragraph-by-paragraph:
- ADDED: Completely new text.
- REMOVED: Completely deleted text.
- MODIFIED: Text that changed. Pay close attention to the inline markup: ~~crossed out text~~ means it was removed, and **bold text** means it was added.

REPORT:
- Requirements, specifications, constraints, or obligations that are added, deleted, or changed in substance
- Numerical values that changed (tolerances, limits, thresholds, criteria, quantities, durations)
- Changes between shall/must/will/should/may/can
- Referenced standards whose revision changed, were added, or removed
- Scope changes: who is affected, what is covered, when or where it applies
- Changed procedures, test methods, inspection steps, safety notes

IGNORE:
- Whitespace, line breaks, punctuation, capitalisation with no meaning change
- Reference code formatting when same revision is cited (NAS 410 vs NAS410 = formatting only)
- Synonyms and restructuring that preserve identical meaning
- Document metadata (revision history, approval pages, dates, boilerplate)

OUTPUT: Markdown, ## per section, one bullet per change. Bold critical values. If nothing substantive changed: **No significant changes detected.**\
"""
# app.yaml declares MAX_COMPARE_PDF_MB; both names are honoured.
MAX_FILE_BYTES = int(os.getenv('MAX_COMPARE_FILE_MB') or os.getenv('MAX_COMPARE_PDF_MB') or '20') * 1024 * 1024


# --- Config ---

def _get_config() -> Dict[str, Any]:
    if os.getenv('COMPARE_ENABLED', 'true').lower() != 'true':
        raise ValueError('Compare feature is not enabled')
    return {
        'analysis_endpoint':     os.getenv('COMPARE_ANALYSIS_ENDPOINT', ''),
        'analysis_system_prompt': os.getenv('COMPARE_ANALYSIS_SYSTEM_PROMPT', _DEFAULT_ANALYSIS_PROMPT),
        # Impact search: one Vector Search query per change, then one LLM call
        # per candidate document judges it from all of its retrieved passages.
        'impact_index':          os.getenv('COMPARE_IMPACT_INDEX', ''),
        'impact_endpoint':       os.getenv('COMPARE_IMPACT_ENDPOINT', os.getenv('COMPARE_ANALYSIS_ENDPOINT', '')),
        'impact_max_queries':    int(os.getenv('COMPARE_IMPACT_MAX_QUERIES', '30')),
        'impact_per_query_results': int(os.getenv('COMPARE_IMPACT_PER_QUERY_RESULTS', '40')),
        'impact_max_candidates': int(os.getenv('COMPARE_IMPACT_MAX_CANDIDATES', '15')),
        'impact_max_query_chars': int(os.getenv('COMPARE_IMPACT_MAX_QUERY_CHARS', '20000')),
        'impact_max_tokens':     int(os.getenv('COMPARE_IMPACT_MAX_TOKENS', '1500')),
        # Documents published before this date are badged "archive" in the UI.
        'impact_archive_before': os.getenv('COMPARE_IMPACT_ARCHIVE_BEFORE', '2018-01-01'),
        # Single-document summary: independent of the diff, a cheap model
        # summarizes whichever of the two uploaded files the user picks.
        # Images need a vision-capable model, hence the separate endpoint.
        'summary_endpoint':       os.getenv('COMPARE_SUMMARY_ENDPOINT', 'databricks-gpt-5-6-luna'),
        'summary_image_endpoint': os.getenv('COMPARE_SUMMARY_IMAGE_ENDPOINT', 'databricks-gpt-5-6-luna'),
        'summary_max_chars':  int(os.getenv('COMPARE_SUMMARY_MAX_CHARS', '300000')),
        'summary_max_tokens': int(os.getenv('COMPARE_SUMMARY_MAX_TOKENS', '800')),
        'volume_path':           os.getenv('COMPARE_VOLUME_PATH', ''),
        'max_tokens':            int(os.getenv('COMPARE_MAX_TOKENS', '8192')),
        'thinking_budget':       int(os.getenv('COMPARE_THINKING_BUDGET', '4000')),
        'temperature':           float(os.getenv('COMPARE_TEMPERATURE', '0.0')),
        # Map-reduce for very large diffs: split at section boundaries when the
        # TEXT CHANGES body exceeds the threshold; one LLM call per chunk.
        'chunk_threshold_chars': int(os.getenv('COMPARE_CHUNK_THRESHOLD_CHARS', '60000')),
        'chunk_size_chars':      int(os.getenv('COMPARE_CHUNK_SIZE_CHARS', '45000')),
    }


# --- File helpers ---

def _sanitize_filename(filename: str, fallback: str = 'document') -> str:
    base = os.path.basename((filename or '').strip())
    if not base:
        return fallback
    safe = re.sub(r'[^A-Za-z0-9._-]+', '_', base).strip('._')
    return safe or fallback


def _validate_file(file: UploadFile, data: bytes) -> None:
    if not data:
        raise ValueError(f'File "{file.filename or "unknown"}" is empty')
    if len(data) > MAX_FILE_BYTES:
        raise ValueError(f'File "{file.filename or "unknown"}" exceeds {MAX_FILE_BYTES // (1024*1024)} MB limit')
    ext = get_extension(file.filename or '')
    if ext not in SUPPORTED_EXTENSIONS:
        raise ValueError(
            f'Unsupported extension "{ext}". Supported: {", ".join(sorted(SUPPORTED_EXTENSIONS))}'
        )


def _is_within_volume(session_path: str, volume_path: str) -> bool:
    """True when session_path is the volume root or a path under it.

    A bare startswith() accepted '<volume>/../../other' and '<volume>_other':
    both leave the configured volume.
    """
    root = posixpath.normpath(volume_path)
    path = posixpath.normpath(session_path)
    return '..' not in session_path.split('/') and (path == root or path.startswith(root + '/'))
