"""image_utils.py — the parsing UDF of `3_parse`: Docling (or a format fallback) parse, image extraction, volume save.

Worker-side. One Spark task = one document: parse, extract the pictures of the Docling document, save them to the
volume (`save_image_to_volume`, `image_fallbacks`), and fall back to `image_fallbacks` when Docling cannot read the file or finds no image.
The LLM description lives in `llm_describe.py` (driver side).
"""

import hashlib
import logging
import signal
import time
from collections import defaultdict
from typing import Any, Dict, List

import pandas as pd

from pyspark.sql import Column
from pyspark.sql import types as T
from pyspark.sql import functions as f
from pyspark.sql.functions import pandas_udf

from config import (
    IMG_SKIP_MAX_DIM,
    IMG_SKIP_MIN_SIDE,
    LLM_OCR_MAX_PAGES,
    LLM_OCR_TEXT_THRESHOLD,
    PARSE_TIMEOUT_SECONDS,
)
from format_parsers import parse_with_docling, parse_xls, parse_xlsx_openpyxl, patch_worker_env
from image_fallbacks import (
    fallback_parse_docx,
    fallback_parse_pptx,
    fallback_parse_ppt_ole,
    render_pdf_pages_for_llm_ocr,
    image_record,
    image_target_path,
    save_image_to_volume,
)
from worker import _cfg, count_tokens, ensure_config

logger = logging.getLogger(__name__)


# --- Spark schemas ---
IMAGE_METADATA_SCHEMA = T.StructType(
    [
        T.StructField("image_id", T.IntegerType(), True),
        T.StructField("page_no", T.IntegerType(), True),
        T.StructField("label", T.StringType(), True),
        T.StructField("area_ratio", T.FloatType(), True),
        T.StructField("captions", T.ArrayType(T.StringType()), True),
        T.StructField("context_text", T.StringType(), True),
        T.StructField("volume_path", T.StringType(), True),
        T.StructField("image_width", T.IntegerType(), True),
        T.StructField("image_height", T.IntegerType(), True),
    ]
)

IMAGE_ARRAY_SCHEMA = T.ArrayType(IMAGE_METADATA_SCHEMA)

FULL_PARSE_SCHEMA = T.StructType(
    [
        T.StructField("text", T.StringType(), True),
        T.StructField("parser_error", T.StringType(), True),
        T.StructField("parser_strategy", T.StringType(), True),
        T.StructField("parse_time_seconds", T.FloatType(), True),
        T.StructField("images", IMAGE_ARRAY_SCHEMA, True),
        T.StructField("timings", T.MapType(T.StringType(), T.FloatType()), True),
    ]
)


# --- Image helpers ---
def _image_md5(pil_img) -> str:
    """MD5 of raw pixel bytes — fast, deterministic deduplication key.

    :param pil_img: PIL image.
    """
    return hashlib.md5(pil_img.tobytes()).hexdigest()


def _get_pil_image(pic, doc) -> Any:
    """PIL image of a Docling picture, trying the document API then the picture's own image; None if neither works.

    :param pic: Docling picture.
    :param doc: Docling document that holds the picture.
    :return: PIL image, or None when neither way works.
    """
    for fn in (lambda: pic.get_image(doc=doc), lambda: pic.image.pil_image if pic.image else None):
        try:
            img = fn()
            if img is not None:
                return img
        except Exception:
            pass
    return None


def _page_size(doc, page_no) -> tuple[float | None, float | None]:
    """(width, height) of a page, or (None, None) when unknown.

    :param doc: Docling document.
    :param page_no: Page number, starting at 1.
    """
    try:
        page = doc.pages.get(page_no)
        if page and page.size:
            return page.size.width, page.size.height
    except Exception:
        pass
    return None, None


def _resolve_caption(doc, ref) -> str | None:
    """Text of a picture caption reference such as `#/texts/12`, or None.

    :param doc: Docling document.
    :param ref: Caption reference of a picture.
    """
    try:
        parts = ref.cref.lstrip("#/").split("/")
        if len(parts) == 2:
            item = getattr(doc, parts[0], [])[int(parts[1])]
            return item.text if hasattr(item, "text") else None
    except Exception:
        pass
    return None


