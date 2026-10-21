"""Excel processor (.xlsx / .xlsm / .xls) — three processing methods.

All three methods use the same cell-level unified diff preprocessing.
They differ only in the system prompt sent to the LLM.

standard:    focused Markdown bullet-list output.
structured:  strict JSON array output (for Excel export).
comparative: external system prompt from app config.
"""

import difflib
import io
import logging
from typing import Any, Dict, List, Tuple

from server.services.compare.processors.base import BaseProcessor, ProcessMetadata, ProcessResult
from server.services.compare.processors._diff_engines import SYSTEM_PROMPT_STANDARD, SYSTEM_PROMPT_STRUCTURED, truncate_diff

logger = logging.getLogger(__name__)


def _sheet_to_lines(ws) -> List[str]:
    """Convert a worksheet to a list of tab-separated row strings.

    :param ws: openpyxl worksheet, read-only
    :return: one tab-separated string per row, trailing empty rows dropped
    """
    rows = []
    for row in ws.iter_rows(values_only=True):
        cells = [str(c) if c is not None else '' for c in row]
        rows.append('\t'.join(cells))
    while rows and not rows[-1].strip():
        rows.pop()
    return rows


def _extract_xlsx(file_bytes: bytes) -> Tuple[List[str], str]:
    """Lines and sheet summary of an .xlsx workbook: a ``[Sheet: ...]`` marker, then the rows of each sheet.

    :param file_bytes: content of the .xlsx file
    :return: ``(lines, one-line summary of the sheets)``
    """
    import openpyxl
    wb = openpyxl.load_workbook(io.BytesIO(file_bytes), read_only=True, data_only=True)
    lines: List[str] = []
    for name in wb.sheetnames:
        ws = wb[name]
        lines.append(f'[Sheet: {name}]')
        lines.extend(_sheet_to_lines(ws))
    wb.close()
    return lines, f'{len(wb.sheetnames)} sheet(s): {", ".join(wb.sheetnames)}'


def _extract_xls(file_bytes: bytes) -> Tuple[List[str], str]:
    """Lines and sheet summary of a legacy .xls workbook, read with xlrd.

    :param file_bytes: content of the .xls file
    :return: ``(lines, one-line summary of the sheets)``
    """
    try:
        import xlrd
    except ImportError:
        raise ValueError('.xls files require the xlrd package. Please convert to .xlsx for best support.')
    wb = xlrd.open_workbook(file_contents=file_bytes)
    lines: List[str] = []
    for name in wb.sheet_names():
        ws = wb.sheet_by_name(name)
        lines.append(f'[Sheet: {name}]')
        for row_idx in range(ws.nrows):
            cells = [str(ws.cell_value(row_idx, col)) for col in range(ws.ncols)]
            lines.append('\t'.join(cells))
    return lines, f'{wb.nsheets} sheet(s): {", ".join(wb.sheet_names())}'


_VOLATILE_COL_THRESHOLD = 0.50  # columns changing in >50% of matched rows are auto-excluded
_VOLATILE_MIN_ROWS = 20         # a column is only judged recalculated from this many matched rows: a handful of edits is a change
_MAX_EMPTY_KEY_RATIO = 0.05     # above this share of empty first-column cells the first column is no key
_ROW_FIELDS_CHARS = 300         # an added or removed row is shown up to this many characters
_CHANGE_CHARS = 200             # a changed cell is shown up to this many characters
_CHANGES_PER_ROW = 10           # changed cells shown per modified row


def _parse_sheets(lines: List[str]) -> List[Tuple[str, str, List[str]]]:
    """Split workbook lines into sheets.

    :param lines: workbook lines, a ``[Sheet: ...]`` marker before the rows of each sheet
    :return: a list of ``(sheet marker, header line, data rows)``
    """
    result: List[Tuple[str, str, List[str]]] = []
    sheet, header, data = '', '', []
    for line in lines:
        if line.startswith('[Sheet:'):
            if sheet:
                result.append((sheet, header, data))
            sheet, header, data = line, '', []
        elif not header and line.strip():
            header = line
        else:
            data.append(line)
    if sheet:
        result.append((sheet, header, data))
    return result


