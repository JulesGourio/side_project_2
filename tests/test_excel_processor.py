"""Excel processor: workbook extensions, text extraction, keyed diff of tabular sheets."""

import io
import zipfile

import openpyxl

from server.services.compare.processors import excel
from server.services.compare.processors.factory import SUPPORTED_EXTENSIONS, file_type_of, get_processor


def _workbook(rows) -> bytes:
    book = openpyxl.Workbook()
    sheet = book.active
    sheet.title = 'Data'
    for row in rows:
        sheet.append(row)
    buffer = io.BytesIO()
    book.save(buffer)
    return buffer.getvalue()


def _as_xlsm(xlsx: bytes) -> bytes:
    """The same workbook with the macro-enabled content type of an .xlsm file."""
    source = zipfile.ZipFile(io.BytesIO(xlsx))
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w') as target:
        for item in source.infolist():
            data = source.read(item.filename)
            if item.filename == '[Content_Types].xml':
                data = data.replace(b'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml',
                                    b'application/vnd.ms-excel.sheet.macroEnabled.main+xml')
            target.writestr(item, data)
    return buffer.getvalue()


def test_extensions_are_read_in_any_case_and_macro_workbooks_are_supported():
    assert file_type_of('Plan.PDF') == 'pdf' and file_type_of('Tableau.XLSM') == 'excel'
    assert '.xlsm' in SUPPORTED_EXTENSIONS
    assert type(get_processor('book.xlsm')).__name__ == 'ExcelProcessor'


def test_a_macro_enabled_workbook_is_read_like_an_xlsx():
    lines, summary = excel._extract_xlsx(_as_xlsm(_workbook([['Ref', 'Value'], ['A', 1]])))
    assert lines[0] == '[Sheet: Data]' and 'A\t1' in lines and 'Data' in summary


HEADER = ['[Sheet: S]', 'Ref\tValue\tRank']


def _diff(old_rows, new_rows):
    return excel._build_diff_keyed(HEADER + old_rows, HEADER + new_rows, 'old.xlsx', 'new.xlsx')


def test_added_removed_and_modified_rows_are_reported_by_key():
    report = _diff(['A\t1\t1', 'B\t2\t2', 'C\t3\t3'], ['A\t1\t1', 'B\t9\t2', 'D\t4\t3'])
    assert 'ADDED rows (1)' in report and '+ D | Value: 4' in report
    assert 'REMOVED rows (1)' in report and '- C | Value: 3' in report
    assert "~ B:" in report and "Value: '2' → '9'" in report


def test_a_column_that_changes_in_most_rows_is_left_out():
    old = [f'K{i}\tx\t{i}' for i in range(6)]
    new = [f'K{i}\tx\t{i + 100}' for i in range(6)]
    report = _diff(old, new)
    assert 'Auto-excluded columns' in report and '"Rank"' in report
    assert 'No significant changes detected' in report


def test_a_sheet_without_data_rows_gives_a_report_instead_of_failing():
    assert 'Key column: Ref' in _diff([], ['A\t1\t1'])


def test_the_diff_falls_back_when_the_first_column_is_no_key():
    assert _diff(['A\t1\t1', 'A\t2\t2'], ['A\t1\t1']) is None                   # repeated key
    assert _diff(['1\ta\tb', '2\tc\td'], ['1\ta\tb', '2\tc\td', '3\te\tf']) is None  # row numbers


def test_a_removed_sheet_is_reported():
    report = excel._build_diff_keyed(HEADER + ['A\t1\t1'], ['[Sheet: Other]', 'Ref\tValue', 'A\t1'], 'a', 'b')
    assert '[Sheet: S]: sheet removed entirely' in report
