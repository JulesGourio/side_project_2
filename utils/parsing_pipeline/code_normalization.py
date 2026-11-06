"""code_normalization.py — one spelling for the codes of the passages and of the search queries.

A code is written several ways in the documents and in the questions (``SF-1027``, ``SF 1027``, ``sf-1027``,
``SF‑1027_BG``, ``MI - 1365``). The keyword side of the hybrid search only matches identical tokens, so every code found
keeps its spelling and gets its canonical form appended in brackets: ``SF-1027`` -> ``SF-1027 [SF1027]``.

The codes are described by ``CODE_PATTERNS``: one named pattern per kind of code (catalog REF formats, standards,
aircraft, regulations...), each with the examples it must find and the texts it must leave alone. The canonical form
is the code without separators, upper-cased, without diacritics and without its language or variant suffix.

The same rules run on both sides, or the brackets never meet: the parsing pipeline writes the passages'
``chunk_preprocessed`` column with ``normalize_codes`` (this module is shipped to the executors, see
``config.WORKER_MODULES``), and the app rewrites its search queries with ``normalize_query`` (imported as
``utils.parsing_pipeline.code_normalization``). Standard library only, for that reason.

``cited_codes`` lists the codes a passage cites (column ``cited_codes``), from which the parsing pipeline builds the
table of the documents that cite each code (``doc_references``, read by the chat for "which documents cite X?").

``CODE_FAMILIES`` is generated from the catalog REFs (``python code_normalization.py <file with one REF per line>``
prints the block). Any change to this module changes the passages: bump ``NORMALIZATION_VERSION`` and run the parsing job with
``run_mode=reprocess``, which rewrites ``chunk_preprocessed`` and ``cited_codes`` from ``chunk_text`` and syncs the indexes.
"""

import re
import sys
import unicodedata
from dataclasses import dataclass
from typing import Dict, Iterable, List, NamedTuple, Pattern, Tuple

# Version of the rules written into the passages: passages normalised by another version need a reprocess.
NORMALIZATION_VERSION = 13


# --- Model ---

class CodeFamily(NamedTuple):
    """Shape of the codes of one prefix, as the catalog REFs write them.

    ``digits``: fewest and most digits of the first number group; ``groups``: fewest and most number groups after the
    prefix (``DLV-33-3010`` has 2); ``group_digits``: fewest and most digits of the next groups (``(0, 0)`` when there
    are none); ``spaced``: a space may separate two number groups (``IAQ 10 01 01``).
    """

    digits: Tuple[int, int]
    groups: Tuple[int, int]
    group_digits: Tuple[int, int]
    spaced: bool


@dataclass(frozen=True)
class CodePattern:
    """One kind of code.

    ``regex`` is compiled with ``re.VERBOSE``; its group ``code`` is the code, what follows it in the match (a language
    suffix) is kept with the code in the text but left out of the canonical form. ``examples`` pairs a text with the
    canonical forms the module finds in it; ``counter_examples`` are texts in which this pattern finds nothing.
    """

    pattern_id: str
    name: str
    regex: str
    examples: Tuple[Tuple[str, str], ...]
    counter_examples: Tuple[str, ...] = ()
    lowercase_in_query: bool = False  # a question may write it in lower case (``sf-1027``)
    min_length: int = 0  # fewest characters of the canonical form


class CodeMatch(NamedTuple):
    """A code of a text: span (language suffix included), canonical form, id of the pattern that found it, and the
    other canonical forms the same span stands for (``DLV-31-9020-10`` -> ``DLV319020`` and ``DLV31902010``)."""

    start: int
    end: int
    canonical: str
    pattern_id: str
    variants: Tuple[str, ...] = ()
    variants_end: int = 0  # end of the longest reading among the variants; 0 when none goes past ``end``


# --- Building blocks of the patterns ---

_DASHES = "\u2010\u2011\u2012\u2013\u2014\u2015\u2212"
_SEP = rf"[-_./{_DASHES}]"
_SPACE = "[ \u00a0]"
# Between a prefix and its number: one space, or one or two separators with up to three spaces around them
# (MI - 1365, SF – 1027, 19 --058).
# A line break counts as a space: tables and wrapped lines write MI, -, 1311 on three lines.
_BLANK = "[ \u00a0\n]"
_GAP = rf"(?:{_BLANK}{{0,3}}{_SEP}{{1,2}}{_BLANK}{{0,3}}|{_BLANK}{{1,3}})?"
_START = r"(?<![^\W_])"
# A code never ends inside a number: neither before a digit nor before a decimal part (12.5, 0,55).
_END = r"(?!\d)(?![.,]\d)"
# Number groups after a code's first number (TR22-137-000, ST-60_048128, Q99.0017): a dot starts a group only before 3
# digits or more, or a version (AP2190.7) and a decimal (H35.25) would join the code; a slash lists other codes
# (PRLAT506/549).
_NUMBER_GROUPS = r"(?:(?:[-_]\d{1,6}|\.\d{3,6})[A-Z]?){0,3}"
# A date written with numbers (29.01.2021, 02/10/00): never the number of a code.
_DATE = r"(?:0?[1-9]|[12]\d|3[01])[-./](?:0?[1-9]|1[0-2])[-./]\d{2,4}(?!\d)"
_LANGUAGES = ("FR", "EN", "GB", "MX", "BG", "CZ", "BR", "ES", "ESP")
# Language suffixes after a code: glued by a separator (SF-1027_BG, MI-1413_Fr, INE028_FR_EN), or after a space when
# no number follows (FORM-566 MX, but not EN 9100).
_LANGUAGE = (rf"(?:{_SEP}(?i:{'|'.join(_LANGUAGES)})(?![^\W_])"
             rf"|{_SPACE}(?:{'|'.join(_LANGUAGES)})(?![^\W_])(?!{_SPACE}?\d))*")
_NON_ALNUM_RE = re.compile(r"[^0-9A-Za-z]")
_SOURCE_LINE_RE = re.compile(r"^\[Source: [^\n]*\]\s*")
_BRACKET_RE = re.compile(r" \[([A-Z0-9]+)\]")
_CHAIN = "-_/." + _DASHES
# Shortest code accepted inside a longer chain of separated parts: shorter ones (A30 in 145.A.30, A01 in
# PTD-TEST-6-A-01) are pieces of another identifier.
_MIN_LENGTH_IN_CHAIN = 5


