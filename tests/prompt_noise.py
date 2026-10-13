"""Heuristics that flag low-value ("noise") rows in a comparison output.

These detectors quantify the kind of rows the structured-comparison prompt is
*supposed* to skip but sometimes still emits — chiefly:

  * NUMBER_RENDERING   : the same numeric value written in digits vs spelled out,
                         e.g. "240 hours" -> "two hundred forty (240) hours".
                         The value is identical; the row is house-style noise.
  * DATE_METADATA      : a document issue / revision date change with no technical
                         content (the prompt's SKIP list already covers this).
  * SYNONYM_SWAP       : a same-meaning word substitution, e.g. "this standard"
                         -> "this document".
  * IMPERATIVE_RATIONALE: the rationale reads as a command ("Update the...",
                         "Vérifier que...") instead of an explanation of the
                         change — a regression back to the pre-2026-08-19
                         action-style rationale (see _diff_engines.py RATIONALE
                         section: rationale is now a summary, never an
                         instruction).

The detectors are deliberately *conservative*: they only fire when the change
is almost certainly noise, so a flagged row is strong evidence the prompt let
something trivial through. Used by tests/test_prompt_noise.py and can be reused
in the debug_prompt notebook to A/B prompt variants.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Optional

# ---------------------------------------------------------------------------
# Number-rendering equivalence
# ---------------------------------------------------------------------------

_NUM_WORD = (
    r'(?:zero|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|'
    r'thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|twenty|'
    r'thirty|forty|fifty|sixty|seventy|eighty|ninety|hundred|thousand)'
)
_NUM_WORD_RE = re.compile(_NUM_WORD, re.IGNORECASE)
_DIGITS_RE = re.compile(r'\d+')

# Synonym pairs that carry no obligation/meaning change in this corpus.
_SYNONYMS = [
    ({'standard'}, {'document'}),
    ({'ensure'}, {'make', 'sure'}),
    ({'correct'}, {'proper'}),
]

_DATE_RE = re.compile(
    r'\b(revision\s+date|issue\s+date|date)\b.*\b('
    r'january|february|march|april|may|june|july|august|september|'
    r'october|november|december|\d{1,2}/\d{1,2}/\d{2,4})',
    re.IGNORECASE,
)


def _digit_tokens(text: str) -> List[str]:
    return sorted(_DIGITS_RE.findall(text or ''))


def _strip_num_words(text: str) -> str:
    """Remove spelled-out number words so two renderings can be compared."""
    return _NUM_WORD_RE.sub('', (text or '').lower())


@dataclass
class NoiseFinding:
    row: int            # 1-based row index (excluding header)
    category: str       # NUMBER_RENDERING | DATE_METADATA | SYNONYM_SWAP | IMPERATIVE_RATIONALE
    criticality: str
    type: str
    before: str
    after: str
    reason: str


def _is_number_rendering(before: str, after: str) -> bool:
    """True when before/after carry the SAME numeric value, differing only by
    digits-vs-spelled-out rendering (e.g. '24 months' / 'twenty-four (24) months')."""
    if not before or not after or before == '--' or after == '--':
        return False
    db, da = _digit_tokens(before), _digit_tokens(after)
    if not db or db != da:
        return False
    # One side must spell numbers out while they share the same digit tokens.
    if _NUM_WORD_RE.search(before) == _NUM_WORD_RE.search(after) is None:
        return False
    # The residual text (numbers removed) must be essentially identical, so we
    # don't flag rows where real wording also changed.
    nb = re.sub(r'[^a-z]', '', _strip_num_words(before))
    na = re.sub(r'[^a-z]', '', _strip_num_words(after))
    shorter, longer = sorted((nb, na), key=len)
    return bool(shorter) and shorter in longer


def _is_date_metadata(before: str, after: str) -> bool:
    return bool(_DATE_RE.search(before or '')) and bool(_DATE_RE.search(after or ''))


def _is_synonym_swap(before: str, after: str) -> bool:
    if not before or not after or before == '--' or after == '--':
        return False
    wb = set(re.findall(r'[a-z]+', before.lower()))
    wa = set(re.findall(r'[a-z]+', after.lower()))
    only_b, only_a = wb - wa, wa - wb
    # The change touches at most one short phrase, and that phrase is a known synonym.
    if len(only_b) > 3 or len(only_a) > 3:
        return False
    for left, right in _SYNONYMS:
        if (left <= only_b and right <= only_a) or (right <= only_b and left <= only_a):
            return True
    return False


# ---------------------------------------------------------------------------
# Rationale style: summary, never a command
# ---------------------------------------------------------------------------

# First-word vocabulary of common French/English imperative or infinitive verbs
# seen in "what to do about it" rationale rows (the style the prompt used
# before 2026-08-19). Deliberately conservative — a curated list, not an NLP
# check — same spirit as _SYNONYMS above.
_IMPERATIVE_FIRST_WORDS = frozenset({
    # French
    'mettre', 'mettez', 'verifier', 'vérifier', 'verifiez', 'vérifiez',
    'assurer', 'assurez', "s'assurer", 'assurez-vous', 'refaire', 'refaites',
    'controler', 'contrôler', 'controlez', 'contrôlez', 'ajouter', 'ajoutez',
    'corriger', 'corrigez', 'remplacer', 'remplacez', 'revoir', 'notifier',
    'notifiez', 'confirmer', 'confirmez', 'contacter', 'contactez',
    'reviser', 'réviser', 'revisez', 'révisez',
    # English
    'update', 'verify', 'check', 'ensure', 'confirm', 'replace', 'revise',
    'adjust', 'notify', 'inspect', 'add', 'remove', 'review', 'redo',
    'contact', 'perform', 'schedule', 're-torque', 'retorque',
})


def _first_word(text: str) -> str:
    m = re.match(r"\S+", text.strip())
    return m.group(0).strip('.,;:!?').lower() if m else ''


def _is_imperative_rationale(rationale: str) -> bool:
    """True when the rationale opens with a command verb instead of explaining
    the change — the pre-2026-08-19 action style the prompt no longer wants."""
    if not rationale or rationale == '--':
        return False
    return _first_word(rationale) in _IMPERATIVE_FIRST_WORDS


def classify_row(row_idx: int, type_: str, criticality: str,
                 before: str, after: str, rationale: str = '') -> Optional[NoiseFinding]:
    """Return a NoiseFinding if the row looks like noise, else None."""
    before, after = (before or ''), (after or '')
    if _is_number_rendering(before, after):
        return NoiseFinding(
            row_idx, 'NUMBER_RENDERING', criticality, type_, before, after,
            'same numeric value, digits vs spelled-out — should be skipped',
        )
    if _is_date_metadata(before, after):
        return NoiseFinding(
            row_idx, 'DATE_METADATA', criticality, type_, before, after,
            'document issue/revision date — SKIP per prompt metadata rule',
        )
    if _is_synonym_swap(before, after):
        return NoiseFinding(
            row_idx, 'SYNONYM_SWAP', criticality, type_, before, after,
            'same-meaning synonym substitution — should be skipped',
        )
    if _is_imperative_rationale(rationale or ''):
        return NoiseFinding(
            row_idx, 'IMPERATIVE_RATIONALE', criticality, type_, before, after,
            f'rationale reads as a command ("{rationale}") — should be a summary',
        )
    return None


def scan_rows(rows: List[dict]) -> List[NoiseFinding]:
    """Scan a list of change dicts (keys: type, criticality, before, after, rationale)."""
    findings = []
    for i, r in enumerate(rows, 1):
        f = classify_row(
            i,
            str(r.get('type', '')),
            str(r.get('criticality', '')),
            str(r.get('before', '')),
            str(r.get('after', '')),
            str(r.get('rationale', '')),
        )
        if f:
            findings.append(f)
    return findings


def scan_xlsx(path: str) -> List[NoiseFinding]:
    """Load a comparison .xlsx (Section/Page/Type/Criticality/Before/After/Rationale)
    and return its noise findings."""
    import openpyxl
    wb = openpyxl.load_workbook(path, read_only=True)
    rows = list(wb.active.iter_rows(values_only=True))
    header = [str(c).strip().lower() if c else '' for c in rows[0]]
    idx = {name: header.index(name) for name in
           ('type', 'criticality', 'before', 'after', 'rationale') if name in header}
    dicts = [
        {k: r[i] for k, i in idx.items()}
        for r in rows[1:]
    ]
    return scan_rows(dicts)
