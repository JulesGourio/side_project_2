"""image_fallbacks.py — text and images of the files Docling cannot (or should not) read.

DOCX / PPTX are read straight from the zip and its XML, which rasterizes nothing and so cannot run out of memory on
a large file; binary .ppt is scanned for text runs and embedded JPEG / PNG; the pages of a scanned PDF are rendered
for the LLM-OCR fallback. Each parser returns {text, parser_strategy, parse_time_seconds, images} or None.
"""

import hashlib
import io
import logging
import posixpath
import re
import time
import zipfile
from typing import Any, Dict, List, Optional

from image_store import image_record, image_target_path, save_image_to_volume

logger = logging.getLogger(__name__)

# Reference slide/frame area (widescreen raster) used to size-filter decorative images of pptx and .ppt, like the
# fixed A4 area of the DOCX fallback.
_XML_FALLBACK_REF_AREA = 1280 * 720
_DOCX_REF_AREA = 794 * 1123

_NS_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_NS_R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_NS_A = "http://schemas.openxmlformats.org/drawingml/2006/main"
_NS_RELS = "http://schemas.openxmlformats.org/package/2006/relationships"


def _save_candidate_images(candidates: List[dict], repeating: set, doc_id: str, volume_base_path: str,
                           min_area_ratio: float, log_prefix: str) -> List[Dict[str, Any]]:
    """Filter repeating / too-small candidates, save the rest to the volume and build their image_metadata entries."""
    images: List[Dict[str, Any]] = []
    for cand in candidates:
        if cand["md5"] in repeating or cand["area_ratio"] < min_area_ratio:
            continue
        image_id = len(images)
        target = image_target_path(volume_base_path, doc_id, image_id)
        volume_path = img_w = img_h = None
        try:
            volume_path, img_w, img_h = save_image_to_volume(cand["pil_img"], target, volume_base_path)
        except Exception as e:
            logger.warning("%s: failed to save image: %s", log_prefix, str(e)[:200])
        images.append(image_record(image_id, cand.get("page_no", image_id + 1), "picture", cand["area_ratio"], [],
                                   cand.get("context_text", ""), volume_path, img_w, img_h))
    return images


def _read_media(zf, prefix: str, ref_area: int, log_prefix: str):
    """(occurrences per md5, decoded images by zip member) for every file of the zip under `prefix`."""
    from PIL import Image

    hash_count: Dict[str, int] = {}
    media_info: Dict[str, dict] = {}
    for member in [n for n in zf.namelist() if n.startswith(prefix)]:
        try:
            img_bytes = zf.read(member)
            md5 = hashlib.md5(img_bytes).hexdigest()
            hash_count[md5] = hash_count.get(md5, 0) + 1
            pil_img = Image.open(io.BytesIO(img_bytes))
            pil_img.load()
            w, h = pil_img.size
            media_info[member] = {"md5": md5, "pil_img": pil_img, "area_ratio": (w * h) / ref_area}
        except Exception as e:
            logger.warning("%s: unreadable embedded image %s: %s", log_prefix, member, str(e)[:200])
    return hash_count, media_info


def _images_of_occurrences(zf, occurrences: List[dict], prefix: str, ref_area: int, doc_id: str,
                           volume_base_path: str, min_area_ratio: float, max_repeat: int, log_prefix: str):
    """Images of the media referenced by `occurrences` ({media_file, page_no, context_text}), repeated ones dropped."""
    hash_count, media_info = _read_media(zf, prefix, ref_area, log_prefix)
    repeating = {h for h, c in hash_count.items() if c > max_repeat}
    candidates = [{**media_info[occ["media_file"]], "page_no": occ["page_no"], "context_text": occ.get("context_text", "")}
                  for occ in occurrences if occ["media_file"] in media_info]
    return _save_candidate_images(candidates, repeating, doc_id, volume_base_path, min_area_ratio, log_prefix)


def _blip_embed_ids(tree):
    """Relationship ids of the pictures (a:blip) of an XML tree."""
    return [blip.get(f"{{{_NS_R}}}embed") for blip in tree.iter(f"{{{_NS_A}}}blip")]


