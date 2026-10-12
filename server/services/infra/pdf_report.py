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
import re
import unicodedata
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

FONT_DIR = Path(__file__).resolve().parents[2] / 'assets' / 'fonts'
_FONT_FILES = {
    '': 'Inter-Regular.ttf',
    'B': 'Inter-SemiBold.ttf',
    'I': 'Inter-Italic.ttf',
    'BI': 'Inter-SemiBoldItalic.ttf',
}
FONT = 'Inter'

KIND_LABELS = {
    'chat_response': 'Chatbot answer',
    'chat_session': 'Chatbot conversation',
    'change_summary': 'Change summary',
    'document_summary': 'Document summary',
    'report': 'Report',
}

# ── Palette (RGB) — accent is the app's --color-accent-primary (#0055A4) ─────
ACCENT = (0, 85, 164)
ACCENT_DARK = (0, 58, 112)
ACCENT_TINT = (238, 244, 251)
INK = (15, 23, 42)          # titles
TEXT = (51, 65, 85)         # body
MUTED = (100, 116, 139)     # labels, footer
FAINT = (148, 163, 184)
RULE = (226, 232, 240)
ZEBRA = (248, 250, 252)
CODE_BG = (243, 245, 248)

# ── Geometry (mm) ────────────────────────────────────────────────────────────
PAGE_W, PAGE_H = 210, 297
MARGIN_X = 20
TOP = 22            # body top on pages 2+ (below the running header)
BOTTOM = 20
W = PAGE_W - 2 * MARGIN_X

BODY_SIZE = 10
LINE_H = 5.4


@dataclass
class Source:
    title: str              # REF
    url: str | None = None
    n: int | None = None    # citation number, None for an uncited source
    detail: str = ''        # Intraqual title, revision and date


@dataclass
class Section:
    markdown: str
    question: str | None = None
    sources: list[Source] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Text cleanup — keep only what the font can draw
# ---------------------------------------------------------------------------

# Glyphs Inter lacks that LLMs like to emit → closest glyph it has.
_GLYPH_FALLBACK = {
    '✅': '✓', '✔': '✓', '☑': '✓', '✘': '✗', '❌': '✗', '❎': '✗',
    '☐': '□', '⬇': '↓', '⟦': '[', '⟧': ']', ' ': ' ', ' ': ' ',
    ' ': '\n', ' ': '\n', '\t': '    ',
    '🔴': '●', '🟠': '●', '🟡': '●', '🟢': '●', '🔵': '●',
}


@lru_cache(maxsize=1)
def _font_charset() -> frozenset[int]:
    from fontTools.ttLib import TTFont

    return frozenset(TTFont(FONT_DIR / _FONT_FILES['']).getBestCmap())


def clean_text(text: str) -> str:
    """NFC-normalise, map missing glyphs, drop what no font here can render (emoji…)."""
    charset = _font_charset()
    out = []
    for ch in unicodedata.normalize('NFC', text or ''):
        ch = _GLYPH_FALLBACK.get(ch, ch)
        if len(ch) == 1 and unicodedata.category(ch) == 'Cf':
            continue  # zero-width / soft hyphen: invisible on screen, stray boxes in a PDF
        if len(ch) != 1 or ch == '\n' or ord(ch) in charset:
            out.append(ch)
        elif unicodedata.category(ch) in ('Zs',):
            out.append(' ')
        # anything else (emoji, variation selectors, zero-width chars) is dropped
    return ''.join(out)


# ---------------------------------------------------------------------------
# Inline Markdown → styled spans
# ---------------------------------------------------------------------------

@dataclass
class Span:
    text: str
    bold: bool = False
    italic: bool = False
    code: bool = False
    link: str | None = None
    cite: int | None = None


