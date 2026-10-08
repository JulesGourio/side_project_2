"""image_utils.py — Image extraction, Volume storage & LLM description.

Worker-side image extraction from a Docling document plus an async vision-LLM
description engine. All tunables come from config.py.

Pipeline (1 Spark task = 1 document)
  Phase 1 (CPU/GPU, sync) : Docling parse + image extraction
  Phase 2 (I/O, sync)     : save JPEG to a Unity Catalog Volume

Public surface
  parse_and_extract_images_udf : parse + extract images + save to Volume (no LLM)
  describe_one_image           : describe a single image via a vision LLM (async)
  describe_all_images          : concurrent descriptions, semaphore-limited (async)

Volume layout : {VOLUME_BASE_PATH}/{IDDOC}/{IDDOC}_IMG_{NNN}.jpg
"""

import io
import os
import base64
import hashlib
import logging
import time
import asyncio
from collections import defaultdict
from typing import Any, Dict, List, Optional

import pandas as pd

from pyspark.sql import types as T
from pyspark.sql import functions as F
from pyspark.sql.functions import pandas_udf

logger = logging.getLogger(__name__)

from config import (
    IMAGE_MAX_DIMENSION,
    IMAGE_JPEG_QUALITY,
    IMAGE_RESAMPLING,
    IMAGE_FORMAT,
    IMAGE_DESCRIPTION_PROMPT,
    IMG_SKIP_MAX_DIM,
    IMG_SKIP_MIN_SIDE,
    LLM_MODEL_ENDPOINT,
    LLM_MAX_TOKENS,
    LLM_TEMPERATURE,
    LLM_MAX_RETRIES,
    LLM_MAX_CONCURRENT,
    LLM_OCR_TEXT_THRESHOLD,
    LLM_OCR_MAX_PAGES,
    LLM_OCR_MAX_TOKENS,
    PDF_PAGE_OCR_PROMPT,
)

from utils import (
    _cfg,
    ensure_config,
    _patch_worker_env,
    count_tokens,
    parse_with_docling,
    _parse_xlsx_openpyxl,
    _parse_xls,
)


# --- Spark schemas ---
IMAGE_METADATA_SCHEMA = T.StructType([
    T.StructField("image_id", T.IntegerType(), True),
    T.StructField("page_no", T.IntegerType(), True),
    T.StructField("label", T.StringType(), True),
    T.StructField("area_ratio", T.FloatType(), True),
    T.StructField("captions", T.ArrayType(T.StringType()), True),
    T.StructField("context_text", T.StringType(), True),
    T.StructField("volume_path", T.StringType(), True),
    T.StructField("image_width", T.IntegerType(), True),
    T.StructField("image_height", T.IntegerType(), True),
])

IMAGE_ARRAY_SCHEMA = T.ArrayType(IMAGE_METADATA_SCHEMA)

# Reference slide/frame area (widescreen raster equivalent) used by the pptx
# and legacy-ppt fallback parsers to size-filter decorative images via
# area_ratio — same role as the fixed A4 reference in the DOCX fallback.
_XML_FALLBACK_REF_AREA = 1280 * 720

FULL_PARSE_SCHEMA = T.StructType([
    T.StructField("text", T.StringType(), True),
    T.StructField("parser_error", T.StringType(), True),
    T.StructField("parser_strategy", T.StringType(), True),
    T.StructField("parse_time_seconds", T.FloatType(), True),
    T.StructField("images", IMAGE_ARRAY_SCHEMA, True),
    T.StructField("timings", T.MapType(T.StringType(), T.FloatType()), True),
])


# --- Image helpers ---
def _image_md5(pil_img) -> str:
    """MD5 of raw pixel bytes — fast, deterministic deduplication key."""
    return hashlib.md5(pil_img.tobytes()).hexdigest()


def _get_pil_image(pic, doc):
    for fn in (lambda: pic.get_image(doc=doc),
               lambda: pic.image.pil_image if pic.image else None):
        try:
            img = fn()
            if img is not None:
                return img
        except Exception:
            pass
    return None


def _save_image_to_volume(pil_img, volume_path: str, volume_base_path: str) -> tuple:
    """Save a PIL image to the Volume.

    The resolved target must stay under volume_base_path (path-traversal guard).
    Uses IMAGE_MAX_DIMENSION, IMAGE_RESAMPLING, IMAGE_FORMAT, IMAGE_JPEG_QUALITY from config.
    Supports PNG (lossless) and JPEG formats.
    """
    from PIL import Image

    resampling = getattr(Image.Resampling, IMAGE_RESAMPLING, Image.Resampling.LANCZOS)
    fmt = IMAGE_FORMAT.upper()

    # Adjust file extension to match format
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


def _page_size(doc, page_no):
    try:
        page = doc.pages.get(page_no)
        if page and page.size:
            return page.size.width, page.size.height
    except Exception:
        pass
    return None, None


def _resolve_caption(doc, ref) -> Optional[str]:
    try:
        parts = ref.cref.lstrip("#/").split("/")
        if len(parts) == 2:
            item = getattr(doc, parts[0], [])[int(parts[1])]
            return item.text if hasattr(item, "text") else None
    except Exception:
        pass
    return None