# --- Code families (generated) ---
# Generated by families_from_refs from the catalog REFs: regenerate rather than edit by hand. Prefixes with a pattern
# of their own (IF) are left out.
CODE_FAMILIES: Dict[str, CodeFamily] = {
    'BEP': CodeFamily(digits=(3, 4), groups=(1, 1), group_digits=(0, 0), spaced=False),
    'CGO': CodeFamily(digits=(2, 4), groups=(2, 3), group_digits=(2, 4), spaced=False),
    'DAP': CodeFamily(digits=(2, 2), groups=(2, 2), group_digits=(4, 4), spaced=False),
    'DGL': CodeFamily(digits=(4, 4), groups=(1, 1), group_digits=(0, 0), spaced=False),
    'DLV': CodeFamily(digits=(2, 4), groups=(1, 2), group_digits=(4, 4), spaced=False),
    'DTAS': CodeFamily(digits=(2, 2), groups=(2, 2), group_digits=(3, 3), spaced=False),
    'FDAQL': CodeFamily(digits=(4, 4), groups=(1, 2), group_digits=(1, 1), spaced=False),
    'FORM': CodeFamily(digits=(3, 3), groups=(1, 2), group_digits=(1, 2), spaced=False),
    'GO': CodeFamily(digits=(4, 4), groups=(1, 1), group_digits=(0, 0), spaced=False),
    'IAQ': CodeFamily(digits=(2, 2), groups=(3, 3), group_digits=(2, 2), spaced=True),
    'IC': CodeFamily(digits=(2, 3), groups=(1, 1), group_digits=(0, 0), spaced=False),
    'INAQ': CodeFamily(digits=(3, 3), groups=(1, 2), group_digits=(1, 1), spaced=False),
    'INE': CodeFamily(digits=(3, 3), groups=(1, 1), group_digits=(0, 0), spaced=False),
    'IP': CodeFamily(digits=(3, 3), groups=(1, 2), group_digits=(1, 3), spaced=False),
    'IQ': CodeFamily(digits=(2, 6), groups=(1, 3), group_digits=(1, 5), spaced=False),
    'LAT': CodeFamily(digits=(3, 3), groups=(4, 4), group_digits=(2, 3), spaced=False),
    'LIS': CodeFamily(digits=(3, 3), groups=(1, 1), group_digits=(0, 0), spaced=False),
    'MI': CodeFamily(digits=(4, 5), groups=(1, 1), group_digits=(0, 0), spaced=False),
    'MIT': CodeFamily(digits=(4, 4), groups=(1, 1), group_digits=(0, 0), spaced=False),
    'MM': CodeFamily(digits=(4, 5), groups=(1, 1), group_digits=(0, 0), spaced=False),
    'MO': CodeFamily(digits=(4, 4), groups=(1, 1), group_digits=(0, 0), spaced=False),
    'MOP': CodeFamily(digits=(3, 3), groups=(1, 1), group_digits=(0, 0), spaced=False),
    'MR': CodeFamily(digits=(4, 4), groups=(1, 1), group_digits=(0, 0), spaced=False),
    'NF': CodeFamily(digits=(3, 5), groups=(1, 1), group_digits=(0, 0), spaced=False),
    'NM': CodeFamily(digits=(3, 3), groups=(1, 1), group_digits=(0, 0), spaced=False),
    'NS': CodeFamily(digits=(4, 5), groups=(1, 2), group_digits=(1, 1), spaced=False),
    'PAQ': CodeFamily(digits=(2, 3), groups=(1, 2), group_digits=(2, 2), spaced=True),
    'PLQ': CodeFamily(digits=(3, 3), groups=(1, 1), group_digits=(0, 0), spaced=False),
    'PRLAT': CodeFamily(digits=(3, 3), groups=(1, 2), group_digits=(1, 1), spaced=False),
    'PRO': CodeFamily(digits=(3, 3), groups=(1, 1), group_digits=(0, 0), spaced=False),
    'QIS': CodeFamily(digits=(2, 2), groups=(2, 2), group_digits=(3, 3), spaced=False),
    'QM': CodeFamily(digits=(4, 4), groups=(1, 1), group_digits=(0, 0), spaced=False),
    'QP': CodeFamily(digits=(4, 6), groups=(1, 1), group_digits=(0, 0), spaced=False),
    'QR': CodeFamily(digits=(4, 4), groups=(1, 1), group_digits=(0, 0), spaced=False),
    'QTP': CodeFamily(digits=(2, 3), groups=(1, 2), group_digits=(3, 3), spaced=False),
    'QTR': CodeFamily(digits=(2, 8), groups=(1, 2), group_digits=(2, 4), spaced=False),
    'RAQ': CodeFamily(digits=(2, 2), groups=(3, 3), group_digits=(2, 2), spaced=True),
    'RLCZ': CodeFamily(digits=(1, 2), groups=(2, 2), group_digits=(2, 4), spaced=False),
    'RLLV': CodeFamily(digits=(2, 2), groups=(2, 2), group_digits=(2, 2), spaced=False),
    'SF': CodeFamily(digits=(4, 5), groups=(1, 2), group_digits=(1, 1), spaced=False),
    'SSPRO': CodeFamily(digits=(3, 3), groups=(1, 1), group_digits=(0, 0), spaced=False),
    'TDA': CodeFamily(digits=(1, 1), groups=(3, 3), group_digits=(2, 4), spaced=False),
    'TDAE': CodeFamily(digits=(2, 2), groups=(2, 2), group_digits=(3, 3), spaced=False),
    'TDAJ': CodeFamily(digits=(2, 5), groups=(1, 2), group_digits=(3, 3), spaced=False),
    'TDAL': CodeFamily(digits=(2, 2), groups=(2, 2), group_digits=(3, 3), spaced=True),
    'TDAM': CodeFamily(digits=(2, 2), groups=(2, 2), group_digits=(3, 3), spaced=False),
    'TDAN': CodeFamily(digits=(2, 2), groups=(2, 2), group_digits=(3, 3), spaced=False),
    'TDRA': CodeFamily(digits=(2, 2), groups=(2, 2), group_digits=(3, 3), spaced=False),
    'UADE': CodeFamily(digits=(2, 2), groups=(2, 2), group_digits=(3, 3), spaced=False),
    'UDA': CodeFamily(digits=(1, 2), groups=(2, 3), group_digits=(2, 3), spaced=False),
    'UDAE': CodeFamily(digits=(2, 2), groups=(2, 2), group_digits=(3, 3), spaced=False),
    'UDB': CodeFamily(digits=(1, 1), groups=(3, 3), group_digits=(2, 4), spaced=False),
    'UDC': CodeFamily(digits=(2, 2), groups=(2, 2), group_digits=(3, 3), spaced=False),
    'UDE': CodeFamily(digits=(1, 2), groups=(2, 3), group_digits=(2, 3), spaced=False),
    'UDH': CodeFamily(digits=(1, 1), groups=(3, 3), group_digits=(2, 3), spaced=False),
    'DAM': CodeFamily(digits=(2, 2), groups=(2, 2), group_digits=(4, 4), spaced=False),
    'PS': CodeFamily(digits=(3, 3), groups=(1, 1), group_digits=(0, 0), spaced=False),
    'UADAE': CodeFamily(digits=(2, 2), groups=(2, 2), group_digits=(3, 3), spaced=False),
    'WP': CodeFamily(digits=(5, 5), groups=(1, 1), group_digits=(0, 0), spaced=False),
    'VA': CodeFamily(digits=(3, 7), groups=(1, 2), group_digits=(1, 1), spaced=False),
    'VDDR': CodeFamily(digits=(2, 2), groups=(2, 2), group_digits=(3, 3), spaced=False),
    'VDDX': CodeFamily(digits=(2, 2), groups=(2, 2), group_digits=(3, 3), spaced=False),
}


# --- Lexicon ---

# Prefixes whose documents write numbers that no family shape describes (PAQ 0921, ITC 000447, LAT 215.300.0090) (ITC 000447, WP 11116).
KNOWN_PREFIXES = ("ITC", "WP", "PS", "DAM", "UADAE", "DGL", "LAT", "PAQ", "IAQ", "AQ", "QUAL")

# Prefixes never treated as codes, even when a catalog REF uses them: words and language codes that a number follows
# in ordinary text ("GB 128", "in 300").
STOP_PREFIXES = frozenset({"GB", "IN", "EN", "FR", "ES", "IS", "OF", "ON", "NO", "AT", "TO", "DE", "LE", "LA", "ET"})

# Prefixes that are also words in lower case: in a question, written in lower case and followed by a space, they are
# words ("if 2 parts", "go 2024", "le 23").
QUERY_STOP_PREFIXES = frozenset({"IF", "GO", "MO", "PRO", "VA", "LA", "LE", "DE", "IN", "IS", "IT", "NO", "ON", "AS",
                                 "DO", "ED"})

# Canonical forms that are words written letter by letter (D.E.S, F.I.N) or fillers (E.E.E): never codes.
STOP_CODES = frozenset({"DES", "FIN", "NON", "OUI", "LES", "ETC", "THE", "AND", "FOR", "PAR", "SUR", "UNE", "AAA",
                        "EEE", "III", "XXX",
                        # labels the pipeline writes before an image description, and signature boilerplate of forms
                        "TABLE", "FLOWCHART", "DIAGRAM", "DATACHART", "SCHEMATIC", "PHOTOTECH", "TEXTDOC",
                        "NAMEDATE", "NODATUM", "JMENODATUM"})

