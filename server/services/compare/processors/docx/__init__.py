"""DOCX / DOC processor — three processing methods.

standard:    paragraph semantic diff + focused Markdown bullet-list output.
structured:  paragraph semantic diff + strict JSON array output (for Excel export).
comparative: section canonical diff + section-grouped output.
"""

import logging
from typing import Any, Dict, List

from ..base import BaseProcessor, ProcessMetadata, ProcessResult
from .._diff_engines import (
    SYSTEM_PROMPT_STANDARD,
    SYSTEM_PROMPT_STRUCTURED,
    extraction_warnings,
    image_diff_blocks_dual,
    image_diff_pairs,
    paragraph_semantic_diff,
    section_canonical_diff,
    truncate_diff,
)
from .extract import _extract_doc, _extract_docx

logger = logging.getLogger(__name__)


def _load_docx(old_bytes, old_name, new_bytes, new_name):
    """Extract text and images from both documents, handling .doc fallback."""
    if old_name.lower().endswith('.doc'):
        old_lines, _ = _extract_doc(old_bytes, old_name)
        old_images: Dict[str, Dict[str, Any]] = {}
    else:
        old_lines, old_images = _extract_docx(old_bytes)

    if new_name.lower().endswith('.doc'):
        new_lines, _ = _extract_doc(new_bytes, new_name)
        new_images: Dict[str, Dict[str, Any]] = {}
    else:
        new_lines, new_images = _extract_docx(new_bytes)

    return '\n'.join(old_lines), '\n'.join(new_lines), old_images, new_images


class DocxProcessor(BaseProcessor):

    def __init__(self, method: str = 'standard') -> None:
        self.method = method

    def build_messages(
        self,
        old_bytes: bytes,
        old_name: str,
        new_bytes: bytes,
        new_name: str,
        system_prompt: str = '',
    ) -> ProcessResult:
        if self.method == 'structured':
            return self._paragraph_diff(old_bytes, old_name, new_bytes, new_name, SYSTEM_PROMPT_STRUCTURED, 'structured')
        if self.method == 'comparative':
            return self._comparative_diff(old_bytes, old_name, new_bytes, new_name, system_prompt)
        return self._paragraph_diff(old_bytes, old_name, new_bytes, new_name, SYSTEM_PROMPT_STANDARD, 'standard')

    def _paragraph_diff(self, old_bytes, old_name, new_bytes, new_name, system_prompt, method_name):
        logger.info('DocxProcessor [%s]: paragraph semantic diff — %s → %s', method_name, old_name, new_name)
        old_text, new_text, old_images, new_images = _load_docx(old_bytes, old_name, new_bytes, new_name)
        warnings = extraction_warnings(old_text, new_text)

        diff_text, filtered = paragraph_semantic_diff(old_text, new_text, page_label='Page')
        diff_text = truncate_diff(diff_text)
        # Estimated page numbers (same estimator as the text tags) enable the
        # position-aware pairing path — important for operator documents that
        # contain several visually similar schemas.
        img_blocks = image_diff_blocks_dual(old_images, new_images, show_position=True)
        img_pairs = image_diff_pairs(old_images, new_images, show_position=True)

        intro = (
            f'Global paragraph alignment: {filtered} trivial lines filtered.\n'
            'Similar paragraphs shown as MODIFIED with ~~removed~~ and **added** words inline.\n'
            'Image page numbers are estimated from content flow (same estimate as the text [Page N] tags).\n\n'
            f'--- TEXT CHANGES ---\n{diff_text}\n\n--- VISUAL CHANGES ---'
        )
        if not diff_text.strip():
            intro = 'No significant text changes detected.\n\n--- VISUAL CHANGES ---'

        content_blocks: List[Dict[str, Any]] = [{'type': 'text', 'text': intro}] + img_blocks
        messages = [
            {'role': 'system', 'content': system_prompt},
            {'role': 'user', 'content': content_blocks},
        ]
        return ProcessResult(
            messages=messages,
            metadata=ProcessMetadata(file_type='docx', method=method_name, old_name=old_name, new_name=new_name),
            image_pairs=img_pairs,
            warnings=warnings,
        )

    def _comparative_diff(self, old_bytes, old_name, new_bytes, new_name, system_prompt):
        logger.info('DocxProcessor [comparative]: section canonical diff — %s → %s', old_name, new_name)
        old_text, new_text, old_images, new_images = _load_docx(old_bytes, old_name, new_bytes, new_name)
        warnings = extraction_warnings(old_text, new_text)

        diff_text, filtered = section_canonical_diff(old_text, new_text)
        diff_text = truncate_diff(diff_text)
        img_blocks = image_diff_blocks_dual(old_images, new_images, show_position=True)
        img_pairs = image_diff_pairs(old_images, new_images, show_position=True)

        intro = (
            f'Section canonical diff: {filtered} trivial pairs filtered.\n'
            'Changes grouped by section.\n\n'
            f'--- CHANGES BY SECTION ---\n{diff_text}\n\n--- VISUAL CHANGES ---'
        )
        if not diff_text.strip():
            intro = 'No significant text changes detected.\n\n--- VISUAL CHANGES ---'

        content_blocks: List[Dict[str, Any]] = [{'type': 'text', 'text': intro}] + img_blocks
        messages: List[Dict[str, Any]] = []
        if system_prompt:
            messages.append({'role': 'system', 'content': system_prompt})
        messages.append({'role': 'user', 'content': content_blocks})
        return ProcessResult(
            messages=messages,
            metadata=ProcessMetadata(file_type='docx', method='comparative', old_name=old_name, new_name=new_name),
            image_pairs=img_pairs,
            warnings=warnings,
        )
