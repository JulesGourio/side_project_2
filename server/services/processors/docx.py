"""DOCX / DOC processor — three processing methods.

standard:    paragraph semantic diff + focused Markdown bullet-list output.
structured:  paragraph semantic diff + strict JSON array output (for Excel export).
comparative: section canonical diff + section-grouped output.
"""

import base64
import io
import logging
import re
from bisect import bisect_right
from typing import Any, Dict, List, Tuple

from docx import Document
from PIL import Image as PILImage

from .base import BaseProcessor, ProcessMetadata, ProcessResult
from ._diff_engines import (
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
from ..conversion import doc_to_text

logger = logging.getLogger(__name__)


_W_TXBX = '{http://schemas.openxmlformats.org/wordprocessingml/2006/main}txbxContent'
_W_T    = '{http://schemas.openxmlformats.org/wordprocessingml/2006/main}t'
_W_P    = '{http://schemas.openxmlformats.org/wordprocessingml/2006/main}p'
_W_BR   = '{http://schemas.openxmlformats.org/wordprocessingml/2006/main}br'
_W_LRPB = '{http://schemas.openxmlformats.org/wordprocessingml/2006/main}lastRenderedPageBreak'

_VML_NS    = 'urn:schemas-microsoft-com:vml'
_VML_SHAPE = f'{{{_VML_NS}}}shape'
_VML_LINE  = f'{{{_VML_NS}}}line'
_VML_STR   = f'{{{_VML_NS}}}stroke'

# VML spt codes that represent connectors/arrows (type attribute values)
_VML_ARROW_TYPES = frozenset({
    '#_x0000_t32',  # straight connector with arrow
    '#_x0000_t64',  # up-down arrow
    '#_x0000_t94',  # notched right arrow
    '#_x0000_t89',  # striped right arrow
    '#_x0000_t70',  # left-right arrow
    '#_x0000_t180', # curved right arrow
})


_W_TYPE = '{http://schemas.openxmlformats.org/wordprocessingml/2006/main}type'

_W_NS = '{http://schemas.openxmlformats.org/wordprocessingml/2006/main}'
_W_TAB, _W_CR, _W_NBH = f'{_W_NS}tab', f'{_W_NS}cr', f'{_W_NS}noBreakHyphen'
_W_TBL, _W_SDT, _W_SDT_CONTENT = f'{_W_NS}tbl', f'{_W_NS}sdt', f'{_W_NS}sdtContent'
_W_PSTYLE, _W_VAL, _W_ID = f'{_W_NS}pStyle', f'{_W_NS}val', f'{_W_NS}id'
_W_FOOTNOTE = f'{_W_NS}footnote'
# Subtrees whose text is not part of the paragraph as the reader sees it:
# tracked deletions and the origin of a tracked move (the text now lives at its
# destination), paragraph properties, text boxes (extracted on their own by
# _textbox_texts) and the legacy fallback copy of a drawing.
_SKIPPED_SUBTREES = frozenset({
    f'{_W_NS}del', f'{_W_NS}moveFrom', f'{_W_NS}pPr', _W_TXBX,
    '{http://schemas.openxmlformats.org/markup-compatibility/2006}Fallback',
})
# Table-of-contents entries ("TOC 1" in English Word, "TM 1" in French Word):
# section title + page number, which changes on every repagination.
_TOC_STYLE_RE = re.compile(r'^(?:TOC|TM)\s?\d', re.I)


def _paragraph_text(p_elem) -> str:
    """Text of one w:p as displayed with every tracked change accepted.

    python-docx's Paragraph.text only reads runs that are DIRECT children of the
    paragraph (plus hyperlinks). Text inside a tracked insertion (w:ins), an
    inline content control (w:sdt), a simple field (w:fldSimple) or a smart tag
    was silently missing, so an edit made with track changes on — the normal
    way a revision is prepared — was invisible to the comparison (2026-10-04).
    """
    out: List[str] = []

    def _walk(el) -> None:
        for child in el:
            tag = child.tag
            if tag in _SKIPPED_SUBTREES:
                continue
            if tag == _W_T:
                out.append(child.text or '')
            elif tag in (_W_TAB, _W_BR, _W_CR):
                out.append(' ')
            elif tag == _W_NBH:
                out.append('-')
            else:
                _walk(child)

    _walk(p_elem)
    return ' '.join(''.join(out).split())


def _is_toc_paragraph(p_elem) -> bool:
    style = p_elem.find(f'{_W_NS}pPr/{_W_PSTYLE}')
    return style is not None and bool(_TOC_STYLE_RE.match(style.get(_W_VAL, '')))


def _iter_block_items(parent):
    """Yield the w:p / w:tbl elements of a body, descending into block-level
    content controls (w:sdt) — cover pages, form sections and locked clauses
    live there and used to be skipped entirely."""
    for child in parent:
        if child.tag in (_W_P, _W_TBL):
            yield child
        elif child.tag == _W_SDT:
            content = child.find(_W_SDT_CONTENT)
            if content is not None:
                yield from _iter_block_items(content)


def _footnote_lines(doc) -> List[str]:
    """'Footnote N: text' for each real footnote (separators have id <= 0)."""
    lines: List[str] = []
    try:
        from lxml import etree
        for rel in doc.part.rels.values():
            if not rel.reltype.endswith('/footnotes') or rel.is_external:
                continue
            root = etree.fromstring(rel.target_part.blob)
            for note in root.iter(_W_FOOTNOTE):
                try:
                    num = int(note.get(_W_ID, '0'))
                except ValueError:
                    continue
                if num <= 0:
                    continue
                text = ' '.join(filter(None, (_paragraph_text(p) for p in note.iter(_W_P))))
                if text:
                    lines.append(f'Footnote {num}: {text}')
    except Exception as e:  # footnotes are a bonus — never fail extraction over them
        logger.debug('DOCX footnote extraction skipped: %s', e)
    return lines

# --- Page-number estimation tuning -------------------------------------------
# A DOCX has no reliable page numbers without a rendering engine (pagination
# depends on fonts, line wrapping, image scaling, margins...). We approximate by
# combining the only trustworthy signals — page-break markers — with a
# content-flow estimate calibrated on real Latécoère documents.
#
#   _PAGE_WEIGHT_CAP : flow "weight" that fills one page. weight = characters of
#                      flowing text + _IMG_WEIGHT per embedded image. Overlay
#                      content (text boxes, VML arrows on a diagram) has weight 0
#                      — it does not consume vertical space, it sits on top of a
#                      page that is already counted. Calibrated to ~0 error on
#                      text documents (A321, FI252) and +6/70 on an image-heavy
#                      CAD assembly document.
#   _IMG_WEIGHT      : vertical space one embedded image is worth, in characters.
_PAGE_WEIGHT_CAP = 2200
_IMG_WEIGHT = 400


def _count_breaks_split(element) -> Tuple[int, int]:
    """Return (explicit_breaks, lastRendered_breaks) separately for one element.

    Explicit <w:br w:type="page"/> are author-forced and always reliable.
    <w:lastRenderedPageBreak/> are Word's cached render boundaries — accurate
    only when the file was freshly saved by Word, stale/partial otherwise.
    Keeping them separate lets the caller drop the lastRendered ones when they
    look stale (see _lrpb_is_stale).
    """
    forced = 0
    lrpb = 0
    for node in element.iter():
        if node.tag == _W_BR and node.get(_W_TYPE) == 'page':
            forced += 1
        elif node.tag == _W_LRPB:
            lrpb += 1
    return forced, lrpb


def _lrpb_is_stale(total_forced: int, total_lrpb: int) -> bool:
    """Decide whether <w:lastRenderedPageBreak/> markers should be ignored.

    On a freshly rendered document Word emits a lastRenderedPageBreak at every
    page boundary — including just before each forced break — so the count is
    normally >= the number of forced breaks. When it is much smaller, the
    document was edited after its last full render and the markers are a stale
    subset that would inflate the page count (e.g. one CAD file had 65 forced
    breaks but only 27 lastRendered, pushing a 70-page doc to 93). In that case
    we discard them and rely on forced breaks + content flow instead.
    """
    return total_lrpb < total_forced * 0.5


def _assign_pages(entries: List[Dict[str, Any]], use_lrpb: bool) -> None:
    """Assign a 1-based page number to each entry, in place (sets entry['page']).

    entries is the document-ordered list produced by the body walk. Each entry
    carries:
        weight : flow weight (0 for overlay content — text boxes / VML arrows)
        forced : explicit page breaks occurring just before this entry
        lrpb   : lastRenderedPageBreaks occurring just before this entry

    A new page starts on every hard break (forced, plus lastRendered when not
    stale) and, between breaks, whenever accumulated flow weight would exceed
    _PAGE_WEIGHT_CAP. The weight rule paginates marker-poor documents (long text
    files with only a couple of explicit breaks) without disturbing marker-rich
    ones, where breaks fire first and keep the accumulator small.
    """
    page = 1
    acc = 0
    for e in entries:
        hard = e['forced'] + (e['lrpb'] if use_lrpb else 0)
        if hard:
            page += hard
            acc = 0
        w = e['weight']
        if acc > 0 and w > 0 and acc + w > _PAGE_WEIGHT_CAP:
            page += 1
            acc = 0
        e['page'] = page
        acc += w


def _extract_doc(doc_bytes: bytes, filename: str) -> Tuple[List[str], List[Tuple[str, str]]]:
    text = doc_to_text(doc_bytes, filename)
    return (text.splitlines() if text else []), []


def _parse_vml_style(style_str: str) -> Dict[str, str]:
    props: Dict[str, str] = {}
    for part in style_str.split(';'):
        part = part.strip()
        if ':' in part:
            k, v = part.split(':', 1)
            props[k.strip()] = v.strip()
    return props


def _vml_shape_lines(body, seen_ids: set = None) -> List[str]:
    """Extract VML connector/arrow geometry as diffable text lines.

    Only covers shapes that are arrows or connectors (by VML type code or
    presence of an endarrow stroke attribute). Text boxes and decorative
    shapes are skipped to avoid noise.

    `seen_ids` may be passed by the caller to share dedup state across repeated
    per-paragraph calls (used to interleave shapes at their real document
    position instead of dumping them all at the end).
    """
    lines: List[str] = []
    if seen_ids is None:
        seen_ids = set()

    for shape in body.iter(_VML_SHAPE):
        stype = shape.get('type', '')
        style = shape.get('style', '')
        if not style:
            continue
        stroke = shape.find(_VML_STR)
        is_arrow = (stype in _VML_ARROW_TYPES) or (
            stroke is not None and stroke.get('endarrow', '') not in ('', 'none')
        )
        if not is_arrow:
            continue
        sid = shape.get('id', '').strip()
        if sid in seen_ids:
            continue
        seen_ids.add(sid)
        p = _parse_vml_style(style)
        parts = [f'[Arrow "{sid}"']
        # Position (margin-left / margin-top) is deliberately EXCLUDED: a pure
        # drag/reflow that only moves an arrow is cosmetic and was being reported
        # as a spurious change. Only size (width/height) and direction (flip)
        # remain — so add/remove, resize, and reversal are still detected, while
        # a position-only move produces an identical line (no diff).
        for key in ('width', 'height', 'flip'):
            if key in p:
                parts.append(f'{key}: {p[key]}')
        lines.append(' '.join(parts) + ']')

    for vline in body.iter(_VML_LINE):
        sid = vline.get('id', '').strip()
        if sid in seen_ids:
            continue
        seen_ids.add(sid)
        frm = vline.get('from', '')
        to  = vline.get('to', '')
        stroke = vline.find(_VML_STR)
        endarrow = stroke.get('endarrow', '') if stroke is not None else ''
        color = vline.get('strokecolor', '')
        parts = [f'[Line "{sid}"']
        if frm:      parts.append(f'from: {frm}')
        if to:       parts.append(f'to: {to}')
        if endarrow: parts.append(f'arrow: {endarrow}')
        if color:    parts.append(f'color: {color}')
        lines.append(' '.join(parts) + ']')

    return lines


def _textbox_texts(element) -> List[str]:
    """Return deduplicated text lines from all w:txbxContent descendants.

    mc:AlternateContent blocks can hold the same w:txbxContent in both
    mc:Choice and mc:Fallback. Deduplicating by content avoids double entries
    regardless of which branch actually carries the text in a given document.
    """
    lines = []
    seen: set = set()
    for txbx in element.iter(_W_TXBX):
        for p_elem in txbx.iter(_W_P):
            t = ''.join(node.text or '' for node in p_elem.iter(_W_T)).strip()
            if t and t not in seen:
                lines.append(t)
                seen.add(t)
    return lines


def _row_cells(row) -> List[Tuple[int, str]]:
    """(column index, text) for each non-empty cell of a table row.

    The column index is kept so a value is labelled with ITS column: the
    previous version dropped empty cells and cells repeating a value already
    seen in the row, which shifted every following label one column to the
    left ("Min: 10 | Max: 10 | Tool: A" came out as "Min: 10 | Max: A").
    A merged cell spans several grid columns but is one w:tc — it is reported
    once, at its first column. Nested tables and text boxes are included.
    """
    seen_tc: set = set()
    result: List[Tuple[int, str]] = []
    for col, c in enumerate(row.cells):
        if id(c._tc) in seen_tc:
            continue
        seen_tc.add(id(c._tc))
        parts = [_paragraph_text(p) for p in c._tc.iter(_W_P) if not _in_textbox(p, c._tc)]
        parts.extend(_textbox_texts(c._tc))
        t = ' '.join(filter(None, parts)).strip()
        if t:
            result.append((col, t))
    return result


def _in_textbox(p_elem, stop) -> bool:
    node = p_elem.getparent()
    while node is not None and node is not stop:
        if node.tag == _W_TXBX:
            return True
        node = node.getparent()
    return False


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
    text_pos = 0          # increments only on non-empty content (the "Para M" tag)
    total_forced = 0      # explicit page breaks across the whole document
    total_lrpb = 0        # lastRenderedPageBreaks across the whole document
    vml_seen: set = set()  # shared dedup state for interleaved VML shapes
    # Breaks/weight from elements that emit no output line (e.g. image-only
    # paragraphs) are carried forward and folded into the next emitted entry.
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
            # Interleave VML arrows/connectors at their real document position
            # (sharing vml_seen for global dedup) instead of dumping them all at
            # the end — otherwise they collapse onto the last page.
            vml_lines = _vml_shape_lines(element, vml_seen)
            n_images = _img_count(element)
            if text or n_images or txbx_lines or vml_lines:
                text_pos += 1
                _harvest_images(element, text_pos)
                # Image weight counts once, on the first emitted line; text boxes
                # and VML arrows are overlay content (weight 0 — they sit on a
                # page that is already accounted for).
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
            header: Dict[int, str] = {}
            seen_rows: set = set()

            for row_idx, row in enumerate(table.rows):
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

                if not header and cells:
                    header = dict(cells)
                    row_text = ' | '.join(val for _, val in cells)
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
                    row_text = ' | '.join(val for _, val in cells)
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

    # Images were recorded against paragraph counters — remap them onto the
    # estimated page numbers just assigned to the text, so DOCX images get
    # "Page N" labels and position-aware pairing like PDF/PPTX. Positions
    # with no text entry (image-only paragraphs) inherit the page of the
    # nearest preceding entry.
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


class DocxProcessor(BaseProcessor):

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

    def _paragraph_diff(self, old_bytes, old_name, new_bytes, new_name, system_prompt, method_name):
        logger.info('DocxProcessor [%s]: paragraph semantic diff — %s → %s', method_name, old_name, new_name)
        old_text, new_text, old_images, new_images = _load_docx(old_bytes, old_name, new_bytes, new_name)
        warnings = extraction_warnings(old_text, new_text)

        diff_text, filtered = paragraph_semantic_diff(old_text, new_text, page_label='Page')
        diff_text = truncate_diff(diff_text)
        # Estimated page numbers (same estimator as the text tags) enable the
        # position-aware pairing path — important for operator documents that
        # contain several visually similar schemas.
        img_blocks = image_diff_blocks_dual(old_images, new_images, show_position=True)
        img_pairs = image_diff_pairs(old_images, new_images, show_position=True)

        intro = (
            f'Global paragraph alignment: {filtered} trivial lines filtered.\n'
            'Similar paragraphs shown as MODIFIED with ~~removed~~ and **added** words inline.\n'
            'Image page numbers are estimated from content flow (same estimate as the text [Page N] tags).\n\n'
            f'--- TEXT CHANGES ---\n{diff_text}\n\n--- VISUAL CHANGES ---'
        )
        if not diff_text.strip():
            intro = 'No significant text changes detected.\n\n--- VISUAL CHANGES ---'

        content_blocks: List[Dict[str, Any]] = [{'type': 'text', 'text': intro}] + img_blocks
        messages = [
            {'role': 'system', 'content': system_prompt},
            {'role': 'user', 'content': content_blocks},
        ]
        return ProcessResult(
            messages=messages,
            metadata=ProcessMetadata(file_type='docx', method=method_name, old_name=old_name, new_name=new_name),
            image_pairs=img_pairs,
            warnings=warnings,
        )

    def _comparative_diff(self, old_bytes, old_name, new_bytes, new_name, system_prompt):
        logger.info('DocxProcessor [comparative]: section canonical diff — %s → %s', old_name, new_name)
        old_text, new_text, old_images, new_images = _load_docx(old_bytes, old_name, new_bytes, new_name)
        warnings = extraction_warnings(old_text, new_text)

        diff_text, filtered = section_canonical_diff(old_text, new_text)
        diff_text = truncate_diff(diff_text)
        img_blocks = image_diff_blocks_dual(old_images, new_images, show_position=True)
        img_pairs = image_diff_pairs(old_images, new_images, show_position=True)

        intro = (
            f'Section canonical diff: {filtered} trivial pairs filtered.\n'
            'Changes grouped by section.\n\n'
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
            metadata=ProcessMetadata(file_type='docx', method='comparative', old_name=old_name, new_name=new_name),
            image_pairs=img_pairs,
            warnings=warnings,
        )