# Capital words that a number follows in running text (PAGE 0012, TEL 05.61.72.40): never the prefix of a code.
SPACED_STOP_WORDS = ("PAGE", "LINE", "FIG", "FIGURE", "TABLE", "BOX", "ORDER", "CEDEX", "TEL", "FAX", "PHONE", "NO",
                     "ART", "TOME", "STEP", "ETAPE", "ITEM", "LOT", "QTY", "QTE", "REV", "ISSUE", "ED", "VOL", "CH",
                     "SECTION", "ANNEX", "ANNEXE", "PIECE", "BP", "CS", "ZI", "ZA", "RN", "RD", "TOTAL")

# Words that a number follows when it identifies a thing: aircraft serial, SAP article, part, serial, work and purchase
# orders (MSN 9237, SAP 1003181, WO 6747502).
SPACED_ID_PREFIXES = ("MSN", "SAP", "PN", "SN", "WO", "PO", "OF")
# Products named by a brand and a number (Turco 4460, Ardrox 970), in any case.
PRODUCT_PREFIXES = ("TURCO", "ARDROX", "SEMKIT", "EPOCH", "UNIMAG", "ALODINE", "SOCOSURF", "BONDERITE")

# Month abbreviations: MAR/2024, OCT2023 are dates, not codes.
MONTHS = ("JAN", "FEB", "FEV", "MAR", "APR", "AVR", "MAY", "MAI", "JUN", "JUIN", "JUL", "JUIL", "AUG", "AOU", "SEP",
          "SEPT", "OCT", "NOV", "DEC")

# Family prefixes that the documents also write in parts (PR/LAT/105, IN/AQ/559, FD_AQL_2193, IN/E/035).
SPLIT_PREFIXES: Dict[str, Tuple[str, ...]] = {
    "PRLAT": ("PR", "LAT"), "INAQ": ("IN", "AQ"), "FDAQL": ("FD", "AQL"), "INE": ("IN", "E")}

# Site codes of the catalog REFs (Q0697MI, E0021F, P00036SF): site letter, 4 or 5 digits, document type.
SITE_LETTERS = "EFHJLNOPQRSTUVW"
SITE_TYPES = ("MIT", "GO", "MI", "MM", "MO", "MQ", "MR", "NF", "NS", "QP", "QR", "SF", "VA", "F")
# Types of the lettres de branchement (LdB IT-243, LdB P-155, LdB - PM-001).
LDB_TYPES = ("CI", "IT", "MB", "MM", "NF", "PM", "PS", "F", "P")
# Programme phases (PRO-R80, PROLAT_R40_FR, OBS_A8P).
PHASE_PREFIXES = ("PROLAT", "PRO", "OBS")
# Prefixes of the segmented internal codes (IN_MRPC003, IQ-BEL-QA-018, LMX-NF-008, EVD-EMB-0006).
SEGMENTED_PREFIXES = ("LMX", "EVD", "IN", "IQ")

# Prefixes of the aerospace and industry standards (NAS1789, ISO 9001, BAC 5578).
STANDARD_PREFIXES = ("PREN", "prEN", "ISO", "NAS", "NSA", "ABS", "ASNA", "BACB", "BACS", "BAC", "BSS", "BAPS", "BMS",
                     "HJPS", "AIPI", "AIPS", "AITM", "IPDA", "ARP", "AMS", "ASTM", "DIN", "SAE", "IPC", "IEC", "HST",
                     "CCR", "JISQ")
# Number of a standard: 3 digits or more, or 2 digits that a dashed group of 3 follows (NE 40-012).
_STANDARD_NUMBER = r"(?:\d{3,6}|\d{2}(?=[-.]\d{3}))"
# Standard prefixes that are also words, or that a year follows in running text: a space before the number is
# allowed only when the number is not a year.
STANDARD_WORD_PREFIXES = ("EN", "AS", "DO", "NE", "ED", "MS", "LN", "HL")
# Airbus models, a closed list: A followed by any number is not an aircraft (a 12.5).
AIRBUS_MODELS = ("220", "300", "310", "318", "319", "320", "321", "330", "340", "350", "380")
# SAP transaction prefixes written with a hyphen (MM-02); the glued spelling is already canonical.
SAP_PREFIXES = ("SE", "SM", "SU", "SP", "ST", "SQ", "ME", "MM", "MB", "VA", "VL", "VF", "CO", "QA", "QE", "QM", "QS",
                "IW", "IE", "IL", "CJ", "CN", "KO", "KS", "MD", "XK", "ZQ", "ZM")
# EASA parts (Part 21, Part-145): never Part 1 or Partie 3.
EASA_PARTS = ("21", "145", "147", "66", "ML", "M", "CAMO")


def _one_of(words: Iterable[str]) -> str:
    """Regex alternation of words, the longest first so that none hides a longer one.

    :param words: literal words
    :return: regex source, without group
    """
    return "|".join(re.escape(w) for w in sorted(words, key=len, reverse=True))


def _prefix_alternatives(prefixes: List[str]) -> str:
    """The prefixes as written in capitals, plus their other spellings: ``Mi-1203``, ``Qp-1304`` (short prefixes: first
    letter in capital and a separator right after, so that ``mi 12`` or ``sf-1027`` in lower case stay words) and
    ``Form 330``, ``form-346`` (prefixes of 4 letters or more, in any case).

    :param prefixes: prefix regexes of one family shape
    :return: regex alternation, without group
    """
    ordered = sorted(prefixes, key=len, reverse=True)
    alternatives = list(ordered)
    for prefix in ordered:
        if not prefix.isalpha():
            continue
        alternatives.append(f"(?i:{prefix})" if len(prefix) >= 4 else f"{prefix[0]}(?i:{prefix[1:]})(?={_SEP})")
    return "|".join(alternatives)


def _family_regex() -> str:
    """Regex of the prefix-number families: one alternative per family shape, the prefixes sharing it alternate.

    :return: regex source with the group ``code``
    """
    by_shape: Dict[CodeFamily, List[str]] = {}
    for prefix, family in CODE_FAMILIES.items():
        if prefix not in STOP_PREFIXES:
            by_shape.setdefault(family, []).append(
                f"{_SEP}?".join(re.escape(p) for p in SPLIT_PREFIXES.get(prefix, (prefix,))))
    alternatives = []
    for family, prefixes in by_shape.items():
        between = f"(?:{_SPACE}?{_SEP}{_SPACE}?|{_SPACE})" if family.spaced else f"{_SPACE}?{_SEP}{_SPACE}?"
        letter = r"(?:[A-Z](?![A-Za-z]))?"
        more = ""
        if family.groups[1] > 1:
            group = rf"{between}\d{{{family.group_digits[0]},{family.group_digits[1]}}}(?!\d){letter}"
            more = rf"(?:{group}){{{family.groups[0] - 1},{family.groups[1] - 1}}}"
        alternatives.append(
            rf"(?:{_prefix_alternatives(prefixes)}){_GAP}"
            rf"\d{{{family.digits[0]},{family.digits[1]}}}(?!\d){letter}{more}")
    return rf"{_START}(?P<code>{'|'.join(alternatives)}){_END}{_LANGUAGE}"


# --- Patterns ---
# Order matters for two codes of the same span: the first pattern wins.

