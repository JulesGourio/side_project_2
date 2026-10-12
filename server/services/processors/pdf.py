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
from typing import Any, Dict, List, Optional

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
    looks_like_table_header,
    image_diff_pairs,
    paragraph_semantic_diff,
    section_canonical_diff,
    truncate_diff,
)

logger = logging.getLogger(__name__)


# --- Fitz import helper ---

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


# --- Text extraction ---

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


# A table row extracted as ONE line keeps its identity across a repagination, so two revisions of the same row pair as
# a single MODIFIED entry;
# as isolated cells, whose grouping PyMuPDF changes between revisions, a table turns into cell soup that the LLM
# merges or drops.
#
# find_tables() is only worth it on text/tabular documents: on a wiring diagram it reads the drawing grid as tables
# and costs ~0.5s per page.
# Blocks per page separates the two cleanly and for free (the blocks are extracted anyway): 13-24 on text/tabular
# documents against 114-180 on schematics.
# The threshold sits with a ~3x margin on both sides.
_TABLE_MAX_BLOCKS_PER_PAGE = int(os.getenv('COMPARE_TABLE_MAX_BLOCKS_PER_PAGE', '60'))
_TABLE_MIN_ROWS = 2
_TABLE_MIN_COLS = 2
# The running header cartouche is itself a bordered grid that find_tables() returns on every page; emitting it as a
# table row would re-create the
# pagination noise. Small grids confined to the header/footer band are skipped (the cartouche spans 4.4%-15.8% of the
# page height, real tables start
# lower); the row-count condition keeps a genuine table that starts high on the page.
_TABLE_HEADER_BAND = 0.18
_TABLE_FOOTER_BAND = 0.88
_TABLE_BAND_MAX_ROWS = 3


# Data rows are labelled with their column header ("Activity: Release the part | Quality manager: X"), as the DOCX
# extractor does: a changed row
# reaches the LLM alone, without the unchanged header row, so it could not say WHICH column a value belongs to. false
# = positional rows only.
_TABLE_LABELS = os.getenv('COMPARE_PDF_TABLE_LABELS', 'true').lower() == 'true'


def _table_rows_for_page(page, state: Optional[Dict[str, Any]] = None) -> List[Any]:
    """[(y, 'cell | cell | cell', bbox), ...] for every real table on the page.

    `state` carries the last header from one page to the next, so a table that
    continues on the following page without repeating its header keeps the same
    labels (otherwise a row would be labelled or not depending on which side of
    a page break it falls).
    """
    out: List[Any] = []
    state = state if state is not None else {}
    continued_header = state.get('header') if state.get('open') else None
    state['open'] = False
    try:
        tables = page.find_tables().tables
    except Exception as e:  # find_tables is best-effort; never fail extraction over it
        logger.debug(f'find_tables skipped: {e}')
        return out
    page_height = max(1.0, page.rect.height)
    first_table = True
    for table in sorted(tables, key=lambda t: t.bbox[1]):
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
        # Empty cells keep their place ("M8 | 22 Nm |  | Wrench B"): dropping them makes a value that moved to another
        # column read the same on both revisions.
        # Columns that are empty on every row (merged-cell artefacts of find_tables) carry no position and are
        # removed.
        norm = [[' '.join((c or '').split()) for c in row] for row in rows]
        n_cols = max((len(r) for r in norm), default=0)
        norm = [r + [''] * (n_cols - len(r)) for r in norm]
        used = [c for c in range(n_cols) if any(r[c] for r in norm)]

        header: Optional[List[str]] = None
        header_row = -1
        if _TABLE_LABELS and norm:
            if looks_like_table_header(norm[0]):
                header, header_row = norm[0], 0
            elif first_table and continued_header and len(continued_header) == n_cols:
                header = continued_header
        first_table = False
        state['header'] = header
        state['open'] = True  # reset to False by the next page if it has no table

        for i, row in enumerate(norm):
            if header is not None and i != header_row:
                cells = [f'{header[c]}: {val}' for c, val in enumerate(row) if val]
            else:
                cells = [row[c] for c in used]
                while cells and not cells[-1]:
                    cells.pop()
            if not cells:
                continue
            # Rows are evenly spread over the table bbox: enough to interleave them
            # with the surrounding free text in reading order.
            y = bbox[1] + height * (i / max(1, len(rows)))
            out.append((y, ' | '.join(cells), bbox))
    return out


# Running header/footer: the same block (digits masked, so "Page 3/12" groups)
# on at least half of the pages, inside these bands of the page height.
_MERGE_PAGE_SPLITS = os.getenv('COMPARE_PDF_MERGE_PAGE_SPLITS', 'true').lower() == 'true'
_RUNNING_TOP_BAND = 0.20
_RUNNING_BOTTOM_BAND = 0.85
_RUNNING_MIN_PAGE_SHARE = 0.5
# "a) …", "b. …", "iv) …": a list item, not the continuation of a sentence.
_LIST_MARKER_RE = re.compile(r'^(?:[a-z]|[ivx]{1,4})[).]\s')
_SENTENCE_END = '.:;!?…'