_INLINE_RE = re.compile(
    r'(?P<code>`[^`\n]+`)'
    r'|(?P<link>\[(?P<ltext>[^\]\n]+)\]\((?P<lurl>[^)\s]+)\))'
    r'|(?P<cite>\[\d{1,3}(?:\s*[,;]\s*\d{1,3})*\])'
    r'|(?P<bi>\*\*\*(?P<bit>.+?)\*\*\*)'
    r'|(?P<b>\*\*(?P<bt>.+?)\*\*)'
    r'|(?P<b2>(?<![\w])__(?P<b2t>[^_\n]+?)__(?![\w]))'
    r'|(?P<i>(?<![\w*])\*(?![\s*])(?P<it>[^*\n]+?)(?<!\s)\*(?![\w*]))'
    r'|(?P<i2>(?<![\w])_(?![\s_])(?P<i2t>[^_\n]+?)(?<!\s)_(?![\w]))'
    r'|(?P<strike>~~(?P<st>.+?)~~)'
    r'|(?P<url>https?://[^\s<>()\]]+[^\s<>()\].,;:!?\'"])'
)
_HTML_BR_RE = re.compile(r'<br\s*/?>', re.IGNORECASE)
_HTML_TAG_RE = re.compile(r'</?(?:b|strong|i|em|u|span|sup|sub|p|div)\b[^>]*>', re.IGNORECASE)


def _unescape(s: str) -> str:
    s = _HTML_TAG_RE.sub('', _HTML_BR_RE.sub(' ', s))
    for a, b in (('&nbsp;', ' '), ('&lt;', '<'), ('&gt;', '>'), ('&quot;', '"'), ('&#39;', "'"), ('&amp;', '&')):
        s = s.replace(a, b)
    return re.sub(r'\\([\\`*_{}\[\]()#+\-.!|>~])', r'\1', s)


def parse_inline(text: str, bold: bool = False, italic: bool = False) -> list[Span]:
    spans: list[Span] = []
    pos = 0

    def plain(s: str) -> None:
        if s:
            spans.append(Span(_unescape(s), bold, italic))

    for m in _INLINE_RE.finditer(text):
        plain(text[pos:m.start()])
        pos = m.end()
        if m.group('code'):
            spans.append(Span(m.group('code')[1:-1], bold, italic, code=True))
        elif m.group('link'):
            for sp in parse_inline(m.group('ltext'), bold, italic):
                sp.link = m.group('lurl')
                spans.append(sp)
        elif m.group('cite'):
            for n in re.findall(r'\d+', m.group('cite')):
                spans.append(Span(n, cite=int(n)))
        elif m.group('bi'):
            spans += parse_inline(m.group('bit'), True, True)
        elif m.group('b') or m.group('b2'):
            spans += parse_inline(m.group('bt') or m.group('b2t'), True, italic)
        elif m.group('i') or m.group('i2'):
            spans += parse_inline(m.group('it') or m.group('i2t'), bold, True)
        elif m.group('strike'):
            spans += parse_inline(m.group('st'), bold, italic)
        elif m.group('url'):
            spans.append(Span(m.group('url'), bold, italic, link=m.group('url')))
    plain(text[pos:])
    return spans


def plain_text(text: str) -> str:
    """Inline Markdown → plain string (table cells, headings)."""
    return ''.join(f'[{s.text}]' if s.cite is not None else s.text for s in parse_inline(text)).strip()


# ---------------------------------------------------------------------------
# Block Markdown → tokens
# ---------------------------------------------------------------------------

_HEADING_RE = re.compile(r'^(#{1,6})\s+(.*?)\s*#*\s*$')
_BULLET_RE = re.compile(r'^(\s*)[-*+•]\s+(.*)$')
_ORDERED_RE = re.compile(r'^(\s*)(\d{1,3})[.)]\s+(.*)$')
_HR_RE = re.compile(r'^\s*([-*_])(\s*\1){2,}\s*$')
_TABLE_SEP_RE = re.compile(r'^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$')


def _indent(ws: str) -> int:
    return len(ws.replace('\t', '    ')) // 2


def _cells(row: str) -> list[str]:
    row = row.strip()
    row = row.removeprefix('|')
    if row.endswith('|') and not row.endswith('\\|'):
        row = row[:-1]
    return [c.strip() for c in re.split(r'(?<!\\)\|', row)]