def _get_image_context(page_texts: list, page_no, bbox, caption_crefs: set,
                       max_tokens: int = 250) -> str:
    """Build before/after textual context around an image, token-budgeted."""
    if not bbox or not page_no or not page_texts:
        return ""
    try:
        img_mid_y = (bbox.t + bbox.b) / 2
        texts = [t for t in page_texts if getattr(t, "self_ref", None) not in caption_crefs]
        if not texts:
            return ""
        above = sorted([t for t in texts if t.prov[0].bbox.b <= img_mid_y],
                       key=lambda t: t.prov[0].bbox.b, reverse=True)
        below = sorted([t for t in texts if t.prov[0].bbox.t > img_mid_y],
                       key=lambda t: t.prov[0].bbox.t)

        def accumulate(sorted_texts, limit):
            acc, toks = [], 0
            for t in sorted_texts:
                s = t.text.strip()
                tc = count_tokens(s)
                if toks + tc > limit and toks > 0:
                    break
                acc.append(s); toks += tc
            return acc

        half = max_tokens // 2
        before = accumulate(above, half)[::-1]
        after = accumulate(below, half)
        before_str, after_str = "\n".join(before), "\n".join(after)
        if before_str or after_str:
            return f"{before_str}\n\n[... IMAGE INSERTED HERE ...]\n\n{after_str}".strip()
        return ""
    except Exception as e:
        logger.warning("Image context extraction failed: %s", str(e)[:200])
        return ""


# --- Image extraction from a Docling document ---
def extract_images_from_doc(doc, doc_id: str, volume_base_path: str,
                            min_area_ratio: float, max_repeat: int,
                            timings: Optional[dict] = None) -> List[Dict[str, Any]]:
    pictures = list(doc.pictures)
    if not pictures:
        return []

    # Pre-group page texts once to avoid O(N*M) scans.
    texts_by_page = defaultdict(list)
    for t in doc.texts:
        if t.prov:
            texts_by_page[t.prov[0].page_no].append(t)

    # Hash + dedup pass.
    t_hash = time.perf_counter()
    hash_to_pages: Dict[str, set] = {}
    pic_cache = []
    for pic in pictures:
        prov = pic.prov[0] if pic.prov else None
        page_no = prov.page_no if prov else None
        pil_img = _get_pil_image(pic, doc)
        md5 = _image_md5(pil_img) if pil_img else None
        if md5:
            hash_to_pages.setdefault(md5, set())
            if page_no is not None:
                hash_to_pages[md5].add(page_no)
        pic_cache.append((pic, page_no, pil_img, md5))
    if timings is not None:
        timings["image_hashing_seconds"] = float(time.perf_counter() - t_hash)

    repeating = {h for h, pages in hash_to_pages.items() if len(pages) > max_repeat}
    manifest: List[Dict[str, Any]] = []
    t_ctx_total = t_vol_total = 0.0

    for pic, page_no, pil_img, md5 in pic_cache:
        if md5 and md5 in repeating:
            continue
        prov = pic.prov[0] if pic.prov else None
        bbox = prov.bbox if prov else None

        area_ratio = None
        if bbox and page_no is not None:
            pw, ph = _page_size(doc, page_no)
            if pw and ph:
                area_ratio = (abs(bbox.r - bbox.l) * abs(bbox.b - bbox.t)) / (pw * ph)
        if area_ratio is not None and area_ratio < min_area_ratio:
            continue

        caption_crefs = {c.cref for c in pic.captions}
        captions = [txt for c in pic.captions for txt in [_resolve_caption(doc, c)] if txt]

        t0 = time.perf_counter()
        context_text = _get_image_context(texts_by_page.get(page_no, []), page_no, bbox, caption_crefs)
        t_ctx_total += time.perf_counter() - t0

        image_id = len(manifest)
        volume_path = img_w = img_h = None
        if pil_img is not None:
            target = os.path.join(volume_base_path, str(doc_id), f"{doc_id}_IMG_{image_id:03d}.jpg")
            t0 = time.perf_counter()
            try:
                volume_path, img_w, img_h = _save_image_to_volume(pil_img, target, volume_base_path)
            except Exception as e:
                logger.warning("Failed to save image to volume: %s", str(e)[:200])
            t_vol_total += time.perf_counter() - t0

        manifest.append({
            "image_id": image_id, "page_no": page_no,
            "label": pic.label.value if hasattr(pic.label, "value") else str(pic.label),
            "area_ratio": round(area_ratio, 4) if area_ratio is not None else None,
            "captions": captions, "context_text": context_text,
            "volume_path": volume_path, "image_width": img_w, "image_height": img_h,
        })

    if timings is not None:
        timings["context_extraction_seconds"] = float(t_ctx_total)
        timings["volume_write_seconds"] = float(t_vol_total)
    return manifest


def _save_candidate_images(candidates: List[dict], repeating: set, doc_id: str,
                           volume_base_path: str, min_area_ratio: float,
                           log_prefix: str) -> List[Dict[str, Any]]:
    """Filter repeating/too-small candidates, save the rest to the Volume, build
    their image_metadata dicts. Shared tail of the docx/pptx/legacy-ppt fallback parsers."""
    images: List[Dict[str, Any]] = []
    for cand in candidates:
        if cand["md5"] in repeating or cand["area_ratio"] < min_area_ratio:
            continue
        image_id = len(images)
        target = os.path.join(volume_base_path, str(doc_id), f"{doc_id}_IMG_{image_id:03d}.jpg")
        volume_path = img_w = img_h = None
        try:
            volume_path, img_w, img_h = _save_image_to_volume(cand["pil_img"], target, volume_base_path)
        except Exception as e:
            logger.warning("%s: failed to save image: %s", log_prefix, str(e)[:200])
        images.append({
            "image_id": image_id, "page_no": cand.get("page_no", image_id + 1), "label": "picture",
            "area_ratio": round(cand["area_ratio"], 4), "captions": [],
            "context_text": cand.get("context_text", ""),
            "volume_path": volume_path, "image_width": img_w, "image_height": img_h,
        })
    return images


