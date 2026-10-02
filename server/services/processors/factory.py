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
from typing import Any, Dict, List, Optional

from .base import BaseProcessor
from .docx import DocxProcessor
from .excel import ExcelProcessor
from .image import ImageProcessor
from .pdf import PDFProcessor
from .pptx import PptxProcessor
from .xml import XMLProcessor

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
    return ('.' + filename.rsplit('.', 1)[-1].lower()) if '.' in filename else ''


def get_processor(filename: str, method_override: Optional[str] = None) -> Optional[BaseProcessor]:
    """Return a processor for the given filename.

    Args:
        filename: Name of the file to process.
        method_override: If provided, use this method instead of the env-var default.
                         Valid values: 'standard', 'structured', 'comparative'.
                         Unknown values fall back to 'standard'.
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
    """
    ext = get_extension(filename)
    file_type = EXTENSION_MAP.get(ext)
    if file_type == 'pdf':
        from .pdf import _extract_text_with_pages
        return _extract_text_with_pages(file_bytes)
    if file_type == 'docx':
        if filename.lower().endswith('.doc'):
            from .docx import _extract_doc
            lines, _ = _extract_doc(file_bytes, filename)
        else:
            from .docx import _extract_docx
            lines, _ = _extract_docx(file_bytes)
        return '\n'.join(lines)
    if file_type == 'pptx':
        from .pptx import _extract_pptx
        lines, _ = _extract_pptx(file_bytes)
        return '\n'.join(lines)
    if file_type == 'excel':
        from .excel import _extract_xls, _extract_xlsx
        lines, _ = _extract_xls(file_bytes) if filename.lower().endswith('.xls') else _extract_xlsx(file_bytes)
        return '\n'.join(lines)
    if file_type == 'xml':
        from .xml import _extract_xml
        return '\n'.join(_extract_xml(file_bytes))
    if file_type == 'image':
        raise ValueError('Images have no text representation — use prepare_image_content instead.')
    raise ValueError(f'Unsupported file extension "{ext}".')


def prepare_image_content(filename: str, file_bytes: bytes) -> List[Dict[str, Any]]:
    """Normalise a single image into an OpenAI-style content block for a
    vision LLM call — same normalisation ImageProcessor already applies to
    each side before a visual diff (resize/JPEG-recompress oversized or
    unsupported formats), just for one image instead of a pair.
    """
    from .image import _normalise_image
    ext = get_extension(filename) or '.jpg'
    img_bytes, mime = _normalise_image(file_bytes, ext)
    b64 = base64.b64encode(img_bytes).decode('ascii')
    return [{'type': 'image_url', 'image_url': {'url': f'data:{mime};base64,{b64}'}}]
