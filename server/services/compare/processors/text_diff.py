"""Template of the processors that diff the text (and images) extracted from two files.

A subclass says how to extract (``extract``) and how the intro of the LLM message reads
(``paragraph_notes`` / ``comparative_notes``); the three methods are shared:

standard:    paragraph semantic diff + focused Markdown bullet-list output.
structured:  paragraph semantic diff + strict JSON array output (for Excel export).
comparative: section canonical diff + section-grouped output.
"""

import logging
from typing import Any, Dict, List, Tuple

from ._diff_engines import (
    SYSTEM_PROMPT_STANDARD,
    SYSTEM_PROMPT_STRUCTURED,
    extraction_warnings,
    image_diff_blocks_dual,
    image_diff_pairs,
    paragraph_semantic_diff,
    section_canonical_diff,
    truncate_diff,
)
from .base import BaseProcessor, ProcessMetadata, ProcessResult

logger = logging.getLogger(__name__)

Images = Dict[str, Dict[str, Any]]


class TextDiffProcessor(BaseProcessor):
    file_type = ''
    page_label = 'Page'          # tag the diff engine reads in the extracted lines: [Page N], [Slide N], [Item N]
    with_images = True
    warn_paragraph = True        # extraction warnings (near-empty text layer) for the paragraph methods
    warn_comparative = True

    def __init__(self, method: str = 'standard') -> None:
        self.method = method

    def extract(self, old_bytes: bytes, old_name: str, new_bytes: bytes,
                new_name: str) -> Tuple[str, str, Images, Images]:
        """(old text, new text, old images, new images); the images are {} for a processor without images."""
        raise NotImplementedError

    def paragraph_notes(self, filtered: int) -> str:
        raise NotImplementedError

    def comparative_notes(self, filtered: int) -> str:
        raise NotImplementedError

    def build_messages(
        self,
        old_bytes: bytes,
        old_name: str,
        new_bytes: bytes,
        new_name: str,
        system_prompt: str = '',
    ) -> ProcessResult:
        if self.method == 'comparative':
            return self._diff(old_bytes, old_name, new_bytes, new_name, system_prompt, 'comparative')
        if self.method == 'structured':
            return self._diff(old_bytes, old_name, new_bytes, new_name, SYSTEM_PROMPT_STRUCTURED, 'structured')
        return self._diff(old_bytes, old_name, new_bytes, new_name, SYSTEM_PROMPT_STANDARD, 'standard')

    def _diff(self, old_bytes: bytes, old_name: str, new_bytes: bytes, new_name: str, system_prompt: str,
              method_name: str) -> ProcessResult:
        comparative = method_name == 'comparative'
        logger.info('%s [%s]: %s — %s → %s', type(self).__name__, method_name,
                    'section canonical diff' if comparative else 'paragraph semantic diff', old_name, new_name)
        old_text, new_text, old_images, new_images = self.extract(old_bytes, old_name, new_bytes, new_name)
        warn = self.warn_comparative if comparative else self.warn_paragraph
        warnings = extraction_warnings(old_text, new_text) if warn else []

        if comparative:
            diff_text, filtered = section_canonical_diff(old_text, new_text)
            notes, title = self.comparative_notes(filtered), 'CHANGES BY SECTION'
        else:
            diff_text, filtered = paragraph_semantic_diff(old_text, new_text, page_label=self.page_label)
            notes, title = self.paragraph_notes(filtered), 'TEXT CHANGES'
        diff_text = truncate_diff(diff_text)

        visual = '\n\n--- VISUAL CHANGES ---' if self.with_images else ''
        if diff_text.strip():
            intro = f'{notes}\n\n--- {title} ---\n{diff_text}{visual}'
        else:
            intro = f'No significant text changes detected.{visual}'
        content: List[Dict[str, Any]] = [{'type': 'text', 'text': intro}]
        image_pairs: List[Dict] = []
        if self.with_images:
            content += image_diff_blocks_dual(old_images, new_images, show_position=True)
            image_pairs = image_diff_pairs(old_images, new_images, show_position=True)

        messages: List[Dict[str, Any]] = []
        if system_prompt or not comparative:
            messages.append({'role': 'system', 'content': system_prompt})
        messages.append({'role': 'user', 'content': content})
        return ProcessResult(
            messages=messages,
            metadata=ProcessMetadata(file_type=self.file_type, method=method_name, old_name=old_name,
                                     new_name=new_name),
            image_pairs=image_pairs,
            warnings=warnings,
        )