# ===========================================================================
# DOCX / PPTX XML fallback (when Docling can't read the file / extract images,
# or when the file is too large for Docling's page/slide rasterization to
# handle without OOM-ing — this fallback never rasterizes anything, so file
# size is not a risk factor here).
# ===========================================================================
def _fallback_parse_docx(content_bytes: bytes, doc_id: str, volume_base_path: str,
                         min_area_ratio: float, max_repeat: int) -> Optional[Dict[str, Any]]:
    import zipfile
    from PIL import Image
    from lxml import etree

    try:
        zf = zipfile.ZipFile(io.BytesIO(content_bytes))
    except Exception as e:
        logger.warning("DOCX fallback: not a readable zip for doc_id=%s: %s", doc_id, str(e)[:200])
        return None
    if "word/document.xml" not in zf.namelist():
        zf.close()
        return None

    t0 = time.time()
    ns_w = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    ns_r = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    ns_a = "http://schemas.openxmlformats.org/drawingml/2006/main"
    ns_rels = "http://schemas.openxmlformats.org/package/2006/relationships"

    rels_map: Dict[str, str] = {}
    for rels_path in [n for n in zf.namelist() if n.startswith("word/_rels/") and n.endswith(".rels")]:
        with zf.open(rels_path) as rf:
            rels_tree = etree.parse(rf)
        for rel in rels_tree.iter(f"{{{ns_rels}}}Relationship"):
            target = rel.get("Target", "")
            if "media/" in target:
                rels_map[rel.get("Id")] = target if target.startswith("word/") else f"word/{target}"

    with zf.open("word/document.xml") as doc_xml:
        tree = etree.parse(doc_xml)
    body = tree.find(f".//{{{ns_w}}}body")
    if body is None:
        zf.close()
        return None

    current_page = 1
    headings: Dict[int, str] = {}
    paragraphs: List[str] = []
    occurrences = []

    for para in body.iter(f"{{{ns_w}}}p"):
        for _ in para.iter(f"{{{ns_w}}}lastRenderedPageBreak"):
            current_page += 1
        for br in para.iter(f"{{{ns_w}}}br"):
            if br.get(f"{{{ns_w}}}type") == "page":
                current_page += 1
        ppr = para.find(f"{{{ns_w}}}pPr")
        if ppr is not None:
            pstyle = ppr.find(f"{{{ns_w}}}pStyle")
            if pstyle is not None:
                style_val = pstyle.get(f"{{{ns_w}}}val", "")
                if any(kw in style_val.lower() for kw in ("heading", "titre", "titulo")):
                    level = next((int(ch) for ch in style_val if ch.isdigit()), 1)
                    heading_text = "".join(t.text for t in para.iter(f"{{{ns_w}}}t") if t.text).strip()
                    if heading_text:
                        headings[level] = heading_text
                        for l in [l for l in headings if l > level]:
                            del headings[l]
            if ppr.find(f"{{{ns_w}}}sectPr") is not None:
                current_page += 1
        para_text = "".join(t.text for t in para.iter(f"{{{ns_w}}}t") if t.text).strip()
        if para_text:
            paragraphs.append(para_text)
        for blip in para.iter(f"{{{ns_a}}}blip"):
            embed_id = blip.get(f"{{{ns_r}}}embed")
            if embed_id and embed_id in rels_map:
                ctx = " > ".join(v for _, v in sorted(headings.items()))
                if para_text:
                    ctx = f"{ctx} [...image...] {para_text[:200]}" if ctx else para_text[:200]
                occurrences.append({"media_file": rels_map[embed_id], "page_no": current_page, "context_text": ctx})

    for part_name in [n for n in zf.namelist()
                      if n.startswith("word/") and n.endswith(".xml") and n != "word/document.xml"
                      and any(kw in n for kw in ("header", "footer"))]:
        with zf.open(part_name) as pf:
            part_tree = etree.parse(pf)
        for blip in part_tree.iter(f"{{{ns_a}}}blip"):
            embed_id = blip.get(f"{{{ns_r}}}embed")
            if embed_id and embed_id in rels_map:
                label = "header" if "header" in part_name else "footer"
                occurrences.append({"media_file": rels_map[embed_id], "page_no": 1,
                                    "context_text": f"[{label} image]"})

    text = "\n".join(paragraphs)
    hash_count: Dict[str, int] = {}
    media_info: Dict[str, dict] = {}
    for mf in [n for n in zf.namelist() if n.startswith("word/media/")]:
        try:
            img_bytes = zf.read(mf)
            md5 = hashlib.md5(img_bytes).hexdigest()
            hash_count[md5] = hash_count.get(md5, 0) + 1
            pil_img = Image.open(io.BytesIO(img_bytes)); pil_img.load()
            w, h = pil_img.size
            media_info[mf] = {"md5": md5, "pil_img": pil_img, "area_ratio": (w * h) / (794 * 1123)}
        except Exception as e:
            logger.warning("DOCX fallback: unreadable embedded image %s: %s", mf, str(e)[:200])
            continue

    repeating = {h for h, c in hash_count.items() if c > max_repeat}
    candidates = []
    for occ in occurrences:
        phys = media_info.get(occ["media_file"])
        if phys:
            candidates.append({**phys, "page_no": occ["page_no"], "context_text": occ.get("context_text", "")})
    images = _save_candidate_images(candidates, repeating, doc_id, volume_base_path,
                                    min_area_ratio, log_prefix="DOCX fallback")

    zf.close()
    return {"text": text, "parser_strategy": "xml_fallback:docx",
            "parse_time_seconds": round(time.time() - t0, 3), "images": images}