def _get_image_context(
    page_texts: list, page_no, bbox, caption_crefs: set, max_tokens: int = 250
) -> str:
    """Build before/after textual context around an image, token-budgeted.

    :param page_texts: Text items of the page.
    :param page_no: Page number of the image.
    :param bbox: Bounding box of the image.
    :param caption_crefs: References of the image captions, left out of the context.
    :param max_tokens: Token budget of the context, split between the text before and after.
    """
    if not bbox or not page_no or not page_texts:
        return ""
    try:
        img_mid_y = (bbox.t + bbox.b) / 2
        texts = [t for t in page_texts if getattr(t, "self_ref", None) not in caption_crefs]
        if not texts:
            return ""
        above = sorted(
            [t for t in texts if t.prov[0].bbox.b <= img_mid_y],
            key=lambda t: t.prov[0].bbox.b,
            reverse=True,
        )
        below = sorted(
            [t for t in texts if t.prov[0].bbox.t > img_mid_y], key=lambda t: t.prov[0].bbox.t
        )

        def accumulate(sorted_texts, limit) -> list[str]:
            """Take texts in order while they fit the token limit; the first one is always taken.

            :param sorted_texts: Text items, sorted by distance from the image.
            :param limit: Token budget.
            :return: stripped texts, in order.
            """
            acc, toks = [], 0
            for t in sorted_texts:
                s = t.text.strip()
                tc = count_tokens(s)
                if toks + tc > limit and toks > 0:
                    break
                acc.append(s)
                toks += tc
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
def extract_images_from_doc(
    doc,
    doc_id: str,
    volume_base_path: str,
    min_area_ratio: float,
    max_repeat: int,
    timings: dict | None = None,
) -> List[Dict[str, Any]]:
    """Save the pictures of a Docling document to the volume and return one metadata dict per image (page, label, caption, nearby text, area ratio); images that are too small (min_area_ratio) or repeated more than max_repeat times are skipped.

    :param doc: Docling document.
    :param doc_id: Id of the document, used in the image paths.
    :param volume_base_path: Folder of the images on the volume.
    :param min_area_ratio: Smallest image, as a share of its page.
    :param max_repeat: Largest number of pages on which the same image may repeat.
    :param timings: Dict filled with the step durations, or None.
    """
    pictures = list(doc.pictures)
    if not pictures:
        return []

    # Pre-group page texts once to avoid O(N*M) scans.
    texts_by_page = defaultdict(list)
    for t in doc.texts:
        if t.prov:
            texts_by_page[t.prov[0].page_no].append(t)

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
        context_text = _get_image_context(
            texts_by_page.get(page_no, []), page_no, bbox, caption_crefs
        )
        t_ctx_total += time.perf_counter() - t0

        image_id = len(manifest)
        volume_path = img_w = img_h = None
        if pil_img is not None:
            target = image_target_path(volume_base_path, doc_id, image_id)
            t0 = time.perf_counter()
            try:
                volume_path, img_w, img_h = save_image_to_volume(pil_img, target, volume_base_path)
            except Exception as e:
                logger.warning("Failed to save image to volume: %s", str(e)[:200])
            t_vol_total += time.perf_counter() - t0

        manifest.append(
            image_record(
                image_id,
                page_no,
                pic.label.value if hasattr(pic.label, "value") else str(pic.label),
                area_ratio,
                captions,
                context_text,
                volume_path,
                img_w,
                img_h,
            )
        )

    if timings is not None:
        timings["context_extraction_seconds"] = float(t_ctx_total)
        timings["volume_write_seconds"] = float(t_vol_total)
    return manifest


def image_status_col(volume_path, width, height) -> Column:
    """Initial status of an image in image_metadata (deterministic, no LLM call).

    EXTRACTION_FAILED  : no image was written to the volume.
    SKIPPED_DECORATIVE : image is physically illegible (too small) or a
                         separator line — no LLM call, no indexing.
    PENDING            : awaiting description.

    Deliberately conservative: does NOT filter on a large width/height ratio
    (a real horizontal flowchart is legitimately wide). See IMG_SKIP_* in config.py.

    :param volume_path: Column with the volume path of the image; null when it was not written.
    :param width: Column with the image width in pixels.
    :param height: Column with the image height in pixels.
    :return: status column: ``EXTRACTION_FAILED``, ``SKIPPED_DECORATIVE`` or ``PENDING``.
    """
    largest = f.greatest(f.coalesce(width, f.lit(0)), f.coalesce(height, f.lit(0)))
    smallest = f.least(f.coalesce(width, f.lit(0)), f.coalesce(height, f.lit(0)))
    decorative = (largest < f.lit(IMG_SKIP_MAX_DIM)) | (smallest < f.lit(IMG_SKIP_MIN_SIDE))
    return (
        f.when(volume_path.isNull(), f.lit("EXTRACTION_FAILED"))
        .when(decorative, f.lit("SKIPPED_DECORATIVE"))
        .otherwise(f.lit("PENDING"))
    )