def tokenize(md: str) -> list[dict]:
    lines = md.replace('\r\n', '\n').replace('\r', '\n').split('\n')
    tokens: list[dict] = []
    i = 0

    def starts_block(ln: str) -> bool:
        s = ln.strip()
        return (not s or s.startswith(('```', '~~~', '>', '|')) or bool(_HEADING_RE.match(s))
                or bool(_BULLET_RE.match(ln)) or bool(_ORDERED_RE.match(ln)) or bool(_HR_RE.match(ln)))

    while i < len(lines):
        line = lines[i]
        s = line.strip()

        if s.startswith(('```', '~~~')):
            fence = s[:3]
            body = []
            i += 1
            while i < len(lines) and not lines[i].strip().startswith(fence):
                body.append(lines[i])
                i += 1
            i += 1
            tokens.append({'kind': 'code', 'text': '\n'.join(body)})
            continue

        if not s:
            i += 1
            continue

        if (m := _HEADING_RE.match(s)) and m.group(2):
            tokens.append({'kind': 'heading', 'level': len(m.group(1)), 'text': m.group(2)})
            i += 1
            continue

        if _HR_RE.match(line):
            tokens.append({'kind': 'hr'})
            i += 1
            continue

        if s.startswith('>'):
            quote = []
            while i < len(lines) and lines[i].strip().startswith('>'):
                quote.append(re.sub(r'^\s*>\s?', '', lines[i]))
                i += 1
            tokens.append({'kind': 'quote', 'tokens': tokenize('\n'.join(quote))})
            continue

        if '|' in s and i + 1 < len(lines) and _TABLE_SEP_RE.match(lines[i + 1]) and '|' in lines[i + 1]:
            header = _cells(s)
            rows = []
            i += 2
            while i < len(lines) and '|' in lines[i] and lines[i].strip():
                rows.append(_cells(lines[i]))
                i += 1
            tokens.append({'kind': 'table', 'header': header, 'rows': rows})
            continue

        bm, om = _BULLET_RE.match(line), _ORDERED_RE.match(line)
        if bm or om:
            depth = _indent((bm or om).group(1))
            text = bm.group(2) if bm else om.group(3)
            i += 1
            # continuation lines (indented, not a new block) belong to this item
            while i < len(lines) and lines[i].strip() and lines[i][:1] in (' ', '\t') and not starts_block(lines[i]):
                text += ' ' + lines[i].strip()
                i += 1
            tok = {'kind': 'item', 'depth': depth, 'text': text, 'ordered': bool(om)}
            if om:
                tok['num'] = om.group(2)
            tokens.append(tok)
            continue

        para = [s]
        i += 1
        while i < len(lines) and not starts_block(lines[i]):
            para.append(lines[i].strip())
            i += 1
        tokens.append({'kind': 'para', 'text': ' '.join(para)})

    # Normalise list depths: the first item of a list is depth 0 whatever its indent.
    base = None
    for t in tokens:
        if t['kind'] == 'item':
            base = t['depth'] if base is None else min(base, t['depth'])
            t['depth'] = min(t['depth'] - base, 4)
        else:
            base = None
    return tokens


# ---------------------------------------------------------------------------
# Renderer
# ---------------------------------------------------------------------------

def _style(bold: bool, italic: bool) -> str:
    return ('B' if bold else '') + ('I' if italic else '')


