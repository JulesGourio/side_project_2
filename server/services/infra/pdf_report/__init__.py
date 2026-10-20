"""PDF exports of the chatbot and the document comparison — one layout for both.

Markdown (as written by the LLM) → A4 PDF with fpdf2, pure Python. The Inter
font ships in server/assets/fonts (SIL OFL, Inter-OFL.txt) so accents, arrows
and typographic quotes render the same on every machine — the Databricks App
container has no system fonts to fall back on.

What changes between documents is only the header: a ``kind`` (chatbot answer,
chatbot conversation, change summary, document summary) gives the label above
the title, and ``meta`` adds label/value lines under it (compared files…).
Each ``Section`` is one block of Markdown, optionally preceded by a question box
(chatbot) and followed by its numbered sources; ``[n]`` markers in the text
become superscript links to source n.
"""

from __future__ import annotations

import datetime as _dt

from server.services.infra.pdf_report.common import KIND_LABELS, MARGIN_X, PAGE_W, RULE, Section, Source
from server.services.infra.pdf_report.markdown import clean_text, parse_inline, plain_text, tokenize
from server.services.infra.pdf_report.render import _cover, _new_pdf, _Writer

__all__ = [
    'KIND_LABELS',
    'Section',
    'Source',
    'build_markdown_pdf',
    'build_report_pdf',
    'clean_text',
    'parse_inline',
    'plain_text',
    'tokenize',
]


def build_report_pdf(
    *,
    kind: str,
    title: str,
    sections: list[Section],
    meta: list[tuple[str, str]] | None = None,
    now: _dt.datetime | None = None,
) -> bytes:
    """PDF of a chatbot answer, a conversation or a comparison: a cover, then one block per section.

    :param kind: kind of document: chatbot answer, chatbot conversation, change summary, document summary or report
    :param title: title on the cover; the kind label when empty
    :param sections: blocks of Markdown, in order
    :param meta: label and value lines under the title; the empty values are left out
    :param now: date of the document, the current time when None
    :return: the PDF bytes
    """
    kind_label = KIND_LABELS.get(kind, KIND_LABELS['report'])
    title = clean_text(title).strip() or kind_label
    meta = [(clean_text(k), clean_text(v)) for k, v in (meta or []) if v]
    now = now or _dt.datetime.now(_dt.UTC)
    date_str = f'{now.day} {now:%B %Y}'

    pdf = _new_pdf(kind_label, title)
    pdf.add_page()
    _cover(pdf, kind_label, title, meta, date_str)

    numbered = len(sections) > 1
    for i, sec in enumerate(sections):
        cite_links = {s.n: s.url for s in sec.sources if s.n is not None and s.url}
        w = _Writer(pdf, cite_links)
        if i:
            pdf.ln(4)
            w.need(30)
        if sec.question:
            w.question(clean_text(sec.question), f'Question {i + 1}' if numbered else 'Question')
        w.markdown(clean_text(sec.markdown))
        w.sources([Source(clean_text(s.title), s.url, s.n, clean_text(s.detail)) for s in sec.sources])
        if numbered and i < len(sections) - 1:
            pdf.ln(3)
            pdf.set_draw_color(*RULE)
            pdf.set_line_width(0.3)
            pdf.line(MARGIN_X, pdf.get_y(), PAGE_W - MARGIN_X, pdf.get_y())
            pdf.ln(3)

    return bytes(pdf.output())


def build_markdown_pdf(markdown_text: str, title: str, kind: str = 'report',
                       meta: list[tuple[str, str]] | None = None) -> bytes:
    """One Markdown body, no question / sources (comparison exports).

    :param markdown_text: Markdown body
    :param title: title of the document
    :param kind: kind of document, see ``build_report_pdf``
    :param meta: label and value lines under the title, None for none
    :return: the PDF bytes
    """
    return build_report_pdf(kind=kind, title=title, meta=meta, sections=[Section(markdown_text)])