def _fallback_parse_pptx(content_bytes: bytes, doc_id: str, volume_base_path: str,
                         min_area_ratio: float, max_repeat: int) -> Optional[Dict[str, Any]]:
    import zipfile
    import re as _re
    import posixpath
    from PIL import Image
    from lxml import etree

    try:
        zf = zipfile.ZipFile(io.BytesIO(content_bytes))
    except Exception as e:
        logger.warning("PPTX fallback: not a readable zip for doc_id=%s: %s", doc_id, str(e)[:200])
        return None
    slide_names = [n for n in zf.namelist() if _re.match(r"^ppt/slides/slide\d+\.xml$", n)]
    if not slide_names:
        zf.close()
        return None
    slide_names.sort(key=lambda n: int(_re.search(r"(\d+)", n).group(1)))

    t0 = time.time()
    ns_a = "http://schemas.openxmlformats.org/drawingml/2006/main"
    ns_r = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    ns_rels = "http://schemas.openxmlformats.org/package/2006/relationships"

    slide_texts: List[str] = []
    occurrences = []
    for slide_idx, slide_name in enumerate(slide_names, start=1):
        rels_path = f"ppt/slides/_rels/{posixpath.basename(slide_name)}.rels"
        rels_map: Dict[str, str] = {}
        if rels_path in zf.namelist():
            with zf.open(rels_path) as rf:
                rels_tree = etree.parse(rf)
            for rel in rels_tree.iter(f"{{{ns_rels}}}Relationship"):
                target = rel.get("Target", "")
                resolved = posixpath.normpath(posixpath.join("ppt/slides", target))
                if "media/" in resolved:
                    rels_map[rel.get("Id")] = resolved

        with zf.open(slide_name) as sf:
            slide_tree = etree.parse(sf)
        texts = [t.text.strip() for t in slide_tree.iter(f"{{{ns_a}}}t") if t.text and t.text.strip()]
        slide_text = "\n".join(texts)
        if slide_text:
            slide_texts.append(f"--- Slide {slide_idx} ---\n{slide_text}")

        for blip in slide_tree.iter(f"{{{ns_a}}}blip"):
            embed_id = blip.get(f"{{{ns_r}}}embed")
            if embed_id and embed_id in rels_map:
                ctx = slide_text[:200] if slide_text else f"Slide {slide_idx}"
                occurrences.append({"media_file": rels_map[embed_id], "page_no": slide_idx, "context_text": ctx})

    text = "\n\n".join(slide_texts)
    hash_count: Dict[str, int] = {}
    media_info: Dict[str, dict] = {}
    for mf in [n for n in zf.namelist() if n.startswith("ppt/media/")]:
        try:
            img_bytes = zf.read(mf)
            md5 = hashlib.md5(img_bytes).hexdigest()
            hash_count[md5] = hash_count.get(md5, 0) + 1
            pil_img = Image.open(io.BytesIO(img_bytes)); pil_img.load()
            w, h = pil_img.size
            media_info[mf] = {"md5": md5, "pil_img": pil_img, "area_ratio": (w * h) / _XML_FALLBACK_REF_AREA}
        except Exception as e:
            logger.warning("PPTX fallback: unreadable embedded image %s: %s", mf, str(e)[:200])
            continue

    repeating = {h for h, c in hash_count.items() if c > max_repeat}
    candidates = []
    for occ in occurrences:
        phys = media_info.get(occ["media_file"])
        if phys:
            candidates.append({**phys, "page_no": occ["page_no"], "context_text": occ.get("context_text", "")})
    images = _save_candidate_images(candidates, repeating, doc_id, volume_base_path,
                                    min_area_ratio, log_prefix="PPTX fallback")

    zf.close()
    return {"text": text, "parser_strategy": "xml_fallback:pptx",
            "parse_time_seconds": round(time.time() - t0, 3), "images": images}


# --- Legacy .ppt (binary OLE compound file, pre-2007) heuristic fallback ---
# Byte-scan heuristic, not a real MS-PPT record parser — no maintained pure-Python one exists.
_OLE_MIN_TEXT_RUN_CHARS = 4


def _extract_ole_text_runs(buf: bytes) -> List[str]:
    runs: List[str] = []
    n = len(buf)
    i = 0
    while i < n:
        j = i
        chars = []
        while j + 1 < n and buf[j + 1] == 0 and 0x20 <= buf[j] < 0x7F:
            chars.append(chr(buf[j]))
            j += 2
        if len(chars) >= _OLE_MIN_TEXT_RUN_CHARS:
            runs.append("".join(chars))
            i = j
            continue
        j = i
        chars = []
        while j < n and 0x20 <= buf[j] < 0x7F:
            chars.append(chr(buf[j]))
            j += 1
        if len(chars) >= _OLE_MIN_TEXT_RUN_CHARS:
            runs.append("".join(chars))
            i = j
            continue
        i += 1
    return runs


def _carve_images_from_bytes(buf: bytes) -> List[bytes]:
    chunks: List[bytes] = []
    n = len(buf)
    i = 0
    png_end_sig = b"IEND\xae\x42\x60\x82"
    while i < n - 3:
        if buf[i:i + 3] == b"\xff\xd8\xff":
            end = buf.find(b"\xff\xd9", i + 3)
            if end != -1:
                chunks.append(buf[i:end + 2])
                i = end + 2
                continue
        elif buf[i:i + 8] == b"\x89PNG\r\n\x1a\n":
            end = buf.find(png_end_sig, i + 8)
            if end != -1:
                chunks.append(buf[i:end + len(png_end_sig)])
                i = end + len(png_end_sig)
                continue
        i += 1
    return chunks


