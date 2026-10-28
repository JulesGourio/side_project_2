# Databricks notebook source
# MAGIC %md
# MAGIC # Canonical token preview
# MAGIC
# MAGIC Runs the code normalization of the parsing pipeline (the rules that write the passages' `chunk_preprocessed` column and rewrite the chat queries, copied in full in the *Code normalization* cell) on the DEV passages and on the UAT chat questions, and shows the text before and after. Nothing is written: the passages and the index stay as they are. The notebook needs nothing else than its own cells.
# MAGIC
# MAGIC | | |
# MAGIC | --- | --- |
# MAGIC | Reads | `dev_proj.qualibot.doc_catalog` and `uat_proj.qualibot.doc_catalog` (REFs, for the coverage), `dev_proj.qualibot.chunks`, `uat_proj.qualibot.chat_turns`, `dev_proj.qualibot.f2_familles_codes` (code patterns, for the comparison) |
# MAGIC | Writes | nothing |
# MAGIC | Rights | the user's own |
# MAGIC
# MAGIC ## How a code is found
# MAGIC
# MAGIC One table of named patterns (`CODE_PATTERNS`), each with the examples it must find and the texts it must leave alone; the first table below lists them. Of two overlapping codes the one starting first wins, then the longest, then the first pattern. The token is the code without separators, upper-cased, without diacritics and without its language or variant suffix (`SF-1027_BG` → `SF1027`, `QIS-46A-352 A` → `QIS46A352`). It is appended after the code, or after the word glued to it (`MI-13991Table [MI13991]`); nothing is added when the code is written as its token, and a second pass adds nothing. In a question, a REF in lower case is a code too (`sf 1027`), except the prefixes that are also words (`if 2`, `go 2024`, `le 23`).

# COMMAND ----------

# Parameters

import logging
import sys
from typing import Iterable

import pandas as pd
from pyspark.sql import Window
from pyspark.sql import functions as F

CATALOGS = ["dev_proj.qualibot.doc_catalog", "uat_proj.qualibot.doc_catalog"]
PASSAGES = "dev_proj.qualibot.chunks"
QUESTIONS = "uat_proj.qualibot.chat_turns"
PATTERNS = "dev_proj.qualibot.f2_familles_codes"
EXAMPLES = 15   # before / after examples per pattern

logger = logging.getLogger("canonical_token_preview")
if not logger.handlers:
    _handler = logging.StreamHandler(sys.stdout)
    _handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(_handler)
logger.setLevel(logging.INFO)

# COMMAND ----------

# MAGIC %md
# MAGIC ## Code normalization
# MAGIC The rules of the parsing pipeline, word for word. The functions defined here also run on the executors.

# COMMAND ----------

# --- code normalization: start ---
import re
import sys
import unicodedata
from dataclasses import dataclass
from typing import Dict, Iterable, List, NamedTuple, Pattern, Tuple

# Version of the rules written into the passages: passages normalised by another version need a rechunk.
NORMALIZATION_VERSION = 2


# --- Model ---

class CodeFamily(NamedTuple):
    """Shape of the codes of one prefix, as the catalog REFs write them.

    ``min_digits``/``max_digits``: digits of the first number group; ``groups``: most number groups after the prefix
    (``DLV-33-3010`` has 2); ``spaced``: a space may separate two number groups (``IAQ 10 01 01``).
    """

    min_digits: int
    max_digits: int
    groups: int
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
    """A code of a text: span (language suffix included), canonical form, id of the pattern that found it."""

    start: int
    end: int
    canonical: str
    pattern_id: str


# --- Building blocks of the patterns ---

