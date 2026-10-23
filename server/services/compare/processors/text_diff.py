"""Template of the processors that diff the text (and images) extracted from two files.

A subclass says how to extract (``extract``) and how the intro of the LLM message reads
(``paragraph_notes`` / ``comparative_notes``); the three methods are shared:

standard:    paragraph semantic diff + focused Markdown bullet-list output.
structured:  paragraph semantic diff + strict JSON array output (for Excel export).
comparative: section canonical diff + section-grouped output.
"""

import logging
from typing import Any, Dict, List, Tuple

from server.services.compare.processors._diff_engines import (
    SYSTEM_PROMPT_STANDARD,
    SYSTEM_PROMPT_STRUCTURED,
    extraction_warnings,
    image_diff_blocks_dual,
    image_diff_pairs,
    paragraph_semantic_diff,
    section_canonical_diff,
    truncate_diff,
)
from server.services.compare.conversion import DOC_TRUNCATED_NOTE
from server.services.compare.processors.base import BaseProcessor, ProcessMetadata, ProcessResult

logger = logging.getLogger(__name__)

Images = Dict[str, Dict[str, Any]]


def _without_doc_note(text: str) -> Tuple[str, bool]:
    """The text without the line that marks a cut .doc, so that line is never diffed.

    :param text: text extracted from one file
    :return: ``(text, True)`` when the cut-document line was present
    """
    lines = text.split('\n')
    if DOC_TRUNCATED_NOTE not in lines:
        return text, False
    return '\n'.join(line for line in lines if line != DOC_TRUNCATED_NOTE), True


class TextDiffProcessor(BaseProcessor):
    file_type = ''
    page_label = 'Page'          # tag the diff engine reads in the extracted lines: [Page N], [Slide N], [Item N]
    with_images = True
    warn_paragraph = True        # extraction warnings (near-empty text layer) for the paragraph methods
    warn_comparative = True

    def __init__(self, method: str = 'standard') -> None:
        """Stores the method: standard, structured or comparative.

        :param method: ``standard``, ``structured`` or ``comparative``
        """
        self.method = method

    def extract(self, old_bytes: bytes, old_name: str, new_bytes: bytes,
                new_name: str) -> Tuple[str, str, Images, Images]:
        """(old text, new text, old images, new images); the images are {} for a processor without images.

        :param old_bytes: content of the old file
        :param old_name: file name of the old file, its extension selects the processing
        :param new_bytes: content of the new file
        :param new_name: file name of the new file
        """
        raise NotImplementedError

    def paragraph_notes(self, filtered: int) -> str:
        """The note that opens the paragraph diff in the message.

        :param filtered: number of trivial entries the diff filtered out
        :return: the note
        """
        raise NotImplementedError

    def comparative_notes(self, filtered: int) -> str:
        """The note that opens the section diff in the message.

        :param filtered: number of trivial entries the diff filtered out
        :return: the note
        """
        raise NotImplementedError

    def build_messages(
        self,
        old_bytes: bytes,
        old_name: str,
        new_bytes: bytes,
        new_name: str,
        system_prompt: str = '',
    ) -> ProcessResult:
        """Message list of this file pair for the method of the processor.

        :param old_bytes: content of the old file
        :param old_name: file name of the old file
        :param new_bytes: content of the new file
        :param new_name: file name of the new file
        :param system_prompt: system prompt of the comparative method; the other methods use their own
        :return: the messages with their metadata and the image pairs
        """
        if self.method == 'comparative':
            return self._diff(old_bytes, old_name, new_bytes, new_name, system_prompt, 'comparative')
        if self.method == 'structured':
            return self._diff(old_bytes, old_name, new_bytes, new_name, SYSTEM_PROMPT_STRUCTURED, 'structured')
        return self._diff(old_bytes, old_name, new_bytes, new_name, SYSTEM_PROMPT_STANDARD, 'standard')

    def _diff(self, old_bytes: bytes, old_name: str, new_bytes: bytes, new_name: str, system_prompt: str,
              method_name: str) -> ProcessResult:
        """Extracts both files, diffs their text and images, and builds the LLM messages.

        :param old_bytes: content of the old file
        :param old_name: file name of the old file
        :param new_bytes: content of the new file
        :param new_name: file name of the new file
        :param system_prompt: system prompt put before the diff
        :param method_name: ``standard``, ``structured`` or ``comparative``
        :return: the messages with their metadata and the image pairs
        """
        comparative = method_name == 'comparative'
        logger.info('%s [%s]: %s — %s → %s', type(self).__name__, method_name,
                    'section canonical diff' if comparative else 'paragraph semantic diff', old_name, new_name)
        old_text, new_text, old_images, new_images = self.extract(old_bytes, old_name, new_bytes, new_name)
        old_text, old_cut = _without_doc_note(old_text)
        new_text, new_cut = _without_doc_note(new_text)
        warn = self.warn_comparative if comparative else self.warn_paragraph
        warnings = extraction_warnings(old_text, new_text) if warn else []
        warnings += [f'The {label} .doc file is too long to be read in full: only its first text fragments were '
                     'compared. Changes further down are missing from this report.'
                     for label, cut in (('OLD', old_cut), ('NEW', new_cut)) if cut]

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
