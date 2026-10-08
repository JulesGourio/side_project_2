"""Docling parsing, chunking and Vector Search helpers of `1_Parse_Chunk_Generic.py`."""
import logging
import os
import tempfile
import time
from pathlib import Path

from databricks.sdk.errors import NotFound
from databricks.sdk.service.vectorsearch import (
    DeltaSyncVectorIndexSpecRequest,
    EmbeddingSourceColumn,
    PipelineType,
    VectorIndexType,
)
from docling.chunking import HybridChunker
from docling.datamodel.base_models import InputFormat
from docling.datamodel.pipeline_options import AcceleratorDevice, AcceleratorOptions, PdfPipelineOptions
from docling.document_converter import DocumentConverter, PdfFormatOption
from docling_core.transforms.chunker.tokenizer.huggingface import HuggingFaceTokenizer

try:
    from docling.datamodel.pipeline_options import TableFormerMode
except ImportError:
    TableFormerMode = None

logger = logging.getLogger("generic_pipeline")

SUPPORTED_EXTENSIONS = {
    "pdf", "docx", "docm", "pptx", "xlsx", "xlsm", "xlsb",
    "html", "htm", "xml", "md", "txt",
    "doc", "rtf", "odt", "ods", "xls",
}
_FORMAT_MAP = {
    ".pdf": "PDF", ".docx": "DOCX", ".docm": "DOCX", ".pptx": "PPTX",
    ".xlsx": "XLSX", ".xlsm": "XLSX", ".xlsb": "XLSX", ".html": "HTML",
    ".htm": "HTML", ".xml": "HTML", ".md": "MD", ".txt": "MD",
}
CHARS_PER_TOKEN = 3.5
MIN_CHUNK_CHARS = 20
INDEX_READY_TIMEOUT_S = 15 * 60


def build_converter(offline_models_dir, use_gpu):
    """Docling converter on the offline models; no OCR and no picture images (no image description here)."""
    opts = PdfPipelineOptions()
    opts.artifacts_path = Path(offline_models_dir)
    opts.do_ocr = False
    opts.do_table_structure = True
    opts.generate_picture_images = False
    opts.images_scale = 0.75
    if TableFormerMode is not None:
        try:
            opts.table_structure_options.mode = TableFormerMode.ACCURATE
        except Exception:
            pass
    opts.accelerator_options = (
        AcceleratorOptions(num_threads=4, device=AcceleratorDevice.CUDA)
        if use_gpu
        else AcceleratorOptions(num_threads=1, device=AcceleratorDevice.CPU)
    )

    try:
        from docling.backend.pypdfium2_backend import PyPdfiumDocumentBackend
        pdf_option = PdfFormatOption(pipeline_options=opts, backend=PyPdfiumDocumentBackend)
    except ImportError:
        pdf_option = PdfFormatOption(pipeline_options=opts)
    return DocumentConverter(format_options={InputFormat.PDF: pdf_option})


def build_chunker(offline_models_dir, max_chunk_tokens):
    """HybridChunker on the offline tokenizer."""
    tokenizer_dir = Path(offline_models_dir) / "sentence-transformers--all-MiniLM-L6-v2"
    tokenizer = HuggingFaceTokenizer.from_pretrained(model_name=tokenizer_dir, max_tokens=max_chunk_tokens)
    return HybridChunker(tokenizer=tokenizer, merge_peers=True)


def _chunk_row(index, text, headers=""):
    return {
        "chunk_index": index,
        "chunk_text": text,
        "chunk_token_count": max(1, int(len(text) / CHARS_PER_TOKEN)),
        "chunk_content_type": "text",
        "semantic_headers": headers,
    }


def parse_and_chunk_file(file_path, file_bytes, extension, converter, chunker):
    """Parse one file with Docling and chunk it. Returns (chunks, error, status)."""
    ext = f".{extension}"
    fmt_name = _FORMAT_MAP.get(ext)
    if not fmt_name:
        return [], f"Unsupported extension: {ext}", ""
    if getattr(InputFormat, fmt_name, None) is None:
        return [], f"Unknown Docling format: {fmt_name}", ""

    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmp:
            tmp.write(file_bytes)
            tmp_path = tmp.name

        doc = converter.convert(tmp_path).document
        md_text = doc.export_to_markdown()
        if not md_text or not md_text.strip():
            return [], None, "EMPTY_TEXT"

        chunks = []
        try:
            for i, chunk in enumerate(chunker.chunk(doc)):
                text = chunk.text if hasattr(chunk, "text") else str(chunk)
                if not text or len(text.strip()) < MIN_CHUNK_CHARS:
                    continue
                headings = getattr(chunk.meta, "headings", None) if getattr(chunk, "meta", None) else None
                chunks.append(_chunk_row(i, text, " > ".join(headings) if headings else ""))
        except Exception as chunk_err:
            logger.warning(f"HybridChunker failed for {file_path}: {chunk_err}. Falling back to paragraph split.")
            paragraphs = [p.strip() for p in md_text.split("\n\n") if len(p.strip()) > MIN_CHUNK_CHARS]
            chunks = [_chunk_row(i, para) for i, para in enumerate(paragraphs)]
        return chunks, None, "SUCCESS"

    except Exception as e:
        return [], str(e)[:500], "ERROR"
    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


def wait_until_ready(w, index_name, timeout_s=INDEX_READY_TIMEOUT_S):
    """Poll the index every 30 s. Returns (ready, indexed_row_count)."""
    deadline = time.time() + timeout_s
    ready, rows = False, 0
    while time.time() < deadline:
        status = w.vector_search_indexes.get_index(index_name=index_name).status
        ready = status.ready if status else False
        rows = status.indexed_row_count if status else 0
        if ready:
            logger.info(f"Index {index_name} is READY - rows={rows}")
            return True, rows
        state = getattr(status, "detailed_state", None) or "unknown"
        logger.info(f"Index not ready yet: state={state}, rows={rows}")
        time.sleep(30)
    logger.warning("Index not ready after 15 min - check its status manually.")
    return ready, rows


def create_and_sync_index(w, index_name, endpoint_name, source_table, embedding_model):
    """Create the Delta Sync index if missing (it syncs by itself), otherwise trigger a sync once ready."""
    try:
        status = w.vector_search_indexes.get_index(index_name=index_name).status
        logger.info(f"Index {index_name} already exists - ready={status.ready}, rows={status.indexed_row_count}")
        created_now = False
    except NotFound:
        logger.info(f"Index {index_name} not found - creating...")
        w.vector_search_indexes.create_index(
            name=index_name,
            endpoint_name=endpoint_name,
            primary_key="chunk_id",
            index_type=VectorIndexType.DELTA_SYNC,
            delta_sync_index_spec=DeltaSyncVectorIndexSpecRequest(
                source_table=source_table,
                pipeline_type=PipelineType.TRIGGERED,
                embedding_source_columns=[
                    EmbeddingSourceColumn(name="chunk_text", embedding_model_endpoint_name=embedding_model)
                ],
            ),
        )
        created_now = True

    ready, rows = wait_until_ready(w, index_name)
    if created_now and ready and not rows:
        logger.info("Triggering the initial sync...")
        w.vector_search_indexes.sync_index(index_name=index_name)
    elif ready and not created_now:
        try:
            w.vector_search_indexes.sync_index(index_name=index_name)
            logger.info("Sync triggered.")
        except Exception as e:
            logger.warning(f"Sync trigger failed (may already be syncing): {e}")