def fallback_parse_docx(content_bytes: bytes, doc_id: str, volume_base_path: str,
                        min_area_ratio: float, max_repeat: int) -> Optional[Dict[str, Any]]:
    """DOCX that Docling cannot parse: text and images read straight from the zip and its XML."""
    from lxml import etree

    try:
        zf = zipfile.ZipFile(io.BytesIO(content_bytes))
    except Exception as e:
        logger.warning("DOCX fallback: not a readable zip for doc_id=%s: %s", doc_id, str(e)[:200])
        return None
    with zf:
        if "word/document.xml" not in zf.namelist():
            return None

        t0 = time.time()
        rels_map: Dict[str, str] = {}
        for rels_path in [n for n in zf.namelist() if n.startswith("word/_rels/") and n.endswith(".rels")]:
            with zf.open(rels_path) as rf:
                rels_tree = etree.parse(rf)
            for rel in rels_tree.iter(f"{{{_NS_RELS}}}Relationship"):
                target = rel.get("Target", "")
                if "media/" in target:
                    rels_map[rel.get("Id")] = target if target.startswith("word/") else f"word/{target}"

        with zf.open("word/document.xml") as doc_xml:
            tree = etree.parse(doc_xml)
        body = tree.find(f".//{{{_NS_W}}}body")
        if body is None:
            return None

        current_page = 1
        headings: Dict[int, str] = {}
        paragraphs: List[str] = []
        occurrences = []

        for para in body.iter(f"{{{_NS_W}}}p"):
            for _ in para.iter(f"{{{_NS_W}}}lastRenderedPageBreak"):
                current_page += 1
            for br in para.iter(f"{{{_NS_W}}}br"):
                if br.get(f"{{{_NS_W}}}type") == "page":
                    current_page += 1
            ppr = para.find(f"{{{_NS_W}}}pPr")
            if ppr is not None:
                pstyle = ppr.find(f"{{{_NS_W}}}pStyle")
                if pstyle is not None:
                    style_val = pstyle.get(f"{{{_NS_W}}}val", "")
                    if any(kw in style_val.lower() for kw in ("heading", "titre", "titulo")):
                        level = next((int(ch) for ch in style_val if ch.isdigit()), 1)
                        heading_text = "".join(t.text for t in para.iter(f"{{{_NS_W}}}t") if t.text).strip()
                        if heading_text:
                            headings[level] = heading_text
                            for deeper in [lvl for lvl in headings if lvl > level]:
                                del headings[deeper]
                if ppr.find(f"{{{_NS_W}}}sectPr") is not None:
                    current_page += 1
            para_text = "".join(t.text for t in para.iter(f"{{{_NS_W}}}t") if t.text).strip()
            if para_text:
                paragraphs.append(para_text)
            for embed_id in _blip_embed_ids(para):
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
            for embed_id in _blip_embed_ids(part_tree):
                if embed_id and embed_id in rels_map:
                    label = "header" if "header" in part_name else "footer"
                    occurrences.append({"media_file": rels_map[embed_id], "page_no": 1,
                                        "context_text": f"[{label} image]"})

        images = _images_of_occurrences(zf, occurrences, "word/media/", _DOCX_REF_AREA, doc_id, volume_base_path,
                                        min_area_ratio, max_repeat, "DOCX fallback")
    return {"text": "\n".join(paragraphs), "parser_strategy": "xml_fallback:docx",
            "parse_time_seconds": round(time.time() - t0, 3), "images": images}


def fallback_parse_pptx(content_bytes: bytes, doc_id: str, volume_base_path: str,
                        min_area_ratio: float, max_repeat: int) -> Optional[Dict[str, Any]]:
    """PPTX that Docling cannot parse: text and images read straight from the zip and its XML."""
    from lxml import etree

    try:
        zf = zipfile.ZipFile(io.BytesIO(content_bytes))
    except Exception as e:
        logger.warning("PPTX fallback: not a readable zip for doc_id=%s: %s", doc_id, str(e)[:200])
        return None
    with zf:
        slide_names = [n for n in zf.namelist() if re.match(r"^ppt/slides/slide\d+\.xml$", n)]
        if not slide_names:
            return None
        slide_names.sort(key=lambda n: int(re.search(r"(\d+)", n).group(1)))

        t0 = time.time()
        slide_texts: List[str] = []
        occurrences = []
        for slide_idx, slide_name in enumerate(slide_names, start=1):
            rels_path = f"ppt/slides/_rels/{posixpath.basename(slide_name)}.rels"
            rels_map: Dict[str, str] = {}
            if rels_path in zf.namelist():
                with zf.open(rels_path) as rf:
                    rels_tree = etree.parse(rf)
                for rel in rels_tree.iter(f"{{{_NS_RELS}}}Relationship"):
                    resolved = posixpath.normpath(posixpath.join("ppt/slides", rel.get("Target", "")))
                    if "media/" in resolved:
                        rels_map[rel.get("Id")] = resolved

            with zf.open(slide_name) as sf:
                slide_tree = etree.parse(sf)
            texts = [t.text.strip() for t in slide_tree.iter(f"{{{_NS_A}}}t") if t.text and t.text.strip()]
            slide_text = "\n".join(texts)
            if slide_text:
                slide_texts.append(f"--- Slide {slide_idx} ---\n{slide_text}")

            for embed_id in _blip_embed_ids(slide_tree):
                if embed_id and embed_id in rels_map:
                    ctx = slide_text[:200] if slide_text else f"Slide {slide_idx}"
                    occurrences.append({"media_file": rels_map[embed_id], "page_no": slide_idx, "context_text": ctx})

        images = _images_of_occurrences(zf, occurrences, "ppt/media/", _XML_FALLBACK_REF_AREA, doc_id,
                                        volume_base_path, min_area_ratio, max_repeat, "PPTX fallback")
    return {"text": "\n\n".join(slide_texts), "parser_strategy": "xml_fallback:pptx",
            "parse_time_seconds": round(time.time() - t0, 3), "images": images}


