"""Glued key of the known codes of a text: ``SF-1027`` gets ``[SF1027]``.

The keyword side of the hybrid search splits ``SF-1027`` into ``SF`` and ``1027``, two common words, so the passage
that cites the code does not come up. The glued key is one rare word, present only where the code is cited, in the
passage as in the question.

No code shape is written here: the text is cut into pieces (letters or digits), neighbouring pieces are glued, and a
run of pieces is kept only when its key is in the dictionary (catalog REFs and the codes kept by the inventory). The
longest run wins. The original text is never changed.
"""

import re
from typing import Iterable, Mapping, NamedTuple

# The block between the matcher markers is embedded as is in the DEV preview notebook; a test keeps both equal.
# --- matcher: start ---
# Bump when the matching or the key changes: stored annotations are recomputed for the new version.
PP_VERSION = 4

# A piece is letters only or digits only (Q0375MI gives Q, 0375, MI).
_PIECE = re.compile(r'[A-Za-z]+|[0-9]+')
# Gap allowed between two pieces of one code: nothing, up to 3 spaces around one or two separators (hyphens,
# unicode dashes, _ / .), or 1 to 3 spaces alone (IAQ 04 04 05, MI - 1365, 19 --058).
_GAP = re.compile(r'(?:[  ]{0,3}[-‐‑−–—_/.]{1,2}[  ]{0,3}|[  ]{1,3})?')
_SPACE = re.compile(r'[ \u00a0]')
# Separators that chain codes into one longer identifier (PTD-TEST-6-A-01, 145.A.30, Rev2025.11_v2.0).
_CHAIN = '-\u2010\u2011\u2212\u2013\u2014_/.'
# Shortest key accepted inside such a chain: shorter ones (A01, A30, V20) are parts of a longer identifier.
_MIN_KEY_IN_CHAIN = 5
# Longest word that a space may join to a code: longer ones are words (STRUCTURE 1000, ALUMINUM 7475).
_MAX_WORD_NEXT_TO_SPACE = 4
# Shortest code written without digits (E.A.S.A, N.D.T): shorter ones are mostly ranges and pairs (A/B, X-Y).
_MIN_LETTERS_KEY = 3
# Longest code, in pieces (IF20037_DLV-31-5207 has 6).
_MAX_PIECES = 12


class CodeMatch(NamedTuple):
    """One known code found in a text."""

    start: int
    end: int
    key: str


def _word_end(text: str, pos: int) -> int:
    """End of the word that contains ``pos``; ``_`` joins a word, as in the keyword tokenizer (``Q0697MI_FR``).

    :param text: text
    :param pos: position in the text
    :return: index of the first character after the word
    """
    while pos < len(text) and (text[pos].isalnum() or text[pos] == '_'):
        pos += 1
    return pos


def _strong(piece: str) -> bool:
    """Whether a piece may sit next to a gap with a space: digits, or a short word ending in upper case.

    :param piece: letters only or digits only
    :return: True for ``1027``, ``BAC`` or ``LdB``, False for ``of``, ``Part`` or ``STRUCTURE``
    """
    return piece.isdigit() or (piece[-1].isupper() and len(piece) <= _MAX_WORD_NEXT_TO_SPACE)


def _inside_number(text: str, pos: int) -> bool:
    """Whether the digit at ``pos`` follows a decimal point or comma of the same number (``5`` in ``0,55``).

    :param text: text
    :param pos: position of a digit, or just after a run of digits for ``pos - 1``
    :return: True when ``text[pos - 2:pos + 1]`` reads digit, ``.`` or ``,``, digit
    """
    return (1 < pos < len(text) and text[pos - 1] in '.,' and text[pos - 2].isdigit()
            and text[pos].isdigit())


