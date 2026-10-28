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
PP_VERSION = 2

# A piece is letters only or digits only (Q0375MI gives Q, 0375, MI).
_PIECE = re.compile(r'[A-Za-z]+|[0-9]+')
# Gap allowed between two pieces of one code: nothing, up to 3 spaces around one or two separators (hyphens,
# unicode dashes, _ / .), or 1 to 3 spaces alone (IAQ 04 04 05, MI - 1365, 19 --058).
_GAP = re.compile(r'(?:[  ]{0,3}[-‐‑−–—_/.]{1,2}[  ]{0,3}|[  ]{1,3})?')
_SPACE = re.compile(r'[ \u00a0]')
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
    """Whether a piece may sit next to a gap with a space: digits, or letters in upper case.

    :param piece: letters only or digits only
    :return: True for ``1027`` or ``BAC``, False for ``of`` or ``Part``
    """
    return piece.isdigit() or piece.isupper()


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

        A code starts at the start of a word. It ends at the end of a word, or between a digit and a glued word of
        letters when the code holds a separator (``MI-13991`` in ``MI-13991Table``, never ``F7`` in ``F7XC5355``).
        A gap with a space joins only upper-case letters or digits (``BAC 5578``, never ``of 10``), and a run without
        digits is upper case with a separator in every gap (``E.A.S.A``, ``N/A``, never ``y a``).

        :param text: question or passage
        :return: the codes found, with their position in the text and the key written for them
        """
        text = text or ''
        pieces = [(m.start(), m.end()) for m in _PIECE.finditer(text)]
        found: list[CodeMatch] = []
        i = 0
        while i < len(pieces):
            start = pieces[i][0]
            if start > 0 and text[start - 1].isalnum():
                i += 1
                continue
            best = None
            key = ''
            has_digit = has_separator = space_only = False
            letters_upper = True
            for j in range(i, min(i + _MAX_PIECES, len(pieces))):
                piece = text[pieces[j][0]:pieces[j][1]]
                if j > i:
                    gap = text[pieces[j - 1][1]:pieces[j][0]]
                    if not _GAP.fullmatch(gap):
                        break
                    previous = text[pieces[j - 1][0]:pieces[j - 1][1]]
                    if _SPACE.search(gap) and not (_strong(previous) and _strong(piece)):
                        break
                    has_separator = has_separator or bool(gap.strip())
                    space_only = space_only or (bool(gap) and not gap.strip())
                has_digit = has_digit or piece.isdigit()
                letters_upper = letters_upper and (piece.isdigit() or piece.isupper())
                key += piece.upper()
                end = pieces[j][1]
                at_word_end = end == len(text) or not text[end].isalnum()
                digit_to_letter = (not at_word_end and piece.isdigit() and has_separator
                                   and text[end:_word_end(text, end)].isalpha())
                letters_ok = has_digit or j == i or (letters_upper and not space_only)
                if (at_word_end or digit_to_letter) and letters_ok and key in self.keys:
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