def _parse_rows(rows: List[str]) -> Dict[str, List[str]] | None:
    """Rows keyed by their first column; None when a key repeats or too many keys are empty.

    :param rows: data rows of one sheet, tab-separated
    :return: the cells of each row by key, or None
    """
    keyed: Dict[str, List[str]] = {}
    empty = 0
    for row in rows:
        if not row.strip():
            continue
        cells = row.split('\t')
        key = cells[0].strip()
        if not key:
            empty += 1
            continue
        if key in keyed:
            return None  # duplicate key → can't use keyed diff
        keyed[key] = cells
    if rows and empty / max(len(rows), 1) > _MAX_EMPTY_KEY_RATIO:
        return None  # too many empty keys
    return keyed


def _is_auto_increment(keys: List[str]) -> bool:
    """True if the keys are sequential integers (1,2,3… or 0,1,2…) — row numbers, not identifiers.

    :param keys: row keys of one sheet
    :return: True when the keys are the row numbers 0 or 1, 2, 3...
    """
    try:
        nums_sorted = sorted(int(k) for k in keys)
        return (
            nums_sorted[0] in (0, 1)
            and all(nums_sorted[i] == nums_sorted[i - 1] + 1 for i in range(1, len(nums_sorted)))
        )
    except (ValueError, TypeError, IndexError):
        return False


def _column_name(headers: List[str], index: int) -> str:
    """The header of a column, ``col_<index>`` when the sheet has none for it.

    :param headers: header cells of the sheet
    :param index: column index
    :return: the column name
    """
    return headers[index] if index < len(headers) else f'col_{index}'


def _cell(cells: List[str], index: int) -> str:
    """A cell of a row, empty beyond the end of the row (a column added or dropped at the end).

    :param cells: cells of the row
    :param index: column index
    :return: the cell text
    """
    return cells[index] if index < len(cells) else ''


def _volatile_columns(old_rows: Dict[str, List[str]], new_rows: Dict[str, List[str]]) -> set:
    """Columns that change in most of the rows present in both revisions (recalculated every cycle).

    Needs ``_VOLATILE_MIN_ROWS`` matched rows: on fewer rows, a column changed everywhere is an edit, not a recalculation.

    :param old_rows: rows of the old sheet by key
    :param new_rows: rows of the new sheet by key
    :return: the indexes of the volatile columns
    """
    common_keys = old_rows.keys() & new_rows.keys()
    if len(common_keys) < _VOLATILE_MIN_ROWS:
        return set()
    col_change: Dict[int, int] = {}
    for key in common_keys:
        o, n = old_rows[key], new_rows[key]
        for ci in range(max(len(o), len(n))):
            if _cell(o, ci) != _cell(n, ci):
                col_change[ci] = col_change.get(ci, 0) + 1
    return {ci for ci, count in col_change.items() if count > len(common_keys) * _VOLATILE_COL_THRESHOLD}


def _row_fields(cells: List[str], headers: List[str], volatile: set) -> str:
    """The retained, non-empty cells of an added or removed row, as ``column: value`` pairs.

    :param cells: cells of the row; the first is the key and is not repeated
    :param headers: header cells of the sheet
    :param volatile: indexes of the columns left out
    :return: the pairs joined by `` | ``, cut to ``_ROW_FIELDS_CHARS`` characters
    """
    fields = [
        f'{_column_name(headers, ci)}: {cells[ci]}'
        for ci in range(len(cells))
        if ci not in volatile and ci > 0 and cells[ci].strip()
    ]
    return ' | '.join(fields)[:_ROW_FIELDS_CHARS]


def _modified_rows(old_rows: Dict[str, List[str]], new_rows: Dict[str, List[str]], headers: List[str],
                   volatile: set) -> List[Tuple[str, List[str]]]:
    """The rows present in both revisions whose retained cells changed.

    :param old_rows: rows of the old sheet by key
    :param new_rows: rows of the new sheet by key
    :param headers: header cells of the sheet
    :param volatile: indexes of the columns left out
    :return: ``(key, ["column: 'old' → 'new'", ...])`` for each modified row, in key order
    """
    modified: List[Tuple[str, List[str]]] = []
    for key in sorted(old_rows.keys() & new_rows.keys()):
        o, n = old_rows[key], new_rows[key]
        changes = [
            f'{_column_name(headers, ci)}: {_cell(o, ci)!r} → {_cell(n, ci)!r}'
            for ci in range(max(len(o), len(n)))
            if ci not in volatile and _cell(o, ci) != _cell(n, ci)
        ]
        if changes:
            modified.append((key, changes))
    return modified