def _fallback_parse_ppt_legacy(content_bytes: bytes, doc_id: str, volume_base_path: str,
                               min_area_ratio: float, max_repeat: int) -> Optional[Dict[str, Any]]:
    t0 = time.time()
    try:
        import olefile
        ole = olefile.OleFileIO(io.BytesIO(content_bytes))
    except Exception as e:
        logger.warning("PPT (legacy) fallback: not a readable OLE container for doc_id=%s: %s", doc_id, str(e)[:200])
        return None

    def _find_stream(name: str):
        name_lower = name.lower()
        for entry in ole.listdir(streams=True, storages=False):
            if entry and entry[-1].lower() == name_lower:
                return entry
        return None

    doc_entry = _find_stream("PowerPoint Document")
    if doc_entry is None:
        ole.close()
        return None
    with ole.openstream(doc_entry) as sf:
        doc_bytes = sf.read()

    pics_bytes = b""
    pics_entry = _find_stream("Pictures")
    if pics_entry is not None:
        with ole.openstream(pics_entry) as pf:
            pics_bytes = pf.read()
    ole.close()

    text = "\n".join(_extract_ole_text_runs(doc_bytes))

    from PIL import Image
    hash_count: Dict[str, int] = {}
    candidates = []
    for raw in _carve_images_from_bytes(pics_bytes):
        try:
            md5 = hashlib.md5(raw).hexdigest()
            hash_count[md5] = hash_count.get(md5, 0) + 1
            pil_img = Image.open(io.BytesIO(raw)); pil_img.load()
            w, h = pil_img.size
            candidates.append({"md5": md5, "pil_img": pil_img, "area_ratio": (w * h) / _XML_FALLBACK_REF_AREA})
        except Exception as e:
            logger.warning("PPT (legacy) fallback: unreadable carved image for doc_id=%s: %s", doc_id, str(e)[:200])
            continue

    repeating = {h for h, c in hash_count.items() if c > max_repeat}
    images = _save_candidate_images(candidates, repeating, doc_id, volume_base_path,
                                    min_area_ratio, log_prefix="PPT (legacy) fallback")

    return {"text": text, "parser_strategy": "ole_heuristic:ppt",
            "parse_time_seconds": round(time.time() - t0, 3), "images": images}


def _render_pdf_pages_for_llm_ocr(content_bytes: bytes, doc_id: str, volume_base_path: str,
                                  max_pages: int) -> List[Dict[str, Any]]:
    """Render the first `max_pages` pages of a PDF to JPEG for LLM-based OCR — last resort when Docling still yields near-empty text."""
    try:
        import pypdfium2 as pdfium
    except Exception as e:
        logger.warning("LLM-OCR page render: pypdfium2 unavailable for doc_id=%s: %s", doc_id, str(e)[:200])
        return []

    images: List[Dict[str, Any]] = []
    try:
        pdf = pdfium.PdfDocument(io.BytesIO(content_bytes))
        total_pages = len(pdf)
        n_pages = min(total_pages, max_pages)
        if total_pages > max_pages:
            logger.warning(
                "LLM-OCR page render: doc_id=%s has %d pages, only rendering the first %d -- "
                "remaining %d page(s) will not be indexed; consider a manual reparse with a higher "
                "PARSING_LLM_OCR_MAX_PAGES if this document's tail matters.",
                doc_id, total_pages, max_pages, total_pages - max_pages,
            )
        for page_no in range(n_pages):
            pil_img = pdf[page_no].render(scale=2.0).to_pil().convert("RGB")
            image_id = len(images)
            target = os.path.join(volume_base_path, str(doc_id), f"{doc_id}_PAGE_{image_id:03d}.jpg")
            volume_path = img_w = img_h = None
            try:
                volume_path, img_w, img_h = _save_image_to_volume(pil_img, target, volume_base_path)
            except Exception as e:
                logger.warning("LLM-OCR page render: failed to save page %d for doc_id=%s: %s",
                               page_no, doc_id, str(e)[:200])
                continue
            images.append({
                "image_id": image_id, "page_no": page_no + 1, "label": "scanned_page",
                "area_ratio": 1.0, "captions": [], "context_text": "",
                "volume_path": volume_path, "image_width": img_w, "image_height": img_h,
            })
    except Exception as e:
        logger.warning("LLM-OCR page render failed for doc_id=%s: %s", doc_id, str(e)[:200])
        return []
    return images


# --- Prompt builder ---
def build_llm_prompt(context_text: str, division: str = "", category: str = "", label: str = "") -> str:
    # Context is passed as one block, not split before/after: it's just a
    # domain hint (the model must not copy it back — see the prompt itself).
    ctx = (context_text or "").replace(" [...image...] ", " ").strip()[:1200]
    # Escape braces so the context cannot interfere with str.format().
    ctx = (ctx or "(none)").replace("{", "{{").replace("}", "}}")
    div = (division or "unknown").replace("{", "{{").replace("}", "}}")
    cat = (category or "unknown").replace("{", "{{").replace("}", "}}")
    template = PDF_PAGE_OCR_PROMPT if label == "scanned_page" else IMAGE_DESCRIPTION_PROMPT
    return template.format(context=ctx, division=div, category=cat)


def is_skip_response(text) -> bool:
    """True if the model refused to describe the image (response starts with SKIP)."""
    return bool(text) and str(text).strip().upper().startswith("SKIP")


