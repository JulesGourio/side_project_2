"""Diff engines shared by the compare processors.

Text engines (engines.py): paragraph_semantic_diff is the primary one, section_canonical_diff the
alternative. Image hashing and pairing live in images.py, the LLM system prompts in prompts.py.
"""

from server.services.compare.processors._diff_engines.engines import paragraph_semantic_diff, section_canonical_diff
from server.services.compare.processors._diff_engines.images import (
    _IMG_JPEG_QUALITY,
    _IMG_MAX_DIM,
    _compute_image_diff_pairs,
    _thumbnail_b64,
    dhash,
    hash16,
    image_diff_blocks_dual,
    image_diff_pairs,
    images_are_similar,
)
from server.services.compare.processors._diff_engines.prompts import SYSTEM_PROMPT_STANDARD, SYSTEM_PROMPT_STRUCTURED
from server.services.compare.processors._diff_engines.text import (
    _MAX_DIFF_CHARS,
    DIFF_TRUNCATED_MARKER,
    extraction_warnings,
    is_substantive_keep_modals,
    looks_like_table_header,
    truncate_diff,
)

__all__ = [
    'DIFF_TRUNCATED_MARKER',
    'SYSTEM_PROMPT_STANDARD',
    'SYSTEM_PROMPT_STRUCTURED',
    '_IMG_JPEG_QUALITY',
    '_IMG_MAX_DIM',
    '_MAX_DIFF_CHARS',
    '_compute_image_diff_pairs',
    '_thumbnail_b64',
    'dhash',
    'extraction_warnings',
    'hash16',
    'image_diff_blocks_dual',
    'image_diff_pairs',
    'images_are_similar',
    'is_substantive_keep_modals',
    'looks_like_table_header',
    'paragraph_semantic_diff',
    'section_canonical_diff',
    'truncate_diff',
]