CODE_PATTERNS: Tuple[CodePattern, ...] = (
    CodePattern(
        "REF-01", "Prefix-number REF (generated families)", _family_regex(),
        examples=(("SF-1027", "SF1027"), ("SF 1027", "SF1027"), ("SF‑1027_BG", "SF1027"), ("MI - 1365", "MI1365"),
                  ("DLV-33-3010", "DLV333010"), ("IAQ 10 01 01", "IAQ100101"), ("QIS-46A-352 A", "QIS46A352"),
                  ("IP-029-005", "IP029005"), ("PR/LAT/506", "PRLAT506"), ("FD_AQL_2193", "FDAQL2193"),
                  ("IN/E/035", "INE035"), ("TDA8 - 00/027", "TDA800027"), ("ŘLCZ-07/18", "RLCZ0718"),
                  ("FORM-566 MX", "FORM566"), ("MI-13991Table", "MI13991")),
        counter_examples=("XSF-1027", "SF-1027A1", "MI-140470", "GB 128", "in 300"),
        lowercase_in_query=True),
    CodePattern(
        "REF-03", "Site code with document type",
        rf"{_START}(?P<code>[{SITE_LETTERS}]\d{{4,5}}{_SEP}?(?:{_one_of(SITE_TYPES)}))(?![^\W_]){_LANGUAGE}",
        examples=(("Q0697MI_FR", "Q0697MI"), ("Q0256MI", "Q0256MI"), ("E0021F", "E0021F"), ("P0234-MI_GB", "P0234MI"),
                  ("F0026MIT", "F0026MIT"), ("P00036SF", "P00036SF"), ("Q0054QR-E", "Q0054QR")),
        counter_examples=("Q0256", "A0256MI", "Q0697MIX"),
        lowercase_in_query=True),
    CodePattern(
        "REF-04", "Lettre de branchement",
        rf"{_START}(?P<code>L[dD][bB]{_GAP}(?:{_one_of(LDB_TYPES)}){_GAP}\d{{3}}){_END}{_LANGUAGE}",
        examples=(("LdB IT-243", "LDBIT243"), ("LDB IT 243", "LDBIT243"), ("LdB-P099", "LDBP099"),
                  ("LdB - PM-001", "LDBPM001")),
        counter_examples=("IT-243", "P-155"),
        lowercase_in_query=True),
    CodePattern(
        "REF-07", "IF instruction",
        rf"{_START}(?P<code>IF{_GAP}2{_SEP}?\d{{2}}{_SEP}?\d{{2}}|IF{_SPACE}2(?:{_SPACE}\d{{2}}){{2}})"
        rf"{_END}{_LANGUAGE}",
        examples=(("IF20322_BR", "IF20322"), ("IF 20316", "IF20316"), ("IF2.03.08_CZ", "IF20308"),
                  ("IF 2.03.08", "IF20308"), ("IF20308_Fr - Annexe 2", "IF20308")),
        counter_examples=("IF 02/08/2001", "IF 2.02", "IF 2", "IF 2024"),
        lowercase_in_query=True),
    CodePattern(
        "REF-10", "Programme phase",
        rf"{_START}(?P<code>(?:{_one_of(PHASE_PREFIXES)})(?:{_SEP}|{_SPACE})[A-Z]\d[0-9A-Z]?|OBS_[A-Z]{{3}})(?![^\W_])"
        rf"{_LANGUAGE}",
        examples=(("PRO-R80", "PROR80"), ("PROLAT_R40_FR", "PROLATR40"), ("OBS_A8P", "OBSA8P"), ("PRO R80", "PROR80"),
                  ("PRO-R40-BUS", "PROR40"), ("OBS_BEP", "OBSBEP")),
        counter_examples=("PRO-R", "PROD-R80"),
        lowercase_in_query=True),
    CodePattern(
        "REF-12", "Numeric REF",
        rf"{_START}(?P<code>19{_SPACE}?-{{1,2}}\d{{3}}|\d{{3}}-\d{{3}}-\d{{2}}-\d{{2,3}}){_END}",
        examples=(("19 --058", "19058"), ("19-001", "19001"), ("999-751-18-03", "9997511803"),
                  ("115-000-00-108", "11500000108")),
        counter_examples=("19058", "28-10-24", "2024-10-28", "19-1")),
    CodePattern(
        "REF-13", "Programme site code",
        rf"{_START}(?P<code>[A-Z]\d[A-Z](?:{_SEP}[A-Z]{{3}})?{_SEP}\d{{3,4}}(?:{_SEP}[A-Z]{{3}}(?![^\W_]))?)"
        rf"{_END}(?!{_SEP}\d)",
        examples=(("E2P_EOS_0011", "E2PEOS0011"), ("A2E_026", "A2E026"), ("H8P-0081", "H8P0081"),
                  ("A1O_011_CMP", "A1O011CMP")),
        counter_examples=("A2E", "A2E_02", "D2D-170-69210-419"),
        lowercase_in_query=True),
    CodePattern(
        "REF-14", "Segmented internal code",
        rf"{_START}(?P<code>(?:{_one_of(SEGMENTED_PREFIXES)}){_SEP}(?:[A-Z]{{2,4}}{_SEP}){{0,2}}[A-Z]{{1,8}}{_SEP}?"
        rf"\d{{3,5}}(?:[-_]\d{{2,5}}(?![A-Za-z0-9]))?){_END}{_LANGUAGE}",
        examples=(("IN_MRPC003_EN", "INMRPC003"), ("IN_APO_0001", "INAPO0001"), ("IQ-BEL-QA-018", "IQBELQA018"),
                  ("IQ_BEL-ME-004", "IQBELME004"), ("LMX-NF-008", "LMXNF008"), ("LMX-MI-14229", "LMXMI14229"),
                  ("EVD-EMB-0006", "EVDEMB0006"), ("IN-PLANNING-002", "INPLANNING002"), ("IN_P_0015_FR", "INP0015"),
                  ("IQ_AUDIT_2016_137", "IQAUDIT2016137")),
        counter_examples=("IN-SITU", "LMX-NF")),
    CodePattern(
        "REF-15", "L site code",
        rf"{_START}(?P<code>L_(?!X+_)(?:\d{{4}}|[0-9A-Z]{{3,4}})_(?!X+(?![^\W_]))[A-Z]{{3}})(?![^\W_])",
        examples=(("L_1263_PGC", "L1263PGC"), ("L_0L5_SPI", "L0L5SPI"), ("L_LVCS_PGC", "LLVCSPGC")),
        counter_examples=("L_12_PGC", "L_XXXX_YYY", "L_1234_XXX")),
    CodePattern(
        "REF-16", "Aircraft programme document",
        rf"{_START}(?P<code>\d{{3}}F-[A-Z]{{4}}-[A-Z]{{2,3}})(?![^\W_])",
        examples=(("777F-MDCD-QP", "777FMDCDQP"),),
        counter_examples=("777F",)),
    CodePattern(
        "REF-17", "Register",
        rf"{_START}(?P<code>R\d{{2}}-\d{{2}}-?\d{{3}}){_END}(?![^\W_])",
        examples=(("R02-01-008", "R0201008"), ("R03-10-002", "R0310002"), ("R03-12001", "R0312001")),
        counter_examples=("R03-12", "R3-10-002")),
    CodePattern(
        "STD-01", "Standard",
        rf"""{_START}(?P<code>(?P<base>(?:
            (?:{_one_of(STANDARD_PREFIXES)})(?:[-_/.]|[ ]{{1,2}})?{_STANDARD_NUMBER}
          | (?:{_one_of(STANDARD_WORD_PREFIXES)})(?:[-_/.]?|[ ](?!(?:19|20)\d\d(?!\d))){_STANDARD_NUMBER}
          | (?:AIPI|AIPS)[-_ ]?\d{{2}}[-. ]?\d{{2}}[-. ]?\d{{3}}
          | (?:AITM|BMS)[-_ ]?\d{{1,2}}[-.]\d{{1,4}}
          | MIL[-_ ]?(?:STD|HDBK|PRF|DTL|SPEC|[A-Z])[-_ ]?\d{{3,5}}
          | ASTM[-_ ]?[A-G]\d{{2,4}}
        ))(?:[A-Z]?(?:[-.]\d{{1,4}}[A-Z]?)?|[A-Z]{{1,3}}\d{{1,4}}[A-Z]{{0,2}}(?P<tail>-\d{{1,4}}[A-Z]?)?|[A-Z]{{2,3}}))
        (?![A-Za-z0-9]){_END}""",
        examples=(("EN 9100", "EN9100"), ("NAS 1789", "NAS1789"), ("ISO 9001:2015", "ISO9001"),
                  ("prEN 6122", "PREN6122"), ("BAC 5578", "BAC5578"), ("NE 40-012", "NE40012"),
                  ("MIL-STD-810G", "MILSTD810G"), ("MIL-A-8625", "MILA8625"), ("AITM6-2001", "AITM62001"),
                  ("BMS 8-276", "BMS8276"), ("AIPI 05-05-001", "AIPI0505001"), ("AIPS01.02.017", "AIPS0102017"),
                  ("ASTM-B117", "ASTMB117"), ("JISQ 9100", "JISQ9100"),
                  ("ABS2306A0410", "ABS2306A0410"), ("NSA936507PC", "NSA936507PC"),
                  ("MS20426AD3-5", "MS20426AD35")),
        counter_examples=("EN 2024", "EN 12", "AS 2019", "NAS 12")),
    CodePattern(
        "STD-02", "Customer specification",
        rf"""{_START}(?P<code>
            DGQT[ ]?\d\.?\d\.?\d\.?\d{{4}} | ADET[ ]?\d{{4}} | MEP[- ]?\d{{2}}-?\d{{3}} | BPS[ ]?\d{{4}}
          | AC[- ]?71\d{{2}} | D6-\d{{5}}(?:-\d)?
        )(?![A-Za-z0-9]){_END}""",
        examples=(("DGQT 0.4.7.0070", "DGQT0470070"), ("ADET 0078", "ADET0078"), ("MEP 10-069", "MEP10069"),
                  ("BPS 4138", "BPS4138"), ("AC7114", "AC7114"), ("D6-35071-1", "D6350711")),
        counter_examples=("AC 7", "MEP 1", "D6-123")),
    CodePattern(
        "ATA-01", "ATA chapter",
        rf"{_START}(?P<code>ATA[ -]?(?:\d{{2}}|100|2200)){_END}(?![A-Za-z0-9])",
        examples=(("ATA 52", "ATA52"), ("ATA-25", "ATA25"), ("ATA 100", "ATA100")),
        counter_examples=("ATA 2024", "ATA")),
    CodePattern(
        "ACF-01", "Aircraft",
        rf"""{_START}(?P<code>
            A[ -]?(?:{_one_of(AIRBUS_MODELS)})(?:[-/]?(?:NEO|neo|XLR|XWB|\d{{3,4}}))? | A400M
          | (?:B|BOEING|Boeing)[ -]?7[0-9]7(?:-(?:\d{{1,3}}|MAX\d{{0,2}}))?
          | F(?:7X|8X|6X|10X|900|2000)(?:EX|LX|DX)? | ATR[ -]?(?:42|72)(?:-\d{{2,3}})?
          | ERJ[ -]?\d{{3}} | E(?:170|175|190|195)(?:-?E2)? | CRJ[ -]?\d{{3,4}}
        )(?![A-Za-z0-9]){_END}""",
        examples=(("A350-900", "A350900"), ("A 320", "A320"), ("ATR 72-600", "ATR72600"),
                  ("Boeing 787-9", "BOEING7879"), ("F7X", "F7X")),
        counter_examples=("a 12.5", "A 12", "A 3200")),
    CodePattern(
        "REG-01", "Aviation regulation",
        rf"""{_START}(?P<code>
            (?:PART|Part|PARTIE|Partie)[-_ .]?(?:{_one_of(EASA_PARTS)})(?:-?[GJ](?![A-Za-z0-9]))?
          | CCAR[-_ ]?(?:21|145|147|66)
          | CS[-_ ]?(?:2[2-9]|3[46]|E|P|APU|ETSO|AWO|VLA|LSA)(?:[-.]\d{{1,4}})?
          | (?:AMC|GM)\d?[ -]?(?:21|145|147|66|ML|M|CS|20|25)(?:[-.]\d{{1,4}})?
          | 21[-.]?[GJ]
        )(?![A-Za-z0-9])(?!\.[A-Z](?![A-Za-z])){_END}""",
        examples=(("PART 21G", "PART21G"), ("Part-145", "PART145"), ("CS-25", "CS25"), ("AMC 20-29", "AMC2029"),
                  ("CCAR-145", "CCAR145")),
        counter_examples=("Partie 3", "PART 1", "CS 1", "AMC1 145.A.30")),
    CodePattern(
        "REG-02", "EASA paragraph",
        rf"{_START}(?P<code>(?:21|145|147|66)\.?A\.?\d{{1,3}}[A-Z]?)(?![A-Za-z0-9]){_END}",
        examples=(("145.A.30", "145A30"), ("21.A.91", "21A91"), ("21A.239", "21A239"), ("21.A.3B", "21A3B")),
        counter_examples=("145.30", "21.B.91")),
    CodePattern(
        "MAT-01", "Material",
        rf"{_START}(?P<code>[2-7]\d{{3}}[- ]?T\d{{1,4}}|1[57][- .]?[45][ ]?PH|TA6V|Ti[- ]?6Al[- ]?4V)"
        rf"(?![A-Za-z0-9]){_END}",
        examples=(("2024-T3", "2024T3"), ("7075 T6", "7075T6"), ("15-5PH", "155PH"), ("Ti-6Al-4V", "TI6AL4V")),
        counter_examples=("2024 T", "8024-T3")),
    CodePattern(
        "ACR-02", "Acronym written with dots",
        r"(?<![A-Za-z0-9.])(?P<code>[A-Z](?:\.[A-Z]){2,}\.?)(?![A-Za-z0-9])",
        examples=(("E.A.S.A", "EASA"), ("N.D.T", "NDT"), ("M.O.E.", "MOE")),
        counter_examples=("D.E.S", "E.E.E", "A.B")),
    CodePattern(
        "SAP-01", "SAP transaction",
        rf"{_START}(?P<code>(?:{_one_of(SAP_PREFIXES)})-\d{{2}}[A-Z]?){_END}(?![A-Za-z0-9])",
        examples=(("MM-02", "MM02"), ("QM-01", "QM01")),
        counter_examples=("MM02", "MM-2")),
    CodePattern(
        "PN-01", "Part number written in groups",
        rf"{_START}(?P<code>[A-Z]\d{{3,}}(?:[ -]\d{{3,}})+){_END}(?![A-Za-z0-9])",
        examples=(("L523 77000", "L52377000"),),
        counter_examples=("A123 456",),
        min_length=8),
    CodePattern(
        "TRM-01", "Quality method",
        rf"{_START}(?P<code>[85]-[DS]|[23]-D)(?![A-Za-z0-9])",
        examples=(("8-D", "8D"), ("5-S", "5S"), ("3-D", "3D")),
        counter_examples=("8-DE", "4-D")),
    CodePattern(
        "REG-03", "FAA or EASA rule written with a space",
        rf"""{_START}(?P<code>
            (?:FAR|AMC|GM|CS|AC|CFR)[ ]?-?[ ]?\d{{2,3}}[.-]\d{{1,4}}[A-Z]?(?:-\d{{1,2}})?(?!\.[A-Z])(?![-.]\d)
          | FAR[ ]?\d{{2,3}} | CS[ ]?-?[ ]?2[2-9]
        )(?![A-Za-z0-9]){_END}""",
        examples=(("FAR 25.1301", "FAR251301"), ("FAR 25-571", "FAR25571"), ("FAR 121", "FAR121"),
                  ("CS -25", "CS25"),
                  ("AC 20-29", "AC2029"), ("AC 25.1529-1", "AC2515291"), ("AC 20-128A", "AC20128A")),
        counter_examples=("AC 01", "AC 1510", "CS 2000", "FAR")),
        # AMC 145.A.60 stays read as the EASA paragraph 145A60 (REG-02).
    CodePattern(
        "FRM-01", "FAA form",
        rf"{_START}(?P<code>(?i:FORM)[ ]?-?[ ]?\d{{4}}-\d{{1,2}})(?![A-Za-z0-9]){_END}",
        examples=(("FORM 8130-3", "FORM81303"), ("Form 8120-10", "FORM812010")),
        counter_examples=("Form 55", "FORM 52")),
    CodePattern(
        "STD-03", "Specification written with a space",
        rf"""{_START}(?P<code>(?:IGC|IPDA|AIMS|NFL|NF[ ]L|DGQT|BATS)[ ]?
            (?:\d{{2}}[-.]\d{{1,3}}(?:[-.]\d{{2,4}})?|\d{{4,8}}))(?![A-Za-z0-9]){_END}""",
        examples=(("IGC 04.11.100", "IGC0411100"), ("IGC 0466100", "IGC0466100"), ("IPDA 83-13", "IPDA8313"),
                  ("AIMS 04-05-001", "AIMS0405001"), ("NFL 06-160", "NFL06160"), ("NF L 15-540", "NFL15540"),
                  ("DGQT 65410", "DGQT65410"), ("DGQT 04.2.286", "DGQT042286"), ("BATS 5402", "BATS5402")),
        counter_examples=("IPDA 71", "DGQT 12", "NFL 17")),
    CodePattern(
        "SEG-01", "Letters, 2 digits, 4 digits",
        rf"(?<![A-Za-z0-9])(?P<code>(?:\d{{2}}-)?[A-Z]{{1,3}}-\d{{2}}-\d{{4}}(?:/\d)?)(?![A-Za-z0-9/]){_END}",
        examples=(("T-34-9020", "T349020"), ("T-34-9020/2", "T3490202"), ("J-42-8000", "J428000"),
                  ("ORJ-42-8213", "ORJ428213"), ("80-T-35-1101", "80T351101")),
        counter_examples=("T-3-12", "I-26-58")),
    CodePattern(
        "DOT-01", "Number written with dots",
        r"(?<![A-Za-z0-9.])(?P<code>(?:AQ|Q)\.[ ]?\d{2}(?:\.\d{2,5}){1,3}|V\d{3}\.\d{5}\.\d{3}\.\d{2})(?![A-Za-z0-9]|\.\d)",
        examples=(("AQ.04.04.05", "AQ040405"), ("AQ.16.00", "AQ1600"), ("Q.11.00.00", "Q110000"),
                  ("V533.20141.206.00", "V5332014120600"), ("Q. 13.00.13", "Q130013")),
        counter_examples=("Q.4", "AQ.1")),
    CodePattern(
        "GEN-01", "Letters and digits written as a code",
        rf"{_START}(?<!°)(?!(?:{_one_of(MONTHS)}){_SEP}?\d)(?P<code>(?P<base>[A-Z]{{1,6}}{_SEP}\d{{2,10}}[A-Z]?){_NUMBER_GROUPS}"
        rf"(?={_END})(?!{_SEP}\d)"
        rf"|(?P<base_glued>[A-Z]{{1,6}}\d{{2,10}}[A-Z]?){_NUMBER_GROUPS}(?={_END}|\.\d{{1,2}}(?![\d.]))(?!{_SEP}\d{{3}}))"
        rf"(?![^\W_.]|\.\d{{3}}|[ ]?%){_LANGUAGE}",
        examples=(("Q0014", "Q0014"), ("P50", "P50"), ("S-02", "S02"), ("P-30", "P30"),
                  ("DLV318007", "DLV318007"), ("QIS-46", "QIS46"), ("MM-093", "MM093"), ("IT-030", "IT030"),
                  ("QP-202", "QP202"), ("AP2190.7", "AP2190"), ("F925144602", "F925144602"),
                  ("Q05-006", "Q05006"), ("TR22-137-000", "TR22137000"), ("DT10-8510", "DT108510"),
                  ("H001-002-12-R", "H00100212"), ("Q99.0017", "Q990017"), ("ST-60\\_048128", "ST60048128")),
        counter_examples=("Page 31", "le 24", "A", "p. 54", "2024-10-28", "x36000", "P5", "19/MAR/2024", "H35.25",
                          "18°C/60%"),
        min_length=3),
    CodePattern(
        "UND-01", "Identifier joined by underscores",
        rf"(?<![A-Za-z0-9_])(?P<code>(?!(?:{'|'.join(_LANGUAGES)})_)[A-Z]{{1,12}}(?:_[A-Z]{{1,12}}){{0,3}}_(?:[A-Z]{{1,12}}|\d{{1,12}}|[A-Z]\d{{1,2}}))"
        rf"(?![A-Za-z0-9_])(?!{_SEP}[A-Za-z0-9])",
        examples=(("ZPP_NOEC", "ZPPNOEC"), ("S_ALR_87013611", "SALR87013611"), ("MRP_CENTER", "MRPCENTER"),
                  ("Z_PUSH_FLOW", "ZPUSHFLOW"), ("FNC_V1", "FNCV1")),
        counter_examples=("FR_EN", "GB_FR", "Mrp_center", "_A_B", "[TEXT_DOC]", "NAME_DATE", "QP_IQ22_184",
                          "EPR_B777", "AFP_AIRBUS_A400M"),
        min_length=5),
    CodePattern(
        "GEN-02", "Known prefix and a number",
        rf"{_START}(?P<code>(?:{_one_of(KNOWN_PREFIXES)})"
        rf"{_GAP}\d{{2,9}}(?:(?:{_SEP}|{_BLANK})\d{{1,6}}){{0,3}}){_END}(?![^\W_]){_LANGUAGE}",
        examples=(("PAQ 0921", "PAQ0921"), ("PAQ 0405", "PAQ0405"), ("ITC 00.04.47", "ITC000447"),
                  ("LAT 215.300.0090", "LAT2153000090"), ("DGL10-56", "DGL1056"),
                  ("IAQ080002", "IAQ080002")),
        counter_examples=("PAQ", "ITC 2"),
        lowercase_in_query=True,
        min_length=4),
    CodePattern(
        "GEN-03", "Capitals, a space and a number that is not a quantity",
        rf"""{_START}(?P<code>
            (?!(?:{_one_of(SPACED_STOP_WORDS + MONTHS)})[ ])[A-Z]{{2,5}}[ ](?!{_DATE})
              (?:0\d{{3,7}}|\d{{2,6}}(?:[-.]\d{{2,6}}){{1,3}})
          | (?:{_one_of(SPACED_ID_PREFIXES)})[ ]?(?!(?:19|20)\d\d(?!\d))\d{{3,10}}(?:[-.]\d{{1,6}}){{0,3}}
          | (?i:{_one_of(PRODUCT_PREFIXES)})[ ]\d{{2,5}}
        )(?![A-Za-z0-9]){_END}""",
        examples=(("ABD 0024", "ABD0024"), ("MSN 0583", "MSN0583"), ("PN 1179-12-300-001", "PN117912300001"),
                  ("SN 01.14.014", "SN0114014"), ("MSN 9237", "MSN9237"), ("SAP 1003181", "SAP1003181"),
                  ("Turco 4460", "TURCO4460")),
        counter_examples=("PAGE 0012", "LOHY 40", "TEL 05.61.72.40.00", "ABD 1500", "MAR 0024", "LOG 09/90",
                          "CRJ 700/900", "LA 29.01.2021", "PO 2024"),
        min_length=6),
)