_DASHES = "\u2010\u2011\u2012\u2013\u2014\u2015\u2212"
_SEP = rf"[-_./{_DASHES}]"
_SPACE = "[ \u00a0]"
# Between a prefix and its number: one space, or one or two separators with up to three spaces around them
# (MI - 1365, SF – 1027, 19 --058).
_GAP = rf"(?:{_SPACE}{{0,3}}{_SEP}{{1,2}}{_SPACE}{{0,3}}|{_SPACE})?"
_START = r"(?<![^\W_])"
# A code never ends inside a number: neither before a digit nor before a decimal part (12.5, 0,55).
_END = r"(?!\d)(?![.,]\d)"
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
    'BEP': CodeFamily(3, 4, 1, False),
    'CGO': CodeFamily(2, 4, 3, False),
    'DAP': CodeFamily(2, 2, 2, False),
    'DGL': CodeFamily(4, 4, 1, False),
    'DLV': CodeFamily(2, 4, 2, False),
    'DTAS': CodeFamily(2, 2, 2, False),
    'FDAQL': CodeFamily(4, 4, 2, False),
    'FORM': CodeFamily(3, 3, 2, False),
    'GO': CodeFamily(4, 4, 1, False),
    'IAQ': CodeFamily(2, 2, 3, True),
    'IC': CodeFamily(2, 3, 1, False),
    'INAQ': CodeFamily(3, 3, 2, False),
    'INE': CodeFamily(3, 3, 1, False),
    'IP': CodeFamily(3, 3, 2, False),
    'IQ': CodeFamily(2, 6, 3, False),
    'LAT': CodeFamily(3, 3, 4, False),
    'LIS': CodeFamily(3, 3, 1, False),
    'MI': CodeFamily(4, 5, 1, False),
    'MIT': CodeFamily(4, 4, 1, False),
    'MM': CodeFamily(4, 5, 1, False),
    'MO': CodeFamily(4, 4, 1, False),
    'MOP': CodeFamily(3, 3, 1, False),
    'MR': CodeFamily(4, 4, 1, False),
    'NF': CodeFamily(3, 5, 1, False),
    'NM': CodeFamily(3, 3, 1, False),
    'NS': CodeFamily(4, 5, 2, False),
    'PAQ': CodeFamily(2, 3, 2, True),
    'PLQ': CodeFamily(3, 3, 1, False),
    'PRLAT': CodeFamily(3, 3, 2, False),
    'PRO': CodeFamily(3, 3, 1, False),
    'QIS': CodeFamily(2, 2, 2, False),
    'QM': CodeFamily(4, 4, 1, False),
    'QP': CodeFamily(4, 6, 1, False),
    'QR': CodeFamily(4, 4, 1, False),
    'QTP': CodeFamily(2, 3, 2, False),
    'QTR': CodeFamily(2, 8, 2, False),
    'RAQ': CodeFamily(2, 2, 3, True),
    'RLCZ': CodeFamily(1, 2, 2, False),
    'RLLV': CodeFamily(2, 2, 2, False),
    'SF': CodeFamily(4, 5, 2, False),
    'SSPRO': CodeFamily(3, 3, 1, False),
    'TDA': CodeFamily(1, 1, 3, False),
    'TDAE': CodeFamily(2, 2, 2, False),
    'TDAJ': CodeFamily(2, 5, 2, False),
    'TDAL': CodeFamily(2, 2, 2, True),
    'TDAM': CodeFamily(2, 2, 2, False),
    'TDAN': CodeFamily(2, 2, 2, False),
    'TDRA': CodeFamily(2, 2, 2, False),
    'UADE': CodeFamily(2, 2, 2, False),
    'UDA': CodeFamily(1, 2, 3, False),
    'UDAE': CodeFamily(2, 2, 2, False),
    'UDB': CodeFamily(1, 1, 3, False),
    'UDC': CodeFamily(2, 2, 2, False),
    'UDE': CodeFamily(1, 2, 3, False),
    'UDH': CodeFamily(1, 1, 3, False),
    'VA': CodeFamily(3, 7, 2, False),
    'VDDR': CodeFamily(2, 2, 2, False),
    'VDDX': CodeFamily(2, 2, 2, False),
}


# --- Lexicon ---

# Prefixes never treated as codes, even when a catalog REF uses them: words and language codes that a number follows
# in ordinary text ("GB 128", "in 300").
STOP_PREFIXES = frozenset({"GB", "IN", "EN", "FR", "ES", "IS", "OF", "ON", "NO", "AT", "TO", "DE", "LE", "LA", "ET"})

# Prefixes that are also words in lower case: in a question, written in lower case and followed by a space, they are
# words ("if 2 parts", "go 2024", "le 23").
QUERY_STOP_PREFIXES = frozenset({"IF", "GO", "MO", "PRO", "VA", "LA", "LE", "DE", "IN", "IS", "IT", "NO", "ON", "AS",
                                 "DO", "ED"})

