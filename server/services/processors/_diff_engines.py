"""Shared diff primitives: text alignment engines, image hashing, and LLM system prompts.

Three text diff engines:
  paragraph_semantic_diff  — primary engine: (Continued) pre-merge, threshold 0.55, modal-aware
  section_canonical_diff   — alternative engine: unified diff grouped by section heading
  legacy_paragraph_diff    — older engine kept for reference (not used by active processors)

System prompts:
  SYSTEM_PROMPT_STANDARD   — standard method: focused Markdown bullet-list output
  SYSTEM_PROMPT_STRUCTURED — structured method: strict JSON array output for Excel export

Never imported by compare.py or the router layer.
"""

import base64
import difflib
import hashlib
import io
import logging
import os
import re
from collections import Counter, defaultdict
from typing import Any, Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_DHASH_SIZE = 8
_DHASH_THRESHOLD = 4           # Hamming distance <= 4 => visually identical
# Second-tier identity check, a 16x16 dhash: small edits to a large diagram (added arrow, changed label) can stay
# within the coarse 8x8 hash budget, while the fine hash still tolerates JPEG recompression.
_DHASH16_SIZE = 16
_DHASH16_THRESHOLD = 12
# Word similarity above which two unmatched paragraphs pair as one MODIFIED entry (below: REMOVED + ADDED). Tunable
# for eval sweeps.
_PAIR_RATIO_THRESHOLD = float(os.getenv('COMPARE_PAIR_RATIO_THRESHOLD', '0.55'))
_SEM_THRESHOLD = 0.05          # canonical distance threshold for is_substantive
_PAIR_PAGE_TOLERANCE  = 15     # max page distance to pair modified images (PDF/PPTX)
_PAIR_MAX_HAMMING     = 25     # max dhash distance to accept a pair as MODIFIED
_PAIR_MAX_AHASH       = 15     # max ahash distance — second gate for DOCX pairing (content check)
_IMG_MAX_DIM = 1024
_IMG_JPEG_QUALITY = 65
_MAX_IMAGE_BLOCKS = 95          # Claude API hard limit is 100 images/documents per request
_MAX_DIFF_CHARS = int(os.getenv('COMPARE_MAX_DIFF_CHARS', '600000'))  # ~150K tokens

# ---------------------------------------------------------------------------
# Regexes
# ---------------------------------------------------------------------------

NUMBER_RE = re.compile(r'\b\d+(?:[.,]\d+)*(?:\s*%|\s*mm|\s*in(?:ch)?|\s*kg|\s*psi)?\b')
PART_NUM_RE = re.compile(r'\d{4,}')  # long digit runs in alphanumeric part numbers (no word-boundary)
NORM_REF_RE = re.compile(
    r'\b(?:ISO|IEC|ASTM|SAE|NAS|EN|AMS|AS|ANSI|BS|DIN|MIL|BSS|BAC|BMS|OSHA|NIST)[- ]?\d[\w\-.]*',
    re.I,
)
MODAL_RE = re.compile(r'\b(?:shall|must|will|should|may|can|could|would)\b', re.I)
# The bare ALL-CAPS branch accepts the punctuation real headings carry (dashes, colon, parens, ampersand, digits): a
# letters-and-spaces-only pattern missed titles like "ANNEX C – NATIONAL AEROSPACE NONDESTRUCTIVE TESTING BOARDS
# (NANDTB)", so their paragraphs inherited the previous heading.
SECTION_RE = re.compile(r"^(?:\d+(?:\.\d+)*\.?\s+[A-Z]|[A-Z][A-Z0-9 \-–—:,./()&']{3,}$)")
# The bare ALL-CAPS half of SECTION_RE, used to spot running headers/footers (company name, standard title, address)
# that recur on every page and would each register as a new section.
_ALLCAPS_SECTION_RE = re.compile(r"^[A-Z][A-Z0-9 \-–—:,./()&']{3,}$")
# Trailing "N/M" page fraction of a running footer stamp ("TRA-0034 - V09 4/39"): masked only when counting heading
# repeats, so each page's stamp counts as the same label.
_TRAILING_PAGE_FRACTION_RE = re.compile(r'\s+\d+\s*/\s*\d+$')

# Matches trailing structural tags: [Page 3], [Para 7], [Page 3, Para 12], [Heading 2], [Slide 4], [Item 12]
_TAG_RE = re.compile(r'\s*\[(?:Page|Para|Heading|Slide|Item)[^\]]*\].*$', re.I)

# ---------------------------------------------------------------------------
# Text primitives
# ---------------------------------------------------------------------------

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


def is_meaningful_change(old_txt: str, new_txt: str) -> bool:
    """Filter lines trivially identical after stripping trailing TOC page numbers."""
    o_strip = re.sub(r'[\.\s]+\d+\s*$', '', old_txt).strip()
    n_strip = re.sub(r'[\.\s]+\d+\s*$', '', new_txt).strip()
    if o_strip and o_strip == n_strip:
        return False
    if old_txt.strip() == new_txt.strip():
        return False
    return is_substantive(old_txt, new_txt)


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


# ---------------------------------------------------------------------------
# Perceptual image hashing (dhash)
# ---------------------------------------------------------------------------

def dhash(img_bytes: bytes, size: int = _DHASH_SIZE) -> str:
    """64-bit difference hash. Resistant to slight compression/resize artifacts.
    Falls back to MD5 hex if PIL is unavailable."""
    try:
        from PIL import Image as PILImage
        img = PILImage.open(io.BytesIO(img_bytes)).convert('L')
        img = img.resize((size + 1, size), PILImage.Resampling.LANCZOS)
        px = list(img.tobytes())  # mode 'L': one byte per pixel
        w = size + 1
        bits = []
        for r in range(size):
            row = px[r * w:(r + 1) * w]
            for c in range(size):
                bits.append('1' if row[c] > row[c + 1] else '0')
        return ''.join(bits)
    except Exception:
        return hashlib.md5(img_bytes).hexdigest()


def ahash(img_bytes: bytes, size: int = _DHASH_SIZE) -> str:
    """64-bit average hash. Insensitive to small shifts; sensitive to content changes.
    Used as a second gate alongside dhash for DOCX image pairing."""
    try:
        from PIL import Image as PILImage
        img = PILImage.open(io.BytesIO(img_bytes)).convert('L')
        img = img.resize((size, size), PILImage.Resampling.LANCZOS)
        px = list(img.tobytes())  # mode 'L': one byte per pixel
        avg = sum(px) / len(px)
        return ''.join('1' if p >= avg else '0' for p in px)
    except Exception:
        return hashlib.md5(img_bytes).hexdigest()


def hamming(a: str, b: str) -> int:
    return sum(x != y for x, y in zip(a, b))


def images_are_similar(h1: str, h2: str) -> bool:
    if len(h1) != len(h2):
        return h1 == h2
    return hamming(h1, h2) <= _DHASH_THRESHOLD


def hash16(img_bytes: bytes) -> str:
    """256-bit fine-grained difference hash (16x16) for the second-tier
    identity check. Falls back to MD5 hex like dhash()."""
    return dhash(img_bytes, size=_DHASH16_SIZE)


def _fine_hashes_match(meta1: Dict[str, Any], meta2: Dict[str, Any]) -> bool:
    """Second-tier identity: compare 16x16 hashes when both sides carry one.

    Missing hashes (old cached extractions, PIL fallback) keep the legacy
    coarse-only behaviour.
    """
    f1 = meta1.get('hash16')
    f2 = meta2.get('hash16')
    if not f1 or not f2:
        return True
    if len(f1) != len(f2):
        return f1 == f2
    return hamming(f1, f2) <= _DHASH16_THRESHOLD


def _thumbnail_b64(b64_str: str, max_px: int = 320) -> str:
    """Resize a base64 JPEG to max_px on its longest side and return new base64."""
    try:
        from PIL import Image as PILImage
        img = PILImage.open(io.BytesIO(base64.b64decode(b64_str))).convert('RGB')
        img.thumbnail((max_px, max_px), PILImage.Resampling.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format='JPEG', quality=70)
        return base64.b64encode(buf.getvalue()).decode('ascii')
    except Exception:
        return b64_str