def image_status_col(volume_path, width, height):
    """Initial status of an image in image_metadata (deterministic, no LLM call).

    EXTRACTION_FAILED  : no image was written to the volume.
    SKIPPED_DECORATIVE : image is physically illegible (too small) or a
                         separator line — no LLM call, no indexing.
    PENDING            : awaiting description.

    Deliberately conservative: does NOT filter on a large width/height ratio
    (a real horizontal flowchart is legitimately wide). See IMG_SKIP_* in config.py.
    """
    largest = F.greatest(F.coalesce(width, F.lit(0)), F.coalesce(height, F.lit(0)))
    smallest = F.least(F.coalesce(width, F.lit(0)), F.coalesce(height, F.lit(0)))
    decorative = (largest < F.lit(IMG_SKIP_MAX_DIM)) | (smallest < F.lit(IMG_SKIP_MIN_SIDE))
    return (
        F.when(volume_path.isNull(), F.lit("EXTRACTION_FAILED"))
         .when(decorative, F.lit("SKIPPED_DECORATIVE"))
         .otherwise(F.lit("PENDING"))
    )


# --- UDF: parse + extract images + save to Volume ---
_TIMING_KEYS = ("docling_import_seconds", "docling_load_seconds", "tmp_file_write_seconds",
                "docling_convert_seconds", "markdown_export_seconds",
                "ocr_fallback_seconds",
                "image_hashing_seconds", "context_extraction_seconds", "volume_write_seconds")


# Per-file timeout (seconds). Files exceeding this are marked TIMEOUT.
try:
    from config import PARSE_TIMEOUT_SECONDS
except ImportError:
    PARSE_TIMEOUT_SECONDS = 200

import signal as _signal


class _ParseTimeout:
    """Kills a parse that exceeds `seconds` (SIGALRM — Spark Linux workers, main thread).

    No-op if SIGALRM is unavailable (e.g. Windows) or seconds<=0. Prevents a
    pathological file (giant PDF/xlsx) from blocking a Spark task for hours.
    """
    def __init__(self, seconds: int):
        self.seconds = int(seconds) if seconds else 0
        self._active = self.seconds > 0 and hasattr(_signal, "SIGALRM")

    def _raise(self, *_):
        raise TimeoutError(f"parse exceeded {self.seconds}s")

    def __enter__(self):
        if self._active:
            try:
                self._old = _signal.signal(_signal.SIGALRM, self._raise)
                _signal.setitimer(_signal.ITIMER_REAL, self.seconds)
            except ValueError:
                # signal.signal() can only be called from the main thread.
                # In Spark UDF workers (USER_ISOLATION cluster), this raises
                # ValueError — disable per-file timeout gracefully.
                self._active = False
        return self

    def __exit__(self, *exc):
        if self._active:
            _signal.setitimer(_signal.ITIMER_REAL, 0)
            _signal.signal(_signal.SIGALRM, self._old)
        return False


def _fallback_or_error(fb: Optional[Dict[str, Any]], ext_lower: str, timings: dict) -> dict:
    if fb:
        return {"text": fb["text"], "parser_error": None, "parser_strategy": fb["parser_strategy"],
                "parse_time_seconds": fb["parse_time_seconds"], "images": fb["images"], "timings": timings}
    return {"text": "", "parser_error": f"fallback_failed:{ext_lower}", "parser_strategy": "fallback_failed",
            "parse_time_seconds": 0.0, "images": [], "timings": timings}