# Canonical forms that are words written letter by letter (D.E.S, F.I.N) or fillers (E.E.E): never codes.
STOP_CODES = frozenset({"DES", "FIN", "NON", "OUI", "LES", "ETC", "THE", "AND", "FOR", "PAR", "SUR", "UNE", "AAA",
                        "EEE", "III", "XXX"})

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
                     "CCR")
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
        group = r"\d{1,6}(?:[A-Z](?![A-Za-z]))?"
        alternatives.append(
            rf"(?:{'|'.join(sorted(prefixes, key=len, reverse=True))}){_GAP}"
            rf"\d{{{family.min_digits},{family.max_digits}}}(?:[A-Z](?![A-Za-z]))?"
            rf"(?:{between}{group}){{0,{family.groups - 1}}}")
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
        rf"{_START}(?P<code>IF{_GAP}2{_SEP}?\d{{2}}{_SEP}?\d{{2}}|IF{_SPACE}2(?:{_SPACE}\d{{2}}){{2}}){_END}{_LANGUAGE}",
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
        rf"{_START}(?P<code>[A-Z]\d[A-Z](?:{_SEP}[A-Z]{{3}})?{_SEP}\d{{3,4}}(?:{_SEP}[A-Z]{{3}}(?![^\W_]))?){_END}",
        examples=(("E2P_EOS_0011", "E2PEOS0011"), ("A2E_026", "A2E026"), ("H8P-0081", "H8P0081"),
                  ("A1O_011_CMP", "A1O011CMP")),
        counter_examples=("A2E", "A2E_02"),
        lowercase_in_query=True),
    CodePattern(
        "REF-14", "Segmented internal code",
        rf"{_START}(?P<code>(?:{_one_of(SEGMENTED_PREFIXES)}){_SEP}(?:[A-Z]{{2,4}}{_SEP}){{0,2}}[A-Z]{{1,8}}{_SEP}?"
        rf"\d{{3,5}}){_END}{_LANGUAGE}",
        examples=(("IN_MRPC003_EN", "INMRPC003"), ("IN_APO_0001", "INAPO0001"), ("IQ-BEL-QA-018", "IQBELQA018"),
                  ("IQ_BEL-ME-004", "IQBELME004"), ("LMX-NF-008", "LMXNF008"), ("LMX-MI-14229", "LMXMI14229"),
                  ("EVD-EMB-0006", "EVDEMB0006"), ("IN-PLANNING-002", "INPLANNING002"), ("IN_P_0015_FR", "INP0015")),
        counter_examples=("IN-SITU", "LMX-NF")),
    CodePattern(
        "REF-15", "L site code",
        rf"{_START}(?P<code>L_(?:\d{{4}}|[0-9A-Z]{{3,4}})_[A-Z]{{3}})(?![^\W_])",
        examples=(("L_1263_PGC", "L1263PGC"), ("L_0L5_SPI", "L0L5SPI"), ("L_LVCS_PGC", "LLVCSPGC")),
        counter_examples=("L_12_PGC",)),
    CodePattern(
        "REF-16", "Aircraft programme document",
        rf"{_START}(?P<code>\d{{3}}F-[A-Z]{{4}}-[A-Z]{{2,3}})(?![^\W_])",
        examples=(("777F-MDCD-QP", "777FMDCDQP"),),
        counter_examples=("777F",)),
    CodePattern(
        "STD-01", "Standard",
        rf"""{_START}(?P<code>(?:
            (?:{_one_of(STANDARD_PREFIXES)})(?:[-_/.]|[ ]{{1,2}})?(?:\d{{3,6}}|\d{{2}}(?=[-.]\d{{3}}))
          | (?:{_one_of(STANDARD_WORD_PREFIXES)})(?:[-_/.]?|[ ](?!(?:19|20)\d\d(?!\d)))(?:\d{{3,6}}|\d{{2}}(?=[-.]\d{{3}}))
          | MIL[-_ ]?(?:STD|HDBK|PRF|DTL|SPEC)[-_ ]?\d{{3,5}}
        )[A-Z]?(?:[-.]\d{{1,4}}[A-Z]?)?)(?![A-Za-z0-9]){_END}""",
        examples=(("EN 9100", "EN9100"), ("NAS 1789", "NAS1789"), ("ISO 9001:2015", "ISO9001"),
                  ("prEN 6122", "PREN6122"), ("BAC 5578", "BAC5578"), ("NE 40-012", "NE40012"),
                  ("MIL-STD-810G", "MILSTD810G")),
        counter_examples=("EN 2024", "EN 12", "AS 2019", "NAS 12")),
    CodePattern(
        "ATA-01", "ATA chapter",
        rf"{_START}(?P<code>ATA[ -]?\d{{2}}){_END}(?![A-Za-z0-9])",
        examples=(("ATA 52", "ATA52"), ("ATA-25", "ATA25")),
        counter_examples=("ATA 2024", "ATA")),
    CodePattern(
        "ACF-01", "Aircraft",
        rf"""{_START}(?P<code>
            A[ -]?(?:{_one_of(AIRBUS_MODELS)})(?:[-/]?(?:NEO|neo|XLR|XWB|\d{{3,4}}))? | A400M
          | (?:B|BOEING|Boeing)[ -]?7[0-9]7(?:-(?:\d{{1,3}}|MAX\d{{0,2}}))?
          | F(?:7X|8X|6X|10X|900|2000)(?:EX|LX|DX)? | ATR[ -]?(?:42|72)(?:-\d{{2,3}})?
          | ERJ[ -]?\d{{3}} | E(?:170|175|190|195)(?:-?E2)? | CRJ[ -]?\d{{3,4}}
        )(?![A-Za-z0-9]){_END}""",
        examples=(("A350-900", "A350900"), ("A 320", "A320"), ("ATR 72-600", "ATR72600"), ("Boeing 787-9", "BOEING7879"),
                  ("F7X", "F7X")),
        counter_examples=("a 12.5", "A 12", "A 3200")),
    CodePattern(
        "REG-01", "EASA regulation",
        rf"""{_START}(?P<code>
            (?:PART|Part|PARTIE|Partie)[-_ .]?(?:{_one_of(EASA_PARTS)})(?:-?[GJ](?![A-Za-z0-9]))?
          | CS[-_ ]?(?:2[2-9]|3[46]|E|P|APU|ETSO|AWO|VLA|LSA)(?:[-.]\d{{1,4}})?
          | (?:AMC|GM)\d?[ -]?(?:21|145|147|66|ML|M|CS|20|25)(?:[-.]\d{{1,4}})?
          | 21[-.]?[GJ]
        )(?![A-Za-z0-9]){_END}""",
        examples=(("PART 21G", "PART21G"), ("Part-145", "PART145"), ("CS-25", "CS25"), ("AMC 20-29", "AMC2029")),
        counter_examples=("Partie 3", "PART 1", "CS 1")),
    CodePattern(
        "MAT-01", "Material",
        rf"{_START}(?P<code>[2-7]\d{{3}}[- ]?T\d{{1,4}}|1[57][- .]?[45][ ]?PH|TA6V|Ti[- ]?6Al[- ]?4V)(?![A-Za-z0-9]){_END}",
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
        rf"{_START}(?P<code>8-D|5-S)(?![A-Za-z0-9])",
        examples=(("8-D", "8D"), ("5-S", "5S")),
        counter_examples=("8-DE",)),
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


