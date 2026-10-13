"""Excel processor (.xlsx / .xls) — three processing methods.

All three methods use the same cell-level unified diff preprocessing.
They differ only in the system prompt sent to the LLM.

standard:    focused Markdown bullet-list output.
structured:  strict JSON array output (for Excel export).
comparative: external system prompt from app config.
"""

import difflib
import io
import logging
from typing import Any, Dict, List, Optional, Tuple

from .base import BaseProcessor, ProcessMetadata, ProcessResult
from ._diff_engines import SYSTEM_PROMPT_STANDARD, SYSTEM_PROMPT_STRUCTURED, truncate_diff

logger = logging.getLogger(__name__)


def _sheet_to_lines(ws) -> List[str]:
    """Convert a worksheet to a list of tab-separated row strings."""
    rows = []
    for row in ws.iter_rows(values_only=True):
        cells = [str(c) if c is not None else '' for c in row]
        rows.append('\t'.join(cells))
    while rows and not rows[-1].strip():
        rows.pop()
    return rows


def _extract_xlsx(file_bytes: bytes) -> Tuple[List[str], str]:
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


def _build_diff_keyed(
    old_lines: List[str], new_lines: List[str], old_name: str, new_name: str
) -> Optional[str]:
    """Key-based diff: match rows by first column, then compare column by column.

    Better than line-level unified_diff for tabular data where:
    - Rows are re-sorted between versions (different weekly ranking)
    - Some columns auto-recalculate every update cycle (leadtime, delta, etc.)

    Columns that change in more than 50% of matched rows are treated as volatile
    (auto-calculated) and excluded from the diff — only structural changes remain.

    Returns None if the first column is not a usable unique key (duplicates or
    >5% empty values), in which case the caller should fall back to unified_diff.
    """
    def _parse_sheets(lines: List[str]) -> List[Tuple[str, str, List[str]]]:
        """Split lines into [(sheet_marker, header_row, [data_rows]), ...]."""
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

    blocks     = _parse_sheets(old_lines)
    new_blocks = _parse_sheets(new_lines)

    out_parts: List[str] = [f'--- {old_name}', f'+++ {new_name}']

    for (sheet, old_header, old_data) in blocks:
        # Find matching new block for this sheet
        new_block = next((b for b in new_blocks if b[0] == sheet), None)
        if new_block is None:
            out_parts.append(f'\n{sheet}: sheet removed entirely')
            continue
        _, new_header, new_data = new_block

        headers = old_header.split('\t') if old_header else []

        # Build key → cells dicts; check for usability as key
        def _parse_rows(rows: List[str]) -> Optional[Dict[str, List[str]]]:
            d: Dict[str, List[str]] = {}
            empty = 0
            for row in rows:
                if not row.strip():
                    continue
                cells = row.split('\t')
                key = cells[0].strip()
                if not key:
                    empty += 1
                    continue
                if key in d:
                    return None  # duplicate key → can't use keyed diff
                d[key] = cells
            if rows and empty / max(len(rows), 1) > 0.05:
                return None  # too many empty keys
            return d

        def _is_auto_increment(keys: List[str]) -> bool:
            """True if the keys are sequential integers (1,2,3… or 0,1,2…) — row numbers, not identifiers."""
            try:
                nums = [int(k) for k in keys]
                nums_sorted = sorted(nums)
                return (
                    nums_sorted[0] in (0, 1)
                    and all(nums_sorted[i] == nums_sorted[i - 1] + 1 for i in range(1, len(nums_sorted)))
                )
            except (ValueError, TypeError):
                return False

        old_rows = _parse_rows(old_data)
        new_rows = _parse_rows(new_data)
        if old_rows is None or new_rows is None:
            return None  # fall back: duplicate or too many empty keys

        # Fall back if first column is just sequential row numbers — keyed diff would misalign on insertions
        if _is_auto_increment(list(old_rows.keys())) or _is_auto_increment(list(new_rows.keys())):
            return None

        old_keys = set(old_rows)
        new_keys = set(new_rows)
        added_keys   = sorted(new_keys - old_keys)
        removed_keys = sorted(old_keys - new_keys)
        common_keys  = old_keys & new_keys

        # Detect volatile columns
        col_change: Dict[int, int] = {}
        for key in common_keys:
            o, n = old_rows[key], new_rows[key]
            for ci in range(min(len(o), len(n))):
                if o[ci] != n[ci]:
                    col_change[ci] = col_change.get(ci, 0) + 1

        volatile: set = {
            ci for ci, cnt in col_change.items()
            if cnt > len(common_keys) * _VOLATILE_COL_THRESHOLD
        }
        volatile_names = [
            headers[ci] if ci < len(headers) else f'col_{ci}'
            for ci in sorted(volatile)
        ]

        out_parts.append(f'\n{sheet}')
        key_col = headers[0] if headers else 'col_0'
        out_parts.append(f'Key column: {key_col}')
        if volatile_names:
            out_parts.append(
                f'Auto-excluded columns (recalculated every update — not shown): '
                + ', '.join(f'"{n}"' for n in volatile_names)
            )

        if added_keys:
            out_parts.append(f'\nADDED rows ({len(added_keys)}):')
            for key in added_keys:
                cells = new_rows[key]
                fields = [
                    f'{headers[ci] if ci < len(headers) else f"col_{ci}"}: {cells[ci]}'
                    for ci in range(len(cells))
                    if ci not in volatile and ci > 0 and cells[ci].strip()
                ]
                out_parts.append(f'  + {key} | ' + ' | '.join(fields)[:300])

        if removed_keys:
            out_parts.append(f'\nREMOVED rows ({len(removed_keys)}):')
            for key in removed_keys:
                cells = old_rows[key]
                fields = [
                    f'{headers[ci] if ci < len(headers) else f"col_{ci}"}: {cells[ci]}'
                    for ci in range(len(cells))
                    if ci not in volatile and ci > 0 and cells[ci].strip()
                ]
                out_parts.append(f'  - {key} | ' + ' | '.join(fields)[:300])

        # Modified rows
        modified: List[Tuple[str, List[str]]] = []
        for key in sorted(common_keys):
            o, n = old_rows[key], new_rows[key]
            changes = []
            for ci in range(min(len(o), len(n))):
                if ci in volatile:
                    continue
                if o[ci] != n[ci]:
                    col_name = headers[ci] if ci < len(headers) else f'col_{ci}'
                    changes.append(f'{col_name}: {o[ci]!r} → {n[ci]!r}')
            if changes:
                modified.append((key, changes))

        if modified:
            out_parts.append(f'\nMODIFIED rows ({len(modified)} with changes in retained columns):')
            for key, changes in modified:
                out_parts.append(f'  ~ {key}:')
                for ch in changes[:10]:  # cap at 10 fields per row
                    out_parts.append(f'    {ch[:200]}')

        if not added_keys and not removed_keys and not modified:
            out_parts.append('\nNo significant changes detected (only auto-calculated columns differ).')

    return '\n'.join(out_parts) if len(out_parts) > 2 else '(No cell differences detected)'


def _build_diff(old_bytes, old_name, new_bytes, new_name) -> Tuple[str, str, str]:
    """Return (diff_text, old_summary, new_summary)."""
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