def _new_pdf(kind_label: str, title: str):
    from fpdf import FPDF

    class _Report(FPDF):
        def header(self):
            # Thin accent stripe on every page.
            self.set_fill_color(*ACCENT)
            self.rect(0, 0, PAGE_W, 1.6, 'F')
            if self.page_no() == 1:
                return
            self.set_font(FONT, 'B', 7.5)
            self.set_text_color(*ACCENT)
            self.set_char_spacing(0.6)
            self.text(MARGIN_X, 11.5, 'QUALIBOT')
            label_x = MARGIN_X + self.get_string_width('QUALIBOT') + 2
            self.set_char_spacing(0)
            self.set_font(FONT, '', 7.5)
            self.set_text_color(*MUTED)
            self.text(label_x, 11.5, f'·  {kind_label}')
            if title != kind_label:
                short = title if len(title) <= 80 else title[:77].rstrip() + '…'
                self.text(PAGE_W - MARGIN_X - self.get_string_width(short), 11.5, short)
            self.set_draw_color(*RULE)
            self.set_line_width(0.2)
            self.line(MARGIN_X, 14.5, PAGE_W - MARGIN_X, 14.5)

        def footer(self):
            y = PAGE_H - 11
            self.set_draw_color(*RULE)
            self.set_line_width(0.2)
            self.line(MARGIN_X, y - 4, PAGE_W - MARGIN_X, y - 4)
            self.set_font(FONT, '', 7.5)
            self.set_text_color(*FAINT)
            self.set_xy(MARGIN_X, y - 3)
            self.cell(W - 20, 4, 'Generated by QualiBOT — AI-generated content, check it against the source documents.')
            # cell(), not text(): only cells get the {nb} page-count alias replaced
            self.set_xy(PAGE_W - MARGIN_X - 20, y - 3)
            self.cell(20, 4, f'{self.page_no()} / {{nb}}', align='R')

    pdf = _Report(format='A4', unit='mm')
    for style, file in _FONT_FILES.items():
        pdf.add_font(FONT, style, str(FONT_DIR / file))
    pdf.set_margins(MARGIN_X, TOP, MARGIN_X)
    pdf.set_auto_page_break(True, margin=BOTTOM)
    pdf.alias_nb_pages()
    pdf.c_margin = 0  # text aligns on the margin; tables and code blocks set their own padding
    pdf.set_title(title)
    pdf.set_author('QualiBOT')
    pdf.set_creator('QualiBOT')
    return pdf


