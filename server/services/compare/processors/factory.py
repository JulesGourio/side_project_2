"""Processor factory — maps file extensions to processor instances.

Method selection via environment variables (override the default 'standard'):
  COMPARE_METHOD_PDF
  COMPARE_METHOD_IMAGE
  COMPARE_METHOD_DOCX
  COMPARE_METHOD_PPTX
  COMPARE_METHOD_EXCEL

Valid method values: standard | structured | comparative
"""

import base64
import os
from typing import Any, Dict, List

from server.services.compare.processors._diff_engines import DIFF_TRUNCATED_MARKER, _MAX_DIFF_CHARS
from server.services.compare.processors.base import BaseProcessor
from server.services.compare.processors.docx import DocxProcessor
from server.services.compare.processors.excel import ExcelProcessor
from server.services.compare.processors.image import ImageProcessor
from server.services.compare.processors.pdf import PDFProcessor
from server.services.compare.processors.pptx import PptxProcessor
from server.services.compare.processors.xml import XMLProcessor

EXTENSION_MAP: dict[str, str] = {
    '.pdf':  'pdf',
    '.jpg':  'image', '.jpeg': 'image', '.png': 'image',
    '.gif':  'image', '.webp': 'image', '.bmp': 'image',
    '.tiff': 'image', '.tif':  'image',
    '.docx': 'docx',  '.doc':  'docx',
    '.pptx': 'pptx',  '.ppt':  'pptx',
    '.xlsx': 'excel', '.xls':  'excel',
    '.xml':  'xml',
}

SUPPORTED_EXTENSIONS = set(EXTENSION_MAP.keys())

_DEFAULTS = {
    'pdf':   'standard',
    'image': 'standard',
    'docx':  'standard',
    'pptx':  'standard',
    'excel': 'standard',
    'xml':   'standard',
}

_ENV_KEYS = {
    'pdf':   'COMPARE_METHOD_PDF',
    'image': 'COMPARE_METHOD_IMAGE',
    'docx':  'COMPARE_METHOD_DOCX',
    'pptx':  'COMPARE_METHOD_PPTX',
    'excel': 'COMPARE_METHOD_EXCEL',
    'xml':   'COMPARE_METHOD_XML',
}


def get_extension(filename: str) -> str:
    """The lowercase extension of a file name, with its dot; empty when it has none.

    :param filename: file name
    :return: the extension, such as ``.pdf``
    """
    return ('.' + filename.rsplit('.', 1)[-1].lower()) if '.' in filename else ''


def file_type_of(filename: str) -> str | None:
    """'pdf' / 'docx' / … for a supported file name, else None.

    :param filename: file name
    :return: the file type, or None when the extension is not supported
    """
    return EXTENSION_MAP.get(get_extension(filename))


def diff_truncation_warnings(messages: List[Dict[str, Any]]) -> List[str]:
    """A user-facing warning when truncate_diff() cut the diff sent to the LLM.

    Without it the report silently stops partway through the document: the truncation
    note reaches only the model.

    :param messages: messages sent to the model
    :return: the warning list, empty when the diff was not cut
    """
    for msg in messages:
        content = msg.get('content')
        blocks = content if isinstance(content, list) else [{'type': 'text', 'text': content or ''}]
        for block in blocks:
            if block.get('type') == 'text' and DIFF_TRUNCATED_MARKER in (block.get('text') or ''):
                return [
                    'The documents differ too much to be analysed in full: only the first '
                    f'{_MAX_DIFF_CHARS:,} characters of the differences were read. '
                    'Changes towards the end of the document are missing from this report.'
                ]
    return []


def get_processor(filename: str, method_override: str | None = None) -> BaseProcessor | None:
    """Return a processor for the given filename.

    Args:
        filename: Name of the file to process.
        method_override: If provided, use this method instead of the env-var default.
                         Valid values: 'standard', 'structured', 'comparative'.
                         Unknown values fall back to 'standard'.

    :param filename: name of the file to process
    :param method_override: method to use instead of the environment default; None or blank keeps the default
    :return: the processor, or None when the file type is not supported
    """
    ext = get_extension(filename)
    file_type = EXTENSION_MAP.get(ext)
    if not file_type:
        return None
    default_method = os.getenv(_ENV_KEYS[file_type], _DEFAULTS[file_type])
    method = method_override.strip() if method_override and method_override.strip() else default_method
    if file_type == 'pdf':
        return PDFProcessor(method=method)
    if file_type == 'image':
        return ImageProcessor(method=method)
    if file_type == 'docx':
        return DocxProcessor(method=method)
    if file_type == 'pptx':
        return PptxProcessor(method=method)
    if file_type == 'excel':
        return ExcelProcessor(method=method)
    if file_type == 'xml':
        return XMLProcessor(method=method)
    return None


def extract_document_text(filename: str, file_bytes: bytes) -> str:
    """Extract plain text from a single document — for features that need one
    document's content rather than a diff (e.g. /compare/summarize).

    Reuses each processor module's own single-file extraction helper (the
    same code path each processor already runs on each side before diffing),
    just without pairing it against a second document. Raises ValueError for
    unsupported extensions and image files (no text representation — see
    prepare_image_content for the vision-model path used for those instead).

    :param filename: name of the file
    :param file_bytes: content of the file
    :return: the plain text of the document
    """
    ext = get_extension(filename)
    file_type = EXTENSION_MAP.get(ext)
    if file_type == 'pdf':
        from server.services.compare.processors.pdf import _extract_text_with_pages
        return _extract_text_with_pages(file_bytes)
    if file_type == 'docx':
        if filename.lower().endswith('.doc'):
            from server.services.compare.processors.docx import _extract_doc
            lines, _ = _extract_doc(file_bytes, filename)
        else:
            from server.services.compare.processors.docx import _extract_docx
            lines, _ = _extract_docx(file_bytes)
        return '\n'.join(lines)
    if file_type == 'pptx':
        from server.services.compare.processors.pptx import _extract_pptx
        lines, _ = _extract_pptx(file_bytes)
        return '\n'.join(lines)
    if file_type == 'excel':
        from server.services.compare.processors.excel import _extract_xls, _extract_xlsx
        lines, _ = _extract_xls(file_bytes) if filename.lower().endswith('.xls') else _extract_xlsx(file_bytes)
        return '\n'.join(lines)
    if file_type == 'xml':
        from server.services.compare.processors.xml import _extract_xml
        return '\n'.join(_extract_xml(file_bytes))
    if file_type == 'image':
        raise ValueError('Images have no text representation — use prepare_image_content instead.')
    raise ValueError(f'Unsupported file extension "{ext}".')


def prepare_image_content(filename: str, file_bytes: bytes) -> List[Dict[str, Any]]:
    """Normalise a single image into an OpenAI-style content block for a
    vision LLM call — same normalisation ImageProcessor already applies to
    each side before a visual diff (resize/JPEG-recompress oversized or
    unsupported formats), just for one image instead of a pair.

    :param filename: name of the image
    :param file_bytes: content of the image
    :return: one image content block for the vision model
    """
    from server.services.compare.processors.image import _normalise_image
    ext = get_extension(filename) or '.jpg'
    img_bytes, mime = _normalise_image(file_bytes, ext)
    b64 = base64.b64encode(img_bytes).decode('ascii')
    return [{'type': 'image_url', 'image_url': {'url': f'data:{mime};base64,{b64}'}}]
