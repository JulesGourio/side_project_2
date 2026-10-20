"""Markdown (as written by the LLM) to styled spans and block tokens, limited to what the font can draw."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from functools import lru_cache

from server.services.infra.pdf_report.common import _FONT_FILES, FONT_DIR

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
    """NFC-normalise, map missing glyphs, drop what no font here can render (emoji…).

    :param text: text to clean, None allowed
    :return: the text, normalised and limited to the glyphs the font draws
    """
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
    """Removes the HTML tags, entities and backslash escapes that the model writes.

    :param s: text to unescape
    :return: the text without them
    """
    s = _HTML_TAG_RE.sub('', _HTML_BR_RE.sub(' ', s))
    for a, b in (('&nbsp;', ' '), ('&lt;', '<'), ('&gt;', '>'), ('&quot;', '"'), ('&#39;', "'"), ('&amp;', '&')):
        s = s.replace(a, b)
    return re.sub(r'\\([\\`*_{}\[\]()#+\-.!|>~])', r'\1', s)


def parse_inline(text: str, bold: bool = False, italic: bool = False) -> list[Span]:
    """Inline Markdown of a text, as styled spans.

    :param text: inline Markdown
    :param bold: True when the text is already bold
    :param italic: True when the text is already italic
    :return: the spans, in order
    """
    spans: list[Span] = []
    pos = 0

    def plain(s: str) -> None:
        """Adds ``s`` as a plain span, with the current style.

        :param s: text between two matches
        """
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
    """Inline Markdown → plain string (table cells, headings).

    :param text: inline Markdown
    :return: the plain text, citations kept as ``[n]``
    """
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
    """List nesting level of an indentation: two spaces, or a tab, per level.

    :param ws: leading whitespace of a line
    :return: the nesting level
    """
    return len(ws.replace('\t', '    ')) // 2


def _cells(row: str) -> list[str]:
    """The cells of a Markdown table row.

    :param row: table row, with or without its outer pipes
    :return: the stripped cells
    """
    row = row.strip()
    row = row.removeprefix('|')
    if row.endswith('|') and not row.endswith('\\|'):
        row = row[:-1]
    return [c.strip() for c in re.split(r'(?<!\\)\|', row)]


def tokenize(md: str) -> list[dict]:
    """Block tokens of a Markdown text: headings, paragraphs, lists, tables, quotes and code blocks.

    :param md: Markdown text
    :return: the tokens in order, each a dict with a ``kind``
    """
    lines = md.replace('\r\n', '\n').replace('\r', '\n').split('\n')
    tokens: list[dict] = []
    i = 0

    def starts_block(ln: str) -> bool:
        """Whether a line starts a new block, so it does not continue the paragraph or item before it.

        :param ln: line to test
        :return: True when it starts a block
        """
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
