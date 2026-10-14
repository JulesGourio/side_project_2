"""Text primitives: structural tags, canonical forms, substantive-change tests and extraction warnings."""

import difflib
import logging
import os
import re
from typing import Dict, List, Set

logger = logging.getLogger(__name__)

_SEM_THRESHOLD = 0.05          # canonical distance threshold for is_substantive
_MAX_DIFF_CHARS = int(os.getenv('COMPARE_MAX_DIFF_CHARS', '600000'))  # ~150K tokens

NUMBER_RE = re.compile(r'\b\d+(?:[.,]\d+)*(?:\s*%|\s*mm|\s*in(?:ch)?|\s*kg|\s*psi)?\b')
PART_NUM_RE = re.compile(r'\d{4,}')  # long digit runs in alphanumeric part numbers (no word-boundary)
NORM_REF_RE = re.compile(
    r'\b(?:ISO|IEC|ASTM|SAE|NAS|EN|AMS|AS|ANSI|BS|DIN|MIL|BSS|BAC|BMS|OSHA|NIST)[- ]?\d[\w\-.]*',
    re.I,
)
MODAL_RE = re.compile(r'\b(?:shall|must|will|should|may|can|could|would)\b', re.I)
# The bare ALL-CAPS branch accepts the punctuation real headings carry (dashes, colon, parens, ampersand, digits),
# otherwise their paragraphs would inherit the previous heading.
SECTION_RE = re.compile(r"^(?:\d+(?:\.\d+)*\.?\s+[A-Z]|[A-Z][A-Z0-9 \-–—:,./()&']{3,}$)")

# Matches trailing structural tags: [Page 3], [Para 7], [Page 3, Para 12], [Heading 2], [Slide 4], [Item 12]
_TAG_RE = re.compile(r'\s*\[(?:Page|Para|Heading|Slide|Item)[^\]]*\].*$', re.I)


def strip_tag(text: str) -> str:
    """Remove trailing structural tags ([Page N], [Para N], [Page N, Para M], [Heading N])."""
    return _TAG_RE.sub('', text).strip()


# Appended by truncate_diff(); factory.diff_truncation_warnings() looks for it so
# the user is told the tail of the document was never analysed.
DIFF_TRUNCATED_MARKER = '[... diff truncated — document too large. Only the first portion is shown.]'


def truncate_diff(text: str) -> str:
    """Truncate diff text to _MAX_DIFF_CHARS to prevent LLM token limit errors."""
    if len(text) <= _MAX_DIFF_CHARS:
        return text
    logger.warning('Diff truncated: %d chars > %d limit', len(text), _MAX_DIFF_CHARS)
    return text[:_MAX_DIFF_CHARS] + f'\n\n{DIFF_TRUNCATED_MARKER}'


# Below this many extracted characters a document almost certainly has no
# usable text layer (scanned PDF, image-only DOCX, corrupt file).
_MIN_EXTRACTED_CHARS = 200


def extraction_warnings(old_text: str, new_text: str) -> List[str]:
    """Detect near-empty text extraction so a scanned/corrupt document doesn't
    silently produce a false 'No significant changes detected'."""
    warnings: List[str] = []
    for label, text in (('OLD', old_text), ('NEW', new_text)):
        if len(strip_tag(text).strip()) < _MIN_EXTRACTED_CHARS:
            warnings.append(
                f'{label} document yielded almost no extractable text (scanned or image-only?). '
                'Text comparison is unreliable — only embedded images were compared.'
            )
    return warnings or unrelated_warnings(old_text, new_text)


# Two revisions of one document share most of their paragraphs. Under this
# share of identical blocks, the user most likely dropped two different
# documents — the report would be one long list of removals and additions.
_UNRELATED_MAX_SHARED = 0.05
_UNRELATED_MIN_BLOCKS = 20


