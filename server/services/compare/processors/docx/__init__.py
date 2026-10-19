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


class DocxProcessor(TextDiffProcessor):
    file_type = 'docx'

    def extract(self, old_bytes, old_name, new_bytes, new_name):
        return _load_docx(old_bytes, old_name, new_bytes, new_name)

    def paragraph_notes(self, filtered):
        return (f'Global paragraph alignment: {filtered} trivial lines filtered.\n'
                'Similar paragraphs shown as MODIFIED with ~~removed~~ and **added** words inline.\n'
                'Image page numbers are estimated from content flow (same estimate as the text [Page N] tags).')

    def comparative_notes(self, filtered):
        return f'Section canonical diff: {filtered} trivial pairs filtered.\nChanges grouped by section.'
