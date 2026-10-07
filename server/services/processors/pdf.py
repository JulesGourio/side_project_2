"""PDF processor — three processing methods.

standard:    paragraph semantic diff + focused Markdown bullet-list output.
structured:  paragraph semantic diff + strict JSON array output (for Excel export).
comparative: section canonical diff + section-grouped output.

Standard and structured share the same diff engine (paragraph_semantic_diff).
Comparative uses section_canonical_diff.
"""

import base64
import io
import logging
import os
import re
from typing import Any, Dict, List

from .base import BaseProcessor, ProcessMetadata, ProcessResult
from ._diff_engines import (
    _IMG_MAX_DIM,
    _IMG_JPEG_QUALITY,
    SYSTEM_PROMPT_STANDARD,
    SYSTEM_PROMPT_STRUCTURED,
    dhash,
    extraction_warnings,
    hash16,
    images_are_similar,
    image_diff_blocks_dual,
    image_diff_pairs,
    paragraph_semantic_diff,
    section_canonical_diff,
    truncate_diff,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Fitz import helper
# ---------------------------------------------------------------------------

def _import_fitz():
    try:
        import pymupdf as fitz  # PyMuPDF >= 1.24
        return fitz
    except ImportError:
        pass
    try:
        import fitz  # PyMuPDF < 1.24
        return fitz
    except ImportError as e:
        raise ImportError(
            f'PyMuPDF could not be imported ({e}). '
            'Ensure "pymupdf>=1.25.0" is in requirements.txt and the app has been redeployed.'
        ) from e


# ---------------------------------------------------------------------------
# Text extraction
# ---------------------------------------------------------------------------

# Standalone pagination artifacts: "12", "- 12 -", "12/70", "Page 12 of 70",
# "Page 12 sur 70", "Page 12". They shift on every repagination and would
# otherwise produce a REMOVED/ADDED pair per page of the document.
_PAGE_ARTIFACT_RE = re.compile(
    r'^\s*(?:[-–—]\s*)?(?:page\s+)?\d{1,4}\s*(?:(?:/|of|de|sur)\s*\d{1,4})?\s*(?:[-–—]\s*)?$',
    re.I,
)


def _is_page_artifact(text: str) -> bool:
    """True for blocks that are just a page number / 'Page X of Y' marker."""
    return bool(_PAGE_ARTIFACT_RE.match(text))


# A table row extracted as ONE line keeps its identity across a repagination, so
# two revisions of the same row pair as a single MODIFIED entry. Without it, a
# table reaches the diff as isolated cells whose grouping PyMuPDF changes between
# revisions, producing cell soup: on MOP_AX the 5 new rows of the deputy table
# arrived as fragments ('/', 'Responsable assurance qualite') and the LLM merged
# or dropped three of them (2026-08-17).
#
# find_tables() is only worth it on text/tabular documents. On a wiring diagram it
# reads the drawing grid as a table (123 of them on WDT017W8850653) and costs 0.5s
# per page (47s for WDT017W8850201 alone, against 13s for the whole current diff).
# Blocks-per-page separates the two cleanly and for free, since the blocks are
# extracted anyway: measured 13-24 on text/tabular documents (MOP_AX 20, AIPI 24,
# NE07-011 14, INAQ604 13) against 114-180 on schematics (WDT653 114, WDT201 147,
# WDT403 180). The threshold sits with a ~3x margin on both sides.
_TABLE_MAX_BLOCKS_PER_PAGE = int(os.getenv('COMPARE_TABLE_MAX_BLOCKS_PER_PAGE', '60'))
_TABLE_MIN_ROWS = 2
_TABLE_MIN_COLS = 2
# The running header cartouche is itself a bordered grid, so find_tables() returns
# it on every page. Emitting it as a table row re-created the pagination noise this
# work removed: 'LATelec | MANUEL D'ORGANISME | M.O.P. CHAPITRE: IA PAGE : ~~1/37~~
# **1/38**', once per page. Skip small grids confined to the header/footer band —
# measured on MOP_AX, the cartouche spans 4.4%-15.8% of page height while the real
# tables start at 17.2%. The row-count condition keeps a genuine table that happens
# to start high on the page.
_TABLE_HEADER_BAND = 0.18
_TABLE_FOOTER_BAND = 0.88
_TABLE_BAND_MAX_ROWS = 3


def _table_rows_for_page(page) -> List[Any]:
    """[(y, 'cell | cell | cell', bbox), ...] for every real table on the page."""
    out: List[Any] = []
    try:
        tables = page.find_tables().tables
    except Exception as e:  # find_tables is best-effort; never fail extraction over it
        logger.debug(f'find_tables skipped: {e}')
        return out
    page_height = max(1.0, page.rect.height)
    for table in tables:
        if table.row_count < _TABLE_MIN_ROWS or table.col_count < _TABLE_MIN_COLS:
            continue
        top, bottom = table.bbox[1] / page_height, table.bbox[3] / page_height
        if table.row_count <= _TABLE_BAND_MAX_ROWS and (
                bottom <= _TABLE_HEADER_BAND or top >= _TABLE_FOOTER_BAND):
            continue
        try:
            rows = table.extract()
        except Exception as e:
            logger.debug(f'table.extract skipped: {e}')
            continue
        bbox = table.bbox
        height = max(1.0, bbox[3] - bbox[1])
        for i, row in enumerate(rows):
            cells = [' '.join((c or '').split()) for c in row]
            cells = [c for c in cells if c]
            if not cells:
                continue
            # Rows are evenly spread over the table bbox: enough to interleave them
            # with the surrounding free text in reading order.
            y = bbox[1] + height * (i / max(1, len(rows)))
            out.append((y, ' | '.join(cells), bbox))
    return out


def _extract_text_with_pages(pdf_bytes: bytes) -> str:
    """Extract text block-by-block, sorted by (y, x) position, each block tagged [Page N].

    Tables are emitted one line per row (cells joined by ' | ') instead of cell by
    cell — see _TABLE_MAX_BLOCKS_PER_PAGE for when that pass runs and why.

    End-of-line hyphenation is undone by PyMuPDF (TEXT_DEHYPHENATE) so a word
    split differently between the two revisions ("instal-\nlation" vs
    "installation") does not surface as a false MODIFIED pair.
    """
    fitz = _import_fitz()
    doc = fitz.open(stream=pdf_bytes, filetype='pdf')
    flags = fitz.TEXTFLAGS_BLOCKS | fitz.TEXT_DEHYPHENATE
    text_blocks: List[str] = []
    try:
        per_page = []
        total_blocks = 0
        for page in doc:
            blocks = [b for b in page.get_text('blocks', flags=flags) if len(b) >= 5]
            total_blocks += len(blocks)
            per_page.append(blocks)
        use_tables = bool(per_page) and (total_blocks / len(per_page)) <= _TABLE_MAX_BLOCKS_PER_PAGE

        for page_num, (page, blocks) in enumerate(zip(doc, per_page), start=1):
            table_rows = _table_rows_for_page(page) if use_tables else []
            # A block inside a table bbox is one of its cells: the row line already
            # carries it, so emitting it again would duplicate the whole table.
            lines = []
            for b in blocks:
                bx0, by0, bx1, by1 = b[0], b[1], b[2], b[3]
                cx, cy = (bx0 + bx1) / 2, (by0 + by1) / 2
                inside = any(tb[0] <= cx <= tb[2] and tb[1] <= cy <= tb[3]
                             for _, _, tb in table_rows)
                if inside:
                    continue
                content = b[4].strip()
                if content and not _is_page_artifact(content):
                    lines.append((by0, ' '.join(content.splitlines())))
            lines += [(y, text) for y, text, _ in table_rows]
            lines.sort(key=lambda t: t[0])
            for _, text in lines:
                if text and not _is_page_artifact(text):
                    text_blocks.append(f'{text} [Page {page_num}]')
    finally:
        doc.close()
    return '\n'.join(text_blocks)


# ---------------------------------------------------------------------------
# Image extraction (perceptual dhash)
# ---------------------------------------------------------------------------

def _extract_and_hash_images(pdf_bytes: bytes) -> Dict[str, Dict[str, Any]]:
    """Extract embedded images, deduplicate by perceptual dhash, record pages.

    Returns: { dhash_str: { 'b64': str, 'pages': [1, 3, ...] } }
    """
    fitz = _import_fitz()
    doc = fitz.open(stream=pdf_bytes, filetype='pdf')
    images: Dict[str, Dict[str, Any]] = {}
    try:
        for page_num, page in enumerate(doc, start=1):
            for img_info in page.get_images(full=True):
                xref = img_info[0]
                try:
                    raw = doc.extract_image(xref)['image']
                    h = dhash(raw)
                    matched = next((k for k in images if images_are_similar(k, h)), None)
                    if matched:
                        if page_num not in images[matched]['pages']:
                            images[matched]['pages'].append(page_num)
                    else:
                        try:
                            from PIL import Image as PILImage
                            img = PILImage.open(io.BytesIO(raw))
                            if img.mode != 'RGB':
                                img = img.convert('RGB')
                            img.thumbnail((_IMG_MAX_DIM, _IMG_MAX_DIM), PILImage.Resampling.LANCZOS)
                            buf = io.BytesIO()
                            img.save(buf, format='JPEG', quality=_IMG_JPEG_QUALITY)
                            b64 = base64.b64encode(buf.getvalue()).decode('ascii')
                        except Exception:
                            b64 = base64.b64encode(raw).decode('ascii')
                        images[h] = {'b64': b64, 'pages': [page_num], 'hash16': hash16(raw)}
                except Exception as e:
                    logger.debug(f'Image extraction skipped (page {page_num}, xref {xref}): {e}')
    finally:
        doc.close()
    return images


# ---------------------------------------------------------------------------
# Processor
# ---------------------------------------------------------------------------

class PDFProcessor(BaseProcessor):

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

    # ------------------------------------------------------------------
    # Standard / Structured — paragraph semantic diff
    # ------------------------------------------------------------------

    def _paragraph_diff(
        self,
        old_bytes: bytes,
        old_name: str,
        new_bytes: bytes,
        new_name: str,
        system_prompt: str,
        method_name: str,
    ) -> ProcessResult:
        logger.info(f'PDFProcessor [{method_name}]: paragraph semantic diff — {old_name} → {new_name}')

        old_text = _extract_text_with_pages(old_bytes)
        new_text = _extract_text_with_pages(new_bytes)
        warnings = extraction_warnings(old_text, new_text)

        diff_text, filtered = paragraph_semantic_diff(old_text, new_text, page_label='Page')
        diff_text = truncate_diff(diff_text)

        old_images = _extract_and_hash_images(old_bytes)
        new_images = _extract_and_hash_images(new_bytes)
        img_blocks = image_diff_blocks_dual(old_images, new_images)
        img_pairs = image_diff_pairs(old_images, new_images)

        intro = (
            f'Global paragraph alignment: {filtered} trivial lines filtered '
            '(within-group modal swaps, whitespace, TOC entries, layout shifts).\n'
            'Page-split blocks pre-merged. Similar paragraphs shown as '
            'MODIFIED with ~~removed~~ and **added** words inline.\n'
            'Each block tagged with its source page.\n\n'
            f'--- TEXT CHANGES ---\n{diff_text}\n\n--- VISUAL CHANGES ---'
        )
        if not diff_text.strip():
            intro = 'No significant text changes detected.\n\n--- VISUAL CHANGES ---'

        content_blocks: List[Dict[str, Any]] = [{'type': 'text', 'text': intro}] + img_blocks
        messages: List[Dict[str, Any]] = [
            {'role': 'system', 'content': system_prompt},
            {'role': 'user', 'content': content_blocks},
        ]

        return ProcessResult(
            messages=messages,
            metadata=ProcessMetadata(file_type='pdf', method=method_name, old_name=old_name, new_name=new_name),
            image_pairs=img_pairs,
            warnings=warnings,
        )

    # ------------------------------------------------------------------
    # Comparative — section canonical diff
    # ------------------------------------------------------------------

    def _comparative_diff(
        self,
        old_bytes: bytes,
        old_name: str,
        new_bytes: bytes,
        new_name: str,
        system_prompt: str,
    ) -> ProcessResult:
        logger.info(f'PDFProcessor [comparative]: section canonical diff — {old_name} → {new_name}')

        old_text = _extract_text_with_pages(old_bytes)
        new_text = _extract_text_with_pages(new_bytes)
        warnings = extraction_warnings(old_text, new_text)

        diff_text, filtered = section_canonical_diff(old_text, new_text)
        diff_text = truncate_diff(diff_text)

        old_images = _extract_and_hash_images(old_bytes)
        new_images = _extract_and_hash_images(new_bytes)
        img_blocks = image_diff_blocks_dual(old_images, new_images)
        img_pairs = image_diff_pairs(old_images, new_images)

        intro = (
            f'Section canonical diff: {filtered} trivial pairs filtered '
            '(modal verbs, whitespace, synonyms).\n'
            'Changes grouped by section. Each line tagged with its source page.\n\n'
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
            metadata=ProcessMetadata(file_type='pdf', method='comparative', old_name=old_name, new_name=new_name),
            image_pairs=img_pairs,
            warnings=warnings,
        )