# --- Matching ---

def _fold(text: str) -> str:
    """The text with each letter replaced by its letter without diacritics (``Ř`` -> ``R``), one character for one.

    :param text: any text
    :return: a text of the same length
    """
    out = []
    for c in text:
        base = unicodedata.normalize("NFKD", c)[0] if ord(c) > 127 else c
        out.append(base if base.isascii() and base.isalpha() and c.isalpha() else c)
    return "".join(out)


_MD_ESCAPE_RE = re.compile(r"\\(?=[-_*\[\]()#.!`~<>+|{}])")


def _unescape(text: str) -> Tuple[str, List[int]]:
    """The text without the Markdown escapes the parsers write (``IQ25\\_385\\_1`` -> ``IQ25_385_1``).

    :param text: passage or question
    :return: the cleaned text and, for each of its characters, the position in ``text`` (empty when unchanged)
    """
    if "\\" not in text:
        return text, []
    kept = [i for i in range(len(text)) if not (text[i] == "\\" and _MD_ESCAPE_RE.match(text, i))]
    return "".join(text[i] for i in kept), kept


def canonical(code: str) -> str:
    """Canonical spelling of a code: upper-cased, letters and digits only, diacritics dropped.

    :param code: code as written, e.g. ``SF-1027``, ``sf 1027`` or ``ŘLCZ-07/18``
    :return: e.g. ``SF1027`` or ``RLCZ0718``
    """
    return _NON_ALNUM_RE.sub("", _fold(code)).upper()


