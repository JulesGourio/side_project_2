"""Exports router — endpoints for generating and saving formatted analysis outputs.

Handles Excel, PDF, and raw text exports of compare analysis results, and the
PDF exports of the chatbot (one layout for both: services/infra/pdf_report.py).
Pure helper functions live in services/export_helpers.py (no FastAPI deps) so
the debug notebook can import them directly without triggering the server
import chain.
"""

import asyncio
import io
import json
import logging
import re

from databricks.sdk import WorkspaceClient
from fastapi import APIRouter, File, Form, UploadFile
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field

from ..services.compare.export_helpers import (
    _build_excel_bytes,
    _build_impact_excel_bytes,
    _parse_json_response,
)
from ..services.infra.lakebase import store_error
from ..services.infra.pdf_report import (
    KIND_LABELS,
    Section,
    Source,
    build_markdown_pdf,
    build_report_pdf,
)
from .compare import _get_config, _is_within_volume, _sanitize_filename

logger = logging.getLogger(__name__)
router = APIRouter()


def _upload(remote_path: str, data: bytes) -> None:
    """Blocking SDK upload — always called through asyncio.to_thread."""
    WorkspaceClient().files.upload(remote_path, io.BytesIO(data), overwrite=True)


# --- /compare/export-excel — convert structured JSON analysis to Excel download ---

@router.post('/compare/export-excel')
async def export_excel(
    json_text: str = Form(...),
    filename: str = Form('comparison'),
    file_type: str = Form(''),
    image_pairs_json: str = Form(''),
):
    """Generate a colour-coded Excel workbook from a structured JSON analysis and return it for download."""
    rows = _parse_json_response(json_text)
    if rows is None:
        return JSONResponse({'error': 'Could not parse JSON from analysis text'}, status_code=400)

    image_pairs = None
    if image_pairs_json:
        try:
            image_pairs = json.loads(image_pairs_json)
            logger.info('export-excel: %d image pair(s) received', len(image_pairs) if image_pairs else 0)
        except Exception as e:
            logger.warning('export-excel: failed to parse image_pairs_json: %s', e)

    try:
        excel_bytes = await asyncio.to_thread(_build_excel_bytes, rows, file_type, image_pairs)
    except ImportError:
        return JSONResponse({'error': 'openpyxl is not installed on the server'}, status_code=500)

    safe_name = re.sub(r'[^A-Za-z0-9._-]+', '_', filename).strip('._') or 'comparison'
    if not safe_name.endswith('.xlsx'):
        safe_name += '.xlsx'

    return Response(
        content=excel_bytes,
        media_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        headers={'Content-Disposition': f'attachment; filename="{safe_name}"'},
    )


@router.post('/compare/impact/export-excel')
async def export_impact_excel(result_json: str = Form(...), filename: str = Form('impact')):
    """Impact search result (as shown in the UI) → Excel action list, one row per conflicting passage."""
    try:
        result = json.loads(result_json)
    except json.JSONDecodeError as e:
        return JSONResponse({'error': f'Invalid result JSON: {e}'}, status_code=400)

    safe_name = re.sub(r'[^A-Za-z0-9._-]+', '_', filename).strip('._') or 'impact'
    return Response(
        content=await asyncio.to_thread(_build_impact_excel_bytes, result),
        media_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        headers={'Content-Disposition': f'attachment; filename="{safe_name}.xlsx"'},
    )


# --- /export/pdf — chatbot answers / conversations and compare summaries as PDF ---

class PdfSource(BaseModel):
    title: str
    url: str | None = None
    n: int | None = None
    detail: str = ''


class PdfSection(BaseModel):
    markdown: str = ''
    question: str | None = None
    sources: list[PdfSource] = Field(default_factory=list)


class PdfMeta(BaseModel):
    label: str
    value: str


class PdfExportRequest(BaseModel):
    kind: str = 'report'                     # pdf_report.KIND_LABELS key — sets the header label
    title: str = ''
    filename: str = 'qualibot'
    meta: list[PdfMeta] = Field(default_factory=list)
    sections: list[PdfSection]


def _pdf_filename(name: str, default: str) -> str:
    safe = re.sub(r'[^A-Za-z0-9._-]+', '_', name).strip('._') or default
    return safe if safe.lower().endswith('.pdf') else f'{safe}.pdf'


@router.post('/export/pdf')
async def export_pdf(req: PdfExportRequest):
    """Render one or more Markdown sections (with optional question and sources) as a PDF download."""
    if req.kind not in KIND_LABELS:
        return JSONResponse({'error': f'Unknown kind {req.kind!r}'}, status_code=400)
    if not any(s.markdown.strip() for s in req.sections):
        return JSONResponse({'error': 'Nothing to export'}, status_code=400)
    sections = [
        Section(s.markdown, s.question, [Source(src.title, src.url, src.n, src.detail) for src in s.sources])
        for s in req.sections
    ]
    try:
        pdf_bytes = await asyncio.to_thread(
            build_report_pdf, kind=req.kind, title=req.title, sections=sections,
            meta=[(m.label, m.value) for m in req.meta],
        )
    except Exception as e:
        logger.error('export-pdf: PDF generation failed: %s', e, exc_info=True)
        store_error(endpoint='/api/export/pdf', error_type=type(e).__name__, error_msg=str(e), exc=e)
        return JSONResponse({'error': f'PDF generation failed: {e}'}, status_code=500)

    return Response(
        content=pdf_bytes,
        media_type='application/pdf',
        headers={'Content-Disposition': f'attachment; filename="{_pdf_filename(req.filename, "qualibot")}"'},
    )


# --- /compare/save-result ---