def _accepted(text: str, start: int, code_end: int, code: str, query: bool) -> bool:
    """Boundary rules that the regexes leave to code: a code glued to a word, a code inside a longer chain, a word in
    lower case in a question.

    :param text: passage or question
    :param start: start of the code
    :param code_end: end of the code (suffix excluded)
    :param code: canonical form
    :param query: the text is a question
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
    before = start > 1 and text[start - 1] in _CHAIN and text[start - 2].isalnum()
    after = code_end + 1 < len(text) and text[code_end] in _CHAIN and text[code_end + 1].isalnum()
    if (before or after) and len(code) < _MIN_LENGTH_IN_CHAIN:
        return False
    if query:
        head = re.match(r"[^\W\d_]*", written).group(0)
        if head.islower() and head.upper() in QUERY_STOP_PREFIXES and written[len(head):len(head) + 1].isspace():
            return False
    return True


def match_codes(text: str, accept_lowercase: bool = False) -> List[CodeMatch]:
    """The codes of a text, left to right, without overlap: of two overlapping codes the one starting first wins, then
    the longest, then the one of the first pattern.

    :param text: passage or question
    :param accept_lowercase: the text is a question: patterns with ``lowercase_in_query`` ignore the case
    :return: the codes found
    """
    if not text:
        return []
    folded = _fold(text)
    found = []
    for rank, (pattern, regex) in enumerate(_COMPILED[accept_lowercase]):
        for m in regex.finditer(folded):
            code = canonical(m.group("code"))
            if len(code) >= pattern.min_length and _accepted(folded, m.start(), m.end("code"), code,
                                                              accept_lowercase):
                found.append((m.start(), -m.end(), rank, CodeMatch(m.start(), m.end(), code, pattern.pattern_id)))
    kept, last_end = [], -1
    for start, _, _, match in sorted(found):
        if start >= last_end:
            kept.append(match)
            last_end = match.end
    return kept


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
    the glued word when there is one (``MI-13991Table [MI13991]``).

    A code written as its canonical form, or already followed by its bracket, is left as it is: running the function
    twice changes nothing.

    :param text: passage or question
    :param accept_lowercase: the text is a question (``sf-1027``)
    :return: the text with the canonical forms added; ``text`` itself when there is nothing to add
    """
    out, position = [], 0
    for m in match_codes(text, accept_lowercase):
        end = _word_end(text, m.end)
        if end < position or text[m.start:end] == m.canonical or m.canonical in _brackets_after(text, end):
            continue
        out += [text[position:end], f" [{m.canonical}]"]
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
    return list(dict.fromkeys(m.canonical for m in match_codes(text, accept_lowercase)))


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