class _Writer:
    def __init__(self, pdf, cite_links: dict[int, str]):
        self.pdf = pdf
        self.cite_links = cite_links

    # ── helpers ──────────────────────────────────────────────────────────────
    def room(self) -> float:
        return self.pdf.page_break_trigger - self.pdf.get_y()

    def need(self, h: float) -> None:
        if self.room() < h:
            self.pdf.add_page()

    def spans(self, spans: list[Span], size: float = BODY_SIZE, color=TEXT, line_h: float = LINE_H,
              bold: bool = False) -> None:
        """Flow styled spans from the current position; wraps at the margins."""
        pdf = self.pdf
        for sp in spans:
            if not sp.text:
                continue
            if sp.cite is not None:
                pdf.set_font(FONT, 'B', size)
                pdf.set_text_color(*ACCENT)
                pdf.char_vpos = 'SUP'
                pdf.write(line_h, f'[{sp.text}]', link=self.cite_links.get(sp.cite, ''))
                pdf.char_vpos = 'LINE'
                continue
            pdf.set_font(FONT, _style(sp.bold or bold, sp.italic), size - (0.5 if sp.code else 0))
            if sp.link:
                pdf.set_text_color(*ACCENT)
                pdf.write(line_h, sp.text, link=sp.link)
            else:
                pdf.set_text_color(*(ACCENT_DARK if sp.code else (INK if sp.bold or bold else color)))
                pdf.write(line_h, sp.text)

    # ── blocks ───────────────────────────────────────────────────────────────
    def heading(self, level: int, text: str, first: bool, keep: float = 26) -> None:
        pdf = self.pdf
        size, color, before = {1: (15, INK, 7), 2: (12.5, ACCENT_DARK, 6), 3: (11, INK, 4.5)}.get(level, (10, INK, 3.5))
        if not first:
            pdf.ln(before)
        self.need(size * 0.5 + keep)  # keep the heading with the start of what follows
        pdf.set_x(pdf.l_margin)
        pdf.set_font(FONT, 'B', size)
        pdf.set_text_color(*color)
        pdf.multi_cell(0, size * 0.48, plain_text(text), new_x='LMARGIN', new_y='NEXT')
        if level <= 2:
            pdf.ln(1.2)
            pdf.set_draw_color(*(ACCENT if level == 1 else RULE))
            pdf.set_line_width(0.5 if level == 1 else 0.25)
            pdf.line(pdf.l_margin, pdf.get_y(), pdf.w - pdf.r_margin, pdf.get_y())
            pdf.ln(3)
        else:
            pdf.ln(1.5)

    def para(self, text: str) -> None:
        pdf = self.pdf
        self.need(LINE_H * 2)
        pdf.set_x(pdf.l_margin)
        self.spans(parse_inline(text))
        pdf.ln(LINE_H)
        pdf.ln(2.2)

    def item(self, tok: dict, next_is_item: bool) -> None:
        pdf = self.pdf
        depth = tok['depth']
        left0 = pdf.l_margin
        marker_x = left0 + depth * 6
        text_x = marker_x + (7 if tok['ordered'] else 4.5)
        self.need(LINE_H * 1.5)
        y = pdf.get_y()
        if tok['ordered']:
            pdf.set_font(FONT, 'B', BODY_SIZE)
            pdf.set_text_color(*ACCENT)
            pdf.text(marker_x, y + LINE_H * 0.72, f"{tok['num']}.")
        elif depth == 0:
            pdf.set_fill_color(*ACCENT)
            pdf.ellipse(marker_x + 0.4, y + LINE_H / 2 - 0.75, 1.5, 1.5, 'F')
        else:
            pdf.set_draw_color(*MUTED)
            pdf.set_line_width(0.3)
            pdf.line(marker_x + 0.2, y + LINE_H / 2, marker_x + 2, y + LINE_H / 2)
        pdf.set_left_margin(text_x)
        pdf.set_xy(text_x, y)
        self.spans(parse_inline(tok['text']))
        pdf.set_left_margin(left0)
        pdf.ln(LINE_H)
        pdf.ln(1.3 if next_is_item else 3)

    def quote(self, tokens: list[dict]) -> None:
        pdf = self.pdf
        left0 = pdf.l_margin
        self.need(LINE_H * 2)
        pdf.ln(1)
        y0, page0 = pdf.get_y(), pdf.page_no()
        pdf.set_left_margin(left0 + 6)
        pdf.set_x(left0 + 6)
        for t in tokens:
            text = t.get('text', '')
            if t['kind'] == 'item':
                text = ('• ' if not t['ordered'] else f"{t['num']}. ") + text
            if text:
                pdf.set_x(pdf.l_margin)
                self.spans(parse_inline(text), color=MUTED)
                pdf.ln(LINE_H)
                pdf.ln(1)
        pdf.set_left_margin(left0)
        y1 = pdf.get_y() - 1
        top = y0 if pdf.page_no() == page0 else TOP
        pdf.set_fill_color(*ACCENT)
        pdf.rect(left0, top, 1, max(y1 - top, LINE_H), 'F')
        pdf.ln(3)

    def code(self, text: str) -> None:
        pdf = self.pdf
        pdf.ln(1)
        pdf.set_font(FONT, '', 8.5)
        pdf.set_text_color(*INK)
        pdf.set_fill_color(*CODE_BG)
        pdf.set_x(pdf.l_margin)
        pdf.multi_cell(0, 4.4, text.rstrip('\n') or ' ', fill=True, padding=(2.5, 3), new_x='LMARGIN', new_y='NEXT')
        pdf.ln(3)

    def hr(self) -> None:
        pdf = self.pdf
        pdf.ln(2)
        pdf.set_draw_color(*RULE)
        pdf.set_line_width(0.25)
        pdf.line(pdf.l_margin, pdf.get_y(), pdf.w - pdf.r_margin, pdf.get_y())
        pdf.ln(5)

    def table(self, header: list[str], rows: list[list[str]]) -> None:
        from fpdf.fonts import FontFace

        pdf = self.pdf
        n = max([len(header)] + [len(r) for r in rows])
        header = [plain_text(c) for c in (header + [''] * n)[:n]]
        rows = [[plain_text(c) for c in (r + [''] * n)[:n]] for r in rows]
        size = 8.5 if n <= 4 else 7.8
        pad = 6

        # Column widths from content: natural width, floored by the longest word.
        nat, low = [], []
        for c in range(n):
            col = [header[c]] + [r[c] for r in rows]
            pdf.set_font(FONT, 'B', size)
            hw = pdf.get_string_width(header[c])
            pdf.set_font(FONT, '', size)
            nat.append(min(max([hw] + [pdf.get_string_width(v) for v in col[1:]]), 90) + pad)
            words = [w for v in col[1:] for w in v.split()] or ['']
            pdf.set_font(FONT, 'B', size)
            head_word = max([pdf.get_string_width(w) for w in header[c].split()] or [0])
            pdf.set_font(FONT, '', size)
            low.append(min(max([head_word] + [pdf.get_string_width(w) for w in words]), 40) + pad)
        if sum(nat) <= W:
            widths = [w * W / sum(nat) for w in nat]  # full width, proportional to content
        elif sum(low) >= W:
            widths = low
        else:
            extra = W - sum(low)
            slack = [a - b for a, b in zip(nat, low)]
            widths = [b + extra * s / (sum(slack) or 1) for b, s in zip(low, slack)]

        self.need(24)
        pdf.ln(1)
        pdf.set_fill_color(*ZEBRA)  # table() fills alternate rows with the current fill colour
        pdf.set_font(FONT, '', size)
        pdf.set_text_color(*TEXT)
        pdf.set_draw_color(*RULE)
        pdf.set_line_width(0.2)
        with pdf.table(
            width=W,
            col_widths=widths,
            align='LEFT',
            line_height=size * 0.5,
            padding=(1.6, 2.2),
            borders_layout='HORIZONTAL_LINES',
            cell_fill_color=ZEBRA,
            cell_fill_mode='ROWS',
            headings_style=FontFace(emphasis='BOLD', color=ACCENT_DARK, fill_color=ACCENT_TINT),
            text_align='LEFT',
            first_row_as_headings=True,
            repeat_headings=1,
        ) as t:
            for r in [header] + rows:
                row = t.row()
                for v in r:
                    row.cell(v)
        pdf.ln(4)

    # ── composite blocks ─────────────────────────────────────────────────────
    def markdown(self, md: str) -> None:
        tokens = tokenize(md)
        for k, tok in enumerate(tokens):
            kind = tok['kind']
            if kind == 'heading':
                # a heading followed by another heading keeps both with the text below
                nxt = tokens[k + 1] if k + 1 < len(tokens) else None
                self.heading(tok['level'], tok['text'], first=k == 0,
                             keep=58 if nxt and nxt['kind'] == 'heading' else 26)
            elif kind == 'para':
                self.para(tok['text'])
            elif kind == 'item':
                nxt = tokens[k + 1] if k + 1 < len(tokens) else None
                self.item(tok, next_is_item=bool(nxt and nxt['kind'] == 'item'))
            elif kind == 'quote':
                self.quote(tok['tokens'])
            elif kind == 'code':
                self.code(tok['text'])
            elif kind == 'table':
                self.table(tok['header'], tok['rows'])
            elif kind == 'hr':
                # an hr right after a heading's own rule is noise
                # (and so is one right before a heading, which draws its own rule)
                if 0 < k < len(tokens) - 1 and 'heading' not in (tokens[k - 1]['kind'], tokens[k + 1]['kind']):
                    self.hr()

    def question(self, text: str, label: str) -> None:
        pdf = self.pdf
        pdf.set_font(FONT, 'B', 10.5)
        lines = pdf.multi_cell(W - 10, 5.4, plain_text(text), dry_run=True, output='LINES')
        h = 9 + len(lines) * 5.4 + 3.5
        self.need(min(h, 80) + 75)  # the question stays with the start of its answer (often 2 headings)
        x, y = pdf.l_margin, pdf.get_y()
        pdf.set_fill_color(*ACCENT_TINT)
        pdf.rect(x, y, W, h, 'F')
        pdf.set_fill_color(*ACCENT)
        pdf.rect(x, y, 1.2, h, 'F')
        pdf.set_font(FONT, 'B', 7.2)
        pdf.set_text_color(*ACCENT)
        pdf.set_char_spacing(0.6)
        pdf.text(x + 6, y + 6, label.upper())
        pdf.set_char_spacing(0)
        pdf.set_xy(x + 6, y + 8)
        pdf.set_font(FONT, 'B', 10.5)
        pdf.set_text_color(*INK)
        pdf.multi_cell(W - 10, 5.4, plain_text(text), new_x='LMARGIN', new_y='NEXT')
        pdf.set_y(y + h + 5)

    def sources(self, sources: list[Source]) -> None:
        if not sources:
            return
        pdf = self.pdf
        pdf.ln(2)
        self.need(14)
        pdf.set_draw_color(*RULE)
        pdf.set_line_width(0.2)
        pdf.line(pdf.l_margin, pdf.get_y(), pdf.l_margin + 30, pdf.get_y())
        pdf.ln(3)
        pdf.set_font(FONT, 'B', 7.2)
        pdf.set_text_color(*MUTED)
        pdf.set_char_spacing(0.6)
        pdf.set_x(pdf.l_margin)
        pdf.cell(0, 4, 'SOURCES', new_x='LMARGIN', new_y='NEXT')
        pdf.set_char_spacing(0)
        pdf.ln(1.2)
        left0 = pdf.l_margin
        for s in sources:
            self.need(5)
            y = pdf.get_y()
            pdf.set_font(FONT, 'B', 8.5)
            pdf.set_text_color(*ACCENT)
            pdf.text(left0, y + 3.3, f'[{s.n}]' if s.n is not None else '•')
            pdf.set_left_margin(left0 + 9)
            pdf.set_xy(left0 + 9, y)
            pdf.set_font(FONT, 'B', 8.5)
            pdf.set_text_color(*(ACCENT_DARK if s.url else INK))
            pdf.write(4.6, s.title, link=s.url or '')
            if s.detail:
                pdf.set_font(FONT, '', 8.5)
                pdf.set_text_color(*MUTED)
                pdf.write(4.6, f'  ·  {s.detail}')
            pdf.set_left_margin(left0)
            pdf.ln(4.6)
            pdf.ln(0.8)