@pandas_udf(FULL_PARSE_SCHEMA)
def parse_and_extract_images_udf(content_series: pd.Series, ext_series: pd.Series,
                                 doc_id_series: pd.Series, volume_path_series: pd.Series,
                                 min_area_ratio_series: pd.Series, max_repeat_series: pd.Series,
                                 enable_timing_series: pd.Series,
                                 xml_only_series: pd.Series) -> pd.DataFrame:
    # xml_only_series must stay a required positional column — pandas UDFs reject a default/Optional[...] one.
    _patch_worker_env()
    ensure_config()
    enable_timing = bool(enable_timing_series.iloc[0]) if len(enable_timing_series) else False
    results = []

    for content, ext, doc_id, vol_path, min_ar, max_rep, xml_only in zip(
        content_series, ext_series, doc_id_series,
        volume_path_series, min_area_ratio_series, max_repeat_series, xml_only_series,
    ):
        if content is None:
            results.append({"text": "", "parser_error": "No Content", "parser_strategy": "none",
                            "parse_time_seconds": 0.0, "images": [], "timings": {}})
            continue

        doc_id_str = str(doc_id) if doc_id is not None else "unknown"
        vol_base = str(vol_path) if vol_path is not None else _cfg("VOLUME_BASE_PATH")
        min_ar_val = float(min_ar) if min_ar is not None else _cfg("MIN_AREA_RATIO", fallback=0.03)
        max_rep_val = int(max_rep) if max_rep is not None else _cfg("MAX_REPEAT", fallback=3)
        timings = {k: 0.0 for k in _TIMING_KEYS} if enable_timing else {}
        ext_lower = str(ext).lower().strip(".")

        # Over the Docling-safe size threshold: skip Docling entirely, go straight to the zip/XML fallback.
        if bool(xml_only) and ext_lower in ("docx", "docm", "pptx", "pptm"):
            fb_fn = _fallback_parse_docx if ext_lower in ("docx", "docm") else _fallback_parse_pptx
            fb = fb_fn(bytes(content), doc_id_str, vol_base, min_ar_val, max_rep_val)
            results.append(_fallback_or_error(fb, ext_lower, timings))
            continue

        # Docling doesn't support legacy binary .ppt at all — go straight to the OLE heuristic fallback.
        if ext_lower == "ppt":
            fb = _fallback_parse_ppt_legacy(bytes(content), doc_id_str, vol_base, min_ar_val, max_rep_val)
            results.append(_fallback_or_error(fb, ext_lower, timings))
            continue

        # Manual "column: value" parser, not Docling — Docling's one large markdown table chunks/embeds poorly.
        try:
            with _ParseTimeout(PARSE_TIMEOUT_SECONDS):
                if ext_lower in ("xlsx", "xlsm", "xlsb"):
                    res = _parse_xlsx_openpyxl(bytes(content), time.perf_counter(), ext_lower)
                    docling_doc = None
                elif ext_lower == "xls":
                    res = _parse_xls(bytes(content), time.perf_counter())
                    docling_doc = None
                else:
                    res = parse_with_docling(content, str(ext), timings=(timings if enable_timing else None))
                    docling_doc = res.pop("_docling_doc", None)
        except TimeoutError as e:
            results.append({"text": "", "parser_error": str(e), "parser_strategy": "timeout",
                            "parse_time_seconds": float(PARSE_TIMEOUT_SECONDS), "images": [], "timings": {}})
            continue
        images = []

        if docling_doc is not None:
            try:
                images = extract_images_from_doc(
                    docling_doc, doc_id=doc_id_str, volume_base_path=vol_base,
                    min_area_ratio=min_ar_val, max_repeat=max_rep_val,
                    timings=(timings if enable_timing else None),
                )
            except Exception as e:
                logger.warning("Image extraction failed for %s: %s", doc_id_str, str(e)[:200])

        if ext_lower in ("docx", "docm", "pptx", "pptm"):
            fb_fn = _fallback_parse_docx if ext_lower in ("docx", "docm") else _fallback_parse_pptx
            # Docling parsed but found no saveable image → try the XML fallback.
            if docling_doc is not None and not any(img.get("volume_path") for img in images):
                fb = fb_fn(bytes(content), doc_id_str, vol_base, min_ar_val, max_rep_val)
                if fb and fb.get("images"):
                    images = fb["images"]
            # Docling failed entirely → take text + images from the fallback.
            elif docling_doc is None and res.get("parser_error"):
                fb = fb_fn(bytes(content), doc_id_str, vol_base, min_ar_val, max_rep_val)
                if fb:
                    if fb.get("text"):
                        res.update(text=fb["text"], parser_error=None,
                                   parser_strategy=fb["parser_strategy"],
                                   parse_time_seconds=fb["parse_time_seconds"])
                    if fb.get("images"):
                        images = fb["images"]

        # Last resort: Docling left near-nothing on this PDF — render pages and queue them for LLM-OCR instead of failing the document.
        # Gated on "not images": a page render duplicates whatever a real extracted image already
        # shows, so only fall back here when Docling found no embedded images to begin with.
        if ext_lower == "pdf" and not images and len((res.get("text") or "").strip()) < LLM_OCR_TEXT_THRESHOLD:
            page_images = _render_pdf_pages_for_llm_ocr(bytes(content), doc_id_str, vol_base, LLM_OCR_MAX_PAGES)
            if page_images:
                images = images + page_images
                res["parser_strategy"] = "pending_llm_ocr:pdf"
                res["parser_error"] = None

        res["images"] = images
        res["timings"] = timings
        results.append(res)

    return pd.DataFrame(results)


# --- Async token-bucket rate limiter ---
class _AsyncRateLimiter:
    """Asyncio-compatible token-bucket rate limiter.

    Limits throughput to `rate_per_minute` requests/minute on average.
    Starts with a single burst token so the first request is instant;
    subsequent tokens refill at the given rate.
    Acquire one token per LLM call to honour the endpoint ITPM quota.
    """
    __slots__ = ("_rate", "_tokens", "_updated_at", "_lock")

    def __init__(self, rate_per_minute: float) -> None:
        self._rate: float       = rate_per_minute / 60.0   # tokens / second
        self._tokens: float     = 1.0                      # 1 initial burst token
        self._updated_at: float = 0.0                      # set on first acquire
        self._lock              = asyncio.Lock()

    async def acquire(self) -> None:
        """Block until one token is available, then consume it."""
        while True:
            async with self._lock:
                loop = asyncio.get_running_loop()
                now  = loop.time()
                if self._updated_at == 0.0:
                    self._updated_at = now
                elapsed      = now - self._updated_at
                self._tokens = min(
                    self._rate * 60,                       # cap: 1 full minute of tokens
                    self._tokens + elapsed * self._rate,
                )
                self._updated_at = now
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return
                wait_s = (1.0 - self._tokens) / self._rate
            await asyncio.sleep(wait_s)


def safe_requests_per_minute(itpm_budget: int, otpm_budget: int, qph_budget: int,
                             avg_in: int, avg_out: int) -> float:
    """Safe requests/min = min of the 3 quota axes (input, output, requests).

    The bottleneck is usually OUTPUT (low OTPM + reasoning tokens).
    Returns a float >= 1, to pass to describe_all_images(requests_per_minute=...).
    """
    rpm_in  = itpm_budget / max(1, avg_in)
    rpm_out = otpm_budget / max(1, avg_out)
    rpm_qph = qph_budget / 60.0
    return max(1.0, min(rpm_in, rpm_out, rpm_qph))