# Catalog REFs end with a language, site or variant suffix (SF-1027_BG, QM-1064-IS, QIS-46L-154 D): not part of the code.
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
    seen: Dict[str, set] = {}
    stats: Dict[str, List] = {}
    for ref in refs:
        m = _REF_SHAPE_RE.match(_REF_SUFFIX_RE.sub("", _fold(ref or "").strip().upper()))
        if not m:
            continue
        prefix, first, rest = m.groups()
        code = canonical(m.group(0))
        if code in seen.setdefault(prefix, set()):
            continue
        seen[prefix].add(code)
        groups = 1 + len(re.findall(r"[-_./ ]\d", rest))
        spaced = " " in rest
        lo, hi, most, any_spaced = stats.get(prefix, [len(first), len(first), groups, spaced])
        stats[prefix] = [min(lo, len(first)), max(hi, len(first)), max(most, groups), any_spaced or spaced]
    return {prefix: CodeFamily(*stats[prefix]) for prefix in sorted(stats)
            if len(seen[prefix]) >= min_refs and prefix not in STOP_PREFIXES | _OWN_PATTERN_PREFIXES}


def render_families(families: Dict[str, CodeFamily]) -> str:
    """The ``CODE_FAMILIES`` block to paste in this module.

    :param families: result of ``families_from_refs``
    :return: Python source of the dict
    """
    lines = [f"    {prefix!r}: CodeFamily({f.min_digits}, {f.max_digits}, {f.groups}, {f.spaced}),"
             for prefix, f in families.items()]
    return "CODE_FAMILIES: Dict[str, CodeFamily] = {\n" + "\n".join(lines) + "\n}\n"
# --- code normalization: end ---

# COMMAND ----------

# MAGIC %md
# MAGIC ## Patterns
# MAGIC Every pattern with the examples it must find and the texts it must leave alone.

# COMMAND ----------

logger.info("normalization version %s, %d patterns, %d families", NORMALIZATION_VERSION, len(CODE_PATTERNS),
            len(CODE_FAMILIES))
display(spark.createDataFrame(
    [(p.pattern_id, p.name, ", ".join(t for t, _ in p.examples), ", ".join(p.counter_examples)) for p in CODE_PATTERNS],
    "pattern_id string, name string, examples string, counter_examples string"))

# COMMAND ----------

# MAGIC %md
# MAGIC ## Coverage of the catalog
# MAGIC The REFs of the DEV and UAT document catalogs that no pattern reads from their first character, by shape (`A` letter, `9` digit). An empty table means every REF is normalised. A REF here is a new format (a pattern to add) or a new prefix (`CODE_FAMILIES` to regenerate).

# COMMAND ----------

refs_df = None
for table in CATALOGS:
    part = spark.table(table).select(F.trim("ref").alias("ref")).where("ref IS NOT NULL AND ref <> ''")
    refs_df = part if refs_df is None else refs_df.unionByName(part)
REFS = sorted(r["ref"] for r in refs_df.distinct().collect())
unread = unmatched_refs(REFS)
logger.info("catalog: %d REFs, %d read by no pattern", len(REFS), len(unread))
display(spark.createDataFrame([(r,) for r in unread] or [("",)], "ref string").where("ref <> ''")
        .withColumn("shape", F.regexp_replace(F.regexp_replace(F.upper("ref"), "[A-Z]", "A"), "[0-9]", "9"))
        .groupBy("shape").agg(F.count("*").alias("refs"), F.slice(F.array_sort(F.collect_list("ref")), 1, 5)
                              .alias("examples"))
        .orderBy(F.desc("refs")))

