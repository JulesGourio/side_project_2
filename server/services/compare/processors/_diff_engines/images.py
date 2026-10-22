"""Perceptual image hashing and OLD/NEW image pairing (MODIFIED, ORIENTATION CHANGED, REMOVED, ADDED)."""

import base64
import hashlib
import io
import logging
from typing import Any, Dict, List, Tuple

logger = logging.getLogger(__name__)

_DHASH_SIZE = 8
_DHASH_THRESHOLD = 4           # Hamming distance <= 4 => visually identical
# Second-tier 16x16 dhash: a small edit to a large diagram can stay within the coarse 8x8 budget.
_DHASH16_SIZE = 16
_DHASH16_THRESHOLD = 12
_PAIR_PAGE_TOLERANCE  = 15     # max page distance to pair modified images (PDF/PPTX)
_PAIR_MAX_HAMMING     = 25     # max dhash distance to accept a pair as MODIFIED
_PAIR_MAX_AHASH       = 15     # max ahash distance — second gate for DOCX pairing (content check)
_IMG_MAX_DIM = 1024
_IMG_JPEG_QUALITY = 65
# Opens the note that tells the model (and, through factory.image_cap_warnings, the user) that images were dropped.
IMAGE_CAP_MARKER = '[Images dropped:'
_MAX_IMAGE_BLOCKS = 95          # the model API accepts at most 100 images/documents per request


def dhash(img_bytes: bytes, size: int = _DHASH_SIZE) -> str:
    """64-bit difference hash. Resistant to slight compression/resize artifacts.
    Falls back to MD5 hex if PIL is unavailable.

    :param img_bytes: image file bytes, in any format PIL reads
    :param size: grid side: the hash has ``size * size`` bits
    :return: the bit string, or the MD5 hex of the bytes when PIL fails
    """
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
    Used as a second gate alongside dhash for DOCX image pairing.

    :param img_bytes: image file bytes, in any format PIL reads
    :param size: grid side: the hash has ``size * size`` bits
    :return: the bit string, or the MD5 hex of the bytes when PIL fails
    """
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
    """Number of positions where two hash strings differ; the shorter one bounds the count.

    :param a: first hash string
    :param b: second hash string
    :return: the number of differing positions
    """
    return sum(x != y for x, y in zip(a, b))


def images_are_similar(h1: str, h2: str) -> bool:
    """Whether two dhash strings are visually identical.

    :param h1: dhash of the first image
    :param h2: dhash of the second image
    :return: True when the hashes differ by at most ``_DHASH_THRESHOLD`` bits; different lengths match only when equal
    """
    if len(h1) != len(h2):
        return h1 == h2
    return hamming(h1, h2) <= _DHASH_THRESHOLD


def hash16(img_bytes: bytes) -> str:
    """256-bit fine-grained difference hash (16x16) for the second-tier
    identity check. Falls back to MD5 hex like dhash().

    :param img_bytes: image file bytes
    :return: the 256-bit hash string of the 16x16 grid
    """
    return dhash(img_bytes, size=_DHASH16_SIZE)


def _fine_hashes_match(meta1: Dict[str, Any], meta2: Dict[str, Any]) -> bool:
    """Second-tier identity: compare 16x16 hashes when both sides carry one.

    Missing hashes (cached extractions without them, PIL fallback) keep the
    coarse-only behaviour.

    :param meta1: image record of the old side; may hold a ``hash16``
    :param meta2: image record of the new side; may hold a ``hash16``
    :return: True when the 16x16 hashes agree, or when one side has none
    """
    f1 = meta1.get('hash16')
    f2 = meta2.get('hash16')
    if not f1 or not f2:
        return True
    if len(f1) != len(f2):
        return f1 == f2
    return hamming(f1, f2) <= _DHASH16_THRESHOLD


def _thumbnail_b64(b64_str: str, max_px: int = 320) -> str:
    """Resize a base64 JPEG to max_px on its longest side and return new base64.

    :param b64_str: base64 image
    :param max_px: length of the longest side after the resize, in pixels
    :return: the base64 JPEG thumbnail, or the input unchanged when it cannot be decoded
    """
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

    :param old_imgs: images of the old document, keyed by hash; each record has ``b64``, ``pages`` and the transform
    :param new_imgs: images of the new document, keyed by hash
    :param show_position: True pairs images by page proximity (PDF, PPTX, DOCX); False by visual similarity only
    :return: ``(modified_pairs, orientation_pairs, remaining_removed, remaining_added)``, the pairs as ``(old hash, new
        hash)``
    """
    def _same_content(h1: str, imgs1: Dict, h2: str, imgs2: Dict) -> bool:
        """Whether an old and a new image are the same picture: both hashes and the flip transform agree.

        :param h1: hash of the old image
        :param imgs1: images of the old side
        :param h2: hash of the new image
        :param imgs2: images of the new side
        :return: True when the two images are the same
        """
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
    # Only images without a content match on the other side are considered: a document holding an image and its flip
    # would otherwise cross-pair its own twin.
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


# --- Image diff blocks (dual — OLD / NEW side-by-side with page-proximity pairing) ---

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

    :param old_imgs: images of the old document, keyed by hash
    :param new_imgs: images of the new document, keyed by hash
    :param show_position: True pairs images by page proximity (PDF, PPTX, DOCX); False by visual similarity only
    :return: the content blocks for the model: MODIFIED pairs, then REMOVED, then ADDED images
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
                    # The label that introduces a dropped image goes with it.
                    if capped and capped[-1].get('type') == 'text':
                        capped.pop()
                    continue
                seen += 1
            capped.append(b)
        capped.append({
            'type': 'text',
            'text': f'\n{IMAGE_CAP_MARKER} {img_count - _MAX_IMAGE_BLOCKS} additional image(s) omitted — limit of {_MAX_IMAGE_BLOCKS} images per request reached.]',
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

    :param old_imgs: images of the old document, keyed by hash
    :param new_imgs: images of the new document, keyed by hash
    :param show_position: True pairs images by page proximity (PDF, PPTX, DOCX); False by visual similarity only
    :return: one dict per image pair: ``status``, ``old_page``, ``new_page``, ``old_b64``, ``new_b64``, ``index``
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
