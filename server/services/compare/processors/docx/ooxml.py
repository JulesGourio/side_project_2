"""Text of the WordprocessingML elements: paragraphs, tables, text boxes, VML arrows and footnotes."""

import logging
import re
from typing import Dict, List, Tuple

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
# Subtrees whose text is not part of the paragraph as the reader sees it: tracked deletions, the origin of a tracked move,
# paragraph properties, text boxes (extracted separately) and the mc:Fallback copy of a drawing.
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
    was silently missing, so an edit made with track changes on (the normal
    way a revision is prepared) was invisible to the comparison.
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
    live there, so they must not be skipped."""
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
            # Uploaded file: no entity expansion, no DTD, no network access.
            parser = etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False, huge_tree=False)
            root = etree.fromstring(rel.target_part.blob, parser)
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
    per-paragraph calls (so shapes interleave at their real document position
    instead of all at the end).
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
        # Position (margin-left / margin-top) is excluded: a pure drag is cosmetic. Size and flip stay, so add, remove,
        # resize and reversal are still detected.
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

    The column index is kept so a value is labelled with ITS column: empty cells keep their
    place in the numbering, so no label shifts to a neighbouring column.
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