_TIMING_KEYS = (
    "docling_import_seconds",
    "docling_load_seconds",
    "tmp_file_write_seconds",
    "docling_convert_seconds",
    "markdown_export_seconds",
    "ocr_fallback_seconds",
    "image_hashing_seconds",
    "context_extraction_seconds",
    "volume_write_seconds",
    "pdf_page_render_seconds",
    "total_seconds",
)


class _ParseTimeout:
    """Kills a parse that exceeds `seconds` (SIGALRM — Spark Linux workers, main thread).

    No-op if SIGALRM is unavailable (e.g. Windows) or seconds<=0. Prevents a
    pathological file (giant PDF/xlsx) from blocking a Spark task for hours.
    """

    def __init__(self, seconds: int):
        """Store the timeout and whether SIGALRM can enforce it.

        :param seconds: Maximum duration in seconds; 0 or less disables the timeout.
        """
        self.seconds = int(seconds) if seconds else 0
        self._active = self.seconds > 0 and hasattr(signal, "SIGALRM")

    def _raise(self, *_) -> None:
        """Raise the timeout error when the timer fires.

        :param _: Signal arguments, unused.
        """
        raise TimeoutError(f"parse exceeded {self.seconds}s")

    def __enter__(self) -> "_ParseTimeout":
        """Arm the timer when the block starts.

        :return: this timeout.
        """
        if self._active:
            try:
                self._old = signal.signal(signal.SIGALRM, self._raise)
                signal.setitimer(signal.ITIMER_REAL, self.seconds)
            except ValueError:
                # signal.signal() can only be called from the main thread.
                # In Spark UDF workers (USER_ISOLATION cluster), this raises
                # ValueError — disable per-file timeout gracefully.
                self._active = False
        return self

    def __exit__(self, *exc) -> bool:
        """Disarm the timer when the block ends.

        :param exc: Exception information of the block, unused.
        :return: False, so an exception of the block propagates.
        """
        if self._active:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, self._old)
        return False


def _fallback_or_error(fb: Dict[str, Any] | None, ext_lower: str, timings: dict) -> dict:
    """Turn a fallback result into the UDF result dict, or into an error result when the fallback failed.

    :param fb: Result of a fallback parser, or None when it failed.
    :param ext_lower: Lowercase file extension, for the error message.
    :param timings: Step durations of the file.
    :return: result of the UDF for this file.
    """
    if fb:
        return {
            "text": fb["text"],
            "parser_error": None,
            "parser_strategy": fb["parser_strategy"],
            "parse_time_seconds": fb["parse_time_seconds"],
            "images": fb["images"],
            "timings": timings,
        }
    return {
        "text": "",
        "parser_error": f"fallback_failed:{ext_lower}",
        "parser_strategy": "fallback_failed",
        "parse_time_seconds": 0.0,
        "images": [],
        "timings": timings,
    }


