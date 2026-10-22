"""Text lines and images of a DOCX (or .doc), in document order, tagged [Page N, Para M]."""

import base64
import io
import logging
from bisect import bisect_right
from typing import Any, Dict, List, Tuple

from docx import Document
from docx.table import Table as DocxTable
from PIL import Image as PILImage

from server.services.compare.processors._diff_engines import _IMG_JPEG_QUALITY, dhash, hash16, images_are_similar, looks_like_table_header
from server.services.compare.conversion import doc_to_text
from server.services.compare.processors.docx.ooxml import (
    _footnote_lines, _header_footer_lines, _is_toc_paragraph, _iter_block_items, _paragraph_text, _row_cells, _textbox_texts,
    _vml_shape_lines,
)
from server.services.compare.processors.docx.pages import _IMG_WEIGHT, _assign_pages, _count_breaks_split, _lrpb_is_stale

logger = logging.getLogger(__name__)

_A_BLIP = '{http://schemas.openxmlformats.org/drawingml/2006/main}blip'
_A_XFRM = '{http://schemas.openxmlformats.org/drawingml/2006/main}xfrm'
_PIC_PIC = '{http://schemas.openxmlformats.org/drawingml/2006/picture}pic'
_R_EMBED = '{http://schemas.openxmlformats.org/officeDocument/2006/relationships}embed'


def _extract_doc(doc_bytes: bytes, filename: str) -> Tuple[List[str], List[Tuple[str, str]]]:
    """Text lines of a binary .doc file; a .doc carries no images here.

    :param doc_bytes: content of the .doc file
    :param filename: file name, for the logs
    :return: ``(text lines, [])``
    """
    text = doc_to_text(doc_bytes, filename)
    return (text.splitlines() if text else []), []


def _img_count(xml_element) -> int:
    """Number of images (DrawingML blips) in an element.

    :param xml_element: XML element to search
    :return: the number of images
    """
    return sum(1 for _ in xml_element.iter(_A_BLIP))


def _positional(cells: List[Tuple[int, str]]) -> str:
    """A table row joined by column position, with empty cells kept.

    :param cells: ``(column index, text)`` pairs of the row
    :return: the cells joined by ``|``
    """
    by_col = dict(cells)
    return ' | '.join(by_col.get(c, '') for c in range(max(by_col) + 1))


def _oriented_image(doc: Any, pic_elem: Any) -> Dict[str, Any] | None:
    """The image of a picture element as a JPEG in its on-page orientation, with its hashes.

    The flipH / flipV of ``a:xfrm`` are applied to the pixels, so the hash and the displayed thumbnail reflect what the page shows.

    :param doc: the python-docx document, which owns the image parts
    :param pic_elem: the ``pic:pic`` element
    :return: ``{'jpeg', 'hash', 'raw_hash', 'hash16', 'transform'}``, or None when the picture has no readable image
    """
    blip = pic_elem.find(f'.//{_A_BLIP}')
    if blip is None:
        return None
    r_id = blip.get(_R_EMBED)
    if not r_id or r_id not in doc.part.rels:
        return None
    raw = doc.part.rels[r_id].target_part.blob
    img = PILImage.open(io.BytesIO(raw)).convert('RGB')
    xfrm = pic_elem.find(f'.//{_A_XFRM}')
    flip_h = xfrm is not None and xfrm.get('flipH') in ('1', 'true')
    flip_v = xfrm is not None and xfrm.get('flipV') in ('1', 'true')
    if flip_h:
        img = img.transpose(PILImage.FLIP_LEFT_RIGHT)
    if flip_v:
        img = img.transpose(PILImage.FLIP_TOP_BOTTOM)
    buf = io.BytesIO()
    img.save(buf, format='JPEG', quality=_IMG_JPEG_QUALITY)
    jpeg = buf.getvalue()
    return {'jpeg': jpeg, 'hash': dhash(jpeg), 'raw_hash': dhash(raw), 'hash16': hash16(jpeg),
            'transform': (flip_h, flip_v)}


