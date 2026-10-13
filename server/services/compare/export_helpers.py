"""Pure export helper functions — no FastAPI / Databricks / asyncpg dependencies.

Extracted here so the notebook (debug_prompt.ipynb) can import them directly
without triggering the server-side import chain in routers/exports.py.
"""

import base64
import io
import json
import logging
import re
from typing import Any

logger = logging.getLogger(__name__)


# --- JSON parsing ---

def _parse_json_response(text: str):
    """Robust JSON array parser — three fallback strategies."""
    stripped = text.strip()
    try:
        data = json.loads(stripped)
        if isinstance(data, list):
            return data
    except json.JSONDecodeError:
        pass

    m = re.search(r'`{3}(?:json)?\s*([\s\S]*?)`{3}', stripped)
    if m:
        try:
            data = json.loads(m.group(1).strip())
            if isinstance(data, list):
                return data
        except json.JSONDecodeError:
            pass

    m = re.search(r'(\[[\s\S]*\])', stripped)
    if m:
        try:
            data = json.loads(m.group(1))
            if isinstance(data, list):
                return data
        except json.JSONDecodeError:
            pass
    return None


# --- Image helpers ---

def _make_composite_image(old_b64: str | None, new_b64: str | None, width: int = 200, height: int = 80):
    """Return a BytesIO of a side-by-side OLD|NEW JPEG thumbnail, or None on failure."""
    try:
        from PIL import Image as PILImage, ImageDraw
        composite = PILImage.new('RGB', (width, height), color=(255, 255, 255))
        half = width // 2
        if old_b64:
            old_img = PILImage.open(io.BytesIO(base64.b64decode(old_b64))).convert('RGB')
            old_img.thumbnail((half - 2, height - 2), PILImage.Resampling.LANCZOS)
            composite.paste(old_img, (1, (height - old_img.height) // 2))
        if new_b64:
            new_img = PILImage.open(io.BytesIO(base64.b64decode(new_b64))).convert('RGB')
            new_img.thumbnail((half - 2, height - 2), PILImage.Resampling.LANCZOS)
            composite.paste(new_img, (half + 1, (height - new_img.height) // 2))
        draw = ImageDraw.Draw(composite)
        draw.line([(half, 0), (half, height)], fill=(200, 200, 200), width=1)
        buf = io.BytesIO()
        composite.save(buf, format='JPEG', quality=80)
        buf.seek(0)
        return buf
    except Exception as e:
        logger.debug('Composite image failed: %s', e)
        return None


# --- Excel export ---

def _build_excel_bytes(rows: list, ft: str, image_pairs: list | None = None) -> bytes:
    """Generate a colour-coded Excel workbook from a list of diff items. ft = file type."""
    import openpyxl
    from openpyxl.comments import Comment
    from openpyxl.drawing.image import Image as XLImage
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter

    ft = (ft or '').lower().strip()

    if ft in ('pdf', 'docx'):
        _page_key = 'page'
        _KEYS = ['section', 'page', 'type', 'criticality', 'before', 'after', 'rationale']
        _HDRS = ['Section', 'Page', 'Type', 'Criticality', 'Before', 'After', 'Rationale']
        _COL_WIDTHS = [22, 8, 24, 16, 42, 42, 50]
        _page_col_idx = 2
    elif ft == 'pptx':
        _page_key = 'page'
        _KEYS = ['section', 'page', 'type', 'criticality', 'before', 'after', 'rationale']
        _HDRS = ['Section', 'Slide', 'Type', 'Criticality', 'Before', 'After', 'Rationale']
        _COL_WIDTHS = [22, 8, 24, 16, 42, 42, 50]
        _page_col_idx = 2
    elif ft == 'xml':
        _page_key = 'page'
        _KEYS = ['section', 'page', 'type', 'criticality', 'before', 'after', 'rationale']
        _HDRS = ['Section', 'No.', 'Type', 'Criticality', 'Before', 'After', 'Rationale']
        _COL_WIDTHS = [22, 6, 24, 16, 42, 42, 50]
        _page_col_idx = None
    else:
        _page_key = None
        _KEYS = ['section', 'type', 'criticality', 'before', 'after', 'rationale']
        _HDRS = ['Section', 'Type', 'Criticality', 'Before', 'After', 'Rationale']
        _COL_WIDTHS = [22, 24, 16, 42, 42, 50]
        _page_col_idx = None

    has_images = bool(image_pairs)
    if has_images:
        _HDRS = list(_HDRS) + ['Image']
        _COL_WIDTHS = list(_COL_WIDTHS) + [30]

    _CRIT_COLORS = {'high': 'F8CECC', 'medium': 'FFF2CC', 'low': 'D5E8D4'}
    _TYPE_COLORS = {
        'value changed':        'FFF2CC',
        'reference updated':    'DAE8FC',
        'requirement modified': 'D5E8D4',
        'requirement added':    'C6EFCE',
        'requirement removed':  'F8CECC',
        'procedure changed':    'E1D5E7',
        'scope changed':        'FFE6CC',
        'modal change':         'E8D5F5',
    }
    _IMAGE_KEYWORDS = {
        'image', 'figure', 'visual', 'photo', 'diagram', 'illustration',
        'screenshot', 'picture', 'schematic', 'drawing', 'graphic', 'chart',
        'plot',
    }

    def _is_visual_row(item: dict) -> bool:
        t = str(item.get('type', '') or '').lower()
        return any(kw in t for kw in _IMAGE_KEYWORDS)

    def _row_page_int(item: dict):
        """First page number in the cell — used for image-pair matching (old page)."""
        raw = str(item.get('page', '') or '') if _page_key else ''
        m = re.search(r'\d+', raw)
        return int(m.group()) if m else None

    def _row_page_new_int(item: dict):
        """Last page number in the cell — the NEW-document page for "old→new" entries.
        The report describes the new document, so rows are ordered by where the
        reader finds them there."""
        raw = str(item.get('page', '') or '') if _page_key else ''
        nums = re.findall(r'\d+', raw)
        return int(nums[-1]) if nums else None

    def _section_tuple(item: dict):
        """Parse a dotted section number ("6.3.1" -> (6, 3, 1)) for stable
        secondary ordering. Returns None for named/unnumbered sections
        (TABLE OF CONTENTS, APPENDIX A, ...) so they sort after numbered ones
        on the same page."""
        sec = str(item.get('section', '') or '')
        m = re.match(r'\s*(\d+(?:\.\d+)+)', sec)
        return tuple(int(x) for x in m.group(1).split('.')) if m else None

    _BIG = 10 ** 9

    def _page_sort_key(item):
        if not isinstance(item, dict) or not _page_key:
            return (1, _BIG, 1, ())
        p = _row_page_new_int(item)
        page_part = (0, p) if p is not None else (1, _BIG)
        sec = _section_tuple(item)
        sec_part = (0, sec) if sec is not None else (1, ())
        return (*page_part, *sec_part)

    img_pairs_list: list = list(image_pairs or [])
    if has_images and img_pairs_list:
        visual_rows = [it for it in rows if isinstance(it, dict) and _is_visual_row(it)]
        used_pair_idx: set = set()

        for item in visual_rows:
            page = _row_page_int(item)
            if page is None:
                continue
            for i, pair in enumerate(img_pairs_list):
                if i in used_pair_idx:
                    continue
                if pair.get('new_page') == page or pair.get('old_page') == page:
                    item['_img_pair'] = pair
                    used_pair_idx.add(i)
                    break

        remaining_pair_idx = [i for i in range(len(img_pairs_list)) if i not in used_pair_idx]
        remaining_rows = [it for it in visual_rows if '_img_pair' not in it]
        for item, pi in zip(remaining_rows, remaining_pair_idx):
            item['_img_pair'] = img_pairs_list[pi]

    if _page_key:
        rows = sorted(rows, key=_page_sort_key)

    img_col_num = len(_HDRS)

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = 'Changes'
    thin = Side(style='thin', color='CCCCCC')
    brd = Border(left=thin, right=thin, top=thin, bottom=thin)

    for ci, h in enumerate(_HDRS, 1):
        c = ws.cell(row=1, column=ci, value=h)
        c.font = Font(bold=True, color='FFFFFF', size=11)
        c.fill = PatternFill('solid', fgColor='2F5496')
        c.alignment = Alignment(horizontal='center', vertical='center', wrap_text=True)
        c.border = brd
        if ft == 'docx' and _page_col_idx and ci == _page_col_idx:
            c.comment = Comment(
                'Page numbers are approximate (estimated from page-break markers in the DOCX file). '
                'The actual change may appear on an adjacent page.',
                'Qualibot',
            )
    ws.row_dimensions[1].height = 22
    ws.freeze_panes = 'A2'
    for ci in range(1, len(_HDRS) + 1):
        ws.column_dimensions[get_column_letter(ci)].width = _COL_WIDTHS[ci - 1] if ci <= len(_COL_WIDTHS) else 30

    _crit_idx = _KEYS.index('criticality')
    _type_idx = _KEYS.index('type')
    _page_key_idx = _KEYS.index('page') if _page_key and 'page' in _KEYS else None

    for ri, item in enumerate(rows, 2):
        if not isinstance(item, dict):
            continue
        row = [str(item.get(k, '')) for k in _KEYS]
        crit_val = row[_crit_idx].lower()
        type_val = row[_type_idx].lower()
        fgcol = _CRIT_COLORS.get(crit_val) or _TYPE_COLORS.get(type_val, 'FFFFFF')
        fill = PatternFill('solid', fgColor=fgcol)
        for ci, val in enumerate(row, 1):
            cell_value: Any = val
            if _page_key_idx is not None and ci == _page_key_idx + 1:
                if '→' in val or '->' in val:
                    cell_value = val
                else:
                    m = re.search(r'\d+', val)
                    if m:
                        cell_value = int(m.group())
            c = ws.cell(row=ri, column=ci, value=cell_value)
            c.alignment = Alignment(
                horizontal='center' if (_page_key_idx is not None and ci == _page_key_idx + 1) else 'left',
                vertical='top',
                wrap_text=True,
            )
            c.fill = fill
            c.border = brd
        ws.row_dimensions[ri].height = 35

        if has_images:
            img_pair = item.get('_img_pair') if _is_visual_row(item) else None
            img_cell = ws.cell(row=ri, column=img_col_num)
            img_cell.fill = fill
            img_cell.border = brd

            if img_pair:
                composite = _make_composite_image(img_pair.get('old_b64'), img_pair.get('new_b64'))
                if composite:
                    try:
                        xl_img = XLImage(composite)
                        xl_img.width = 220
                        xl_img.height = 75
                        col_letter = get_column_letter(img_col_num)
                        ws.add_image(xl_img, f'{col_letter}{ri}')
                        ws.row_dimensions[ri].height = 60
                    except Exception as exc:
                        logger.warning('Excel image insert failed row %d: %s', ri, exc)
                        img_cell.value = f'[{img_pair.get("status", "?")}]'

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf.getvalue()


_IMPACT_STATUS_LABEL = {'impacted': 'Impacted', 'check': 'To check', 'not_impacted': 'Not impacted', 'error': 'Failed'}
_IMPACT_STATUS_FILL = {'impacted': 'D5E8D4', 'check': 'FFF2CC', 'error': 'F8CECC'}


def _build_impact_excel_bytes(result: dict) -> bytes:
    """Impact search result → workbook: one row per conflicting passage (the action list),
    plus one row per judged document and the change list."""
    import openpyxl
    from openpyxl.styles import Alignment, Font, PatternFill

    changes = {c.get('id'): c for c in result.get('changes', [])}
    docs = result.get('documents', [])
    wrap = Alignment(wrap_text=True, vertical='top')

    def _sheet(ws, headers, widths, rows):
        ws.append(headers)
        for cell in ws[1]:
            cell.font = Font(bold=True)
        for i, w in enumerate(widths, start=1):
            ws.column_dimensions[openpyxl.utils.get_column_letter(i)].width = w
        for values, status in rows:
            ws.append(values)
            fill = _IMPACT_STATUS_FILL.get(status)
            for cell in ws[ws.max_row]:
                cell.alignment = wrap
                if fill:
                    cell.fill = PatternFill('solid', fgColor=fill)
        ws.freeze_panes = 'A2'

    def _change_detail(ids):
        return '\n'.join(f"{i}: {changes[i].get('text', '')}" for i in ids if i in changes)

    wb = openpyxl.Workbook()
    passage_rows = []
    for d in docs:
        for p in d.get('passages', []):
            passage_rows.append(([
                d.get('ref', ''), d.get('title', ''), d.get('division', ''), d.get('doc_date', ''),
                'yes' if d.get('archive') else '', _IMPACT_STATUS_LABEL.get(d.get('status'), ''),
                ', '.join(p.get('changes', [])), _change_detail(p.get('changes', [])),
                p.get('section', ''), p.get('quote', ''), p.get('explanation', ''), d.get('url', ''),
            ], d.get('status')))
    _sheet(wb.active, ['Ref', 'Title', 'Division', 'Published', 'Archive', 'Verdict', 'Changes', 'Change detail',
                       'Section', 'Quote', 'Explanation', 'Link'],
           [16, 34, 10, 12, 8, 13, 10, 45, 28, 45, 50, 40], passage_rows)
    wb.active.title = 'Passages'

    doc_rows = [([
        d.get('ref', ''), d.get('title', ''), d.get('division', ''), d.get('doc_date', ''),
        'yes' if d.get('archive') else '',
        _IMPACT_STATUS_LABEL.get(d.get('status'), ''), d.get('confidence', ''),
        d.get('reason', ''), ', '.join(d.get('change_ids', [])), d.get('url', ''),
    ], d.get('status')) for d in docs]
    _sheet(wb.create_sheet('Documents'),
           ['Ref', 'Title', 'Division', 'Published', 'Archive', 'Verdict', 'Confidence', 'Reason',
            'Found by changes', 'Link'],
           [16, 34, 10, 12, 8, 13, 11, 70, 16, 40], doc_rows)

    change_rows = [([c.get('id', ''), c.get('criticality', ''), c.get('section', ''), c.get('before', ''),
                     c.get('after', ''), c.get('summary', ''), 'yes' if c.get('searched', True) else 'no'], None)
                   for c in result.get('changes', [])]
    _sheet(wb.create_sheet('Changes'),
           ['Id', 'Criticality', 'Section', 'Before', 'After', 'Rationale / detail', 'Searched'],
           [6, 11, 24, 40, 40, 50, 9], change_rows)

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
