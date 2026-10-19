"""XML processor — three processing methods.

standard:    paragraph semantic diff + focused Markdown bullet-list output.
structured:  paragraph semantic diff + strict JSON array output (for Excel export).
comparative: section canonical diff + section-grouped output.
"""

import logging
import xml.etree.ElementTree as ET
from typing import List

from server.services.compare.processors.text_diff import TextDiffProcessor

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


class XMLProcessor(TextDiffProcessor):
    file_type = 'xml'

    page_label = 'Item'
    with_images = False
    warn_paragraph = False
    warn_comparative = False

    def extract(self, old_bytes, old_name, new_bytes, new_name):
        return '\n'.join(_extract_xml(old_bytes)), '\n'.join(_extract_xml(new_bytes)), {}, {}

    def paragraph_notes(self, filtered):
        return (f'Global paragraph alignment: {filtered} trivial lines filtered.\n'
                'Similar elements shown as MODIFIED with ~~removed~~ and **added** words inline.')

    def comparative_notes(self, filtered):
        return f'Section canonical diff: {filtered} trivial pairs filtered.\nChanges grouped by section.'
