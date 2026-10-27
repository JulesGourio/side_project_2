"""Canonical key of the document codes written in a text (``ST-1027``, ``st 1027``, ``ST_1027`` -> ``ST1027``).

Only codes whose prefix is a prefix of a catalog REF are recognised; every other token is left alone. A keyword search
splits ``ST-1027`` into ``ST`` and ``1027``, so a passage that spells the code differently from the question is not
found; the key is the one spelling both sides can share. It equals ``doc_catalog.canon_ref`` of the REF.
"""

import re
from typing import Iterable, List, NamedTuple, Set

# Same language suffixes as ``infra.doc_catalog``, which a test keeps in step.
LANG_SUFFIXES = ('FR', 'EN', 'GB', 'MX', 'BG', 'CZ', 'BR', 'ES')

# Bump when the recognised spellings or the key change: stored annotations are recomputed for the new version.
PP_VERSION = 1

# Separators inside a code: hyphen-like characters (hyphen, U+2010, non-breaking hyphen, minus), underscore, slash and dot.
# An en or em dash joins the parts of a code only when it touches both of them, since it also separates a title.
_SEP = '[-\u2010\u2011\u2212_/.]'
_TIGHT_DASH = '[\u2013\u2014]'
# Separator between the groups of a code, with at most one space on each side.
_GROUP_SEP = rf'(?:\s?{_SEP}\s?|{_TIGHT_DASH})'
# Separator between the prefix and the first digits: observed with up to three spaces around the dash (``TDA8 - 00/027``).
_PREFIX_SEP = rf'(?:\s{{0,3}}{_SEP}\s{{0,3}}|{_TIGHT_DASH})'
_DIGITS = rf'\d+(?:{_GROUP_SEP}\d+)*'

# A REF starts with: letters then one digit before a separator (``UDE2-00``), else letters (and a second word) before
# its digits (``IF20316``, ``LdB IT-243``).
_PREFIX_WITH_DIGIT = re.compile(r'([A-Za-z]+[0-9])(?=[-_/. ])')
_PREFIX_LETTERS = re.compile(r'([A-Za-z]+(?: [A-Za-z]+)?)(?=[-_/. ]?[0-9])')
# Letters attached to the digits at the end of a REF, before any language suffix (``Q0375MI``).
_TAIL = re.compile(r'[0-9]([A-Za-z]{1,3})(?:_[A-Za-z]{2})?$')


class CodeMatch(NamedTuple):
    """One code found in a text."""

    start: int
    end: int
    key: str


class CodeKeys:
    """Recognises the codes of a text and gives their canonical key; built from the catalog REFs."""

    def __init__(self, refs: Iterable[str]) -> None:
        """Learns the prefixes and the attached letters of the REFs.

        :param refs: REFs of the catalog
        """
        prefixes: Set[str] = set()
        tails: Set[str] = set()
        for ref in refs:
            ref = (ref or '').strip()
            m = _PREFIX_WITH_DIGIT.match(ref) or _PREFIX_LETTERS.match(ref)
            if m:
                prefixes.add(m.group(1).upper())
            t = _TAIL.search(ref)
            if t:
                tails.add(t.group(1).upper())
        self.prefixes = frozenset(prefixes)
        self.tails = frozenset(tails)
        self._patterns = self._compile()

    def _compile(self) -> List['re.Pattern[str]']:
        """Compiles the patterns: one for prefixes of two letters or more, one for single letters.

        A single letter is a code prefix only when attached to its digits or to a dash, never separated by a space.

        :return: the compiled patterns, empty when no prefix is known
        """
        tail = rf'(?P<tail>{"|".join(sorted(map(re.escape, self.tails), key=len, reverse=True))})?' if self.tails else ''
        lang = rf'(?:[_-](?P<lang>{"|".join(LANG_SUFFIXES)}))?'
        patterns = []
        for multi in (True, False):
            group = [p for p in self.prefixes if (len(p.replace(' ', '')) >= 2) == multi]
            if not group:
                continue
            alt = '|'.join(re.escape(p).replace(r'\ ', r'\s+') for p in sorted(group, key=len, reverse=True))
            link = rf'(?:{_PREFIX_SEP}|\s{{1,3}}|(?=\d))' if multi else rf'(?:{_PREFIX_SEP}|(?=\d))'
            patterns.append(re.compile(
                rf'(?<![A-Za-z0-9])(?P<prefix>{alt}){link}(?P<digits>{_DIGITS}){tail}{lang}(?![A-Za-z0-9])',
                re.IGNORECASE))
        return patterns

    def find(self, text: str) -> List[CodeMatch]:
        """Codes of a text, in order of appearance, without overlaps.

        :param text: question or passage
        :return: the codes with their position and key
        """
        found: List[CodeMatch] = []
        for pattern in self._patterns:
            for m in pattern.finditer(text or ''):
                key = (re.sub(r'\s+', '', m.group('prefix')) + re.sub(r'\D', '', m.group('digits'))
                       + (m.group('tail') or '')).upper()
                found.append(CodeMatch(m.start(), m.end(), key))
        found.sort(key=lambda c: (c.start, -c.end))
        kept: List[CodeMatch] = []
        for code in found:
            if not kept or code.start >= kept[-1].end:
                kept.append(code)
        return kept

    def normalize(self, text: str) -> str:
        """The text with each code replaced by its key.

        :param text: question or passage
        :return: the text with ``ST-1027`` written ``ST1027``
        """
        out, last = [], 0
        for code in self.find(text):
            out.append(text[last:code.start])
            out.append(code.key)
            last = code.end
        out.append(text[last:])
        return ''.join(out)

    def annotate(self, text: str) -> str:
        """The text with the key added after each code written differently (``ST-1027 [ST1027]``).

        Idempotent: a code already followed by its key is left as it is.

        :param text: passage
        :return: the annotated passage
        """
        out, last = [], 0
        for code in self.find(text):
            out.append(text[last:code.end])
            last = code.end
            written = text[code.start:code.end]
            if written.upper() != code.key and not text.startswith(f' [{code.key}]', code.end):
                out.append(f' [{code.key}]')
        out.append(text[last:])
        return ''.join(out)