# Binary .ppt (OLE compound file): a byte-scan heuristic, not a real MS-PPT record parser (no maintained
# pure-Python one exists).
_OLE_MIN_TEXT_RUN_CHARS = 4


def _extract_ole_text_runs(buf: bytes) -> List[str]:
    """Printable UTF-16LE text runs of a binary OLE stream (.ppt text), at least _OLE_MIN_TEXT_RUN_CHARS long."""
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
    """JPEG and PNG images cut out of a binary buffer by their start and end signatures."""
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


def fallback_parse_ppt_legacy(content_bytes: bytes, doc_id: str, volume_base_path: str,
                              min_area_ratio: float, max_repeat: int) -> Optional[Dict[str, Any]]:
    """Binary .ppt: text runs and carved images read from the OLE container, since Docling does not handle the format."""
    from PIL import Image

    t0 = time.time()
    try:
        import olefile
        ole = olefile.OleFileIO(io.BytesIO(content_bytes))
    except Exception as e:
        logger.warning("PPT (legacy) fallback: not a readable OLE container for doc_id=%s: %s", doc_id, str(e)[:200])
        return None

    def _find_stream(name: str):
        for entry in ole.listdir(streams=True, storages=False):
            if entry and entry[-1].lower() == name.lower():
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

    hash_count: Dict[str, int] = {}
    candidates = []
    for raw in _carve_images_from_bytes(pics_bytes):
        try:
            md5 = hashlib.md5(raw).hexdigest()
            hash_count[md5] = hash_count.get(md5, 0) + 1
            pil_img = Image.open(io.BytesIO(raw))
            pil_img.load()
            w, h = pil_img.size
            candidates.append({"md5": md5, "pil_img": pil_img, "area_ratio": (w * h) / _XML_FALLBACK_REF_AREA})
        except Exception as e:
            logger.warning("PPT (legacy) fallback: unreadable carved image for doc_id=%s: %s", doc_id, str(e)[:200])

    repeating = {h for h, c in hash_count.items() if c > max_repeat}
    images = _save_candidate_images(candidates, repeating, doc_id, volume_base_path, min_area_ratio,
                                    "PPT (legacy) fallback")
    return {"text": "\n".join(_extract_ole_text_runs(doc_bytes)), "parser_strategy": "ole_heuristic:ppt",
            "parse_time_seconds": round(time.time() - t0, 3), "images": images}


def render_pdf_pages_for_llm_ocr(content_bytes: bytes, doc_id: str, volume_base_path: str,
                                 max_pages: int) -> List[Dict[str, Any]]:
    """Render the first `max_pages` pages of a PDF to images for LLM-based OCR, the last resort when Docling still
    yields near-empty text."""
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
            target = image_target_path(volume_base_path, doc_id, image_id, kind="PAGE")
            try:
                volume_path, img_w, img_h = save_image_to_volume(pil_img, target, volume_base_path)
            except Exception as e:
                logger.warning("LLM-OCR page render: failed to save page %d for doc_id=%s: %s",
                               page_no, doc_id, str(e)[:200])
                continue
            images.append(image_record(image_id, page_no + 1, "scanned_page", 1.0, [], "", volume_path, img_w, img_h))
    except Exception as e:
        logger.warning("LLM-OCR page render failed for doc_id=%s: %s", doc_id, str(e)[:200])
        return []
    return images