class _DocxWalk:
    """The walk over the body of a .docx: output lines with their page estimate, and the images by hash."""

    def __init__(self, doc: Any) -> None:
        """Start a walk.

        :param doc: the python-docx document to walk
        """
        self.doc = doc
        self.entries: List[Dict[str, Any]] = []  # one per output line; page assigned after the walk
        self.images: Dict[str, Dict[str, Any]] = {}
        self.text_pos = 0
        self.total_forced = 0
        self.total_lrpb = 0
        self.vml_seen: set = set()
        # Breaks and weight of elements with no output line (image-only paragraphs) carry forward into the next entry.
        self.pend_forced = 0
        self.pend_lrpb = 0
        self.pend_weight = 0

    def emit(self, text: str, weight: int) -> None:
        """Append one output entry, folding in any pending breaks/weight.

        :param text: text of the output line
        :param weight: weight of the line for the page estimate; the pending weight of image-only paragraphs is added
        """
        self.entries.append({
            'text': text,
            'pos': self.text_pos,
            'weight': weight + self.pend_weight,
            'forced': self.pend_forced,
            'lrpb': self.pend_lrpb,
        })
        self.pend_forced = 0
        self.pend_lrpb = 0
        self.pend_weight = 0

    def count_breaks(self, xml_element: Any) -> None:
        """Add the page-break markers of an element to the pending and total counts.

        Forced and last-rendered breaks are kept apart so a stale last-rendered count can be dropped.

        :param xml_element: paragraph or table row
        """
        forced, lrpb = _count_breaks_split(xml_element)
        self.pend_forced += forced
        self.pend_lrpb += lrpb
        self.total_forced += forced
        self.total_lrpb += lrpb

    def harvest_images(self, xml_element: Any, position: int) -> None:
        """Records the images of an element against the position of its output line.

        :param xml_element: paragraph or table row holding the images
        :param position: counter value of the output line
        """
        for pic_elem in xml_element.iter(_PIC_PIC):
            try:
                image = _oriented_image(self.doc, pic_elem)
                if image is None:
                    continue
                # Match only if both hash and transform agree: same image with a different flip must produce a
                # separate entry so the change is visible.
                matched = next(
                    (k for k in self.images
                     if images_are_similar(k, image['hash']) and self.images[k].get('transform') == image['transform']),
                    None,
                )
                if matched:
                    if position not in self.images[matched]['pages']:
                        self.images[matched]['pages'].append(position)
                else:
                    self.images[image['hash']] = {
                        'b64': base64.b64encode(image['jpeg']).decode('ascii'),
                        'pages': [position],
                        'transform': image['transform'],
                        'raw_hash': image['raw_hash'],
                        'hash16': image['hash16'],
                    }
            except Exception as e:
                logger.debug('DOCX image extraction skipped: %s', e)

    def walk_paragraph(self, element: Any) -> None:
        """Emit the lines of a body paragraph: its text, then its text boxes and VML shapes.

        :param element: the ``w:p`` element
        """
        if _is_toc_paragraph(element):
            return
        self.count_breaks(element)
        text = _paragraph_text(element)
        txbx_lines = _textbox_texts(element)
        # VML arrows are interleaved at their real position (sharing vml_seen), not dumped at the end where they would
        # collapse onto the last page.
        vml_lines = _vml_shape_lines(element, self.vml_seen)
        n_images = _img_count(element)
        if not (text or n_images or txbx_lines or vml_lines):
            return
        self.text_pos += 1
        self.harvest_images(element, self.text_pos)
        # The image weight counts once, on the first emitted line; text boxes and VML arrows are overlay content (weight 0).
        img_weight = n_images * _IMG_WEIGHT
        if text:
            self.emit(text, len(text) + img_weight)
            img_weight = 0
        for line in txbx_lines + vml_lines:
            self.emit(line, img_weight)
            img_weight = 0
        if img_weight:
            # Image-only paragraph: no line emitted, carry its weight on.
            self.pend_weight += img_weight

    def walk_table(self, element: Any) -> None:
        """Emit the lines of a table: one per row, labelled with the column names of its header row.

        The first non-empty row gives the column labels only when it looks like a header; otherwise rows stay positional
        (empty cells kept, so a value never shifts column).

        :param element: the ``w:tbl`` element
        """
        table = DocxTable(element, self.doc)
        header: Dict[int, str] = {}
        header_checked = False
        seen_rows: set = set()

        for row in table.rows:
            cells = _row_cells(row)
            n_images = _img_count(row._tr)
            vml_lines = _vml_shape_lines(row._tr, self.vml_seen)
            self.count_breaks(row._tr)  # page breaks can appear inside table cells too

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
                    self.text_pos += 1
                    self.harvest_images(row._tr, self.text_pos)
                    self.emit(row_text, len(row_text) + n_images * _IMG_WEIGHT)
                continue

            if header and cells:
                row_text = ' | '.join(f'{header.get(col, str(col + 1))}: {val}' for col, val in cells)
            elif cells:
                row_text = _positional(cells)
            else:
                row_text = ''

            if row_text or n_images or vml_lines:
                self.text_pos += 1
                self.harvest_images(row._tr, self.text_pos)
                img_weight = n_images * _IMG_WEIGHT
                if row_text and row_text not in seen_rows:
                    seen_rows.add(row_text)
                    self.emit(row_text, len(row_text) + img_weight)
                    img_weight = 0
                for line in vml_lines:
                    self.emit(line, img_weight)
                    img_weight = 0
                if img_weight:
                    self.pend_weight += img_weight

    def finish(self) -> Tuple[List[str], Dict[str, Dict[str, Any]]]:
        """Footnotes, headers and footers, page numbers and image pages, once the whole body is walked.

        Page numbers are decided now that the break-marker totals are known. Images were recorded against paragraph
        counters: they are remapped onto the estimated pages so DOCX images get the same position-aware pairing as
        PDF/PPTX. An image-only paragraph inherits the page of the preceding entry.

        :return: ``(text lines tagged [Page N, Para M], images keyed by hash)``
        """
        for line in _footnote_lines(self.doc) + _header_footer_lines(self.doc):
            self.text_pos += 1
            self.emit(line, 0)

        _assign_pages(self.entries, not _lrpb_is_stale(self.total_forced, self.total_lrpb))
        texts = [f"{e['text']} [Page {e['page']}, Para {e['pos']}]" for e in self.entries]

        pos_to_page = {e['pos']: e['page'] for e in self.entries}
        sorted_pos = sorted(pos_to_page)

        def page_for(pos: int) -> int:
            """Page of a paragraph position; a position without an entry takes the page of the entry before it.

            :param pos: paragraph counter
            :return: the page number, 1 before the first entry
            """
            if pos in pos_to_page:
                return pos_to_page[pos]
            i = bisect_right(sorted_pos, pos) - 1
            return pos_to_page[sorted_pos[i]] if i >= 0 else 1

        for meta in self.images.values():
            meta['pages'] = sorted({page_for(p) for p in meta['pages']}) or [1]
        return texts, self.images


def _extract_docx(docx_bytes: bytes) -> Tuple[List[str], Dict[str, Dict[str, Any]]]:
    """Return (text_lines, dhash_images_dict).

    Extracts paragraphs AND tables in document order, each tagged [Page N, Para M].
    Tags are stripped by the diff engine (strip_tag) before comparison and output,
    so they never appear in the report or cause false positives.
    Page numbers are derived from explicit page breaks (<w:br w:type="page"/>) and
    implicit breaks recorded by Word at save time (<w:lastRenderedPageBreak/>).
    Table data rows are labelled with column names from the header row.

    :param docx_bytes: content of the .docx file
    :return: ``(text lines, images keyed by hash)``
    """
    try:
        doc = Document(io.BytesIO(docx_bytes))
    except Exception as e:
        logger.warning('python-docx could not open file: %s', e)
        return [], {}

    walk = _DocxWalk(doc)
    for element in _iter_block_items(doc.element.body):
        tag = element.tag.split('}')[-1] if '}' in element.tag else element.tag
        if tag == 'p':
            walk.walk_paragraph(element)
        elif tag == 'tbl':
            walk.walk_table(element)
    return walk.finish()