def _sheet_diff(sheet: str, old_header: str, new_header: str, old_rows: Dict[str, List[str]],
                new_rows: Dict[str, List[str]]) -> List[str]:
    """The report lines of one sheet present in both workbooks.

    :param sheet: the ``[Sheet: ...]`` marker
    :param old_header: header line of the old sheet
    :param new_header: header line of the new sheet
    :param old_rows: rows of the old sheet by key
    :param new_rows: rows of the new sheet by key
    :return: the report lines
    """
    old_headers = old_header.split('\t') if old_header else []
    # A column added at the end is named by the new header.
    headers = old_headers + (new_header.split('\t')[len(old_headers):] if new_header else [])
    added_keys = sorted(new_rows.keys() - old_rows.keys())
    removed_keys = sorted(old_rows.keys() - new_rows.keys())
    volatile = _volatile_columns(old_rows, new_rows)
    volatile_names = [_column_name(headers, ci) for ci in sorted(volatile)]

    out = [f'\n{sheet}', f'Key column: {headers[0] if headers else "col_0"}']
    if new_header and new_header != old_header:
        out.append(f'Header changed: {old_header.replace(chr(9), " | ")!r} → {new_header.replace(chr(9), " | ")!r}')
    if volatile_names:
        out.append(
            'Auto-excluded columns (recalculated every update — not shown): '
            + ', '.join(f'"{n}"' for n in volatile_names)
        )
    if added_keys:
        out.append(f'\nADDED rows ({len(added_keys)}):')
        out.extend(f'  + {key} | {_row_fields(new_rows[key], headers, volatile)}' for key in added_keys)
    if removed_keys:
        out.append(f'\nREMOVED rows ({len(removed_keys)}):')
        out.extend(f'  - {key} | {_row_fields(old_rows[key], headers, volatile)}' for key in removed_keys)

    modified = _modified_rows(old_rows, new_rows, headers, volatile)
    if modified:
        out.append(f'\nMODIFIED rows ({len(modified)} with changes in retained columns):')
        for key, changes in modified:
            out.append(f'  ~ {key}:')
            out.extend(f'    {change[:_CHANGE_CHARS]}' for change in changes[:_CHANGES_PER_ROW])
    if not added_keys and not removed_keys and not modified and not (new_header and new_header != old_header):
        out.append('\nNo significant changes detected (only auto-calculated columns differ).')
    return out


def _build_diff_keyed(
    old_lines: List[str], new_lines: List[str], old_name: str, new_name: str
) -> str | None:
    """Key-based diff: match rows by first column, then compare column by column.

    Better than line-level unified_diff for tabular data where:
    - Rows are re-sorted between versions (different weekly ranking)
    - Some columns auto-recalculate every update cycle (leadtime, delta, etc.)

    Columns that change in more than 50% of matched rows are treated as volatile
    (auto-calculated) and excluded from the diff — only structural changes remain.

    Returns None if the first column is not a usable unique key (duplicates or
    >5% empty values), in which case the caller should fall back to unified_diff.

    :param old_lines: lines of the old workbook, from ``_extract_xlsx`` or ``_extract_xls``
    :param new_lines: lines of the new workbook, same format
    :param old_name: file name of the old workbook
    :param new_name: file name of the new workbook
    :return: the keyed diff, or None when the first column is not a usable key
    """
    new_blocks = _parse_sheets(new_lines)
    old_blocks = _parse_sheets(old_lines)
    out_parts: List[str] = [f'--- {old_name}', f'+++ {new_name}']

    for sheet, old_header, old_data in old_blocks:
        new_block = next((b for b in new_blocks if b[0] == sheet), None)
        if new_block is None:
            out_parts.append(f'\n{sheet}: sheet removed entirely')
            continue
        old_rows = _parse_rows(old_data)
        new_rows = _parse_rows(new_block[2])
        if old_rows is None or new_rows is None:
            return None  # fall back: duplicate or too many empty keys
        # Fall back if first column is just sequential row numbers — keyed diff would misalign on insertions
        if _is_auto_increment(list(old_rows)) or _is_auto_increment(list(new_rows)):
            return None
        out_parts.extend(_sheet_diff(sheet, old_header, new_block[1], old_rows, new_rows))

    old_sheets = {block[0] for block in old_blocks}
    for sheet, _, new_data in new_blocks:
        if sheet not in old_sheets:
            out_parts.append(f'\n{sheet}: sheet added ({len([row for row in new_data if row.strip()])} rows)')

    return '\n'.join(out_parts) if len(out_parts) > 2 else '(No cell differences detected)'


