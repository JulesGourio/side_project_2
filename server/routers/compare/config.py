"""Settings of the compare routes: environment variables and their defaults.

Read at call time by ``get_config()`` (one dict per request), except ``APP_VERSION`` and ``MAX_FILE_BYTES``, read once.
"""

import os
from typing import Any, Dict

APP_VERSION = os.getenv('APP_VERSION', '2')

# MAX_COMPARE_FILE_MB takes precedence over MAX_COMPARE_PDF_MB.
MAX_FILE_BYTES = int(os.getenv('MAX_COMPARE_FILE_MB') or os.getenv('MAX_COMPARE_PDF_MB') or '20') * 1024 * 1024

# Analyses running at once: building the messages is CPU-bound and each run holds a model stream.
MAX_CONCURRENT_ANALYSES = int(os.getenv('COMPARE_MAX_CONCURRENT', '5'))

# --- Defaults ---

# Impact search: one Vector Search query per change, then one LLM call per candidate document judges it from all of
# its retrieved passages.
DEFAULT_IMPACT_MAX_QUERIES = 30
DEFAULT_IMPACT_PER_QUERY_RESULTS = 40
DEFAULT_IMPACT_MAX_CANDIDATES = 15
DEFAULT_IMPACT_MAX_QUERY_CHARS = 20000
DEFAULT_IMPACT_MAX_TOKENS = 1500
# Documents published before this date are badged "archive" in the UI.
DEFAULT_IMPACT_ARCHIVE_BEFORE = '2018-01-01'

# Single-document summary: independent of the diff, on a cheap model. Images need a vision-capable model, hence the
# separate endpoint.
DEFAULT_SUMMARY_ENDPOINT = 'databricks-gpt-5-6-luna'
DEFAULT_SUMMARY_IMAGE_ENDPOINT = 'databricks-gpt-5-6-luna'
DEFAULT_SUMMARY_MAX_CHARS = 300000
DEFAULT_SUMMARY_MAX_TOKENS = 800

# Analysis call.
DEFAULT_MAX_TOKENS = 8192
DEFAULT_THINKING_BUDGET = 4000
DEFAULT_TEMPERATURE = 0.0

# Map-reduce for very large diffs: split at section boundaries when the TEXT CHANGES body exceeds the threshold; one
# LLM call per chunk.
DEFAULT_CHUNK_THRESHOLD_CHARS = 60000
DEFAULT_CHUNK_SIZE_CHARS = 45000

# Default system prompt (MODIFIED/REMOVED/ADDED inline markup), used when COMPARE_ANALYSIS_SYSTEM_PROMPT is not set.
DEFAULT_ANALYSIS_PROMPT = """\
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


def get_config() -> Dict[str, Any]:
    """The compare settings for one request.

    :return: the endpoints, the volume path, and the limits of the analysis, the impact search and the summary.
    :raises ValueError: when ``COMPARE_ENABLED`` is not ``true``.
    """
    if os.getenv('COMPARE_ENABLED', 'true').lower() != 'true':
        raise ValueError('Compare feature is not enabled')
    return {
        'analysis_endpoint': os.getenv('COMPARE_ANALYSIS_ENDPOINT', ''),
        'analysis_system_prompt': os.getenv('COMPARE_ANALYSIS_SYSTEM_PROMPT', DEFAULT_ANALYSIS_PROMPT),
        'impact_index': os.getenv('COMPARE_IMPACT_INDEX', ''),
        'impact_endpoint': os.getenv('COMPARE_IMPACT_ENDPOINT', os.getenv('COMPARE_ANALYSIS_ENDPOINT', '')),
        'impact_max_queries': int(os.getenv('COMPARE_IMPACT_MAX_QUERIES', DEFAULT_IMPACT_MAX_QUERIES)),
        'impact_per_query_results': int(os.getenv('COMPARE_IMPACT_PER_QUERY_RESULTS', DEFAULT_IMPACT_PER_QUERY_RESULTS)),
        'impact_max_candidates': int(os.getenv('COMPARE_IMPACT_MAX_CANDIDATES', DEFAULT_IMPACT_MAX_CANDIDATES)),
        'impact_max_query_chars': int(os.getenv('COMPARE_IMPACT_MAX_QUERY_CHARS', DEFAULT_IMPACT_MAX_QUERY_CHARS)),
        'impact_max_tokens': int(os.getenv('COMPARE_IMPACT_MAX_TOKENS', DEFAULT_IMPACT_MAX_TOKENS)),
        'impact_archive_before': os.getenv('COMPARE_IMPACT_ARCHIVE_BEFORE', DEFAULT_IMPACT_ARCHIVE_BEFORE),
        'summary_endpoint': os.getenv('COMPARE_SUMMARY_ENDPOINT', DEFAULT_SUMMARY_ENDPOINT),
        'summary_image_endpoint': os.getenv('COMPARE_SUMMARY_IMAGE_ENDPOINT', DEFAULT_SUMMARY_IMAGE_ENDPOINT),
        'summary_max_chars': int(os.getenv('COMPARE_SUMMARY_MAX_CHARS', DEFAULT_SUMMARY_MAX_CHARS)),
        'summary_max_tokens': int(os.getenv('COMPARE_SUMMARY_MAX_TOKENS', DEFAULT_SUMMARY_MAX_TOKENS)),
        'volume_path': os.getenv('COMPARE_VOLUME_PATH', ''),
        'max_tokens': int(os.getenv('COMPARE_MAX_TOKENS', DEFAULT_MAX_TOKENS)),
        'thinking_budget': int(os.getenv('COMPARE_THINKING_BUDGET', DEFAULT_THINKING_BUDGET)),
        'temperature': float(os.getenv('COMPARE_TEMPERATURE', DEFAULT_TEMPERATURE)),
        'chunk_threshold_chars': int(os.getenv('COMPARE_CHUNK_THRESHOLD_CHARS', DEFAULT_CHUNK_THRESHOLD_CHARS)),
        'chunk_size_chars': int(os.getenv('COMPARE_CHUNK_SIZE_CHARS', DEFAULT_CHUNK_SIZE_CHARS)),
    }