def _compile(query: bool) -> Tuple[Tuple[CodePattern, Pattern], ...]:
    """``CODE_PATTERNS`` compiled for passages or for questions.

    :param query: compile for questions (patterns with ``lowercase_in_query`` ignore the case)
    :return: ``(pattern, compiled regex)`` pairs, in the order of ``CODE_PATTERNS``
    """
    return tuple((p, re.compile(p.regex, re.VERBOSE | (re.IGNORECASE if query and p.lowercase_in_query else 0)))
                 for p in CODE_PATTERNS)


# Patterns that read any code-like token: a specific pattern starting at the same place knows the code's shape better.
_GENERIC_PATTERNS = frozenset({"GEN-01", "GEN-03"})
# Patterns that wrap a whole identifier: the codes of other patterns inside it are kept as variants
# (QAP 117-110-10-02 also stands for 1171101002, ECM_EVD_LAT_P_0001 for EVDLATP0001).
_WRAPPER_PATTERNS = frozenset({"UND-01", "GEN-03"})
# Closed lists: a longer reading starting with them is another thing (ERJ 170-190 is two aircraft, 7050 T7451 115 a
# material and a size), and a chain around them never hides them (ST_A320_90VU).
_CLOSED_PATTERNS = frozenset({"ACF-01", "MAT-01"})

