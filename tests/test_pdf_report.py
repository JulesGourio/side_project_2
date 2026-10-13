"""PDF exports (server/services/infra/pdf_report/): layout per kind, Markdown parsing, endpoint."""

import pymupdf
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from server.routers import exports
from server.services.infra.pdf_report import (
    Section,
    Source,
    build_markdown_pdf,
    build_report_pdf,
    clean_text,
    parse_inline,
    tokenize,
)

MD = """# Résumé

Texte avec **gras**, *italique*, `QP-0098` et une citation [2].

- Point « un » → suite
  - sous-point
1. premier

> citation

| Section | Changement |
|---|---|
| §3.1 | Étendu à LMX ✅ |
"""


def _text(pdf: bytes) -> str:
    return '\n'.join(page.get_text() for page in pymupdf.open(stream=pdf, filetype='pdf'))


def test_chat_answer_has_its_own_label_and_linked_sources():
    pdf = build_report_pdf(
        kind='chat_response', title='Quelle est la règle FAI ?',
        sections=[Section(MD, sources=[Source('Q0102QP_GB', 'https://x/q', 2, 'First Article Inspection · Version F')])],
    )
    text = _text(pdf)
    assert 'CHATBOT ANSWER' in text
    assert 'COMPARISON' not in text.upper()
    assert 'Quelle est la règle FAI ?' in text
    assert '« un » → suite' in text
    assert 'SOURCES' in text and 'Q0102QP_GB' in text and 'First Article Inspection' in text
    links = [lk['uri'] for page in pymupdf.open(stream=pdf, filetype='pdf') for lk in page.get_links()]
    assert 'https://x/q' in links  # the source line and the inline [2] marker


def test_change_summary_header_lists_both_files():
    text = _text(build_markdown_pdf(MD, 'QP-0102 F', 'change_summary',
                                    [('Original (A)', 'old.pdf'), ('Revised (B)', 'new.pdf')]))
    assert 'CHANGE SUMMARY' in text
    assert 'old.pdf' in text and 'new.pdf' in text
    assert 'Étendu à LMX ✓' in text  # emoji mapped to a glyph the font has


def test_session_numbers_questions_and_paginates():
    sections = [Section(MD * 6, question=f'Question numéro {i}') for i in range(3)]
    pdf = build_report_pdf(kind='chat_session', title='Chatbot conversation', sections=sections)
    text = _text(pdf)
    assert 'QUESTION 1' in text and 'QUESTION 3' in text
    doc = pymupdf.open(stream=pdf, filetype='pdf')
    assert doc.page_count > 1
    assert f'2 / {doc.page_count}' in text  # page-count alias resolved


def test_tokenize_blocks():
    kinds = [t['kind'] for t in tokenize(MD)]
    assert kinds == ['heading', 'para', 'item', 'item', 'item', 'quote', 'table']
    items = [t for t in tokenize(MD) if t['kind'] == 'item']
    assert [t['depth'] for t in items] == [0, 1, 0] and items[2]['ordered']


def test_parse_inline_styles_and_citations():
    spans = parse_inline('a **b** *c* [d](https://e) [3, 4] snake_case_name')
    assert [(s.text, s.bold, s.italic) for s in spans if s.text.strip()][:3] == [
        ('a ', False, False), ('b', True, False), ('c', False, True)]
    assert any(s.link == 'https://e' and s.text == 'd' for s in spans)
    assert [s.cite for s in spans if s.cite] == [3, 4]
    assert ''.join(s.text for s in spans).endswith('snake_case_name')


def test_clean_text_drops_unrenderable_chars():
    assert clean_text('ok 😀\u200b✔') == 'ok ✓'


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(exports.router, prefix='/api')
    return TestClient(app)


def test_export_endpoint(client):
    r = client.post('/api/export/pdf', json={
        'kind': 'document_summary', 'title': 'Doc', 'filename': 'summary doc',
        'meta': [{'label': 'File', 'value': 'doc.pdf'}], 'sections': [{'markdown': MD}],
    })
    assert r.status_code == 200
    assert r.headers['content-type'] == 'application/pdf'
    assert 'filename="summary_doc.pdf"' in r.headers['content-disposition']
    assert 'DOCUMENT SUMMARY' in _text(r.content)


def test_export_endpoint_rejects_unknown_kind_and_empty(client):
    assert client.post('/api/export/pdf', json={'kind': 'x', 'sections': [{'markdown': 'a'}]}).status_code == 400
    assert client.post('/api/export/pdf', json={'kind': 'report', 'sections': [{'markdown': ' '}]}).status_code == 400
