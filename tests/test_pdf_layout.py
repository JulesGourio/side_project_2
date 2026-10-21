"""Layout of the PDF report builder: words, links, drawings and fonts per page of small generated reports."""

import datetime as dt

import pymupdf

from server.services.infra.pdf_report import Section, Source, build_report_pdf

NOW = dt.datetime(2026, 10, 13, 9, 0, tzinfo=dt.UTC)

TABLE = """| Section | Changement |
|---|---|
| §3.1 | Étendu |
| §4.2 | Valeur 40 Nm |
"""


def _open(data: bytes) -> pymupdf.Document:
    """Opens a generated report.

    :param data: PDF bytes
    :return: the document
    """
    return pymupdf.open(stream=data, filetype='pdf')


def _report(markdown: str, **kwargs) -> pymupdf.Document:
    """A change-summary report with a single section.

    :param markdown: Markdown of the section
    :param kwargs: extra arguments of ``build_report_pdf``
    :return: the document
    """
    args = {'kind': 'change_summary', 'title': 'Résumé', 'now': NOW, 'sections': [Section(markdown)], **kwargs}
    return _open(build_report_pdf(**args))


def _words(page: pymupdf.Page) -> list[str]:
    """Words of a page in reading order.

    :param page: PDF page
    :return: the words
    """
    return [w[4] for w in page.get_text('words')]


def _fonts(page: pymupdf.Page) -> dict[str, tuple[str, float]]:
    """Font name and size of each text span of a page.

    :param page: PDF page
    :return: ``{span text: (font, size)}``
    """
    spans = [s for b in page.get_text('dict')['blocks'] for ln in b.get('lines', []) for s in ln['spans']]
    return {s['text']: (s['font'], round(s['size'], 1)) for s in spans}


def test_header_title_date_and_metadata_of_a_change_summary():
    doc = _report('Corps.', meta=[('Old file', 'a.pdf'), ('New file', 'b.pdf'), ('Skipped', '')])
    assert len(doc) == 1
    words = _words(doc[0])
    assert words[:4] == ['QUALIBOT', '·', 'CHANGE', 'SUMMARY']
    assert words[4:7] == ['13', 'October', '2026']
    assert words[7] == 'Résumé'
    assert words[8:16] == ['OLD', 'FILE', 'a.pdf', 'NEW', 'FILE', 'b.pdf', 'Corps.', 'Generated']
    assert 'SKIPPED' not in words
    assert (doc.metadata['title'], doc.metadata['author']) == ('Résumé', 'QualiBOT')


def test_empty_report_has_one_page_with_header_and_footer_only():
    doc = _open(build_report_pdf(kind='unknown', title='', now=NOW, sections=[Section('')]))
    assert len(doc) == 1
    assert _words(doc[0])[:5] == ['QUALIBOT', '·', 'REPORT', '13', 'October']
    assert ' '.join(_words(doc[0])[-3:]) == '1 / 1'
    assert doc[0].get_links() == []
    assert doc.metadata['title'] == 'Report'


def test_headings_and_inline_styles_use_distinct_fonts_and_sizes():
    doc = _report('# Grand\n\nPlain **gras** *ital* `code` ~~barre~~ fin\n')
    fonts = _fonts(doc[0])
    assert fonts['Grand'] == ('InterSemiBold', 15.0)
    assert fonts['Plain '] == ('Inter', 10.0)
    assert fonts['gras'] == ('InterSemiBold', 10.0)
    assert fonts['ital'] == ('InterItalic', 10.0)
    assert fonts['code'] == ('Inter', 9.5)
    assert fonts[' barre fin'] == ('Inter', 10.0)


def test_lists_render_with_nested_indentation_and_numbering():
    doc = _report('- un\n  - deux\n1. premier\n')
    page = doc[0]
    x = {w[4]: round(w[0]) for w in page.get_text('words')}
    assert x['un'] < x['deux']
    assert x['1.'] < x['premier']
    assert _fonts(page)['1.'][0] == 'InterSemiBold'


def test_markdown_link_and_bare_url_become_clickable_with_their_rectangle():
    doc = _report('Voir [Intraqual](https://example.org/a) et https://example.org/b fin')
    page = doc[0]
    links = {lk['uri']: lk['from'] for lk in page.get_links()}
    assert set(links) == {'https://example.org/a', 'https://example.org/b'}
    words = {w[4]: pymupdf.Rect(w[:4]) for w in page.get_text('words')}
    assert words['Intraqual'].intersects(links['https://example.org/a'])
    assert words['https://example.org/b'].intersects(links['https://example.org/b'])
    assert 'Intraqual' in words and '[Intraqual](https://example.org/a)' not in words


def test_citation_marker_links_to_its_source_and_sources_are_listed():
    sources = [Source('QP-1518', 'https://example.org/qp', 1, 'Qualification'), Source('MR-1465', None, 2)]
    doc = _open(build_report_pdf(
        kind='chat_session', title='T', now=NOW,
        sections=[Section('Réponse [1].', question='Quelle règle ?', sources=sources)]))
    page = doc[0]
    words = _words(page)
    assert words[2:4] == ['CHATBOT', 'CONVERSATION']
    assert words[words.index('QUESTION'):words.index('QUESTION') + 3] == ['QUESTION', 'Quelle', 'règle']
    assert 'SOURCES' in words and 'QP-1518' in words and 'MR-1465' in words
    links = page.get_links()
    assert [lk['uri'] for lk in links] == ['https://example.org/qp'] * 2
    marker, source_line = sorted(links, key=lambda lk: lk['from'].y0)
    answer = next(pymupdf.Rect(w[:4]) for w in page.get_text('words') if w[4] == '[1].')
    assert marker['from'].intersects(answer)
    assert source_line['from'].y0 > marker['from'].y0


def test_ruled_table_is_drawn_as_filled_header_and_row_cells_with_all_its_text():
    page = _report(TABLE)[0]
    words = _words(page)
    for expected in ('Section', 'Changement', '§3.1', 'Étendu', '§4.2', 'Valeur', '40', 'Nm'):
        assert expected in words
    cells = [pymupdf.Rect(d['rect']) for d in page.get_drawings() if d['type'] == 'f' and d['rect'].y0 > 50]  # below the top bar
    assert len(cells) == 6  # 3 rows x 2 columns
    assert len({round(c.x0) for c in cells}) == 2 and len({round(c.y0) for c in cells}) == 3
    word = {w[4]: pymupdf.Rect(w[:4]) for w in page.get_text('words')}
    assert word['Section'].y0 < word['§3.1'].y0 < word['§4.2'].y0
    assert word['§3.1'].x0 < word['Étendu'].x0


def test_long_text_wraps_onto_following_pages_with_a_running_header_and_page_numbers():
    text = '\n\n'.join(f'Paragraphe numero {i} ' * 12 for i in range(30))
    doc = _report(text)
    assert len(doc) == 3
    assert _words(doc[0])[2:4] == ['CHANGE', 'SUMMARY']
    for number, page in enumerate(doc[1:], start=2):
        assert _words(page)[:3] == ['QUALIBOT', '·', 'Change']
        assert ' '.join(_words(page)[-3:]) == f'{number} / 3'
    body = [w for page in doc for w in _words(page) if w.isdigit()]
    assert {'0', '29'} <= set(body)
    assert '29' in _words(doc[2])