# COMMAND ----------

# MAGIC %md
# MAGIC ## Passages
# MAGIC Every DEV passage goes through the code normalization. First table: passages changed, distinct tokens and tokens appended, per pattern. A passage counts once per pattern it holds.

# COMMAND ----------

SCHEMA = ("id string, token string, pattern_id string, written string, appended boolean, word string, cut boolean, "
          "before string, after string")
COLUMNS = ["id", "token", "pattern_id", "written", "appended", "word", "cut", "before", "after"]


def annotated(batches: Iterable[pd.DataFrame], query: bool) -> Iterable[pd.DataFrame]:
    """Codes found in each text, with an extract before and after the token is appended.

    :param batches: pandas batches with the columns id and txt
    :param query: the texts are questions (``normalize_query``), else passages (``normalize_codes``)
    :return: pandas batches with the columns of SCHEMA, one row per code found; appended is False when nothing is
        added for that code
    """
    for pdf in batches:
        rows = []
        for i, txt in zip(pdf["id"], pdf["txt"]):
            txt = txt or ""
            for start, end, token, pattern_id in match_codes(txt, accept_lowercase=query):
                stop = _word_end(txt, end)
                written = txt[start:end]
                appended = not (txt[start:stop] == token or token in _brackets_after(txt, stop))
                # The extract runs from word boundary to word boundary, so no code in it is cut.
                left = txt.rfind(" ", 0, max(0, start - 60)) + 1
                right = txt.find(" ", stop + 40)
                before = txt[left:right if right >= 0 else len(txt)]
                after = normalize_codes(before, accept_lowercase=query)
                w_left = max(txt.rfind(c, 0, start) for c in " \n\t") + 1
                w_right = min([k for k in (txt.find(c, stop) for c in " \n\t") if k >= 0] or [len(txt)])
                cut = any(c.isalnum() for c in txt[w_left:start] + txt[stop:w_right])
                rows.append((i, token, pattern_id, written, appended, txt[w_left:w_right], cut, before, after))
        yield pd.DataFrame(rows, columns=COLUMNS)


passages = spark.table(PASSAGES).select(F.col("chunk_id").cast("string").alias("id"), F.col("chunk_text").alias("txt"))
found = passages.mapInPandas(lambda b: annotated(b, query=False), SCHEMA).cache()
hits = found.where("appended")
total = passages.count()
changed = hits.select("id").distinct().count()
logger.info("DEV passages: %d, changed: %d (%.1f %%)", total, changed, 100 * changed / max(total, 1))
display(hits.groupBy("pattern_id")
        .agg(F.countDistinct("id").alias("passages"), F.countDistinct("token").alias("tokens"),
             F.count("*").alias("appended"))
        .orderBy(F.desc("passages")))

# COMMAND ----------

# MAGIC %md
# MAGIC ## Before and after, per pattern
# MAGIC The most frequent tokens of each pattern, with one extract of a passage before and after.

# COMMAND ----------

per_token = hits.groupBy("pattern_id", "token").agg(F.count("*").alias("n"), F.first("written").alias("written"),
                                                 F.first("before").alias("before"), F.first("after").alias("after"))
w = Window.partitionBy("pattern_id").orderBy(F.desc("n"))
display(per_token.withColumn("rank", F.row_number().over(w)).where(F.col("rank") <= EXAMPLES)
        .orderBy("pattern_id", "rank").select("pattern_id", "token", "written", "n", "before", "after"))

# COMMAND ----------

# MAGIC %md
# MAGIC ## REF-shaped codes absent from the catalog
# MAGIC Tokens of the REF patterns that are the token of no catalog REF: obsolete REFs, other sites, typing errors, or text that only looks like a REF. One row per token, most frequent first, with its spellings: a token here that is not a document code means a pattern is too wide.

# COMMAND ----------

catalog_tokens = sorted({code for ref in REFS for code in canonical_codes(ref)})
display(hits.where(F.col("pattern_id").startswith("REF-"))
        .join(spark.createDataFrame([(t,) for t in catalog_tokens], "token string"), "token", "left_anti")
        .groupBy("pattern_id", "token")
        .agg(F.count("*").alias("n"), F.countDistinct("id").alias("passages"),
             F.slice(F.array_sort(F.collect_set("written")), 1, 8).alias("spellings"))
        .orderBy(F.desc("n")).limit(500))