# --- Async LLM image-description engine ---
async def describe_one_image(client, row_data: dict, semaphore: asyncio.Semaphore,
                             model: str = None, max_tokens: int = None,
                             temperature: float = None, max_retries: int = None,
                             timeout: float = 90.0,
                             rate_limiter: "_AsyncRateLimiter | None" = None) -> dict:
    """Describe one image. Retries on transient errors; never raises (returns a
    status of DONE / ERROR). Reads the JPEG from the Volume and sends it base64."""
    from tenacity import AsyncRetrying, stop_after_attempt, wait_exponential

    model = model or LLM_MODEL_ENDPOINT
    max_tokens = max_tokens or LLM_MAX_TOKENS
    # Full-page transcription runs longer than a short image description -- give it its own,
    # more generous budget regardless of what the batch was called with.
    if row_data.get("label") == "scanned_page":
        max_tokens = max(max_tokens, LLM_OCR_MAX_TOKENS)
    temperature = temperature if temperature is not None else LLM_TEMPERATURE
    max_retries = max_retries or LLM_MAX_RETRIES

    try:
        with open(row_data["volume_path"], "rb") as f:
            image_b64 = base64.b64encode(f.read()).decode()
    except Exception as e:
        return {**row_data, "description": f"[ERROR: failed to read image: {str(e)[:200]}]",
                "input_tokens": 0, "output_tokens": 0, "status": "ERROR"}

    # Build prompt with category context from image_metadata
    category_parts = [p for p in (row_data.get("niveau_plus_1", ""),
                                   row_data.get("niveau_plus_2", "")) if p]
    messages = [{"role": "user", "content": [
        {"type": "text", "text": build_llm_prompt(
            row_data.get("context_text", ""),
            division=row_data.get("division", ""),
            category=" > ".join(category_parts) if category_parts else "",
            label=row_data.get("label", ""),
        )},
        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"}},
    ]}]

    try:
        async for attempt in AsyncRetrying(
            stop=stop_after_attempt(max_retries),
            wait=wait_exponential(multiplier=2, min=2, max=30), reraise=True,
        ):
            with attempt:
                # Acquire rate-limit token INSIDE the retry loop so each
                # retry also waits for the token bucket — prevents 429 cascades.
                if rate_limiter is not None:
                    await rate_limiter.acquire()
                async with semaphore:
                    resp = await asyncio.wait_for(
                        client.chat.completions.create(
                            model=model, messages=messages,
                            max_tokens=max_tokens, temperature=temperature,
                        ), timeout=timeout,
                    )
        usage = getattr(resp, "usage", None)
        content = resp.choices[0].message.content
        finish_reason = resp.choices[0].finish_reason
        # Some endpoints (e.g. Gemini) return content as a list of parts.
        if isinstance(content, list):
            content = "".join(p.get("text", "") if isinstance(p, dict) else str(p) for p in content)
        content = (content or "").strip()
        in_tok = getattr(usage, "prompt_tokens", 0) if usage else 0
        out_tok = getattr(usage, "completion_tokens", 0) if usage else 0
        # Model judged the image to carry no information -> SKIPPED (not indexed downstream).
        if is_skip_response(content):
            status = "SKIPPED"
        elif not content and finish_reason == "length":
            # GPT-5 reasoning tokens exhausted the max_tokens budget — no visible content.
            status = "ERROR"
            content = f"[ERROR: max_tokens={max_tokens} exhausted by reasoning (output_tokens={out_tok}, finish_reason=length) — increase LLM_MAX_TOKENS]"
        elif not content:
            # Blank response with no SKIP prefix — a failed call.
            status = "ERROR"
            content = f"[ERROR: empty response (finish_reason={finish_reason}, output_tokens={out_tok})]"
        else:
            status = "DONE"
        return {**row_data, "description": content,
                "input_tokens": in_tok, "output_tokens": out_tok,
                "status": status}
    except Exception as e:
        _err = str(e)[:200]
        if "429" in _err or "REQUEST_LIMIT_EXCEEDED" in _err:
            _desc = f"[RATE_LIMITED after {max_retries} retries: {_err}]"
        elif "timeout" in _err.lower() or isinstance(e, asyncio.TimeoutError):
            _desc = f"[TIMEOUT after {max_retries} retries: {_err}]"
        else:
            _desc = f"[ERROR after {max_retries} retries: {_err}]"
        return {**row_data, "description": _desc,
                "input_tokens": 0, "output_tokens": 0, "status": "ERROR"}


async def describe_all_images(rows: List[dict], ws_host: str, ws_token: str,
                              model: str = None, max_tokens: int = None,
                              temperature: float = None, max_retries: int = None,
                              max_concurrent: int = None,
                              requests_per_minute: float = None) -> List[dict]:
    """Describe all images concurrently with semaphore + optional token-bucket rate limiter.

    `requests_per_minute` limits throughput to stay within the endpoint ITPM quota.
    Derive it as: floor(LLM_ITPM_BUDGET / avg_input_tokens_per_image).
    When None, no rate limiting is applied (legacy behaviour).
    """
    from openai import AsyncOpenAI

    model = model or LLM_MODEL_ENDPOINT
    max_tokens = max_tokens or LLM_MAX_TOKENS
    temperature = temperature if temperature is not None else LLM_TEMPERATURE
    max_retries = max_retries or LLM_MAX_RETRIES
    max_concurrent = max_concurrent or LLM_MAX_CONCURRENT
    rate_lim = _AsyncRateLimiter(requests_per_minute) if requests_per_minute else None

    client = AsyncOpenAI(api_key=ws_token, base_url=f"https://{ws_host}/serving-endpoints")
    semaphore = asyncio.Semaphore(max_concurrent)
    try:
        tasks = [describe_one_image(client, r, semaphore, model=model, max_tokens=max_tokens,
                                    temperature=temperature, max_retries=max_retries,
                                    rate_limiter=rate_lim) for r in rows]
        results = await asyncio.gather(*tasks, return_exceptions=True)
    finally:
        try:
            await client.close()
        except Exception:
            pass

    final = []
    for i, res in enumerate(results):
        if isinstance(res, Exception):
            final.append({**rows[i], "description": f"[System Error: {str(res)[:150]}]",
                          "input_tokens": 0, "output_tokens": 0, "status": "ERROR"})
        else:
            final.append(res)
    return final