def _build_diff(old_bytes, old_name, new_bytes, new_name) -> Tuple[str, str, str]:
    """Return (diff_text, old_summary, new_summary).

    :param old_bytes: content of the old workbook
    :param old_name: file name of the old workbook, its extension selects the reader
    :param new_bytes: content of the new workbook
    :param new_name: file name of the new workbook
    :return: ``(diff_text, old_summary, new_summary)``
    """
    ext = ('.' + old_name.rsplit('.', 1)[-1].lower()) if '.' in old_name else '.xlsx'
    extract = _extract_xls if ext == '.xls' else _extract_xlsx
    old_lines, old_summary = extract(old_bytes)
    new_lines, new_summary = extract(new_bytes)

    # Try key-based diff first (better for tables with a stable row identifier)
    diff_text = _build_diff_keyed(old_lines, new_lines, old_name, new_name)
    if diff_text is not None:
        return diff_text, old_summary, new_summary

    # Fallback: unified diff with autojunk=False
    old_src = [ln + '\n' for ln in old_lines]
    new_src = [ln + '\n' for ln in new_lines]
    sm = difflib.SequenceMatcher(None, old_src, new_src, autojunk=False)
    started = False
    parts: List[str] = []
    for group in sm.get_grouped_opcodes(3):
        if not started:
            started = True
            parts += [f'--- {old_name}\n', f'+++ {new_name}\n']
        first, last = group[0], group[-1]
        parts.append(f'@@ -{first[1]+1},{last[2]-first[1]} +{first[3]+1},{last[4]-first[3]} @@\n')
        for tag, i1, i2, j1, j2 in group:
            if tag == 'equal':
                parts.extend(' ' + l for l in old_src[i1:i2])
            if tag in ('replace', 'delete'):
                parts.extend('-' + l for l in old_src[i1:i2])
            if tag in ('replace', 'insert'):
                parts.extend('+' + l for l in new_src[j1:j2])
    diff_text = ''.join(parts) if parts else '(No cell differences detected)'
    return diff_text, old_summary, new_summary


class ExcelProcessor(BaseProcessor):

    def __init__(self, method: str = 'standard') -> None:
        """Stores the method: standard, structured or comparative.

        :param method: method of the comparison
        """
        self.method = method

    def build_messages(
        self,
        old_bytes: bytes,
        old_name: str,
        new_bytes: bytes,
        new_name: str,
        system_prompt: str = '',
    ) -> ProcessResult:
        """Message list of a cell-level comparison of two workbooks.

        :param old_bytes: content of the old workbook
        :param old_name: file name of the old workbook, its extension selects the reader
        :param new_bytes: content of the new workbook
        :param new_name: file name of the new workbook
        :param system_prompt: system prompt of the comparative method; the other methods use their own
        :return: the messages with their metadata
        """
        if self.method == 'structured':
            sp = SYSTEM_PROMPT_STRUCTURED
        elif self.method == 'comparative':
            sp = system_prompt
        else:
            sp = SYSTEM_PROMPT_STANDARD

        logger.info(f'ExcelProcessor [{self.method}]: cell diff — {old_name} → {new_name}')
        diff_text, old_summary, new_summary = _build_diff(old_bytes, old_name, new_bytes, new_name)
        diff_text = truncate_diff(diff_text)

        text = (
            f'Excel spreadsheet comparison\n'
            f'  Document A: {old_name} — {old_summary}\n'
            f'  Document B: {new_name} — {new_summary}\n\n'
            f'Cell-level unified diff:\n\n'
            f'```diff\n{diff_text}\n```'
        )

        messages: List[Dict[str, Any]] = []
        if sp:
            messages.append({'role': 'system', 'content': sp})
        messages.append({'role': 'user', 'content': [{'type': 'text', 'text': text}]})

        return ProcessResult(
            messages=messages,
            metadata=ProcessMetadata(file_type='excel', method=self.method, old_name=old_name, new_name=new_name),
        )