_COMPILED: Dict[bool, Tuple[Tuple[CodePattern, Pattern], ...]] = {False: _compile(False), True: _compile(True)}


def _word_end(text: str, pos: int) -> int:
    """End of the run of letters and digits that starts at ``pos``.

    :param text: text
    :param pos: position in the text
    :return: index of the first character after the run
    """
    while pos < len(text) and text[pos].isalnum():
        pos += 1
    return pos


def _word_start(text: str, pos: int) -> int:
    """Start of the run of letters and digits that ends at ``pos``.

    :param text: text
    :param pos: position in the text, just after the run
    :return: index of the first character of the run
    """
    while pos > 0 and text[pos - 1].isalnum():
        pos -= 1
    return pos


def _accepted(text: str, start: int, code_end: int, code: str, query: bool, closed: bool = False) -> bool:
    """Boundary rules that the regexes leave to code: a code glued to a word, a code inside a longer chain, a word in
    lower case in a question.

    :param text: passage or question
    :param start: start of the code
    :param code_end: end of the code (suffix excluded)
    :param code: canonical form
    :param query: the text is a question
    :param closed: the code comes from a closed list (aircraft, materials): a chain around it never hides it
    :return: True when the span is a code of the text
    """
    if code in STOP_CODES:
        return False
    written = text[start:code_end]
    if code_end < len(text) and text[code_end].isalnum():
        # Only a code holding a separator and ending in a digit may be glued to a word (MI-13991Table).
        glued = text[code_end:_word_end(text, code_end)]
        if not (written[-1].isdigit() and _NON_ALNUM_RE.search(written) and glued.isalpha()):
            return False
    # A slash separates the items of a list (A320/A321, F7X/F8X), not the parts of one identifier. A word before the
    # separator names what the code is about (EPR_B777, Projet_A350); a short or numbered part starts a longer
    # identifier (145.A.30, PTD-TEST-6-A-01).
    head = text[_word_start(text, start - 1):start - 1] if start > 1 else ""
    before = (start > 1 and text[start - 1] in _CHAIN and text[start - 1] != "/" and text[start - 2].isalnum()
              and (len(head) <= 2 or any(c.isdigit() for c in head)))
    # Only a number continues the chain (A30 in A30-12): a word after it is a suffix (A320_PROG, IQ22_PP) or the next
    # item of a list (A320/A321, F7X/F8X).
    after = code_end + 1 < len(text) and text[code_end] in _CHAIN and text[code_end + 1].isdigit()
    if (before or after) and len(code) < _MIN_LENGTH_IN_CHAIN and not closed:
        return False
    if query:
        head = re.match(r"[^\W\d_]*", written).group(0)
        if head.islower() and head.upper() in QUERY_STOP_PREFIXES and written[len(head):len(head) + 1].isspace():
            return False
    return True


def match_codes(text: str, accept_lowercase: bool = False) -> List[CodeMatch]:
    """The codes of a text, left to right, without overlap: of two overlapping codes the one starting first wins, then
    a specific pattern over a generic one (MI-13793-07 is MI13793, the shape of its family), then the longest, then
    the one of the first pattern.

    :param text: passage or question
    :param accept_lowercase: the text is a question: patterns with ``lowercase_in_query`` ignore the case
    :return: the codes found
    """
    if not text:
        return []
    cleaned, back = _unescape(text)
    folded = _fold(cleaned)
    found = []
    for rank, (pattern, regex) in enumerate(_COMPILED[accept_lowercase]):
        for m in regex.finditer(folded):
            code = canonical(m.group("code"))
            if len(code) >= pattern.min_length and _accepted(folded, m.start(), m.end("code"), code,
                                                              accept_lowercase, pattern.pattern_id in _CLOSED_PATTERNS):
                start, end = (back[m.start()], back[m.end() - 1] + 1) if back else (m.start(), m.end())
                # A dash number after a part reference is its length or size: the reference without it is a code too.
                bases = [canonical(folded[m.start("code"):m.start(g)]) for g in regex.groupindex
                         if g.startswith("tail") and m.group(g)]
                bases += [canonical(m.group(g)) for g in regex.groupindex if g.startswith("base") and m.group(g)]
                variants = tuple(dict.fromkeys(b for b in bases if b and b != code))
                found.append((start, pattern.pattern_id in _GENERIC_PATTERNS, -end, rank,
                              CodeMatch(start, end, code, pattern.pattern_id, variants)))
    candidates = [f[-1] for f in found]
    kept, last_end = [], -1
    for start, _, _, _, match in sorted(found):
        if start >= last_end:
            forms, longest = _variants(match, candidates)
            kept.append(match._replace(variants=forms, variants_end=longest))
            last_end = match.end
    return kept


def _variants(kept: CodeMatch, candidates: List[CodeMatch]) -> Tuple[Tuple[str, ...], int]:
    """The other canonical forms a kept code stands for: a longer reading of the same identifier
    (``DLV-31-9020-10``: ``DLV31902010`` beside ``DLV319020``), and the codes of other patterns inside a wrapping
    identifier (``QAP 117-110-10-02``: ``1171101002``).

    :param kept: the code kept for its span, with its own variants
    :param candidates: every code the patterns found in the text, overlapping or not
    :return: the variants, without the code itself, in order of first appearance, and the end of the longest reading
        among them (0 when none goes past the code)
    """
    forms, longest = list(kept.variants), 0
    for other in candidates:
        if other.canonical == kept.canonical or other.pattern_id == kept.pattern_id:
            continue
        longer = (other.start == kept.start and other.end > kept.end and other.canonical.startswith(kept.canonical)
                  and kept.pattern_id not in _CLOSED_PATTERNS)
        inner = (kept.pattern_id in _WRAPPER_PATTERNS and other.pattern_id not in _GENERIC_PATTERNS | _WRAPPER_PATTERNS
                 and kept.start <= other.start and other.end <= kept.end)
        if longer or inner:
            forms.append(other.canonical)
            if longer:
                longest = max(longest, other.end)
    return tuple(dict.fromkeys(f for f in forms if f != kept.canonical)), longest