# COMMAND ----------

# MAGIC %md
# MAGIC ## Codes inside a longer identifier
# MAGIC Tokens appended to a code that is only part of a longer identifier (between two spaces): `SF-1154-1_Fr [SF1154]`, `AMC1 145.A.30 [AMC1145]`. One row per token and identifier, most frequent first.

# COMMAND ----------

display(hits.where("cut").groupBy("pattern_id", "token", "word").agg(F.count("*").alias("n"))
        .orderBy(F.desc("n")).limit(500))

# COMMAND ----------

# MAGIC %md
# MAGIC ## Codes without digits
# MAGIC Every token appended to a code written without digits (catalog codes such as `M.O.P`, acronyms with dots), with its spellings.

# COMMAND ----------

display(hits.where(~F.col("written").rlike("[0-9]")).groupBy("pattern_id", "token")
        .agg(F.count("*").alias("n"), F.slice(F.array_sort(F.collect_set("written")), 1, 10).alias("spellings"))
        .orderBy(F.desc("n")))

# COMMAND ----------

# MAGIC %md
# MAGIC ## Codes of the code patterns that the normalization misses
# MAGIC Codes that the code patterns notebook classified `CANONICAL_TOKEN` and that DEV documents cite, whose token the normalization finds in no DEV passage, by number of documents. Separated acronyms are left out: written plainly (`SAP`) they are already their token. Expected: documents outside the chat scope (the inventory reads every parsed document, the passages only the documents in scope), and codes written only as their token. Anything else is a code the normalization misses.

# COMMAND ----------

found_tokens = found.select("token").distinct()
display(spark.table(PATTERNS).where((F.col("env") == "dev") & (F.col("treatment") == "CANONICAL_TOKEN")
                                    & (F.col("pattern_id") != "ACR-02") & (F.col("document_count") > 0))
        .join(found_tokens, F.col("canonical_token") == F.col("token"), "left_anti")
        .select("pattern_id", "code_key", "canonical_token", F.slice("spellings", 1, 6).alias("spellings"),
                "document_count", F.slice("top_documents", 1, 3).alias("top_documents"))
        .orderBy(F.desc("document_count")).limit(500))

# COMMAND ----------

# MAGIC %md
# MAGIC ## Tokens the code patterns did not ask for
# MAGIC Tokens the normalization appends whose code the code patterns notebook classified otherwise (`KEEP_AS_IS`, `GLOSSARY_CANDIDATE`) or did not see, most frequent first. A token here is a code the normalization annotates although the analysis left it alone: either the analysis or the normalization is wrong.

# COMMAND ----------

wanted = (spark.table(PATTERNS).where((F.col("env") == "dev") & (F.col("treatment") == "CANONICAL_TOKEN"))
          .select(F.col("canonical_token").alias("token")).distinct())
classified = (spark.table(PATTERNS).where(F.col("env") == "dev")
              .groupBy(F.col("code_key").alias("token"))
              .agg(F.first("pattern_id").alias("pattern_id"), F.first("treatment").alias("treatment")))
display(hits.join(wanted, "token", "left_anti").groupBy("pattern_id", "token")
        .agg(F.count("*").alias("n"), F.slice(F.array_sort(F.collect_set("written")), 1, 6).alias("spellings"),
             F.first("before").alias("before"))
        .join(classified, "token", "left").orderBy(F.desc("n")).limit(500))

# COMMAND ----------

# MAGIC %md
# MAGIC ## Chat questions
# MAGIC Every UAT chat question that gets a token, before and after (`normalize_query`). A question must carry the same token as the passages that cite its code.

# COMMAND ----------

questions = (spark.table(QUESTIONS).where(F.col("question").isNotNull())
             .select(F.col("id").cast("string").alias("id"),
                     F.regexp_replace("question", r"\[Division:[^\]]*\]", " ").alias("txt")))
q_hits = questions.mapInPandas(lambda b: annotated(b, query=True), SCHEMA).where("appended")
logger.info("UAT questions with a token: %d", q_hits.select("id").distinct().count())
display(q_hits.select("pattern_id", "token", "written", "after").orderBy("pattern_id", "token"))