def _compute_image_diff_pairs(
    old_imgs: Dict[str, Dict[str, Any]],
    new_imgs: Dict[str, Dict[str, Any]],
    show_position: bool,
) -> Tuple[List[Tuple[str, str]], List[Tuple[str, str]], List[str], List[str]]:
    """Core pairing logic shared by image_diff_blocks_dual and image_diff_pairs.

    Returns (modified_pairs, orientation_pairs, remaining_removed, remaining_added).
    """
    def _same_content(h1: str, imgs1: Dict, h2: str, imgs2: Dict) -> bool:
        if not images_are_similar(h1, h2):
            return False
        if not _fine_hashes_match(imgs1[h1], imgs2[h2]):
            return False  # subtly edited diagram — report as MODIFIED, not "unchanged"
        t1 = imgs1[h1].get('transform')
        t2 = imgs2[h2].get('transform')
        if t1 is not None and t2 is not None:
            return t1 == t2
        return True

    removed = [h for h in old_imgs if not any(_same_content(h, old_imgs, nh, new_imgs) for nh in new_imgs)]
    added   = [h for h in new_imgs if not any(_same_content(oh, old_imgs, h, new_imgs) for oh in old_imgs)]

    # ── Orientation-change detection (same raw image, flipped or rotated) ──
    # Only images without a content match on the other side are considered: when BOTH documents hold an image and its
    # flip, each already matches its own twin and must not be cross-paired.
    orientation_pairs: List[Tuple[str, str]] = []
    matched_orient_old: set = set()
    matched_orient_new: set = set()
    for oh in removed:
        ov = old_imgs[oh]
        rh = ov.get('raw_hash')
        t_old = ov.get('transform')
        if rh is None or t_old is None:
            continue
        for nh in added:
            nv = new_imgs[nh]
            if (nh not in matched_orient_new
                    and nv.get('raw_hash') == rh
                    and nv.get('transform') != t_old
                    and oh not in matched_orient_old):
                orientation_pairs.append((oh, nh))
                matched_orient_old.add(oh)
                matched_orient_new.add(nh)
                break

    removed = [h for h in removed if h not in matched_orient_old]
    added   = [h for h in added   if h not in matched_orient_new]

    modified_pairs: List[Tuple[str, str]] = []
    remaining_removed: List[str] = []

    if show_position:
        # Adaptive page tolerance: _PAIR_PAGE_TOLERANCE (15) means "anywhere" in a short deck, so it is scaled down
        # for small documents.
        max_page = 0
        for d in (old_imgs, new_imgs):
            for v in d.values():
                pages = v.get('pages') or []
                if pages:
                    max_page = max(max_page, max(pages))
        page_tol = min(_PAIR_PAGE_TOLERANCE, max(3, max_page // 3))

        # ahash for the content gate below: two images can fall within the dhash budget by coincidence while being
        # visibly unrelated.
        old_ahash_map = {h: ahash(base64.b64decode(old_imgs[h]['b64'])) for h in removed}
        new_ahash_map = {h: ahash(base64.b64decode(new_imgs[h]['b64'])) for h in added}

        removed_sorted = sorted(removed, key=lambda x: old_imgs[x]['pages'][0])
        added_sorted   = sorted(added,   key=lambda x: new_imgs[x]['pages'][0])
        for rh in removed_sorted:
            r_page = old_imgs[rh]['pages'][0]
            paired = None
            for ah in added_sorted:
                if abs(r_page - new_imgs[ah]['pages'][0]) > page_tol:
                    continue
                if len(rh) == len(ah) and hamming(rh, ah) > _PAIR_MAX_HAMMING:
                    continue
                # Second gate: reject candidates whose average-hash distance is too large even if their dhash passed.
                if rh in old_ahash_map and ah in new_ahash_map:
                    if hamming(old_ahash_map[rh], new_ahash_map[ah]) > _PAIR_MAX_AHASH:
                        continue
                paired = ah
                break
            if paired:
                modified_pairs.append((rh, paired))
                added_sorted.remove(paired)
            else:
                remaining_removed.append(rh)
        added = added_sorted

        # Final fallback: exactly ONE leftover REMOVED and ONE leftover ADDED on the same page is almost always a
        # figure redrawn in place; pair them so the LLM compares OLD/NEW side by side.
        removed_by_page: Dict[int, List[str]] = {}
        for rh in remaining_removed:
            removed_by_page.setdefault(old_imgs[rh]['pages'][0], []).append(rh)
        added_by_page: Dict[int, List[str]] = {}
        for ah in added:
            added_by_page.setdefault(new_imgs[ah]['pages'][0], []).append(ah)
        for page, rlist in removed_by_page.items():
            alist = added_by_page.get(page, [])
            if len(rlist) == 1 and len(alist) == 1:
                modified_pairs.append((rlist[0], alist[0]))
                remaining_removed.remove(rlist[0])
                added.remove(alist[0])
    else:
        old_ahash = {h: ahash(base64.b64decode(old_imgs[h]['b64'])) for h in removed}
        new_ahash = {h: ahash(base64.b64decode(new_imgs[h]['b64'])) for h in added}

        sims: List[Tuple[int, str, str]] = []
        for rh in removed:
            for ah in added:
                if len(rh) == len(ah):
                    sims.append((hamming(rh, ah), rh, ah))
        sims.sort(key=lambda x: x[0])
        matched_r: set = set()
        matched_a: set = set()
        for dist, rh, ah in sims:
            if dist > _PAIR_MAX_HAMMING:
                break
            if rh in matched_r or ah in matched_a:
                continue
            ah_dist = hamming(old_ahash[rh], new_ahash[ah])
            if ah_dist > _PAIR_MAX_AHASH:
                continue
            modified_pairs.append((rh, ah))
            matched_r.add(rh)
            matched_a.add(ah)
        remaining_removed = [h for h in removed if h not in matched_r]
        added = [h for h in added if h not in matched_a]

    return modified_pairs, orientation_pairs, remaining_removed, added


# ---------------------------------------------------------------------------
# Image diff blocks (dual — OLD / NEW side-by-side with page-proximity pairing)
# ---------------------------------------------------------------------------

def image_diff_blocks_dual(
    old_imgs: Dict[str, Dict[str, Any]],
    new_imgs: Dict[str, Dict[str, Any]],
    show_position: bool = True,
) -> List[Dict[str, Any]]:
    """Build LLM content blocks for image changes.

    show_position=True (PDF/PPTX/DOCX): pairs unmatched images by page
      proximity (adaptive tolerance) + hamming distance (≤25). Page numbers
      are real for PDF/PPTX and content-flow estimates for DOCX (same
      estimator as the text [Page N] tags).

    show_position=False: pairs unmatched images by visual similarity only
      (best hamming match, no position constraint).

    Returns MODIFIED (OLD→NEW side-by-side), then REMOVED, then ADDED blocks.
    """
    modified_pairs, orientation_pairs, removed, added = _compute_image_diff_pairs(
        old_imgs, new_imgs, show_position,
    )

    if not removed and not added and not modified_pairs and not orientation_pairs:
        return [{'type': 'text', 'text': '\nNo embedded images added, removed, or modified.'}]

    blocks: List[Dict[str, Any]] = []

    if orientation_pairs:
        blocks.append({'type': 'text', 'text': f'\n{len(orientation_pairs)} image(s) ORIENTATION CHANGED (flipped/rotated):'})
        for rh, ah in orientation_pairs:
            t_old = old_imgs[rh].get('transform', (False, False))
            t_new = new_imgs[ah].get('transform', (False, False))
            flip_desc = (
                f'flipH: {t_old[0]} → {t_new[0]}'
                + (f', flipV: {t_old[1]} → {t_new[1]}' if t_old[1] != t_new[1] else '')
            )
            blocks += [
                {'type': 'text', 'text': f'{flip_desc} | IMAGE (new orientation):'},
                {'type': 'image_url', 'image_url': {'url': f"data:image/jpeg;base64,{new_imgs[ah]['b64']}"}},
            ]

    if modified_pairs:
        blocks.append({'type': 'text', 'text': f'\n{len(modified_pairs)} image(s) MODIFIED (OLD → NEW):'})
        for rh, ah in modified_pairs:
            dist = hamming(rh, ah) if len(rh) == len(ah) else -1
            score = f'hamming={dist}/64'
            if show_position:
                r_page = old_imgs[rh]['pages'][0]
                a_page = new_imgs[ah]['pages'][0]
                label = f'Page {r_page} → {a_page} | {score} | OLD IMAGE:'
            else:
                label = f'{score} | OLD IMAGE:'
            blocks += [
                {'type': 'text', 'text': label},
                {'type': 'image_url', 'image_url': {'url': f"data:image/jpeg;base64,{old_imgs[rh]['b64']}"}},
                {'type': 'text', 'text': 'NEW IMAGE:'},
                {'type': 'image_url', 'image_url': {'url': f"data:image/jpeg;base64,{new_imgs[ah]['b64']}"}},
            ]

    for h in removed:
        label = f"REMOVED image (page {old_imgs[h]['pages'][0]}):" if show_position else 'REMOVED image:'
        blocks += [
            {'type': 'text', 'text': label},
            {'type': 'image_url', 'image_url': {'url': f"data:image/jpeg;base64,{old_imgs[h]['b64']}"}},
        ]

    for h in added:
        label = f"ADDED image (page {new_imgs[h]['pages'][0]}):" if show_position else 'ADDED image:'
        blocks += [
            {'type': 'text', 'text': label},
            {'type': 'image_url', 'image_url': {'url': f"data:image/jpeg;base64,{new_imgs[h]['b64']}"}},
        ]

    # Cap to the API limit of 100 images per request
    img_count = sum(1 for b in blocks if b.get('type') == 'image_url')
    if img_count > _MAX_IMAGE_BLOCKS:
        capped: List[Dict[str, Any]] = []
        seen = 0
        for b in blocks:
            if b.get('type') == 'image_url':
                if seen >= _MAX_IMAGE_BLOCKS:
                    continue
                seen += 1
            capped.append(b)
        capped.append({
            'type': 'text',
            'text': f'\n[{img_count - _MAX_IMAGE_BLOCKS} additional image(s) omitted — API limit of {_MAX_IMAGE_BLOCKS} images per request reached.]',
        })
        logger.warning('Image blocks capped: %d → %d', img_count, _MAX_IMAGE_BLOCKS)
        blocks = capped

    return blocks


def image_diff_pairs(
    old_imgs: Dict[str, Dict[str, Any]],
    new_imgs: Dict[str, Dict[str, Any]],
    show_position: bool = True,
) -> List[Dict[str, Any]]:
    """Build a list of image pairs for SSE streaming and Excel export.

    Each entry: {status, old_page, new_page, old_b64, new_b64, index}
    Images are thumbnailed to ≤320 px to keep SSE payload small.
    """
    modified_pairs, orientation_pairs, removed, added = _compute_image_diff_pairs(
        old_imgs, new_imgs, show_position,
    )

    pairs: List[Dict[str, Any]] = []
    idx = 0

    for rh, ah in orientation_pairs:
        pairs.append({
            'status': 'orientation_changed',
            'old_page': old_imgs[rh]['pages'][0] if show_position else None,
            'new_page': new_imgs[ah]['pages'][0] if show_position else None,
            'old_b64': _thumbnail_b64(old_imgs[rh]['b64']),
            'new_b64': _thumbnail_b64(new_imgs[ah]['b64']),
            'index': idx,
        })
        idx += 1

    for rh, ah in modified_pairs:
        pairs.append({
            'status': 'modified',
            'old_page': old_imgs[rh]['pages'][0] if show_position else None,
            'new_page': new_imgs[ah]['pages'][0] if show_position else None,
            'old_b64': _thumbnail_b64(old_imgs[rh]['b64']),
            'new_b64': _thumbnail_b64(new_imgs[ah]['b64']),
            'index': idx,
        })
        idx += 1

    for h in removed:
        pairs.append({
            'status': 'removed',
            'old_page': old_imgs[h]['pages'][0] if show_position else None,
            'new_page': None,
            'old_b64': _thumbnail_b64(old_imgs[h]['b64']),
            'new_b64': None,
            'index': idx,
        })
        idx += 1

    for h in added:
        pairs.append({
            'status': 'added',
            'old_page': None,
            'new_page': new_imgs[h]['pages'][0] if show_position else None,
            'old_b64': None,
            'new_b64': _thumbnail_b64(new_imgs[h]['b64']),
            'index': idx,
        })
        idx += 1

    return pairs


# ---------------------------------------------------------------------------
# Legacy paragraph diff (lower threshold, kept for reference)
# ---------------------------------------------------------------------------

def legacy_paragraph_diff(old_text: str, new_text: str) -> Tuple[str, int]:
    """Paragraph-level semantic alignment — legacy engine.

    Uses a lower similarity threshold (0.40) and minimum 2 words per block.
    Superseded by paragraph_semantic_diff (threshold 0.55, min 3 words, modal-aware).

    Returns:
        (diff_string, filtered_count)
    """

    def _clean(s: str) -> str:
        s = re.sub(r'[\.\-_]{2,}', ' ', s)
        return re.sub(r'\s+', ' ', s).strip().lower()

    def _parse(text: str) -> List[Dict[str, Any]]:
        result: List[Dict[str, Any]] = []
        cur_sec = 'Preamble'
        for idx, block in enumerate(text.splitlines()):
            bare = strip_tag(block)
            is_toc = bool(re.search(r'\.{3,}\s*\d+$', bare))
            if SECTION_RE.match(bare) and 2 < len(bare) < 100 and not is_toc:
                cur_sec = re.sub(
                    r'\s*\((?:Continued|Suite|Cont\.)\)\s*$', '', bare, flags=re.I
                ).strip() or bare
            if bare.strip():
                result.append({'idx': idx, 'sec': cur_sec, 'txt': bare, 'clean': _clean(bare)})
        return result

    old_items = _parse(old_text)
    new_items = _parse(new_text)

    matched_new: set = set()
    unmatched_old: List[Dict[str, Any]] = []
    for o in old_items:
        matched_idx = next(
            (n['idx'] for n in new_items
             if n['clean'] == o['clean'] and n['idx'] not in matched_new),
            None,
        )
        if matched_idx is not None:
            matched_new.add(matched_idx)
        else:
            unmatched_old.append(o)

    new_leftovers = [n for n in new_items if n['idx'] not in matched_new]

    sims: List[Tuple[float, int, int]] = []
    for i, o in enumerate(unmatched_old):
        o_words = o['clean'].split()
        if len(o_words) < 2:
            continue
        for j, n in enumerate(new_leftovers):
            n_words = n['clean'].split()
            ratio = difflib.SequenceMatcher(None, o_words, n_words, autojunk=False).ratio()
            if ratio > 0.40:
                sims.append((ratio, i, j))

    sims.sort(key=lambda x: x[0], reverse=True)
    matched_o: set = set()
    matched_n: set = set()
    results: List[Dict[str, Any]] = []
    filtered = 0

    for ratio, i, j in sims:
        if i in matched_o or j in matched_n:
            continue
        matched_o.add(i)
        matched_n.add(j)
        o = unmatched_old[i]
        n = new_leftovers[j]
        if is_meaningful_change(o['txt'], n['txt']):
            diff_str = inline_word_diff(o['txt'], n['txt'])
            results.append({'sec': o['sec'], 'content': f'MODIFIED: {diff_str}', 'sort_idx': o['idx']})
        else:
            filtered += 1

    for i, o in enumerate(unmatched_old):
        if i in matched_o:
            continue
        if '(Continued)' in o['txt'] and SECTION_RE.match(o['txt']):
            filtered += 1
        else:
            results.append({'sec': o['sec'], 'content': f"REMOVED: {o['txt']}", 'sort_idx': o['idx']})

    for j, n in enumerate(new_leftovers):
        if j in matched_n:
            continue
        if '(Continued)' in n['txt'] and SECTION_RE.match(n['txt']):
            filtered += 1
        else:
            results.append({'sec': n['sec'], 'content': f"ADDED: {n['txt']}", 'sort_idx': n['idx'] + 1_000_000})

    sections_order: Dict[str, int] = {}
    idx_sec = 0
    for item in old_items + new_items:
        if item['sec'] not in sections_order:
            sections_order[item['sec']] = idx_sec
            idx_sec += 1

    results.sort(key=lambda x: (sections_order.get(x['sec'], 999_999), x['sort_idx']))

    out: List[str] = []
    cur_sec = ''
    for r in results:
        if r['sec'] != cur_sec:
            out.append(f"\n## {r['sec']}")
            cur_sec = r['sec']
        out.append(r['content'])

    return '\n'.join(out), filtered


# ---------------------------------------------------------------------------
# System prompts
# ---------------------------------------------------------------------------

SYSTEM_PROMPT_STANDARD = """\
You are an expert document comparison analyst specialising in technical, regulatory, and quality-management documentation.

Your goal is a HIGH-SIGNAL report: every significant change, nothing trivial.

DIFF FORMAT:
The diff is provided paragraph-by-paragraph:
- ADDED / REMOVED: text completely new or gone.
- MODIFIED: ~~strikethrough~~ = old text; **bold** = new text; plain = unchanged context.
  **Bold** is a replacement, not independent new content.
- RELOCATED: verified BY EXACT TEXT MATCH (not your judgment) to still exist elsewhere in the
  new document — moved or restructured, not deleted. NEVER report this as a lost requirement;
  skip it, or note the renumbering only if the section reference itself matters to the reader.

MODAL VERB EQUIVALENCES — do NOT report swaps within the same group:
  Mandatory group: shall = must = will (all express the same obligation level)
  Permissive group: may = can = could (all express the same permission level)
Only report a modal change when it CROSSES groups:
  e.g. 'can' changed to 'shall' (permission becomes requirement) — REPORT
  e.g. 'shall' changed to 'may' (requirement becomes optional) — REPORT
  e.g. 'must' changed to 'shall' — SKIP (same group)

REPORT:
- Requirements or obligations added, deleted, or substantively changed
- Numerical values that changed (tolerances, limits, thresholds, quantities, durations)
- Normative references added, removed, or cited at a different revision
- Scope changes: applicability, exceptions added or removed
- Changed procedures, test methods, inspection steps, safety notes
- Modal verb changes that cross groups (permissive ↔ mandatory)
- Visual changes: images, figures, diagrams, or charts that were added, removed, or modified

SKIP ENTIRELY:
- shall ↔ must ↔ will swaps (same obligation level)
- may ↔ can ↔ could swaps (same permission level)
- Synonym substitutions with no change in obligation ('correct' = 'proper', 'ensure' = 'make sure', etc.)
- Number rendering: digits vs spelled-out with the SAME value ('24 months' = 'twenty-four (24) months', '2 years' = 'two (2) years'). Report a value change ONLY when the magnitude or unit actually differs.
- Word-order swaps with unchanged meaning ('practical and specific' = 'specific and practical')
- Consolidation or restructuring that preserves all requirements unchanged
- Table or heading label changes with no requirement impact
- Reference code formatting only (NAS 410 vs NAS410 when same revision)
- Document metadata: dates (issue/revision date), revision tables, approval pages, boilerplate

LANGUAGE: write the entire report — headings, bullets, and the final
"no significant changes" line if it applies — in the SAME language as the
document being compared. Do not switch to English when the document is not
in English.

OUTPUT — Markdown, one ## heading per section, one bullet (`- `) per change. Bold critical values.
Emit sections in DOCUMENT ORDER (top to bottom, following the section numbering / page order of the new document), not grouped by change type or criticality.
Apply these Markdown formatting rules so the report renders cleanly and never overflows the page:
- Leave one blank line before and after every `##` heading and every bullet list.
- One change per bullet, on a single line. Keep each bullet to one concise sentence;
  do not paste a whole source paragraph — quote only the part that changed.
- Do NOT keep long unbroken strings (full paths, long IDs with no spaces). Quote the
  meaningful fragment instead (e.g. `drawing 7053-06`, not the entire 60-character path).
- Use plain Markdown only — `##`, `- `, `**bold**`, `~~strikethrough~~`. No HTML, no tables, no code fences.
If nothing substantive changed overall: **No significant changes detected.**\
"""

SYSTEM_PROMPT_STRUCTURED = """\
You are a technical change impact analyst. Output ONLY valid JSON — no prose, no markdown, no code fences.

OUTPUT FORMAT — a JSON array, one object per atomic change:

[
  {
    "section": "<section title from document>",
    "page": "<number from [Page N] or [Slide N] annotation in the diff, e.g. \"3\" or \"3→5\" for MODIFIED (old→new); null if unavailable>",
    "type": "<TYPE — see list below>",
    "criticality": "High|Medium|Low",
    "before": "<what this said in the OLD version — a self-contained, readable phrase; \"--\" if the content is new>",
    "after":  "<what it says in the NEW version — a self-contained, readable phrase; \"--\" if the content was removed>",
    "rationale": "<one short sentence explaining what the change actually consists of, in plain terms — not an instruction; \"\" when the change is trivial/cosmetic>"
  }
]

TYPES — copy verbatim into the "type" field:
  Value changed        -- tolerance, limit, threshold, quantity, or duration changed
  Reference updated    -- normative document added, removed, or different revision cited
  Requirement modified -- existing obligation or specification rephrased with a meaning change
  Requirement added    -- new content (obligation, specification, note, warning, or definition) with no predecessor
  Requirement removed  -- existing content entirely eliminated with no replacement
  Procedure changed    -- process step, test method, inspection sequence, or criterion changed
  Scope changed        -- applicability, exception, inclusion, or exclusion changed
  Modal change         -- obligation level crossed between permission and mandatory
  Visual change        -- an embedded image, figure, diagram, chart, or illustration was added, removed, or visually modified
  Editorial/Structural -- section renumbering, text relocation, or Table of Contents (TOC) updates

CRITICALITY — evaluate in order; assign the FIRST level whose criteria are met:

  HIGH — stop here if ANY of the following applies:
    • A numeric value changed: tolerance, limit, threshold, quantity, or duration
    • A requirement was REMOVED (Note: relocating content or updating a TOC is NOT a removal)
    • Obligation degraded: shall/must/will → may/can/could (mandatory became optional)
    • Obligation raised: may/can/could → shall/must/will (optional became mandatory)
    • Scope NARROWED: fewer items covered, exception added, applicability restricted
    • A safety or airworthiness normative document changed revision or was removed
    • A normative reference changed revision, was removed, or newly added
    • A safety-critical figure, diagram, or technical illustration was modified or removed

  MEDIUM — use if NO High criterion applies, and ANY of the following applies:
    • A procedure or test method step changed (how work is performed)
    • A requirement was MODIFIED: meaning changed, obligation level unchanged
    • Scope EXPANDED: more items covered, exception removed
    • A new requirement, specification, or procedure added with no predecessor
    • A figure, diagram, or illustration was modified in a way that affects how work is performed

  LOW — use for everything else:
    • Structural changes: renumbering, content relocation, or Table of Contents (TOC) updates
    • Change adds clarity, minor detail, or editorial wording with limited compliance risk
    • A decorative or cosmetic image changed with no technical impact
    • ALWAYS use Low when in doubt — include the row, do not omit it

DIFF FORMAT:
  ADDED / REMOVED: text completely new or gone.
  MODIFIED: ~~strikethrough~~ = old text; **bold** = new text; plain = unchanged context.
  A MODIFIED block is ONE evolved element — do NOT split into separate added + removed rows.
  RELOCATED: verified BY EXACT TEXT MATCH (not your judgment) to still exist elsewhere in the
  new document — moved or restructured, not deleted. NEVER type this as "Requirement removed"
  and NEVER use High criticality for it. OMIT the row entirely: text that merely moved
  is not a change the reader needs. Emit a row only if the relocation itself carries a
  compliance consequence (a requirement left the scope of the chapter that governs it),
  and then say what that consequence is.

RULE FOR ADDED BLOCKS: An ADDED block is entirely new content. ALWAYS create a row for it
  unless it is clearly document metadata (see SKIP below). Use "before": "--".
  Default type: "Requirement added". Adjust to a more specific type if the content clearly
  fits (e.g. "Reference updated", "Scope changed", or "Editorial/Structural" for relocated text).
  Do NOT skip an ADDED block just because it seems like a minor addition — use Low criticality.

RULE FOR REMOVED BLOCKS: A REMOVED block is content that no longer exists. ALWAYS create a
  row for it unless it is clearly document metadata. Use "after": "--".
  Default type: "Requirement removed". High criticality ONLY if an actual obligation or specification was permanently eliminated, NOT if a section was just moved or a TOC updated.

RULE FOR VISUAL CHANGES (images in the diff): The section "--- VISUAL CHANGES ---" contains
  embedded images from the document. Examine each image carefully and compare OLD vs NEW.
  Emit ONE ROW PER VISIBLE DIFFERENCE, not one row per image: an org chart that gains a
  box, renames another and splits a site is THREE rows. Summarising a redrawn figure in a
  single vague row is a failure — it is the one case where the reader cannot check for
  themselves.
    - type: "Visual change"
    - section: the nearest document section title, or "Figures" if unknown
    - before: the specific element as it appeared in OLD — read and quote the box label,
              node name, person, connector or value that differs; "--" if newly added
    - after: the same element as it appears in NEW; "--" if removed
    - criticality: High if it is a safety diagram or changes technical specifications;
                   Medium if it affects a procedure, sequence diagram, or technical drawing;
                   Low if cosmetic or decorative only
    - rationale: one short sentence explaining what changed in the image; "" if cosmetic/decorative only
  Name elements, never the figure as a whole:
    Good: before: "branche « Responsable de Production Partie 21G — Directeur des Achats »
                   (Renaud DURAND)"
          after:  "branche « Responsable de Production Partie 21G — Vice président des
                   Achats » (Renaud DURAND)"
    Bad:  before: "organigramme avec les responsables production, qualité et sécurité"
          after:  "organigramme mis à jour"
  RENAME vs ADDITION - check before you claim a rename: if the OLD element is
  STILL PRESENT in the new image, the new element is an ADDITION, not a rename.
  Count the boxes/nodes on each side first. On MOP_AX the model reported
  "Responsable de la Gestion de la Securite renamed to Responsable de la
  Surveillance de la Conformite a la Partie IS" while the new chart clearly shows
  BOTH boxes (7 boxes -> 8) - a fabricated rename that hides a real new function.

  NEVER emit a row saying an element is unchanged ("still present", "no change on
  this site"). A row exists only for a difference.

  Up to 40 words per field here — reading a diagram takes more words than quoting text.
  Do NOT skip visual changes. If no images changed, skip this rule.

IMPORTANT — VML shape metadata: Lines like `[Arrow "id" left: ... top: ...]` and
  `[Line "id" ...]` in the TEXT CHANGES section are VML connector/arrow position metadata,
  NOT embedded images. If such a line changed, report it as "Procedure changed" or
  "Editorial/Structural" — NEVER as "Visual change". The "Visual change" type is reserved
  exclusively for actual images shown in the "--- VISUAL CHANGES ---" section.

EQUIVALENCES — never create rows for swaps within:
  Obligation synonyms: shall = must = will = are to be = are required to
  Permission synonyms: may = can = could = is permitted to
  DO report obligation <-> permission crossings (type: Modal change, High).

SKIP entirely:
  - Pure formatting: whitespace, capitalisation, punctuation with no meaning change
  - Obligation synonym swaps (shall / must / will)
  - Permission synonym swaps (may / can / could)
  - Document metadata ONLY: revision history tables, approval/signature blocks, document
    issue dates, page numbers, headers/footers with no technical content
  - Reference formatting only: NAS 410 vs NAS410 = same document, same revision

SAME-VALUE / SAME-MEANING — these are NOT changes; SKIP entirely (output no row):
  - NUMERIC RENDERING: a number written in digits vs spelled out is the SAME value
    when magnitude and unit are unchanged. Report "Value changed" ONLY when the
    magnitude or unit actually differs (e.g. 240 -> 200 hours, or hours -> days).
      SKIP: "240 hours" vs "two hundred forty (240) hours"
      SKIP: "24 months" vs "twenty-four (24) months"
      SKIP: "up to 2 years" vs "up to two (2) years"
      SKIP: "8 tasks" vs "eight (8) tasks"
  - NUMBER PUNCTUATION: "3-4 year" vs "3-4 year" (hyphen vs en-dash) = same.
  - DOCUMENT DATES: issue date, revision date, "REVISION DATE: ..." = metadata, not
    technical content. SKIP even when the date value itself changed.
  - SYNONYM / WORD-ORDER with unchanged meaning: "this standard" vs "this document",
    "practical and specific" vs "specific and practical". SKIP.

RESTRUCTURED TABLE - do not compare across columns that no longer mean the same:
  A table row is diffed cell by position. If the table's HEADER row also changed,
  position N in the old table and position N in the new one are different columns,
  and reporting "86 -> 0" for them is false. When you see a header change for the
  same table, say in the row that the columns were redefined and give the new
  reading, instead of presenting a value change that did not happen.
    Good: "colonne 3 redefinie: 'Production' (86) devient 'Effectif en sous-traitance
           APRSeur' (0) - les deux chiffres ne mesurent pas la meme chose"
    Bad:  "production 86 -> 0"

NEVER EMIT A ROW FOR - these are layout, and the reader does not care:
  - A page number, page count, footer, header, or revision stamp. A row whose substance
    is "PAGE 1/38" or "Indice : AX" is always wrong, whatever its criticality.
  - Reordering with unchanged substance. If the same items are simply listed in another
    order, or moved to the next page, there is NO row - not even a Low one. Never write
    "order unchanged in substance", "moved after", "still present": if you find yourself
    writing that, delete the row.
  - A block-boundary artifact: the diff extracts PDF text block by block, so a section
    title often lands glued to the end of the neighbouring paragraph. "Paragraph X is
    followed by the heading Y" describes the extraction, not the document.
  - Anything unchanged. A row exists only for a difference.
  - The document's own revision/amendment history table (the "indice"/"Raisons de
    l'evolution" changelog). It DESCRIBES the changes; the changes themselves are
    reported from the body. A row about "the revision table now mentions X" is a
    duplicate of the row about X.
  Preferring to emit a doubtful row applies to CONTENT you are unsure about, never to
  layout you recognised as layout.

If nothing qualifies: output exactly []

LANGUAGE: write "section", "before", "after" and "rationale" in the SAME language
  as the document being compared. Keep "type" and "criticality" in English,
  verbatim from the lists above.

OVERRIDING RULE — THE READER HAS NOT OPENED THE DOCUMENT:
  Every row is read on its own, in a spreadsheet, by someone who will never see
  the diff. A row that cannot be understood without the source document is a
  FAILED row, worse than no row at all. Prefer a longer sentence that is clear
  over a short one that is cryptic. Being complete beats being brief, always.

BEFORE / AFTER — a readable phrase, not a fragment:

  Say what the OLD version stated and what the NEW version states, each as a
  phrase that stands on its own. Keep the subject: who or what the statement is
  about. Do not strip a sentence down to the words that literally differ.

    Good:  before: "délai de notification à l'autorité : 30 jours"
           after:  "délai de notification à l'autorité : 15 jours"
    Bad:   before: "30 jours"        after: "15 jours"
           (the reader cannot tell what these 30 days were)

    Good:  before: "suppléance du responsable de production assurée par le
                    directeur commercial et développement"
           after:  "suppléance du responsable de production assurée par le
                    directeur des opérations"
    Bad:   before: "directeur commercial et développement"

  Table cells arrive as separate fragments, one per cell. Reassemble the
  fragments of the SAME table row into one readable row, and name the row:
    Good:  after: "ligne « Responsable Qualité Part21G » : suppléant =
                   responsable assurance qualité (titulaire F. Galinier)"
    Bad:   after: "Responsable assurance qualité"

  Always carry the unit with a number, and the revision with a reference.
  No meta-phrases ("le document indique que", "cette section décrit").
  Target 15-40 words; go beyond only when the change genuinely needs it.
  Use "--" only when the content is entirely absent from that version.

RATIONALE — one short sentence explaining the change, or empty:

  Write a normal, grammatical sentence in the document's language that explains,
  in plain terms, what the change actually consists of. This is a SUMMARY, not
  an instruction: never start with an imperative verb, never tell the reader
  what to do. Never write a list of nouns with a verb tacked on at the end —
  that is not a sentence and cannot be read.

    Good: "L'organigramme ajoute un responsable de la conformité Partie IS,
           absent de la matrice des responsabilités précédente."
    Bad:  "Mettre à jour l'organigramme et la matrice des responsabilités pour
           y faire figurer le responsable de la conformité Partie IS."
           (this is an instruction, not an explanation of the change)

    Good: "Le plan de formation ne couvre plus l'effectif révisé du site de
           Toulouse."
    Bad:  "Plan de formation et dimensionnement des ressources recalculer."
           (noun pile, unreadable — never produce this shape)

  Do not invent a cause or a downstream consequence that is not stated in the
  source — explain the change itself, not a guessed motivation or effect.
  If the change is trivial or cosmetic (renumbering, table of contents, pure
  formatting — the same rows that get Low criticality with no compliance
  impact), output "".
  Target one sentence, up to 40 words. Never merely repeat the before/after
  fields word for word — add the plain-language gist, not a duplicate quote.

ONE ROW PER CHANGE — do not merge, do not sample:
  • Every ADDED / REMOVED / MODIFIED entry in the diff deserves its own row,
    EXCEPT fragments of the same table row (reassemble those into one row) and
    the metadata listed under SKIP.
  • Near-identical entries are still DISTINCT changes. Six new table rows that
    differ only by which function they name are six rows, not one — emitting one
    "example" row and dropping the rest is a failure.
  • Never write "etc.", "and others", "several similar changes".

IMPORTANT: output ONLY the JSON array. No explanation. No markdown. No code fences.\
"""


# ---------------------------------------------------------------------------
# Modal-aware helpers for paragraph_semantic_diff
# ---------------------------------------------------------------------------

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


# Word-level net behind the character-distance gate below. That gate measures the change against the WHOLE paragraph,
# so one decisive word in a long paragraph ("shall record" -> "shall not record", "before" -> "after") was dropped
# before the LLM saw it.
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


# ---------------------------------------------------------------------------
# Relocation detection: catches content the pairing loop missed because it moved into a differently-shaped paragraph
# (merged into a longer list, split into its own subsection, reworded). The LLM cannot reliably re-derive this from
# the diff, so a cheap deterministic substring check does it.
# ---------------------------------------------------------------------------

_RELOCATION_MIN_WORDS = 6
# Below _RELOCATION_MIN_WORDS the relocation check never runs, so short blocks need their own net: a block still
# present VERBATIM in the other revision is a segmentation artifact (table cells regrouped by the PDF text extraction,
# a header-dedup pass dropping a repeat on one side only), never a real change. The occurrence COUNT means nothing for
# a repeated header or table cell.
_ARTIFACT_MAX_WORDS = int(os.getenv('COMPARE_ARTIFACT_MAX_WORDS', '12'))
# "Still present verbatim" alone is not enough: on a wiring diagram the change can be a short annotation moving
# sheets. The surviving occurrence must also sit at roughly the SAME relative position (a repagination moves a block
# by a fraction of a percent, a real relocation by far more). Fractional position, not page number, so a document-wide
# page offset does not look like a move.
_ARTIFACT_MAX_POS_DRIFT = float(os.getenv('COMPARE_ARTIFACT_MAX_POS_DRIFT', '0.035'))
# A label recurring this many times in BOTH documents is a diagram annotation stamped once per connection. There the
# count IS the signal and no positional rule separates a genuine extra occurrence from a re-segmentation, so dense
# labels are exempt from the net and stay visible.
_ARTIFACT_DENSE_LABEL_MIN = int(os.getenv('COMPARE_ARTIFACT_DENSE_LABEL_MIN', '20'))
# Merged-cell net (_drop_merged_cells): only fragments of at least this many
# words, absorbed into a container at least this many times longer. Both bounds
# exist to keep short diagram labels out of it.
_ARTIFACT_MERGED_MIN_WORDS = 1
_ARTIFACT_MERGED_MIN_RATIO = 3.0
# Above this many (old x new) occurrences of the SAME text, the exact-match DP is skipped for index-wise pairing: both
# lists are sorted by position, so it is right whenever the extra occurrences sit at the end. The DP only pays off on
# small, ambiguous groups.
_DUP_ALIGN_MAX_CELLS = 250_000


def _align_duplicates(
    olds: List[Dict[str, Any]],
    news: List[Dict[str, Any]],
    n_old: int,
    n_new: int,
) -> Tuple[List[Dict[str, Any]], Set[int]]:
    """Pair equal-text blocks order-preservingly, minimising position drift.

    Returns (unmatched_olds, unmatched_new_idxs).
    """
    m, k = len(olds), len(news)
    if m == 1 and k == 1:
        return [], set()
    if m * k > _DUP_ALIGN_MAX_CELLS:
        pairs = min(m, k)
        return olds[pairs:], {n['idx'] for n in news[pairs:]}

    po = [o['idx'] / n_old for o in olds]
    pn = [n['idx'] / n_new for n in news]

    # cost[i][j] = min total drift aligning olds[i:] with news[j:], skipping the
    # surplus on whichever side is longer.
    INF = float('inf')
    cost = [[INF] * (k + 1) for _ in range(m + 1)]
    for i in range(m + 1):
        cost[i][k] = 0.0 if i == m else INF  # every old must be paired or skipped below
    for j in range(k + 1):
        cost[m][j] = 0.0
    for i in range(m - 1, -1, -1):
        for j in range(k - 1, -1, -1):
            best = cost[i][j + 1]                      # skip news[j]
            if k - j >= m - i:                          # enough news left to skip olds[i]
                best = min(best, cost[i + 1][j])
            best = min(best, abs(po[i] - pn[j]) + cost[i + 1][j + 1])
            cost[i][j] = best
        cost[i][k] = INF

    unmatched_olds: List[Dict[str, Any]] = []
    unmatched_new: Set[int] = set()
    i = j = 0
    while i < m and j < k:
        if cost[i][j] == abs(po[i] - pn[j]) + cost[i + 1][j + 1]:
            i += 1
            j += 1
        elif cost[i][j] == cost[i][j + 1]:
            unmatched_new.add(news[j]['idx'])
            j += 1
        else:
            unmatched_olds.append(olds[i])
            i += 1
    unmatched_olds.extend(olds[i:])
    unmatched_new.update(n['idx'] for n in news[j:])
    return unmatched_olds, unmatched_new


def _position_index(items: List[Dict[str, Any]], n_total: int) -> Dict[str, List[float]]:
    """Short block text -> fractional positions where it occurs in that document."""
    index: Dict[str, List[float]] = defaultdict(list)
    for item in items:
        clean = item['clean']
        if clean and len(clean.split()) <= _ARTIFACT_MAX_WORDS:
            index[clean].append(item['idx'] / n_total)
    return index


def _drop_present_verbatim(
    leftovers: List[Dict[str, Any]],
    n_own: int,
    other_index: Dict[str, List[float]],
    own_joined: str,
    other_joined: str,
    already_reported: str = '',
) -> Tuple[List[Dict[str, Any]], int]:
    """Drop short leftovers that still exist verbatim at the same spot elsewhere.

    Safety net behind _align_duplicates, for the
    residual segmentation surplus those two cannot cancel.

    Gated on a surplus test so the net cannot swallow a genuine addition of text
    that already existed elsewhere: WDT017W8850201 gained 6 'TO PATCH'
    annotations on sheets 80-82 and every one was suppressed because another
    'TO PATCH' sat nearby. The test counts occurrences as SUBSTRINGS of the whole
    document, not as standalone blocks — the artifacts this net exists for are
    precisely cases where the text survived merged into a neighbouring block, so
    a block-level count reads them as a surplus and protects them (MOP_AX
    'ANNEXE 4': 2 standalone blocks in AW vs 1 in AX, but 16 substring hits in
    each because of the 14 page headers).
    """
    kept: List[Dict[str, Any]] = []
    dropped = 0
    # A text in surplus justifies at most `surplus` entries, not one per leftover: the surplus is budgeted per text
    # and given to the leftovers furthest from any surviving occurrence (the likeliest genuine losses).
    # A loss already visible inside a MODIFIED entry's ~~strikethrough~~ must not also be charged to a leftover
    # elsewhere.
    budget: Dict[str, int] = {}
    for item in leftovers:
        clean = item['clean']
        if clean and clean not in budget:
            surplus = own_joined.count(clean) - other_joined.count(clean)
            budget[clean] = max(0, surplus - already_reported.count(clean))
    order = sorted(
        range(len(leftovers)),
        key=lambda k: -min(
            (abs(p - leftovers[k]['idx'] / n_own)
             for p in other_index.get(leftovers[k]['clean'], ())),
            default=1.0,
        ),
    )
    verdict: Dict[int, bool] = {}
    for k in order:
        item = leftovers[k]
        clean = item['clean']
        words = clean.split() if clean else []
        if not words or len(words) > _ARTIFACT_MAX_WORDS:
            verdict[k] = True
            continue
        pos = item['idx'] / n_own
        if not any(abs(p - pos) <= _ARTIFACT_MAX_POS_DRIFT for p in other_index.get(clean, ())):
            verdict[k] = True
            continue
        own_n, other_n = own_joined.count(clean), other_joined.count(clean)
        if min(own_n, other_n) >= _ARTIFACT_DENSE_LABEL_MIN:
            verdict[k] = True
            continue
        if budget.get(clean, 0) > 0:
            budget[clean] -= 1
            verdict[k] = True
            continue
        verdict[k] = False
    for k, item in enumerate(leftovers):
        if verdict.get(k, True):
            kept.append(item)
        else:
            dropped += 1
    return kept, dropped


def _drop_merged_cells(
    leftovers: List[Dict[str, Any]],
    n_own: int,
    other_items: List[Dict[str, Any]],
    n_other: int,
    own_joined: str,
    other_joined: str,
) -> Tuple[List[Dict[str, Any]], int]:
    """Drop leftovers whose whole text was absorbed into a longer nearby block.

    The other half of the segmentation problem: instead of a repeated cell
    surviving on one side only, a cell that stood alone in one revision is
    merged into its neighbour in the other. There is no standalone counterpart
    to match, so the block becomes a REMOVED/ADDED for text that did not change
    at all — MOP_AX's revision-log cell "Ajout de l'Annexe 4" (a line of the
    change-history table, merged into the surrounding sentence in AX).

    Deliberately narrow: same relative position, and the container must be much
    longer than the fragment. Without the length ratio this also swallows a
    label sitting next to a longer sibling that merely starts with it
    ('TO PATCH' vs 'TO PATCH DC12_14+').
    """
    kept: List[Dict[str, Any]] = []
    dropped = 0
    for item in leftovers:
        clean = item['clean']
        words = clean.split() if clean else []
        if not (_ARTIFACT_MERGED_MIN_WORDS <= len(words) <= _ARTIFACT_MAX_WORDS):
            kept.append(item)
            continue
        # Same surplus gate as _drop_present_verbatim: an occurrence COUNT that grew or shrank is content, not layout.
        if own_joined.count(clean) != other_joined.count(clean):
            kept.append(item)
            continue
        pos = item['idx'] / n_own
        needle_len = len(clean)
        if any(
            len(o['clean']) >= needle_len * _ARTIFACT_MERGED_MIN_RATIO
            and abs(o['idx'] / n_other - pos) <= _ARTIFACT_MAX_POS_DRIFT
            and clean in o['clean']
            for o in other_items
        ):
            dropped += 1
            continue
        kept.append(item)
    return kept, dropped

# High bar on purpose: a REMOVED block is often several sentences merged into one paragraph, and a genuinely mixed
# paragraph (part relocated, part deleted) can still rack up a long verbatim run from the relocated half alone. 0.75
# sits between the highest measured false positive (0.70) and the lowest confirmed true positive (0.79).
_RELOCATION_COVERAGE_THRESHOLD = 0.75
# A single long verbatim run is a much stronger relocation signal than scattered short matches: recurring boilerplate
# phrasing alone can reach 50%+ scattered coverage for a paragraph that exists nowhere else. Gating on both keeps a
# real removal from being masked as a relocation.
_RELOCATION_MIN_RUN_WORDS = 8
_STRIKETHROUGH_RE = re.compile(r'~~(.+?)~~', re.S)


class _RelocationIndex:
    """The new document, indexed once, to ask "does this removed text still
    exist somewhere?" for every REMOVED entry and struck-through span.

    Built once per comparison: constructing a SequenceMatcher per entry
    re-indexed the whole new document each time, which was 80% of the diff
    time on a large pair (10 s out of 12 for 600 removed paragraphs).
    """

    def __init__(self, new_text: str) -> None:
        words = canonicalize_keep_modals(new_text).split()
        self._matcher = difflib.SequenceMatcher(None, autojunk=False)
        self._matcher.set_seq2(words)
        run = _RELOCATION_MIN_RUN_WORDS
        self._runs = {tuple(words[k:k + run]) for k in range(len(words) - run + 1)}

    def signal(self, text: str) -> Tuple[float, int]:
        """(coverage, longest_run_words): coverage is the fraction of `text`'s
        words matched, in order, anywhere in the new document; longest_run_words
        is the single longest contiguous match.

        Uses difflib's word-level LCS (get_matching_blocks()) — unlike a rigid
        n-gram scan, one inserted word ("training program" -> "training and
        examination program") doesn't fracture an otherwise-long match into two
        just-under-threshold fragments.
        """
        words = canonicalize_keep_modals(text).split()
        n = len(words)
        if n < _RELOCATION_MIN_WORDS:
            return 0.0, 0
        self._matcher.set_seq1(words)
        blocks = self._matcher.get_matching_blocks()
        return sum(b.size for b in blocks) / n, max((b.size for b in blocks), default=0)

    def is_relocated(self, text: str) -> bool:
        # A contiguous run of _RELOCATION_MIN_RUN_WORDS is required anyway: without one in the new document the costly
        # alignment is skipped.
        words = canonicalize_keep_modals(text).split()
        run = _RELOCATION_MIN_RUN_WORDS
        if not any(tuple(words[k:k + run]) in self._runs for k in range(len(words) - run + 1)):
            return False
        coverage, longest_run = self.signal(text)
        return coverage >= _RELOCATION_COVERAGE_THRESHOLD and longest_run >= run


def _tag_relocated(results: List[Dict[str, Any]], new_text: str) -> int:
    """Mark content that moved rather than disappeared, so the analysis
    prompt sees a plain fact ("this text is still present, just moved")
    instead of having to infer it from reading the whole diff itself:

    - A standalone REMOVED entry whose substance reappears elsewhere becomes
      a RELOCATED entry.
    - A ~~struck-through~~ span inside a MODIFIED entry gets an inline note
      when its substance reappears elsewhere — the surrounding MODIFIED
      block is left intact (never split into separate rows).

    Returns the number of entries/spans annotated.
    """
    index = _RelocationIndex(new_text)
    tagged = 0

    for r in results:
        m = _DIFF_ENTRY_RE.match(r['content'])
        if not m:
            continue

        if m.group(1) == 'REMOVED':
            removed_text = m.group(3)
            if index.is_relocated(removed_text):
                tag = f' [{m.group(2)}]' if m.group(2) else ''
                r['content'] = (
                    f'RELOCATED{tag}: {removed_text} '
                    '(same content found elsewhere in the new document — moved, not eliminated)'
                )
                tagged += 1

        elif m.group(1) == 'MODIFIED':
            def _annotate(span_match: 're.Match') -> str:
                nonlocal tagged
                span = span_match.group(1)
                if index.is_relocated(span):
                    tagged += 1
                    return f'~~{span}~~ [RELOCATED elsewhere in the new document, not eliminated]'
                return span_match.group(0)

            new_body = _STRIKETHROUGH_RE.sub(_annotate, m.group(3))
            if new_body != m.group(3):
                tag = f' [{m.group(2)}]' if m.group(2) else ''
                r['content'] = f'MODIFIED{tag}: {new_body}'

    return tagged


# ---------------------------------------------------------------------------
# Primary text diff engine — paragraph semantic diff
# ---------------------------------------------------------------------------

_CONT_RE = re.compile(
    r'^((?:\d+(?:\.\d+)*\.?\s+)?[A-Z][A-Z\s\d]*?)\s*\((Continued|Suite|Cont\.)\)(?:\s+(.*))?',
    re.I | re.DOTALL,
)


# A change whose identical text recurs on at least this many distinct pages is title-block/boilerplate (revision
# stamp, classification field, footer): one real change rendered once per page by the PDF extraction. Such repeats are
# 85-95% of all entries on wiring-diagram revisions.
_BOILERPLATE_MIN_PAGES = int(os.getenv('COMPARE_BOILERPLATE_MIN_PAGES', '3'))

def _dedup_repeated_headers(items: List[Dict[str, Any]], window: int = 25) -> List[Dict[str, Any]]:
    """Drop a short block that reappears within `window` source lines.

    Table column headers repeated at page-break continuations, running footers,
    and wiring labels re-emitted a few blocks apart are all layout, not content.
    Removing this pass entirely (tried 2026-08-17) surfaced 151 extra entries on
    WDT017W8850653 and every sampled one was verified noise: '7168-MLB22' still
    occurs 3 times in the new revision, '13/07/2023' and 'PILOT STATION' are
    identical on both sides. So the pass stays.

    It is NOT symmetric under repagination — the same repeat can fall inside the
    window in one revision and outside it in the other, leaving a surplus copy
    that becomes a phantom REMOVED. That asymmetry is caught downstream by
    _drop_present_verbatim / _drop_merged_cells rather than by tuning `window`,
    because no window value is symmetric (a page anchor was also tried and ate
    real wiring labels on dense sheets).
    """
    MAX_WORDS = 15
    seen: Dict[str, int] = {}
    result: List[Dict[str, Any]] = []
    for item in items:
        clean = item['clean']
        if 0 < len(clean.split()) <= MAX_WORDS:
            prev = seen.get(clean)
            if prev is not None and (item['idx'] - prev) <= window:
                seen[clean] = item['idx']
                continue
        seen[clean] = item['idx']
        result.append(item)
    return result


# A paragraph cut in two (or two merged into one) between revisions: up to this
# many consecutive blocks, no further apart than this many source lines.
_RESEGMENT_MAX_RUN = 3
_RESEGMENT_MAX_GAP = 3
_RESEGMENT_MIN_WORDS = 8


def _cancel_resegmented(
    olds: List[Dict[str, Any]], news: List[Dict[str, Any]],
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], int]:
    """Remove blocks whose text is unchanged but cut differently.

    One block on a side equal to 2-3 consecutive leftover blocks on the other
    is the same text with a paragraph break added or removed: no wording
    changed. Left alone it reached the LLM as an ADDED half plus a MODIFIED
    whose other half is struck through — two phantom changes (2026-10-04).

    Returns (remaining_olds, remaining_news, n_blocks_cancelled).
    """
    def _runs(items: List[Dict[str, Any]]) -> Dict[str, Tuple[int, ...]]:
        out: Dict[str, Tuple[int, ...]] = {}
        for a in range(len(items)):
            text = items[a]['clean']
            for b in range(a + 1, min(a + _RESEGMENT_MAX_RUN, len(items))):
                if items[b]['idx'] - items[b - 1]['idx'] > _RESEGMENT_MAX_GAP:
                    break
                text = f"{text} {items[b]['clean']}"
                out.setdefault(text, tuple(range(a, b + 1)))
        return out

    drop_old: Set[int] = set()
    drop_new: Set[int] = set()
    for wholes, parts, drop_w, drop_p in ((olds, news, drop_old, drop_new), (news, olds, drop_new, drop_old)):
        runs = _runs(parts)
        for i, whole in enumerate(wholes):
            if i in drop_w or len(whole['clean'].split()) < _RESEGMENT_MIN_WORDS:
                continue
            run = runs.get(whole['clean'])
            if run and not any(k in drop_p for k in run):
                drop_w.add(i)
                drop_p.update(run)
    if not drop_old and not drop_new:
        return olds, news, 0
    return (
        [o for i, o in enumerate(olds) if i not in drop_old],
        [n for j, n in enumerate(news) if j not in drop_new],
        len(drop_old) + len(drop_new),
    )


_DIFF_ENTRY_RE = re.compile(r'^(MODIFIED|ADDED|REMOVED|RELOCATED)(?:\s+\[([^\]]*)\])?:\s?(.*)$', re.S)

# Pagination fragments inside repeated header/footer text ('Page 2', 'PAGE: 1 of 34', 'Sheet 5/48'): masked ONLY
# around these keywords when grouping repeats, so a footer embedding its own page number still groups. Bare numbers
# are never masked: two wiring rows differing by a pin number stay distinct.
# A footer only reaches the diff when its page number CHANGED, i.e. as a MODIFIED entry carrying "PAGE : ~~16/37~~
# **16/38**": two numbers wrapped in inline markup. The trailing repeat group swallows the second (post-markup-strip)
# number.
_PAGINATION_RE = re.compile(
    r'\b(page|sheet|folio|feuille|list|strana|str)\s*[:.]?\s*'
    r'\d+(?:\s*(?:of|/|sur|z)\s*\d+)?'
    r'(?:\s+\d+(?:\s*(?:of|/|sur|z)\s*\d+)?)*',
    re.I,
)
# Inline word-diff markup, stripped before computing a grouping key only.
_MARKUP_RE = re.compile(r'~~|\*\*')
# Table-of-contents rows: dot/dash leaders running to a page number. Section renames and renumbering surface as their
# own body entries, so the TOC echo is noise. Matches mid-text too (inline markup wraps the leaders in ~~…~~/**…**);
# the digit requirement keeps plain '------' separators out.
_TOC_LINE_RE = re.compile(r'(?:\.{4,}\s*\d*\s*$|[.\-]{6,}.{0,40}\d)')


# "PAGE : ~~1/37~~ **1/38**" is a repagination substitution. When stripping every such pair from a MODIFIED entry
# leaves no markup, the entry's only change is the page number and it is layout. _collapse_page_repeats cannot help:
# the footer is glued to the head-office address, so that block occurs once.
_PAGE_FRACTION_SUB_RE = re.compile(
    r'~~\s*(?:page\s*[:.]?\s*)?\d{1,4}\s*/\s*\d{1,4}\s*~~\s*\*\*\s*(?:page\s*[:.]?\s*)?\d{1,4}\s*/\s*\d{1,4}\s*\*\*',
    re.I,
)
_ANY_MARKUP_RE = re.compile(r'~~[^~]*~~|\*\*[^*]*\*\*')


def _is_pagination_only(content: str) -> bool:
    m = _DIFF_ENTRY_RE.match(content)
    if not m or m.group(1) != 'MODIFIED':
        return False
    stripped = _PAGE_FRACTION_SUB_RE.sub('', m.group(3))
    return bool(_PAGE_FRACTION_SUB_RE.search(m.group(3))) and not _ANY_MARKUP_RE.search(stripped)


def _collapse_page_repeats(results: List[Dict[str, Any]], page_label: Optional[str]) -> Tuple[List[Dict[str, Any]], int]:
    """Collapse per-page repeats of the SAME change into one annotated entry.

    Returns (collapsed_results, n_entries_absorbed). Entries with no page tag
    are never collapsed (nothing proves they are page artefacts). The collapsed
    entry keeps the first occurrence's section/position and says explicitly how
    many pages repeat it — no information is lost for the LLM, but a 48-page
    title-block stamp stops drowning the real changes (and the token budget).
    Two extra grouping rules, measured on utils/compare_eval (2026-07-17):
    pagination fragments are masked so 'Issue A6 Page N' repeats group across
    pages, and dot-leader TOC rows group per kind into one summary entry.
    """
    if not page_label:
        return results, 0

    def _group_key(kind: str, text: str) -> Tuple[str, str]:
        if _TOC_LINE_RE.search(text):
            return kind, '<toc>'
        return kind, _PAGINATION_RE.sub(r'\1 #', _MARKUP_RE.sub('', text))

    groups: Dict[Tuple[str, str], List[Tuple[int, Dict[str, Any], str]]] = defaultdict(list)
    parsed: List[Optional[Tuple[str, int, str]]] = []
    for r in results:
        m = _DIFF_ENTRY_RE.match(r['content'])
        pm = re.search(r'(\d+)', m.group(2) or '') if m else None
        if not m or not pm:
            parsed.append(None)
            continue
        kind, text = m.group(1), m.group(3)
        page = int(pm.group(1))
        parsed.append((kind, page, text))
        groups[_group_key(kind, text)].append((page, r, text))

    collapsed_keys = set()
    for key, members in groups.items():
        if key[1] == '<toc>':
            # A rebuilt TOC lands on ONE page as dozens of dot-leader rows: group by row count, page spread proves
            # nothing here.
            if len(members) >= _BOILERPLATE_MIN_PAGES:
                collapsed_keys.add(key)
        elif len({p for p, _, _ in members}) >= _BOILERPLATE_MIN_PAGES:
            collapsed_keys.add(key)

    out: List[Dict[str, Any]] = []
    emitted: set = set()
    absorbed = 0
    for r, p in zip(results, parsed):
        if p is None:
            out.append(r)
            continue
        kind, _page, text = p
        key = _group_key(kind, text)
        if key not in collapsed_keys:
            out.append(r)
            continue
        if key in emitted:
            absorbed += 1
            continue
        emitted.add(key)
        members = groups[key]
        pages = sorted({pg for pg, _, _ in members})
        first = min(members, key=lambda m_: m_[1]['sort_idx'])
        span = f'{page_label} {pages[0]}-{pages[-1]}'
        if key[1] == '<toc>':
            content = (f'{kind} [{span}]: {len(members)} table-of-contents lines '
                       f'(dot-leader rows — the section changes themselves appear as '
                       f'their own entries), e.g.: {first[2][:100]}')
        else:
            content = (f'{kind} [{span}, repeated on {len(pages)} pages — recurring '
                       f'header/footer block]: {first[2]}')
        out.append({**first[1], 'content': content})
    return out, absorbed


# A 'replace' span from inline_word_diff(): "~~old words~~ **new words**" with
# only whitespace between the two markup runs.
_WORD_SUB_RE = re.compile(r'~~([^~]*)~~\s*\*\*([^*]*)\*\*')
_SYSTEMIC_SUB_MIN = int(os.getenv('COMPARE_SYSTEMIC_SUB_MIN', '5'))


def _collapse_systemic_substitutions(
    results: List[Dict[str, Any]], min_occurrences: int = _SYSTEMIC_SUB_MIN
) -> Tuple[List[Dict[str, Any]], int]:
    """Collapse a document-wide restyle (bullet marker swap, ANNEX->APPENDIX
    rename, spelling unification) that recurs identically across many MODIFIED
    entries into one annotated example.

    Without this, a single global find/replace-style edit — applied identically
    across dozens of otherwise-unrelated paragraphs — multiplies into one diff
    entry per occurrence and drowns out the genuinely distinct changes. Only
    entries made up ENTIRELY of already-known recurring pairs are collapsed: an
    entry mixing a systemic swap with other wording changes is always kept in
    full, so nothing substantive is silently dropped. Any pair where either
    side contains a digit is never treated as systemic — a value repeated
    across many table rows (e.g. a pin number) must stay visible per row.
    """

    def _norm(tok: str) -> str:
        return re.sub(r'\s+', ' ', tok).strip().casefold()

    entry_pairs: Dict[int, List[Tuple[str, str]]] = {}
    pair_entries: Dict[Tuple[str, str], set] = defaultdict(set)

    for idx, r in enumerate(results):
        if not r['content'].startswith('MODIFIED'):
            continue
        pairs = [
            (_norm(m.group(1)), _norm(m.group(2)))
            for m in _WORD_SUB_RE.finditer(r['content'])
        ]
        pairs = [p for p in pairs if p[0] or p[1]]
        if not pairs:
            continue
        entry_pairs[idx] = pairs
        for p in set(pairs):
            if any(ch.isdigit() for ch in p[0] + p[1]):
                continue
            pair_entries[p].add(idx)

    systemic = {p for p, idxs in pair_entries.items() if len(idxs) >= min_occurrences}
    if not systemic:
        return results, 0

    signature_entries: Dict[Tuple[Tuple[str, str], ...], List[int]] = defaultdict(list)
    for idx, pairs in entry_pairs.items():
        uniq = set(pairs)
        if uniq and uniq <= systemic:
            signature_entries[tuple(sorted(uniq))].append(idx)

    to_collapse = {sig: idxs for sig, idxs in signature_entries.items() if len(idxs) >= min_occurrences}
    if not to_collapse:
        return results, 0

    idx_to_sig = {idx: sig for sig, idxs in to_collapse.items() for idx in idxs}
    out: List[Dict[str, Any]] = []
    absorbed = 0
    emitted: set = set()
    for idx, r in enumerate(results):
        sig = idx_to_sig.get(idx)
        if sig is None:
            out.append(r)
            continue
        if sig in emitted:
            absorbed += 1
            continue
        emitted.add(sig)
        idxs = to_collapse[sig]
        first = results[min(idxs, key=lambda i: results[i]['sort_idx'])]
        desc = ', '.join(f"'{o}' -> '{n}'" for o, n in sig)
        note = f' [same substitution repeated in {len(idxs) - 1} more entries — {desc}]'
        out.append({**first, 'content': first['content'] + note})
    return out, absorbed


def paragraph_semantic_diff(old_text: str, new_text: str, page_label: Optional[str] = None) -> Tuple[str, int]:
    """Primary paragraph-level semantic alignment engine.

    1. Pre-merges '(Continued)' page-split blocks to eliminate false REMOVED/ADDED
       entries caused by content reflowing across pages.
    2. Exact matches are removed first (unchanged content).
    3. Near matches (word-level ratio > 0.55, min 3 words) become MODIFIED with
       inline ~~removed~~ / **added** markup.
    4. Remaining old → REMOVED, remaining new → ADDED.
    5. Results grouped by section (## heading), sorted by document order.
    6. Modal-aware: within-group swaps (shall↔must) are ignored; cross-group
       swaps (shall→may) are reported.
    7. Per-page boilerplate repeats (same change on >=_BOILERPLATE_MIN_PAGES
       distinct pages — revision stamps, classification fields) collapse into
       one annotated entry.
    8. Document-wide restyles (same word-level substitution recurring across
       >=_SYSTEMIC_SUB_MIN otherwise-unrelated MODIFIED entries — e.g. a bullet
       marker or terminology swap applied everywhere) collapse into one
       annotated example.

    Returns:
        (diff_string, filtered_count)
    """

    def _clean(s: str) -> str:
        s = re.sub(r'[\.\-_]{2,}', ' ', s)
        return re.sub(r'\s+', ' ', s).strip().lower()

    def _parse(text: str) -> List[Dict[str, Any]]:
        lines = text.splitlines()
        bare_lines = [strip_tag(b) for b in lines]
        # Mask a trailing page fraction ("... V09 4/39") before counting repeats: a footer stamp embedding its page
        # number is a different string on every page.
        _heading_key = lambda b: _TRAILING_PAGE_FRACTION_RE.sub('', b)
        # A bare ALL-CAPS line repeating >=_BOILERPLATE_MIN_PAGES times is a running header/footer (page title,
        # company address), not a section: treating it as one would scatter every real section's content under that
        # recurring label. Numbered headings are exempt: a real clause number essentially never repeats this often.
        running_header_counts = Counter(_heading_key(b) for b in bare_lines if _ALLCAPS_SECTION_RE.match(b))

        result: List[Dict[str, Any]] = []
        cur_sec = 'Preamble'
        for idx, (block, bare) in enumerate(zip(lines, bare_lines)):
            pm = re.search(r'\[(?:Page|Slide|Item)\s+(\d+)', block, re.I) if page_label else None
            page_num = int(pm.group(1)) if pm else None
            is_toc = bool(re.search(r'\.{3,}\s*\d+$', bare))
            is_running_header = running_header_counts.get(_heading_key(bare), 0) >= _BOILERPLATE_MIN_PAGES
            if SECTION_RE.match(bare) and 2 < len(bare) < 100 and not is_toc and not is_running_header:
                cur_sec = re.sub(
                    r'\s*\((?:Continued|Suite|Cont\.)\)\s*$', '', bare, flags=re.I
                ).strip() or bare
            if bare.strip():
                result.append({'idx': idx, 'sec': cur_sec, 'txt': bare, 'clean': _clean(bare), 'page': page_num})
        return result

    def _merge_continued(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        result: List[Dict[str, Any]] = []
        last_content_idx: Dict[str, int] = {}
        for item in items:
            m = _CONT_RE.match(item['txt'])
            if m:
                continuation = (m.group(3) or '').strip()
                sec_from_header = m.group(1).strip()
                target = next(
                    (c for c in [sec_from_header.lower(), item['sec'].lower()] if c in last_content_idx),
                    None,
                )
                if target is not None and continuation:
                    ti = last_content_idx[target]
                    result[ti]['txt'] += ' ' + continuation
                    result[ti]['clean'] += ' ' + _clean(continuation)
                    continue
                elif not continuation:
                    continue  # Pure page-break header — drop to avoid false MODIFIED
            sec_key = item['sec'].lower()
            if len(item['txt'].split()) >= 10:
                last_content_idx[sec_key] = len(result)
            result.append(item)
        return result

    # Raw (pre-dedup) blocks are kept: the artifact nets below count occurrences on them, because the dedup pass is
    # what makes counts look uneven between the two revisions.
    old_raw = _merge_continued(_parse(old_text))
    new_raw = _merge_continued(_parse(new_text))
    old_items = _dedup_repeated_headers(old_raw)
    new_items = _dedup_repeated_headers(new_raw)
    # Newline-joined so a single-line block can only match inside one block.
    old_joined = '\n'.join(i['clean'] for i in old_raw)
    new_joined = '\n'.join(i['clean'] for i in new_raw)

    # Denominators for the fractional 'pos' each result carries: old-document and
    # new-document block indexes live in different spaces, and interleaving
    # REMOVED with ADDED needs one axis comparable across both.
    n_old = max(1, (old_items[-1]['idx'] if old_items else 0) + 1)
    n_new = max(1, (new_items[-1]['idx'] if new_items else 0) + 1)

    # Exact matching, grouped by clean text so repeated blocks pair by POSITION instead of document order: with a FIFO
    # queue, a text occurring twice in one revision and once in the other always consumed the FIRST old occurrence,
    # leaving a phantom removal of an element present, unchanged, in both.
    old_by_clean: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for o in old_items:
        old_by_clean[o['clean']].append(o)
    new_by_clean: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for n in new_items:
        new_by_clean[n['clean']].append(n)

    matched_new: set = set()
    unmatched_old: List[Dict[str, Any]] = []
    for clean, olds in old_by_clean.items():
        news = new_by_clean.get(clean)
        if not news:
            unmatched_old.extend(olds)
            continue
        keep_old, keep_new = _align_duplicates(olds, news, n_old, n_new)
        unmatched_old.extend(keep_old)
        matched_new.update(n['idx'] for n in news if n['idx'] not in keep_new)

    new_leftovers = [n for n in new_items if n['idx'] not in matched_new]
    unmatched_old.sort(key=lambda o: o['idx'])
    unmatched_old, new_leftovers, resegmented = _cancel_resegmented(unmatched_old, new_leftovers)

    # Every leftover old block against every leftover new block. Two true upper bounds of ratio() (lengths, then
    # shared words) discard a pair before any SequenceMatcher work, without changing a pairing decision; one matcher
    # per new block is reused for every old block that survives.
    sims: List[Tuple[float, int, int]] = []
    old_words_cache = [o['clean'].split() for o in unmatched_old]
    old_counts = [Counter(words) for words in old_words_cache]
    for j, n in enumerate(new_leftovers):
        n_words = n['clean'].split()
        ln = len(n_words)
        if ln < 3:
            continue
        n_count = Counter(n_words)
        sm: Optional[difflib.SequenceMatcher] = None
        for i, o_words in enumerate(old_words_cache):
            lo = len(o_words)
            if lo < 3:
                continue
            if 2.0 * min(lo, ln) / (lo + ln) <= _PAIR_RATIO_THRESHOLD:
                continue
            o_count = old_counts[i]
            small, large = (o_count, n_count) if len(o_count) <= len(n_count) else (n_count, o_count)
            shared = sum(min(c, large[w]) for w, c in small.items() if w in large)
            if 2.0 * shared / (lo + ln) <= _PAIR_RATIO_THRESHOLD:
                continue
            if sm is None:
                sm = difflib.SequenceMatcher(None, autojunk=False)
                sm.set_seq2(n_words)
            sm.set_seq1(o_words)
            ratio = sm.ratio()
            if ratio > _PAIR_RATIO_THRESHOLD:
                sims.append((ratio, i, j))

    # Best ratio first; ties in (old, new) order, as the old-major scan gave.
    sims.sort(key=lambda x: (-x[0], x[1], x[2]))
    matched_o: set = set()
    matched_n: set = set()
    results: List[Dict[str, Any]] = []
    filtered = resegmented

    def _emit_modified(o: Dict[str, Any], n: Dict[str, Any]) -> None:
        nonlocal filtered
        if is_substantive_keep_modals(o['txt'], n['txt']):
            diff_str = inline_word_diff(o['txt'], n['txt'])
            op, np_ = o.get('page'), n.get('page')
            lbl = page_label or 'Page'
            if op and np_ and op != np_:
                page_tag = f' [{lbl} {op}→{np_}]'
            elif op or np_:
                page_tag = f' [{lbl} {op or np_}]'
            else:
                page_tag = ''
            results.append({
                'sec': o['sec'],
                'content': f'MODIFIED{page_tag}: {diff_str}',
                'sort_idx': o['idx'],
                'from_new': False,
                'page': op or np_,
                'pos': o['idx'] / n_old,
            })
        else:
            filtered += 1

    for ratio, i, j in sims:
        if i in matched_o or j in matched_n:
            continue
        matched_o.add(i)
        matched_n.add(j)
        _emit_modified(unmatched_old[i], new_leftovers[j])

    # ── Second-chance pairing: table rows sharing the same row key ──────────
    # DOCX tables are emitted as "RowLabel: col: val | …". A heavily rewritten row (similarity under the 0.55 gate) is
    # still the SAME row evolving: pair leftovers whose row label and section match instead of reporting REMOVED +
    # ADDED.
    def _row_key(txt: str) -> Optional[str]:
        head, sep, _ = txt.partition(':')
        if not sep:
            return None
        key = _clean(head)
        return key if len(key) >= 6 and 2 <= len(key.split()) <= 8 else None

    keyed_new: Dict[Tuple[str, str], List[int]] = {}
    for j, n in enumerate(new_leftovers):
        if j in matched_n:
            continue
        k = _row_key(n['txt'])
        if k:
            keyed_new.setdefault((n['sec'], k), []).append(j)
    for i, o in enumerate(unmatched_old):
        if i in matched_o:
            continue
        k = _row_key(o['txt'])
        if not k:
            continue
        bucket = keyed_new.get((o['sec'], k))
        if not bucket:
            continue
        j = bucket.pop(0)
        matched_o.add(i)
        matched_n.add(j)
        _emit_modified(o, new_leftovers[j])

    # ── Third-chance pairing: same text behind a different leading number ───
    # "3. INSPECTION" -> "4. INSPECTION" is too short for the similarity pass, so every heading after an inserted
    # section came out as REMOVED + ADDED. Paired, it is one entry showing only the number ("2.5 mm max" -> "3.5 mm
    # max" has the same shape and must never be hidden).
    def _numbered_key(txt: str) -> Optional[str]:
        head, _, rest = txt.strip().partition(' ')
        rest = _clean(rest)
        if not rest or not any(ch.isdigit() for ch in head) or not any(ch.isalpha() for ch in rest):
            return None
        return rest

    numbered_new: Dict[str, List[int]] = {}
    for j, n in enumerate(new_leftovers):
        if j not in matched_n:
            k = _numbered_key(n['txt'])
            if k:
                numbered_new.setdefault(k, []).append(j)
    for i, o in enumerate(unmatched_old):
        if i in matched_o:
            continue
        bucket = numbered_new.get(_numbered_key(o['txt']) or '')
        if not bucket:
            continue
        j = bucket.pop(0)
        matched_o.add(i)
        matched_n.add(j)
        _emit_modified(o, new_leftovers[j])

    # The artifact filter runs on the FINAL leftovers only, never on the pairing pool: filtering earlier removes
    # candidates that would legitimately pair into a MODIFIED entry, downgrading real changes.
    old_index = _position_index(old_items, n_old)
    new_index = _position_index(new_items, n_new)
    # Canonicalized text of every ~~removed~~ span already emitted in a MODIFIED
    # entry, so the artifact nets can tell a second report from a first one.
    struck_old = '\n'.join(_clean(m) for r in results
                           for m in _STRIKETHROUGH_RE.findall(r['content']))
    added_new = '\n'.join(_clean(m) for r in results
                          for m in re.findall(r'\*\*([^*]*)\*\*', r['content']))
    leftover_old, dropped_old = _drop_present_verbatim(
        [o for i, o in enumerate(unmatched_old) if i not in matched_o],
        n_old, new_index, old_joined, new_joined, struck_old,
    )
    leftover_new, dropped_new = _drop_present_verbatim(
        [n for j, n in enumerate(new_leftovers) if j not in matched_n],
        n_new, old_index, new_joined, old_joined, added_new,
    )
    leftover_old, merged_old = _drop_merged_cells(
        leftover_old, n_old, new_items, n_new, old_joined, new_joined)
    leftover_new, merged_new = _drop_merged_cells(
        leftover_new, n_new, old_items, n_old, new_joined, old_joined)
    filtered += merged_old + merged_new
    filtered += dropped_old + dropped_new

    lbl = page_label or 'Page'

    for o in leftover_old:
        page_tag = f' [{lbl} {o["page"]}]' if o.get('page') and page_label else ''
        results.append({
            'sec': o['sec'],
            'content': f"REMOVED{page_tag}: {o['txt']}",
            'sort_idx': o['idx'],
            'from_new': False,
            'page': o.get('page'),
            'pos': o['idx'] / n_old,
        })

    for n in leftover_new:
        page_tag = f' [{lbl} {n["page"]}]' if n.get('page') and page_label else ''
        results.append({
            'sec': n['sec'],
            'content': f"ADDED{page_tag}: {n['txt']}",
            'sort_idx': n['idx'],
            'from_new': True,
            'page': n.get('page'),
            'pos': n['idx'] / n_new,
        })

    _tag_relocated(results, new_text)
    before_pagination = len(results)
    results = [r for r in results if not _is_pagination_only(r['content'])]
    filtered += before_pagination - len(results)
    results, _absorbed = _collapse_page_repeats(results, page_label)
    results, _absorbed_sub = _collapse_systemic_substitutions(results)

    # Section order for display: aligned via SequenceMatcher on normalized headings rather than "all old sections,
    # then all new ones". A revision that re-cases headings or bumps clause numbers would otherwise match almost no
    # heading and dump the new document's order after the old one. Normalized keys keep renamed/renumbered sections
    # next to their neighbours.
    def _sec_align_key(s: str) -> str:
        s = re.sub(r'^\d+(?:\.\d+)*\.?\s*', '', s)
        return re.sub(r'\s+', ' ', s).strip().casefold()

    def _runs_with_item_map(items: List[Dict[str, Any]]) -> Tuple[List[str], Dict[int, int]]:
        """One entry per contiguous run of the same heading, plus a map from
        each item's own 'idx' to the run it belongs to.

        Unlike dict.fromkeys()/a plain sections_order[label]=... dict, this
        keeps a heading that recurs at DISTANT points in the same document (a
        generic subsection name like 'General', or a numbered workflow step
        repeated across two unrelated tables) as separate, individually
        addressable runs instead of silently collapsing every occurrence onto
        the first one's slot — that collapse is what let content from an
        unrelated, much-later table get pulled up next to an earlier one with
        the same label (regression found on a document full of recurring
        numbered flowchart steps, 2026-07-29). Looking runs up by each result's
        own sort_idx (below), rather than by the shared label string, is what
        keeps repeated labels distinguishable.
        """
        seq: List[str] = []
        idx_to_run: Dict[int, int] = {}
        for i in items:
            if not seq or seq[-1] != i['sec']:
                seq.append(i['sec'])
            idx_to_run[i['idx']] = len(seq) - 1
        return seq, idx_to_run

    old_sec_seq, old_idx_to_run = _runs_with_item_map(old_items)
    new_sec_seq, new_idx_to_run = _runs_with_item_map(new_items)

    # A normalized key may match ACROSS documents only if it identifies a single run on EACH side; a recurring key is
    # ambiguous and gets a per-occurrence sentinel, so SequenceMatcher never pairs it with the wrong occurrence.
    old_raw_keys = [_sec_align_key(s) for s in old_sec_seq]
    new_raw_keys = [_sec_align_key(s) for s in new_sec_seq]
    old_key_counts = Counter(old_raw_keys)
    new_key_counts = Counter(new_raw_keys)

    def _disambiguate(raw_keys: List[str], counts_here: Counter, counts_other: Counter) -> List[str]:
        return [
            k if counts_here[k] <= 1 and counts_other.get(k, 0) <= 1 else f'{k}\x00{i}'
            for i, k in enumerate(raw_keys)
        ]

    old_sec_keys = _disambiguate(old_raw_keys, old_key_counts, new_key_counts)
    new_sec_keys = _disambiguate(new_raw_keys, new_key_counts, old_key_counts)

    old_run_order: List[int] = [0] * len(old_sec_seq)
    new_run_order: List[int] = [0] * len(new_sec_seq)
    idx_sec = 0
    sec_sm = difflib.SequenceMatcher(None, old_sec_keys, new_sec_keys, autojunk=False)
    for tag, i1, i2, j1, j2 in sec_sm.get_opcodes():
        if tag == 'equal':
            for k in range(i2 - i1):
                old_run_order[i1 + k] = idx_sec
                new_run_order[j1 + k] = idx_sec
                idx_sec += 1
        else:
            for k in range(i1, i2):
                old_run_order[k] = idx_sec
                idx_sec += 1
            for k in range(j1, j2):
                new_run_order[k] = idx_sec
                idx_sec += 1

    def _order_key(r: Dict[str, Any]) -> Tuple[int, int, float]:
        """(section run, page, fractional position) — one axis for both documents.

        Ordering used to tiebreak on sort_idx alone, with ADDED entries offset by
        +1_000_000 to mark them as new-document indexes. That offset also made
        every ADDED in a section sort AFTER every MODIFIED/REMOVED in it, so in a
        30-page section the LLM read ~40 MODIFIED/REMOVED and only then ~40 ADDED:
        a REMOVED and the ADDED that replaces it ended up 60 lines apart and could
        no longer be read as one change (MOP_AX, 2026-08-17). Page number first,
        then the fractional block position, interleaves both sides by locality.
        """
        if r.get('from_new'):
            run = new_idx_to_run.get(r['sort_idx'])
            order = new_run_order[run] if run is not None else 999_999
        else:
            run = old_idx_to_run.get(r['sort_idx'])
            order = old_run_order[run] if run is not None else 999_999
        return order, r.get('page') or 0, r.get('pos') or 0.0

    results.sort(key=_order_key)

    # Same section label, but old and new runs get distinct order slots (repeated labels are kept unmergeable, see
    # _runs_with_item_map), which put every old-side entry before every new-side one and separated a REMOVED from the
    # ADDED replacing it. Re-sort each contiguous same-label group by page: purely local, so nothing crosses a
    # section.
    i = 0
    while i < len(results):
        j = i + 1
        while j < len(results) and results[j]['sec'] == results[i]['sec']:
            j += 1
        results[i:j] = sorted(results[i:j], key=lambda r: (r.get('page') or 0, r.get('pos') or 0.0))
        i = j

    out: List[str] = []
    cur_sec = ''
    for r in results:
        if r['sec'] != cur_sec:
            out.append(f"\n## {r['sec']}")
            cur_sec = r['sec']
        out.append(r['content'])

    return '\n'.join(out), filtered


# ---------------------------------------------------------------------------
# Alternative text diff engine — section canonical diff
# ---------------------------------------------------------------------------

def section_canonical_diff(old_text: str, new_text: str) -> Tuple[str, int]:
    """Alternative diff engine using classic unified diff grouped by section heading.

    Uses unified_diff n=3, filters trivial pairs with is_substantive
    (canonicalization + number/ref check). Context lines are removed from output —
    only ADDED / REMOVED blocks are emitted.

    Returns:
        (diff_string, filtered_count)
    """
    raw = list(difflib.unified_diff(old_text.splitlines(), new_text.splitlines(), n=3, lineterm=''))
    cur_sec = ''
    sec_emitted = False
    out: List[str] = []
    filtered = 0
    i = 2  # skip the --- / +++ header lines
    while i < len(raw):
        line = raw[i]
        if line.startswith('@@'):
            sec_emitted = False
            i += 1
        elif line.startswith(' '):
            bare = strip_tag(line[1:])
            if SECTION_RE.match(bare) and len(bare) < 100:
                cur_sec = bare
                sec_emitted = False
            i += 1
        elif line.startswith('-'):
            removed: List[str] = []
            added: List[str] = []
            while i < len(raw) and raw[i].startswith('-'):
                removed.append(raw[i][1:])
                i += 1
            while i < len(raw) and raw[i].startswith('+'):
                added.append(raw[i][1:])
                i += 1
            old_chunk = ' '.join(strip_tag(l) for l in removed)
            new_chunk = ' '.join(strip_tag(l) for l in added)
            if not removed or not added or is_substantive(old_chunk, new_chunk):
                if cur_sec and not sec_emitted:
                    out.append(f'\n## {cur_sec}')
                    sec_emitted = True
                for l in removed:
                    out.append(f'REMOVED: {l}')
                for l in added:
                    out.append(f'ADDED: {l}')
            else:
                filtered += 1
        elif line.startswith('+'):
            if cur_sec and not sec_emitted:
                out.append(f'\n## {cur_sec}')
                sec_emitted = True
            out.append(f'ADDED: {line[1:]}')
            i += 1
        else:
            i += 1
    return '\n'.join(out), filtered
