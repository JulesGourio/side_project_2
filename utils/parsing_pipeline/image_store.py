"""image_store.py — writes an extracted image to the volume and describes it for `image_metadata`.

Volume layout: {VOLUME_BASE_PATH}/{IDDOC}/{IDDOC}_IMG_{NNN}.png (page renders: `_PAGE_{NNN}`).
Shared by the Docling extraction (`image_utils`) and the XML / OLE fallbacks (`image_fallbacks`).
"""

import os
from typing import Any, Dict, Optional

from config import IMAGE_FORMAT, IMAGE_JPEG_QUALITY, IMAGE_MAX_DIMENSION, IMAGE_RESAMPLING


def save_image_to_volume(pil_img, volume_path: str, volume_base_path: str) -> tuple:
    """Save a PIL image to the Volume.

    The resolved target must stay under volume_base_path (path-traversal guard).
    Uses IMAGE_MAX_DIMENSION, IMAGE_RESAMPLING, IMAGE_FORMAT, IMAGE_JPEG_QUALITY from config.
    Supports PNG (lossless) and JPEG formats.
    """
    from PIL import Image

    resampling = getattr(Image.Resampling, IMAGE_RESAMPLING, Image.Resampling.LANCZOS)
    fmt = IMAGE_FORMAT.upper()

    base_path, _ = os.path.splitext(volume_path)
    ext = ".png" if fmt == "PNG" else ".jpg"
    volume_path = base_path + ext

    base = os.path.normpath(volume_base_path)
    target = os.path.normpath(volume_path)
    if not target.startswith(base):
        raise ValueError(f"Refusing to write outside volume base: {target}")

    # PNG supports RGBA; JPEG needs RGB
    if fmt == "JPEG" and pil_img.mode in ("RGBA", "P"):
        pil_img = pil_img.convert("RGB")

    pil_img.thumbnail((IMAGE_MAX_DIMENSION, IMAGE_MAX_DIMENSION), resampling)
    w, h = pil_img.size

    os.makedirs(os.path.dirname(target), exist_ok=True)

    # Explicit file handle + fsync — pil_img.save(path) can leave a truncated file on the volume.
    with open(target, "wb") as fh:
        if fmt == "PNG":
            pil_img.save(fh, format="PNG", optimize=True)
        else:
            pil_img.save(fh, format="JPEG", quality=IMAGE_JPEG_QUALITY)
        fh.flush()
        os.fsync(fh.fileno())

    # Belt-and-suspenders: verify the write landed before reporting success.
    if not os.path.exists(target) or os.path.getsize(target) == 0:
        raise IOError(f"image write did not land on volume: {target}")

    return target, w, h


def image_target_path(volume_base_path: str, doc_id, image_id: int, kind: str = "IMG") -> str:
    """Volume path of an image before the extension is settled by `save_image_to_volume`."""
    return os.path.join(volume_base_path, str(doc_id), f"{doc_id}_{kind}_{image_id:03d}.jpg")


def image_record(image_id: int, page_no: Optional[int], label: str, area_ratio: Optional[float], captions: list,
                 context_text: str, volume_path: Optional[str], width: Optional[int],
                 height: Optional[int]) -> Dict[str, Any]:
    """One entry of the `images` array of a parsed document (`image_utils.IMAGE_METADATA_SCHEMA`)."""
    return {
        "image_id": image_id, "page_no": page_no, "label": label,
        "area_ratio": round(area_ratio, 4) if area_ratio is not None else None,
        "captions": captions, "context_text": context_text,
        "volume_path": volume_path, "image_width": width, "image_height": height,
    }
