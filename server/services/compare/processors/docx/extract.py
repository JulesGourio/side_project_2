"""Text lines and images of a DOCX (or .doc), in document order, tagged [Page N, Para M]."""

import base64
import io
import logging
from bisect import bisect_right
from typing import Any, Dict, List, Tuple

from docx import Document
from PIL import Image as PILImage

from server.services.compare.processors._diff_engines import _IMG_JPEG_QUALITY, dhash, hash16, images_are_similar, looks_like_table_header
from server.services.compare.conversion import doc_to_text
from server.services.compare.processors.docx.ooxml import (
    _footnote_lines, _is_toc_paragraph, _iter_block_items, _paragraph_text, _row_cells, _textbox_texts,
    _vml_shape_lines,
)
from server.services.compare.processors.docx.pages import _IMG_WEIGHT, _assign_pages, _count_breaks_split, _lrpb_is_stale

logger = logging.getLogger(__name__)

def _extract_doc(doc_bytes: bytes, filename: str) -> Tuple[List[str], List[Tuple[str, str]]]:
    text = doc_to_text(doc_bytes, filename)
    return (text.splitlines() if text else []), []


def _extract_docx(docx_bytes: bytes) -> Tuple[List[str], Dict[str, Dict[str, Any]]]:
    """Return (text_lines, dhash_images_dict).

    Extracts paragraphs AND tables in document order, each tagged [Page N, Para M].
    Tags are stripped by the diff engine (strip_tag) before comparison and output,
    so they never appear in the report or cause false positives.
    Page numbers are derived from explicit page breaks (<w:br w:type="page"/>) and
    implicit breaks recorded by Word at save time (<w:lastRenderedPageBreak/>).
    Table data rows are labelled with column names from the header row.
    """
    try:
        doc = Document(io.BytesIO(docx_bytes))
    except Exception as e:
        logger.warning('python-docx could not open file: %s', e)
        return [], {}

    from docx.table import Table as DocxTable

    _A_BLIP  = '{http://schemas.openxmlformats.org/drawingml/2006/main}blip'
    _A_XFRM  = '{http://schemas.openxmlformats.org/drawingml/2006/main}xfrm'
    _PIC_PIC = '{http://schemas.openxmlformats.org/drawingml/2006/picture}pic'
    _R_EMBED = '{http://schemas.openxmlformats.org/officeDocument/2006/relationships}embed'

    entries: List[Dict[str, Any]] = []  # one per output line; page assigned after the walk
    images: Dict[str, Dict[str, Any]] = {}
    text_pos = 0
    total_forced = 0
    total_lrpb = 0
    vml_seen: set = set()
    # Breaks and weight of elements with no output line (image-only paragraphs) carry forward into the next entry.
    pend_forced = 0
    pend_lrpb = 0
    pend_weight = 0

    def _emit(text: str, weight: int) -> None:
        """Append one output entry, folding in any pending breaks/weight."""
        nonlocal pend_forced, pend_lrpb, pend_weight
        entries.append({
            'text': text,
            'pos': text_pos,
            'weight': weight + pend_weight,
            'forced': pend_forced,
            'lrpb': pend_lrpb,
        })
        pend_forced = 0
        pend_lrpb = 0
        pend_weight = 0

    def _harvest_images(xml_element, position: int) -> None:
        for pic_elem in xml_element.iter(_PIC_PIC):
            blip = pic_elem.find(f'.//{_A_BLIP}')
            if blip is None:
                continue
            r_id = blip.get(_R_EMBED)
            if not r_id or r_id not in doc.part.rels:
                continue
            try:
                raw = doc.part.rels[r_id].target_part.blob
                raw_h = dhash(raw)  # hash of original bytes, used to pair flipped images
                img = PILImage.open(io.BytesIO(raw)).convert('RGB')
                # Read flipH/flipV from a:xfrm, apply to image so the hash
                # and displayed thumbnail reflect the on-page orientation.
                xfrm = pic_elem.find(f'.//{_A_XFRM}')
                flip_h = xfrm is not None and xfrm.get('flipH') in ('1', 'true')
                flip_v = xfrm is not None and xfrm.get('flipV') in ('1', 'true')
                if flip_h:
                    img = img.transpose(PILImage.FLIP_LEFT_RIGHT)
                if flip_v:
                    img = img.transpose(PILImage.FLIP_TOP_BOTTOM)
                buf = io.BytesIO()
                img.save(buf, format='JPEG', quality=_IMG_JPEG_QUALITY)
                transformed = buf.getvalue()
                h = dhash(transformed)
                transform = (flip_h, flip_v)
                # Match only if both hash and transform agree: same image with a
                # different flip must produce a separate entry so the change is visible.
                matched = next(
                    (k for k in images
                     if images_are_similar(k, h) and images[k].get('transform') == transform),
                    None,
                )
                if matched:
                    if position not in images[matched]['pages']:
                        images[matched]['pages'].append(position)
                else:
                    b64 = base64.b64encode(transformed).decode('ascii')
                    images[h] = {
                        'b64': b64,
                        'pages': [position],
                        'transform': transform,
                        'raw_hash': raw_h,
                        'hash16': hash16(transformed),
                    }
            except Exception as e:
                logger.debug('DOCX image extraction skipped: %s', e)

    def _img_count(xml_element) -> int:
        return sum(1 for _ in xml_element.iter(_A_BLIP))

    for element in _iter_block_items(doc.element.body):
        tag = element.tag.split('}')[-1] if '}' in element.tag else element.tag

        if tag == 'p':
            if _is_toc_paragraph(element):
                continue
            # Page-break markers preceding this paragraph's content. Forced and
            # lastRendered are kept separate so stale lastRendered can be dropped.
            f, l = _count_breaks_split(element)
            pend_forced += f
            pend_lrpb += l
            total_forced += f
            total_lrpb += l

            text = _paragraph_text(element)
            txbx_lines = _textbox_texts(element)
            # VML arrows are interleaved at their real position (sharing vml_seen), not dumped at the end where they would
            # collapse onto the last page.
            vml_lines = _vml_shape_lines(element, vml_seen)
            n_images = _img_count(element)
            if text or n_images or txbx_lines or vml_lines:
                text_pos += 1
                _harvest_images(element, text_pos)
                # The image weight counts once, on the first emitted line; text boxes and VML arrows are overlay content (weight 0).
                img_weight = n_images * _IMG_WEIGHT
                if text:
                    _emit(text, len(text) + img_weight)
                    img_weight = 0
                for line in txbx_lines:
                    _emit(line, img_weight)
                    img_weight = 0
                for line in vml_lines:
                    _emit(line, img_weight)
                    img_weight = 0
                if img_weight:
                    # Image-only paragraph: no line emitted, carry its weight on.
                    pend_weight += img_weight

        elif tag == 'tbl':
            table = DocxTable(element, doc)
            # The first non-empty row gives the column labels only when it looks like a header; otherwise rows stay positional
            # (empty cells kept, so a value never shifts column).
            header: Dict[int, str] = {}
            header_checked = False
            seen_rows: set = set()

            def _positional(cells: List[Tuple[int, str]]) -> str:
                by_col = dict(cells)
                return ' | '.join(by_col.get(c, '') for c in range(max(by_col) + 1))

            for row in table.rows:
                cells = _row_cells(row)
                n_images = _img_count(row._tr)
                vml_lines = _vml_shape_lines(row._tr, vml_seen)
                # Page breaks can appear inside table cells too
                f, l = _count_breaks_split(row._tr)
                pend_forced += f
                pend_lrpb += l
                total_forced += f
                total_lrpb += l

                if not cells and not n_images and not vml_lines:
                    continue

                if not header and cells and not header_checked:
                    header_checked = True
                    n_cols = len({id(c._tc) for c in row.cells})
                    if len(cells) == n_cols and looks_like_table_header([val for _, val in cells]):
                        header = dict(cells)
                    row_text = ' | '.join(val for _, val in cells) if header else _positional(cells)
                    if row_text not in seen_rows:
                        seen_rows.add(row_text)
                        text_pos += 1
                        _harvest_images(row._tr, text_pos)
                        _emit(row_text, len(row_text) + n_images * _IMG_WEIGHT)
                    continue

                if header and cells:
                    parts = [f'{header.get(col, str(col + 1))}: {val}' for col, val in cells]
                    row_text = ' | '.join(parts)
                elif cells:
                    row_text = _positional(cells)
                else:
                    row_text = ''

                if row_text or n_images or vml_lines:
                    text_pos += 1
                    _harvest_images(row._tr, text_pos)
                    img_weight = n_images * _IMG_WEIGHT
                    if row_text and row_text not in seen_rows:
                        seen_rows.add(row_text)
                        _emit(row_text, len(row_text) + img_weight)
                        img_weight = 0
                    for line in vml_lines:
                        _emit(line, img_weight)
                        img_weight = 0
                    if img_weight:
                        pend_weight += img_weight

    for line in _footnote_lines(doc):
        text_pos += 1
        _emit(line, 0)

    # Decide page numbers now that the whole document (and its break-marker
    # totals) is known, then format the [Page N, Para M] tags.
    use_lrpb = not _lrpb_is_stale(total_forced, total_lrpb)
    _assign_pages(entries, use_lrpb)
    texts = [f"{e['text']} [Page {e['page']}, Para {e['pos']}]" for e in entries]

    # Images were recorded against paragraph counters: remap them onto the estimated pages so DOCX images get the same
    # position-aware pairing as PDF/PPTX. An image-only paragraph inherits the page of the preceding entry.
    pos_to_page = {e['pos']: e['page'] for e in entries}
    sorted_pos = sorted(pos_to_page)

    def _page_for(pos: int) -> int:
        if pos in pos_to_page:
            return pos_to_page[pos]
        i = bisect_right(sorted_pos, pos) - 1
        return pos_to_page[sorted_pos[i]] if i >= 0 else 1

    for meta in images.values():
        meta['pages'] = sorted({_page_for(p) for p in meta['pages']}) or [1]

    return texts, images