def unrelated_warnings(old_text: str, new_text: str) -> List[str]:
    def _blocks(text: str) -> Set[str]:
        out = set()
        for line in text.splitlines():
            bare = ' '.join(strip_tag(line).lower().split())
            if len(bare.split()) >= 4:
                out.add(bare)
        return out

    old_blocks, new_blocks = _blocks(old_text), _blocks(new_text)
    smaller = min(len(old_blocks), len(new_blocks))
    if smaller < _UNRELATED_MIN_BLOCKS or len(old_blocks & new_blocks) / smaller >= _UNRELATED_MAX_SHARED:
        return []
    return ['The two documents have almost no paragraph in common. '
            'Check that they are two revisions of the same document.']


# A table's first row is used as column labels only when it looks like a header:
# enough columns, every cell filled, short, worded, and mostly without digits.
# Key/value forms and cartouches (two columns, values in the first row) are left
# positional — labelling them turns one changed value into a change on every row.
_HEADER_MIN_COLS = 3
_HEADER_MAX_CELL_WORDS = 6


def looks_like_table_header(cells: List[str]) -> bool:
    return (
        len(cells) >= _HEADER_MIN_COLS
        and all(any(ch.isalpha() for ch in c) for c in cells)
        and all(len(c.split()) <= _HEADER_MAX_CELL_WORDS for c in cells)
        and 2 * sum(any(ch.isdigit() for ch in c) for c in cells) < len(cells)
    )


def canonicalize(text: str) -> str:
    """Lowercase + strip modal verbs + normalize whitespace.
    Used only for comparison/scoring — never sent to the LLM."""
    text = text.lower()
    text = MODAL_RE.sub(' ', text)
    text = re.sub(r'[^a-z0-9%./ ]+', ' ', text)
    return re.sub(r'\s+', ' ', text).strip()


def is_substantive(old: str, new: str) -> bool:
    """True if the pair represents a genuine substantive change."""
    oc, nc = canonicalize(old), canonicalize(new)
    dist = 1.0 - difflib.SequenceMatcher(None, oc, nc).ratio()
    if dist >= _SEM_THRESHOLD:
        return True
    if set(NUMBER_RE.findall(old)) != set(NUMBER_RE.findall(new)):
        return True
    if set(PART_NUM_RE.findall(old)) != set(PART_NUM_RE.findall(new)):
        return True
    old_refs = {r.upper().replace(' ', '') for r in NORM_REF_RE.findall(old)}
    new_refs = {r.upper().replace(' ', '') for r in NORM_REF_RE.findall(new)}
    return old_refs != new_refs


def inline_word_diff(old_text: str, new_text: str) -> str:
    """Show word-level changes: ~~removed words~~ **added words**."""
    old_words = old_text.split()
    new_words = new_text.split()
    sm = difflib.SequenceMatcher(None, old_words, new_words, autojunk=False)
    out = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == 'equal':
            out.append(' '.join(old_words[i1:i2]))
        elif tag == 'replace':
            out.append(f"~~{' '.join(old_words[i1:i2])}~~ **{' '.join(new_words[j1:j2])}**")
        elif tag == 'delete':
            out.append(f"~~{' '.join(old_words[i1:i2])}~~")
        elif tag == 'insert':
            out.append(f"**{' '.join(new_words[j1:j2])}**")
    return ' '.join(out).strip()


def canonicalize_keep_modals(text: str) -> str:
    """Normalize text for comparison: keep modal verbs as distinct words.

    Unlike canonicalize(), modal verbs (shall/must/will/should/may/can/could) are
    preserved so the similarity scorer can detect them as differences.
    Within-group swaps (shall→must) surface as MODIFIED pairs; the LLM system prompt
    filters them via the EQUIVALENCES rule.
    """
    text = text.lower()
    text = re.sub(r'[^a-z0-9%./ ]+', ' ', text)
    return re.sub(r'\s+', ' ', text).strip()


_MANDATORY_MODALS = frozenset({'shall', 'must', 'will'})
_PERMISSIVE_MODALS = frozenset({'may', 'can', 'could'})


