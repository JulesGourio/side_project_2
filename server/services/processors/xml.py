"""XML processor — three processing methods.

standard:    paragraph semantic diff + focused Markdown bullet-list output.
structured:  paragraph semantic diff + strict JSON array output (for Excel export).
comparative: section canonical diff + section-grouped output.
"""

import logging
import xml.etree.ElementTree as ET
from typing import Any, Dict, List

from .base import BaseProcessor, ProcessMetadata, ProcessResult
from ._diff_engines import (
    SYSTEM_PROMPT_STANDARD,
    SYSTEM_PROMPT_STRUCTURED,
    paragraph_semantic_diff,
    section_canonical_diff,
    truncate_diff,
)

logger = logging.getLogger(__name__)


def _extract_xml(xml_bytes: bytes) -> List[str]:
    """Walk the XML tree depth-first and return text lines.

    Extracts both element.text (before first child) and element.tail
    (after closing tag, belonging to parent). Skips whitespace-only content.
    Each non-empty text node is tagged with a trailing [Item N] counter so
    the diff engine can track document-order position for Excel sorting.
    """
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as e:
        logger.warning('XML parse error: %s', e)
        return []

    lines: List[str] = []
    counter = [0]

    def walk(element: ET.Element) -> None:
        text = (element.text or '').strip()
        if text:
            counter[0] += 1
            lines.append(f'{text} [Item {counter[0]}]')
        for child in element:
            walk(child)
            tail = (child.tail or '').strip()
            if tail:
                counter[0] += 1
                lines.append(f'{tail} [Item {counter[0]}]')

    walk(root)
    return lines


class XMLProcessor(BaseProcessor):

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

    def _paragraph_diff(
        self,
        old_bytes: bytes,
        old_name: str,
        new_bytes: bytes,
        new_name: str,
        system_prompt: str,
        method_name: str,
    ) -> ProcessResult:
        logger.info('XMLProcessor [%s]: paragraph semantic diff — %s → %s', method_name, old_name, new_name)
        old_text = '\n'.join(_extract_xml(old_bytes))
        new_text = '\n'.join(_extract_xml(new_bytes))

        diff_text, filtered = paragraph_semantic_diff(old_text, new_text, page_label='Item')
        diff_text = truncate_diff(diff_text)

        intro = (
            f'Global paragraph alignment: {filtered} trivial lines filtered.\n'
            'Similar elements shown as MODIFIED with ~~removed~~ and **added** words inline.\n\n'
            f'--- TEXT CHANGES ---\n{diff_text}'
        )
        if not diff_text.strip():
            intro = 'No significant text changes detected.'

        messages: List[Dict[str, Any]] = [
            {'role': 'system', 'content': system_prompt},
            {'role': 'user', 'content': [{'type': 'text', 'text': intro}]},
        ]
        return ProcessResult(
            messages=messages,
            metadata=ProcessMetadata(file_type='xml', method=method_name, old_name=old_name, new_name=new_name),
        )

    def _comparative_diff(
        self,
        old_bytes: bytes,
        old_name: str,
        new_bytes: bytes,
        new_name: str,
        system_prompt: str,
    ) -> ProcessResult:
        logger.info('XMLProcessor [comparative]: section canonical diff — %s → %s', old_name, new_name)
        old_text = '\n'.join(_extract_xml(old_bytes))
        new_text = '\n'.join(_extract_xml(new_bytes))

        diff_text, filtered = section_canonical_diff(old_text, new_text)
        diff_text = truncate_diff(diff_text)

        intro = (
            f'Section canonical diff: {filtered} trivial pairs filtered.\n'
            'Changes grouped by section.\n\n'
            f'--- CHANGES BY SECTION ---\n{diff_text}'
        )
        if not diff_text.strip():
            intro = 'No significant text changes detected.'

        messages: List[Dict[str, Any]] = []
        if system_prompt:
            messages.append({'role': 'system', 'content': system_prompt})
        messages.append({'role': 'user', 'content': [{'type': 'text', 'text': intro}]})
        return ProcessResult(
            messages=messages,
            metadata=ProcessMetadata(file_type='xml', method='comparative', old_name=old_name, new_name=new_name),
        )