@pandas_udf(FULL_PARSE_SCHEMA)
def parse_and_extract_images_udf(
    content_series: pd.Series,
    ext_series: pd.Series,
    doc_id_series: pd.Series,
    volume_path_series: pd.Series,
    min_area_ratio_series: pd.Series,
    max_repeat_series: pd.Series,
    enable_timing_series: pd.Series,
    xml_only_series: pd.Series,
) -> pd.DataFrame:
    """Pandas UDF run on the executors: parse each document (Docling, or a fallback by format) and extract its images, one result per row.

    :param content_series: Document bytes.
    :param ext_series: File extensions.
    :param doc_id_series: Ids of the documents.
    :param volume_path_series: Base folder of the images on the volume.
    :param min_area_ratio_series: Smallest image, as a share of its page.
    :param max_repeat_series: Largest number of pages on which an image may repeat.
    :param enable_timing_series: Whether to record the step durations; the first row decides.
    :param xml_only_series: Whether the file goes straight to the XML fallback.
    """
    # xml_only_series must stay a required positional column — pandas UDFs reject a default/... | None one.
    patch_worker_env()
    ensure_config(str(volume_path_series.iloc[0]) if len(volume_path_series) else None)
    enable_timing = bool(enable_timing_series.iloc[0]) if len(enable_timing_series) else False
    results = []

    for content, ext, doc_id, vol_path, min_ar, max_rep, xml_only in zip(
        content_series,
        ext_series,
        doc_id_series,
        volume_path_series,
        min_area_ratio_series,
        max_repeat_series,
        xml_only_series,
    ):
        if content is None:
            results.append(
                {
                    "text": "",
                    "parser_error": "No Content",
                    "parser_strategy": "none",
                    "parse_time_seconds": 0.0,
                    "images": [],
                    "timings": {},
                }
            )
            continue

        doc_id_str = str(doc_id) if doc_id is not None else "unknown"
        vol_base = str(vol_path) if vol_path is not None else _cfg("VOLUME_BASE_PATH")
        min_ar_val = float(min_ar) if min_ar is not None else _cfg("MIN_AREA_RATIO", fallback=0.03)
        max_rep_val = int(max_rep) if max_rep is not None else _cfg("MAX_REPEAT", fallback=3)
        timings = {k: 0.0 for k in _TIMING_KEYS} if enable_timing else {}
        t_doc = time.perf_counter()
        ext_lower = str(ext).lower().strip(".")

        # Over the Docling-safe size threshold: skip Docling entirely, go straight to the zip/XML fallback.
        if bool(xml_only) and ext_lower in ("docx", "docm", "pptx", "pptm"):
            fb_fn = fallback_parse_docx if ext_lower in ("docx", "docm") else fallback_parse_pptx
            fb = fb_fn(bytes(content), doc_id_str, vol_base, min_ar_val, max_rep_val)
            results.append(_fallback_or_error(fb, ext_lower, timings))
            continue

        # Docling does not read binary .ppt — go straight to the OLE heuristic fallback.
        if ext_lower == "ppt":
            fb = fallback_parse_ppt_ole(
                bytes(content), doc_id_str, vol_base, min_ar_val, max_rep_val
            )
            results.append(_fallback_or_error(fb, ext_lower, timings))
            continue

        # Spreadsheets use the column-value parser: Docling turns each sheet into one large markdown table, which chunks badly.
        try:
            with _ParseTimeout(PARSE_TIMEOUT_SECONDS):
                if ext_lower in ("xlsx", "xlsm", "xlsb"):
                    res = parse_xlsx_openpyxl(bytes(content), time.perf_counter(), ext_lower)
                    docling_doc = None
                elif ext_lower == "xls":
                    res = parse_xls(bytes(content), time.perf_counter())
                    docling_doc = None
                else:
                    res = parse_with_docling(
                        content, str(ext), timings=(timings if enable_timing else None)
                    )
                    docling_doc = res.pop("_docling_doc", None)
        except TimeoutError as e:
            results.append(
                {
                    "text": "",
                    "parser_error": str(e),
                    "parser_strategy": "timeout",
                    "parse_time_seconds": float(PARSE_TIMEOUT_SECONDS),
                    "images": [],
                    "timings": {},
                }
            )
            continue
        images = []

        if docling_doc is not None:
            try:
                images = extract_images_from_doc(
                    docling_doc,
                    doc_id=doc_id_str,
                    volume_base_path=vol_base,
                    min_area_ratio=min_ar_val,
                    max_repeat=max_rep_val,
                    timings=(timings if enable_timing else None),
                )
            except Exception as e:
                logger.warning("Image extraction failed for %s: %s", doc_id_str, str(e)[:200])

        if ext_lower in ("docx", "docm", "pptx", "pptm"):
            fb_fn = fallback_parse_docx if ext_lower in ("docx", "docm") else fallback_parse_pptx
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
                        res.update(
                            text=fb["text"],
                            parser_error=None,
                            parser_strategy=fb["parser_strategy"],
                            parse_time_seconds=fb["parse_time_seconds"],
                        )
                    if fb.get("images"):
                        images = fb["images"]

        # Last resort: Docling left near-nothing on this PDF — render pages and queue them for LLM-OCR instead of failing the document.
        # Gated on "not images": a page render duplicates whatever a real extracted image already
        # shows, so only fall back here when Docling found no embedded images to begin with.
        if (
            ext_lower == "pdf"
            and not images
            and len((res.get("text") or "").strip()) < LLM_OCR_TEXT_THRESHOLD
        ):
            t_render = time.perf_counter()
            page_images = render_pdf_pages_for_llm_ocr(
                bytes(content), doc_id_str, vol_base, LLM_OCR_MAX_PAGES
            )
            if enable_timing:
                timings["pdf_page_render_seconds"] = float(time.perf_counter() - t_render)
            if page_images:
                images = images + page_images
                res["parser_strategy"] = "pending_llm_ocr:pdf"
                res["parser_error"] = None

        if enable_timing:
            timings["total_seconds"] = float(time.perf_counter() - t_doc)
        res["images"] = images
        res["timings"] = timings
        results.append(res)

    return pd.DataFrame(results)
