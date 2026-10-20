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

    :param xml_bytes: content of the XML file
    :return: the text lines, each with its ``[Item N]`` tag; empty when the XML does not parse
    """
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as e:
        logger.warning('XML parse error: %s', e)
        return []

    lines: List[str] = []
    counter = [0]

    def walk(element: ET.Element) -> None:
        """Appends the text of an element and of its children, depth first.

        :param element: XML element to walk
        """
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
        """Text of the two XML files; an XML file has no images.

        :param old_bytes: content of the old file
        :param old_name: file name of the old file, its extension selects the processing
        :param new_bytes: content of the new file
        :param new_name: file name of the new file
        :return: ``(old text, new text, {}, {})``
        """
        return '\n'.join(_extract_xml(old_bytes)), '\n'.join(_extract_xml(new_bytes)), {}, {}

    def paragraph_notes(self, filtered):
        """The note that opens the paragraph diff of an XML file.

        :param filtered: number of trivial entries the diff filtered out
        :return: the note
        """
        return (f'Global paragraph alignment: {filtered} trivial lines filtered.\n'
                'Similar elements shown as MODIFIED with ~~removed~~ and **added** words inline.')

    def comparative_notes(self, filtered):
        """The note that opens the section diff of an XML file.

        :param filtered: number of trivial entries the diff filtered out
        :return: the note
        """
        return f'Section canonical diff: {filtered} trivial pairs filtered.\nChanges grouped by section.'
