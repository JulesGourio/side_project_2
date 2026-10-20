"""DOCX / DOC processor — three processing methods.

standard:    paragraph semantic diff + focused Markdown bullet-list output.
structured:  paragraph semantic diff + strict JSON array output (for Excel export).
comparative: section canonical diff + section-grouped output.
"""

from typing import Any, Dict

from server.services.compare.processors.text_diff import TextDiffProcessor
from server.services.compare.processors.docx.extract import _extract_doc, _extract_docx

__all__ = ['DocxProcessor', '_extract_doc', '_extract_docx']


def _load_docx(old_bytes, old_name, new_bytes, new_name):
    """Extract text and images from both documents, handling .doc fallback.

    :param old_bytes: content of the old file
    :param old_name: file name of the old file, its extension selects the processing
    :param new_bytes: content of the new file
    :param new_name: file name of the new file
    :return: ``(old text, new text, old images, new images)``
    """
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


class DocxProcessor(TextDiffProcessor):
    file_type = 'docx'

    def extract(self, old_bytes, old_name, new_bytes, new_name):
        """Text and images of the two DOCX files.

        :param old_bytes: content of the old file
        :param old_name: file name of the old file, its extension selects the processing
        :param new_bytes: content of the new file
        :param new_name: file name of the new file
        :return: ``(old text, new text, old images, new images)``
        """
        return _load_docx(old_bytes, old_name, new_bytes, new_name)

    def paragraph_notes(self, filtered):
        """The note that opens the paragraph diff of a DOCX file.

        :param filtered: number of trivial entries the diff filtered out
        :return: the note
        """
        return (f'Global paragraph alignment: {filtered} trivial lines filtered.\n'
                'Similar paragraphs shown as MODIFIED with ~~removed~~ and **added** words inline.\n'
                'Image page numbers are estimated from content flow (same estimate as the text [Page N] tags).')

    def comparative_notes(self, filtered):
        """The note that opens the section diff of a DOCX file.

        :param filtered: number of trivial entries the diff filtered out
        :return: the note
        """
        return f'Section canonical diff: {filtered} trivial pairs filtered.\nChanges grouped by section.'