@router.post('/compare/save-result')
async def save_result_to_session(
    session_path: str = Form(...),
    filename: str = Form(...),
    content: str = Form(...),
):
    try:
        cfg = _get_config()
    except ValueError as e:
        return JSONResponse({'error': str(e)}, status_code=400)

    volume_path = cfg['volume_path'].rstrip('/')
    if not volume_path or volume_path.startswith('TODO'):
        return JSONResponse({'error': 'volume_path not configured'}, status_code=400)
    if not _is_within_volume(session_path, volume_path):
        return JSONResponse({'error': 'Invalid session_path'}, status_code=400)

    safe_name = _sanitize_filename(filename)
    if not (safe_name.endswith('.md') or safe_name.endswith('.txt')):
        return JSONResponse({'error': 'filename must end with .md or .txt'}, status_code=400)

    try:
        remote_path = f'{session_path}/{safe_name}'
        await asyncio.to_thread(_upload, remote_path, content.encode('utf-8'))
        return {'success': True, 'path': remote_path}
    except Exception as e:
        return JSONResponse({'error': str(e)}, status_code=500)


# --- /compare/save-excel  /compare/save-pdf — auto-save formatted exports to volume ---

@router.post('/compare/save-excel')
async def save_excel_to_session(
    session_path: str = Form(...),
    json_text: str = Form(...),
    file_type: str = Form(''),
    filename: str = Form('analysis'),
    image_pairs_json: str = Form(''),
):
    """Generate Excel from a structured JSON analysis and save it to the session volume directory."""
    try:
        cfg = _get_config()
    except ValueError as e:
        return JSONResponse({'error': str(e)}, status_code=400)

    volume_path = cfg['volume_path'].rstrip('/')
    if not volume_path or volume_path.startswith('TODO'):
        return JSONResponse({'error': 'volume_path not configured'}, status_code=400)
    if not _is_within_volume(session_path, volume_path):
        return JSONResponse({'error': 'Invalid session_path'}, status_code=400)

    rows = _parse_json_response(json_text)
    if rows is None:
        return JSONResponse({'error': 'Could not parse JSON analysis text'}, status_code=400)

    image_pairs = None
    if image_pairs_json:
        try:
            image_pairs = json.loads(image_pairs_json)
            logger.info('save-excel: %d image pair(s) received', len(image_pairs) if image_pairs else 0)
        except Exception as e2:
            logger.warning('save-excel: failed to parse image_pairs_json: %s', e2)

    try:
        excel_bytes = await asyncio.to_thread(_build_excel_bytes, rows, file_type, image_pairs)
    except Exception as e:
        logger.error('save-excel: Excel generation failed: %s', e, exc_info=True)
        store_error(endpoint='/api/compare/save-excel', error_type=type(e).__name__, error_msg=str(e), exc=e)
        return JSONResponse({'error': f'Excel generation failed: {e}'}, status_code=500)

    safe_name = re.sub(r'[^A-Za-z0-9._-]+', '_', filename).strip('._') or 'analysis'
    if not safe_name.endswith('.xlsx'):
        safe_name += '.xlsx'

    try:
        remote_path = f'{session_path}/{safe_name}'
        await asyncio.to_thread(_upload, remote_path, excel_bytes)
        return {'success': True, 'path': remote_path}
    except Exception as e:
        logger.error('save-excel: volume upload failed: %s', e, exc_info=True)
        store_error(endpoint='/api/compare/save-excel', error_type=type(e).__name__, error_msg=str(e), exc=e)
        return JSONResponse({'error': str(e)}, status_code=500)


@router.post('/compare/save-pdf')
async def save_pdf_to_session(
    session_path: str = Form(...),
    markdown_text: str = Form(None),   # optional — server generates PDF from markdown
    pdf_file: UploadFile = File(None),  # optional — client uploads a pre-rendered PDF
    filename: str = Form('analysis'),
    title: str = Form('Change summary'),
    old_name: str = Form(''),
    new_name: str = Form(''),
):
    """Save a PDF to volume. Can accept EITHER an uploaded client PDF, OR generate one server-side from markdown."""
    try:
        cfg = _get_config()
    except ValueError as e:
        return JSONResponse({'error': str(e)}, status_code=400)

    volume_path = cfg['volume_path'].rstrip('/')
    if not volume_path or volume_path.startswith('TODO'):
        return JSONResponse({'error': 'volume_path not configured'}, status_code=400)
    if not _is_within_volume(session_path, volume_path):
        return JSONResponse({'error': 'Invalid session_path'}, status_code=400)

    try:
        if markdown_text:
            meta = [('Original (A)', old_name), ('Revised (B)', new_name)]
            pdf_bytes = await asyncio.to_thread(build_markdown_pdf, markdown_text, title, 'change_summary', meta)
        elif pdf_file:
            pdf_bytes = await pdf_file.read()
        else:
            return JSONResponse({'error': 'Must provide either markdown_text or pdf_file'}, status_code=400)
    except Exception as e:
        logger.error('save-pdf: processing failed: %s', e, exc_info=True)
        store_error(endpoint='/api/compare/save-pdf', error_type=type(e).__name__, error_msg=str(e), exc=e)
        return JSONResponse({'error': f'Could not process PDF: {e}'}, status_code=500)

    safe_name = _pdf_filename(filename, 'analysis')

    try:
        remote_path = f'{session_path}/{safe_name}'
        await asyncio.to_thread(_upload, remote_path, pdf_bytes)
        return {'success': True, 'path': remote_path}
    except Exception as e:
        logger.error('save-pdf: volume upload failed: %s', e, exc_info=True)
        store_error(endpoint='/api/compare/save-pdf', error_type=type(e).__name__, error_msg=str(e), exc=e)
        return JSONResponse({'error': str(e)}, status_code=500)