def _continues_across_pages(tail: str, head: str) -> bool:
    """True when `head` (first body block of a page) is the rest of the sentence
    left open by `tail` (last body block of the previous page)."""
    tail, head = tail.rstrip(), head.lstrip()
    if not tail or not head or len(tail.split()) < 4:
        return False
    if tail[-1] in _SENTENCE_END or not head[0].islower():
        return False
    return not _LIST_MARKER_RE.match(head)


def _merge_page_split_paragraphs(pages: List[List[Dict[str, Any]]]) -> None:
    """Re-join, in place, a paragraph that a page break cut into two blocks.

    Where the break falls depends on everything above it: one paragraph added
    on page 1 moves every later break, and each paragraph that used to straddle
    a break (or now does) reached the diff as two half-blocks on one side and a
    whole block on the other. On a 6-page procedure, one
    inserted paragraph produced 6 phantom MODIFIED entries.

    Running headers/footers sit between the two halves in reading order, so
    they are identified first (same text on most pages, in the top or bottom
    band) and skipped when looking for a page's first/last body block. Table
    rows are never merged.
    """
    n_pages = len(pages)
    if n_pages < 2:
        return
    key = lambda line: re.sub(r'\d+', '#', line['text'])  # noqa: E731
    seen_on: Dict[str, set] = {}
    for idx, lines in enumerate(pages):
        for line in lines:
            if line['rel_y'] <= _RUNNING_TOP_BAND or line['rel_y'] >= _RUNNING_BOTTOM_BAND:
                seen_on.setdefault(key(line), set()).add(idx)
    running = {k for k, on in seen_on.items() if len(on) >= max(2, n_pages * _RUNNING_MIN_PAGE_SHARE)}

    def _body(lines: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        return [
            line for line in lines
            if not ((line['rel_y'] <= _RUNNING_TOP_BAND or line['rel_y'] >= _RUNNING_BOTTOM_BAND)
                    and key(line) in running)
        ]

    tail: Any = None
    for lines in pages:
        body = _body(lines)
        if not body:
            continue
        head = body[0]
        if (tail is not None and not tail['table'] and not head['table']
                and _continues_across_pages(tail['text'], head['text'])):
            joiner = '' if tail['text'].rstrip().endswith('-') else ' '
            tail['text'] = tail['text'].rstrip() + joiner + head['text'].lstrip()
            lines.remove(head)
            body = body[1:]
            if not body:
                continue  # the paragraph may run on to a third page
        tail = body[-1]


def _extract_text_with_pages(pdf_bytes: bytes) -> str:
    """Extract text block-by-block, sorted by (y, x) position, each block tagged [Page N].

    Tables are emitted one line per row (cells joined by ' | ') instead of cell by
    cell — see _TABLE_MAX_BLOCKS_PER_PAGE for when that pass runs and why.

    End-of-line hyphenation is undone by PyMuPDF (TEXT_DEHYPHENATE) so a word
    split differently between the two revisions ("instal-\nlation" vs
    "installation") does not surface as a false MODIFIED pair. Ligatures are
    expanded for the same reason: one revision exported with "ﬁ" glyphs and the
    other without would differ on every word containing "fi". A paragraph cut by
    a page break is re-joined (_merge_page_split_paragraphs).
    """
    fitz = _import_fitz()
    doc = fitz.open(stream=pdf_bytes, filetype='pdf')
    flags = (fitz.TEXTFLAGS_BLOCKS | fitz.TEXT_DEHYPHENATE) & ~fitz.TEXT_PRESERVE_LIGATURES
    text_blocks: List[str] = []
    try:
        per_page = []
        total_blocks = 0
        for page in doc:
            blocks = [b for b in page.get_text('blocks', flags=flags) if len(b) >= 5]
            total_blocks += len(blocks)
            per_page.append(blocks)
        use_tables = bool(per_page) and (total_blocks / len(per_page)) <= _TABLE_MAX_BLOCKS_PER_PAGE

        pages: List[List[Dict[str, Any]]] = []
        table_state: Dict[str, Any] = {}
        for page, blocks in zip(doc, per_page):
            table_rows = _table_rows_for_page(page, table_state) if use_tables else []
            page_height = max(1.0, page.rect.height)
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
                    lines.append((by0, ' '.join(content.splitlines()), False))
            lines += [(y, text, True) for y, text, _ in table_rows]
            lines.sort(key=lambda t: t[0])
            pages.append([
                {'text': text, 'table': is_table, 'rel_y': y / page_height}
                for y, text, is_table in lines
                if text and not _is_page_artifact(text)
            ])

        if _MERGE_PAGE_SPLITS:
            _merge_page_split_paragraphs(pages)
        for page_num, page_lines in enumerate(pages, start=1):
            for line in page_lines:
                text_blocks.append(f"{line['text']} [Page {page_num}]")
    finally:
        doc.close()
    return '\n'.join(text_blocks)


# --- Image extraction (perceptual dhash) ---

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


# --- Processor ---

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

    # --- Standard / Structured — paragraph semantic diff ---

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

    # --- Comparative — section canonical diff ---

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