def _modal_group_profile(text: str) -> List[str]:
    """Map each modal verb to its obligation group: M(andatory)/P(ermissive)/A(dvisory).

    Sorted multiset — within-group swaps (shall→must) produce identical
    profiles, cross-group swaps (may→shall) differ.
    """
    groups = []
    for m in MODAL_RE.findall(text):
        m = m.lower()
        groups.append('M' if m in _MANDATORY_MODALS else 'P' if m in _PERMISSIVE_MODALS else 'A')
    return sorted(groups)


# Word-level net behind the character-distance gate below. That gate measures against the WHOLE paragraph, so a decisive
# word in a long paragraph ("shall record" -> "shall not record") would otherwise be lost.
# Here any added, removed or replaced word counts, except punctuation/case/hyphenation, articles, spelling variants
# and modal swaps inside one obligation group.
_WORD_LEVEL_CHECK = os.getenv('COMPARE_WORD_LEVEL_CHECK', 'true').lower() == 'true'
# Unicode words (accents kept) plus the comparison operators, which carry the meaning of a limit on their own, and the
# table cell separator ("Release | | X" vs "Release | | | X" is a column change).
_WORD_TOKEN_RE = re.compile(r'[^\W_]+|[<>≤≥=±|]')
_MODAL_TOKEN_GROUP: Dict[str, str] = {
    **{w: '\x00mandatory' for w in ('shall', 'must', 'will', 'doit', 'doivent', 'devra', 'devront')},
    **{w: '\x00permissive' for w in ('may', 'can', 'could', 'peut', 'peuvent', 'pourra', 'pourront')},
}
_TRIVIAL_TOKENS = frozenset({'the', 'a', 'an', 'of', 'le', 'la', 'les', 'l', 'un', 'une', 'de', 'du', 'des', 'd'})
# "colour"/"color", "organisation"/"organization", a plural: same word.
_SPELLING_VARIANT_RATIO = 0.85


def _word_tokens(text: str) -> List[str]:
    return [_MODAL_TOKEN_GROUP.get(w, w) for w in _WORD_TOKEN_RE.findall(text.lower())]


def _has_word_level_change(old: str, new: str) -> bool:
    old_words, new_words = _word_tokens(old), _word_tokens(new)
    if old_words == new_words:
        return False
    sm = difflib.SequenceMatcher(None, old_words, new_words, autojunk=False)
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == 'equal':
            continue
        o = ''.join(w for w in old_words[i1:i2] if w not in _TRIVIAL_TOKENS)
        n = ''.join(w for w in new_words[j1:j2] if w not in _TRIVIAL_TOKENS)
        if o == n:  # hyphenation / spacing / articles only
            continue
        if o and n and difflib.SequenceMatcher(None, o, n, autojunk=False).ratio() >= _SPELLING_VARIANT_RATIO:
            continue
        return True
    return False


def is_substantive_keep_modals(old: str, new: str) -> bool:
    """True if the pair is a genuine change, counting cross-group modal switches."""
    oc = canonicalize_keep_modals(old)
    nc = canonicalize_keep_modals(new)
    dist = 1.0 - difflib.SequenceMatcher(None, oc, nc).ratio()
    if dist >= _SEM_THRESHOLD:
        return True
    if _WORD_LEVEL_CHECK and _has_word_level_change(old, new):
        return True
    if set(NUMBER_RE.findall(old)) != set(NUMBER_RE.findall(new)):
        return True
    if set(PART_NUM_RE.findall(old)) != set(PART_NUM_RE.findall(new)):
        return True
    # Cross-group modal switch (e.g. may→shall in a long paragraph) changes the
    # obligation level even when the character distance is tiny — never drop it.
    if _modal_group_profile(old) != _modal_group_profile(new):
        return True
    old_refs = {r.upper().replace(' ', '') for r in NORM_REF_RE.findall(old)}
    new_refs = {r.upper().replace(' ', '') for r in NORM_REF_RE.findall(new)}
    return old_refs != new_refs


_DIFF_ENTRY_RE = re.compile(r'^(MODIFIED|ADDED|REMOVED|RELOCATED)(?:\s+\[([^\]]*)\])?:\s?(.*)$', re.S)