class CodeKeys:
    """Finds the known codes of a text and their glued key."""

    def __init__(self, keys: Mapping[str, str] | Iterable[str]) -> None:
        """Builds the dictionary of keys.

        :param keys: glued upper-case keys (``SF1027``), or a mapping from the glued spelling found in a text to the
            key written for it, so that every spelling of one document gives one key (``LDBIT243`` and ``IT243``
            both give ``LDBIT243``, ``Q0697MIFR`` gives ``Q0697MI``)
        """
        pairs = keys.items() if isinstance(keys, Mapping) else ((k, k) for k in keys)
        self.keys = {k.strip().upper(): v.strip().upper() for k, v in pairs if k and k.strip() and v and v.strip()}

    def find(self, text: str) -> list[CodeMatch]:
        """Known codes of the text, left to right, without overlap.

        A code starts at the start of a word and ends at the end of a word, or between a digit and a glued word of
        letters when the code holds a separator (``MI-13991`` in ``MI-13991Table``, never ``F7`` in ``F7XC5355``).
        Inside a longer chain of separated parts, only keys of 5 characters or more count (never ``A01`` in
        ``PTD-TEST-6-A-01``). A number never stops at a decimal point (never ``T31`` in ``T3 1,2``). A gap with a
        space joins only digits or words of at most 4 letters ending in upper case (``BAC 5578``, ``LdB IT-243``;
        never ``of 10`` or ``STRUCTURE 1000``) and never ends on a word (never ``NAS1789 - NUT``). A code without
        digits has 3 letters or more, in upper case, joined by separators without spaces (``E.A.S.A``; never ``A/B``
        or ``SAP`` alone).

        :param text: question or passage
        :return: the codes found, with their position in the text and the key written for them
        """
        text = text or ''
        pieces = [(m.start(), m.end()) for m in _PIECE.finditer(text)]
        found: list[CodeMatch] = []
        i = 0
        while i < len(pieces):
            start = pieces[i][0]
            if (start > 0 and text[start - 1].isalnum()) or _inside_number(text, start):
                i += 1
                continue
            chain_before = start > 1 and text[start - 1] in _CHAIN and text[start - 2].isalnum()
            best = None
            key = ''
            has_digit = has_separator = has_space = ends_on_spaced_word = False
            letters_upper = True
            for j in range(i, min(i + _MAX_PIECES, len(pieces))):
                piece = text[pieces[j][0]:pieces[j][1]]
                ends_on_spaced_word = False
                if j > i:
                    gap = text[pieces[j - 1][1]:pieces[j][0]]
                    if not _GAP.fullmatch(gap):
                        break
                    previous = text[pieces[j - 1][0]:pieces[j - 1][1]]
                    if _SPACE.search(gap):
                        if not (_strong(previous) and _strong(piece)):
                            break
                        has_space = True
                        ends_on_spaced_word = piece.isalpha() and len(piece) > 1
                    has_separator = has_separator or bool(gap.strip())
                has_digit = has_digit or piece.isdigit()
                letters_upper = letters_upper and (piece.isdigit() or piece.isupper())
                key += piece.upper()
                end = pieces[j][1]
                at_word_end = end == len(text) or not text[end].isalnum()
                digit_to_letter = (not at_word_end and piece.isdigit() and has_separator
                                   and text[end:_word_end(text, end)].isalpha())
                chain_after = end + 1 < len(text) and text[end] in _CHAIN and text[end + 1].isalnum()
                if has_digit:
                    shape_ok = not ends_on_spaced_word and not (piece.isdigit() and _inside_number(text, end + 1))
                else:
                    shape_ok = j > i and letters_upper and not has_space and len(key) >= _MIN_LETTERS_KEY
                if (chain_before or chain_after) and len(key) < _MIN_KEY_IN_CHAIN:
                    shape_ok = False
                if (at_word_end or digit_to_letter) and shape_ok and key in self.keys:
                    best = (j, end, self.keys[key])
            if best:
                j, end, k = best
                found.append(CodeMatch(start, end, k))
                i = j + 1
            else:
                i += 1
        return found

    def annotate(self, text: str) -> str:
        """The text with the key added after each code not already written as its key.

        ``SF-1027`` becomes ``SF-1027 [SF1027]``, ``MI-13991Table`` becomes ``MI-13991Table [MI13991]``, ``SF1027``
        stays as it is. Idempotent: a code already followed by its key gets no second one.

        :param text: question or passage
        :return: the annotated text
        """
        text = text or ''
        out, last = [], 0
        for m in self.find(text):
            written = text[m.start:m.end]
            end = _word_end(text, m.end)
            if written.upper() == m.key and end == m.end:
                continue
            tag = f' [{m.key}]'
            if text.startswith(tag, end) or end < last:
                continue
            out.append(text[last:end])
            out.append(tag)
            last = end
        out.append(text[last:])
        return ''.join(out)
# --- matcher: end ---