def find_codes(text: str, accept_lowercase: bool = False) -> List[Tuple[int, int, str]]:
    """The codes of a text, as ``(start, end, canonical form)``.

    :param text: passage or question
    :param accept_lowercase: the text is a question (``sf-1027``)
    :return: one triple per code; the span includes a language suffix (``SF-1027_BG``)
    """
    return [(m.start, m.end, m.canonical) for m in match_codes(text, accept_lowercase)]


def _brackets_after(text: str, pos: int) -> List[str]:
    """Canonical forms already written in brackets right after ``pos`` (`` [SF1027] [MI1365]``).

    :param text: text
    :param pos: end of a code
    :return: the forms, in order
    """
    found = []
    m = _BRACKET_RE.match(text, pos)
    while m:
        found.append(m.group(1))
        m = _BRACKET_RE.match(text, m.end())
    return found


def normalize_codes(text: str, accept_lowercase: bool = False) -> str:
    """The text with `` [canonical]`` after each code written another way: ``SF-1027`` -> ``SF-1027 [SF1027]``; after
    the glued word when there is one (``MI-13991Table [MI13991]``); the variants of a code follow its canonical form
    (``DLV-31-9020-10 [DLV319020] [DLV31902010]``).

    A code written as its canonical form, or already followed by its bracket, is left as it is: running the function
    twice changes nothing.

    :param text: passage or question
    :param accept_lowercase: the text is a question (``sf-1027``)
    :return: the text with the canonical forms added; ``text`` itself when there is nothing to add
    """
    out, position = [], 0
    for m in match_codes(text, accept_lowercase):
        end = _word_end(text, max(m.end, m.variants_end))
        if end < position:
            continue
        written = [] if text[m.start:_word_end(text, m.end)] == m.canonical else [m.canonical]
        present = set(_brackets_after(text, end))
        forms = [f for f in dict.fromkeys(written + list(m.variants)) if f not in present]
        if not forms:
            continue
        out += [text[position:end], "".join(f" [{f}]" for f in forms)]
        position = end
    return "".join(out) + text[position:] if out else text


def normalize_query(query: str) -> str:
    """``normalize_codes`` for a search query: a question may write a REF in lower case (``sf-1027``, ``sf 1027``).

    :param query: search text
    :return: the query with the canonical forms added
    """
    return normalize_codes(query, accept_lowercase=True)


def canonical_codes(text: str, accept_lowercase: bool = False) -> List[str]:
    """The distinct canonical codes of a text, in order of first appearance.

    :param text: passage or question
    :param accept_lowercase: the text is a question
    :return: e.g. ``["SF1027", "QP1518"]``
    """
    return list(dict.fromkeys(c for m in match_codes(text, accept_lowercase) for c in (m.canonical, *m.variants)))


def cited_codes(chunk_text: str, ref: str) -> List[str]:
    """The codes a passage cites: the canonical codes of its body, without its ``[Source: …]`` line and without the
    codes of its own document (``Q0102QP_GB`` citing ``SF-1027`` -> ``["SF1027"]``).

    :param chunk_text: passage text, with or without its source line
    :param ref: REF of the passage's document
    :return: the distinct canonical codes, in order of first appearance
    """
    own = set(canonical_codes(ref or ""))
    body = _SOURCE_LINE_RE.sub("", chunk_text or "", count=1)
    return [code for code in canonical_codes(body) if code not in own]


def unmatched_refs(refs: Iterable[str]) -> List[str]:
    """The catalog REFs that no pattern reads from their first character: a new REF format, or a family to
    regenerate.

    :param refs: REFs as the catalog writes them
    :return: those REFs, sorted
    """
    return sorted({r.strip() for r in refs if r and r.strip()
                   and not any(m.start == 0 for m in match_codes(r.strip()))})


# --- Generation of CODE_FAMILIES ---

# Catalog REFs end with a language, site or variant suffix (SF-1027_BG, QM-1064-IS, QIS-46L-154 D), not part of
# the code.
_REF_SUFFIX_RE = re.compile(r"(?:[-_. ]+[A-Z]+)+$")
_REF_SHAPE_RE = re.compile(r"^([A-Z]{2,6})[-_. ]?(\d+)[A-Z]?((?:[-_./ ]\d+[A-Z]?)*)$")
_MIN_REFS_PER_FAMILY = 2
# Prefixes read by a pattern of their own.
_OWN_PATTERN_PREFIXES = frozenset({"IF"})


def families_from_refs(refs: Iterable[str], min_refs: int = _MIN_REFS_PER_FAMILY) -> Dict[str, CodeFamily]:
    """Code families of a list of catalog REFs: one per letter prefix followed by numbers (``SF-1027``, ``DLV-33-3010``,
    ``IAQ 10 01 01``). REFs of another shape (``Q0102QP``, ``LdB P-139``) are left out: other patterns read them.

    :param refs: REFs, any case, with or without language suffix
    :param min_refs: fewest distinct codes a prefix needs to become a family (fewer is more likely noise)
    :return: the families by prefix, prefixes in alphabetical order
    """
    codes: Dict[str, set] = {}
    first_digits: Dict[str, List[int]] = {}
    group_counts: Dict[str, List[int]] = {}
    next_digits: Dict[str, List[int]] = {}
    spaced: Dict[str, bool] = {}
    for ref in refs:
        m = _REF_SHAPE_RE.match(_REF_SUFFIX_RE.sub("", _fold(ref or "").strip().upper()))
        if not m:
            continue
        prefix, first, rest = m.groups()
        code = canonical(m.group(0))
        if code in codes.setdefault(prefix, set()):
            continue
        codes[prefix].add(code)
        later = re.findall(r"[-_./ ](\d+)", rest)
        first_digits.setdefault(prefix, []).append(len(first))
        group_counts.setdefault(prefix, []).append(1 + len(later))
        next_digits.setdefault(prefix, []).extend(len(g) for g in later)
        spaced[prefix] = spaced.get(prefix, False) or " " in rest
    return {
        prefix: CodeFamily(
            digits=(min(first_digits[prefix]), max(first_digits[prefix])),
            groups=(min(group_counts[prefix]), max(group_counts[prefix])),
            group_digits=(min(next_digits[prefix]), max(next_digits[prefix])) if next_digits[prefix] else (0, 0),
            spaced=spaced[prefix])
        for prefix in sorted(codes)
        if len(codes[prefix]) >= min_refs and prefix not in STOP_PREFIXES | _OWN_PATTERN_PREFIXES}


def render_families(families: Dict[str, CodeFamily]) -> str:
    """The ``CODE_FAMILIES`` block to paste in this module.

    :param families: result of ``families_from_refs``
    :return: Python source of the dict
    """
    lines = [f"    {prefix!r}: CodeFamily(digits={f.digits}, groups={f.groups}, group_digits={f.group_digits}, "
             f"spaced={f.spaced}),"
             for prefix, f in families.items()]
    return "CODE_FAMILIES: Dict[str, CodeFamily] = {\n" + "\n".join(lines) + "\n}\n"


if __name__ == "__main__":
    with open(sys.argv[1], encoding="utf-8") as fh:
        sys.stdout.write(render_families(families_from_refs(fh.read().splitlines())))