def _cover(pdf, kind_label: str, title: str, meta: list[tuple[str, str]], date_str: str) -> None:
    x = MARGIN_X
    pdf.set_font(FONT, 'B', 8)
    pdf.set_text_color(*ACCENT)
    pdf.set_char_spacing(0.8)
    pdf.text(x, 15, 'QUALIBOT')
    label_x = x + pdf.get_string_width('QUALIBOT') + 2.5
    pdf.set_char_spacing(0.4)
    pdf.set_font(FONT, '', 8)
    pdf.set_text_color(*MUTED)
    pdf.text(label_x, 15, f'·  {kind_label.upper()}')
    pdf.set_char_spacing(0)
    pdf.text(PAGE_W - MARGIN_X - pdf.get_string_width(date_str), 15, date_str)

    pdf.set_xy(x, 21)
    pdf.set_font(FONT, 'B', 19)
    lines = pdf.multi_cell(W, 8.2, title, dry_run=True, output='LINES')
    if len(lines) > 3:
        title = ' '.join(lines[:3])[: max(len(lines[0]) * 3 - 2, 20)].rstrip() + '…'
    pdf.set_text_color(*INK)
    pdf.multi_cell(W, 8.2, title, new_x='LMARGIN', new_y='NEXT')

    if meta:
        pdf.ln(2.5)
        pdf.set_font(FONT, 'B', 7)
        label_w = max(pdf.get_string_width(k.upper()) for k, _ in meta) + 6
        for k, v in meta:
            y = pdf.get_y()
            pdf.set_font(FONT, 'B', 7)
            pdf.set_text_color(*MUTED)
            pdf.set_char_spacing(0.4)
            pdf.text(x, y + 3.6, k.upper())
            pdf.set_char_spacing(0)
            pdf.set_xy(x + label_w, y)
            pdf.set_font(FONT, '', 9)
            pdf.set_text_color(*TEXT)
            pdf.multi_cell(W - label_w, 5, v, new_x='LMARGIN', new_y='NEXT')
            pdf.ln(0.6)

    pdf.ln(3)
    pdf.set_draw_color(*RULE)
    pdf.set_line_width(0.3)
    pdf.line(x, pdf.get_y(), PAGE_W - MARGIN_X, pdf.get_y())
    pdf.ln(7)


def build_report_pdf(
    *,
    kind: str,
    title: str,
    sections: list[Section],
    meta: list[tuple[str, str]] | None = None,
    now: _dt.datetime | None = None,
) -> bytes:
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
    """One Markdown body, no question / sources (comparison exports)."""
    return build_report_pdf(kind=kind, title=title, meta=meta, sections=[Section(markdown_text)])
