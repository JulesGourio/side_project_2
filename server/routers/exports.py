"""Exports router: Excel, PDF and raw text downloads of compare analyses and chatbot answers.

Pure helpers live in services/compare/export_helpers.py (no FastAPI import).
PDF layout: services/infra/pdf_report/.
"""

import asyncio
import io
import json
import logging
import re

from databricks.sdk import WorkspaceClient
from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field

from server.services.compare.export_helpers import (
    _build_excel_bytes,
    _build_impact_excel_bytes,
    _parse_json_response,
)
from server.services.infra.lakebase import store_error
from server.services.infra.user import require_compare
from server.services.infra.pdf_report import (
    KIND_LABELS,
    Section,
    Source,
    build_markdown_pdf,
    build_report_pdf,
)
from server.routers.compare.config import get_config
from server.routers.compare.common import _sanitize_filename, owns_session
from server.routers.errors import INTERNAL_ERROR

logger = logging.getLogger(__name__)
router = APIRouter()


def _upload(remote_path: str, data: bytes) -> None:
    """Blocking SDK upload — always called through asyncio.to_thread.

    :param remote_path: destination on the volume.
    :param data: file content.
    """
    WorkspaceClient().files.upload(remote_path, io.BytesIO(data), overwrite=True)


# --- /compare/export-excel — convert structured JSON analysis to Excel download ---

@router.post('/compare/export-excel', dependencies=[Depends(require_compare)], response_model=None)
async def export_excel(
    json_text: str = Form(...),
    filename: str = Form('comparison'),
    file_type: str = Form(''),
    image_pairs_json: str = Form(''),
) -> Response:
    """Generate a colour-coded Excel workbook from a structured JSON analysis and return it for download.

    :param json_text: the structured JSON analysis returned by the model.
    :param filename: download name, without extension.
    :param file_type: type of the compared files, passed to the workbook builder.
    :param image_pairs_json: JSON of the image pairs to embed, '' for none.
    """
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


@router.post('/compare/impact/export-excel', dependencies=[Depends(require_compare)], response_model=None)
async def export_impact_excel(result_json: str = Form(...), filename: str = Form('impact')) -> Response:
    """Impact search result (as shown in the UI) → Excel action list, one row per conflicting passage.

    :param result_json: the impact search result as shown in the UI, serialised to JSON.
    :param filename: download name, without extension.
    """
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
    """A safe download name ending in ``.pdf``.

    :param name: the name asked by the client.
    :param default: name used when nothing usable is left.
    :return: the file name.
    """
    safe = re.sub(r'[^A-Za-z0-9._-]+', '_', name).strip('._') or default
    return safe if safe.lower().endswith('.pdf') else f'{safe}.pdf'


@router.post('/export/pdf', dependencies=[Depends(require_compare)], response_model=None)
async def export_pdf(req: PdfExportRequest) -> Response:
    """Render one or more Markdown sections (with optional question and sources) as a PDF download.

    :param req: the kind of report, its title, metadata and Markdown sections.
    """
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
        return JSONResponse({'error': INTERNAL_ERROR}, status_code=500)

    return Response(
        content=pdf_bytes,
        media_type='application/pdf',
        headers={'Content-Disposition': f'attachment; filename="{_pdf_filename(req.filename, "qualibot")}"'},
    )


# --- /compare/save-result ---

@router.post('/compare/save-result', dependencies=[Depends(require_compare)], response_model=None)
async def save_result_to_session(
    request: Request,
    session_path: str = Form(...),
    filename: str = Form(...),
    content: str = Form(...),
) -> dict | JSONResponse:
    """Save a text result in the session folder of the volume.

    :param request: the incoming request, for the caller's identity: only the caller's own sessions are writable.
    :param session_path: folder of the saved session; it must be the caller's.
    :param filename: name of the file to write.
    :param content: text to write.
    :return: ``{'success': True, 'path'}``, or an error response (400 invalid request or volume not configured, 500 on a failed upload).
    """
    try:
        cfg = get_config()
    except ValueError as e:
        return JSONResponse({'error': str(e)}, status_code=400)

    volume_path = cfg['volume_path'].rstrip('/')
    if not volume_path or volume_path.startswith('TODO'):
        return JSONResponse({'error': 'volume_path not configured'}, status_code=400)
    if not await owns_session(request, volume_path, session_path):
        return JSONResponse({'error': 'Invalid session_path'}, status_code=403)

    safe_name = _sanitize_filename(filename)
    if not (safe_name.endswith('.md') or safe_name.endswith('.txt')):
        return JSONResponse({'error': 'filename must end with .md or .txt'}, status_code=400)

    try:
        remote_path = f'{session_path}/{safe_name}'
        await asyncio.to_thread(_upload, remote_path, content.encode('utf-8'))
        return {'success': True, 'path': remote_path}
    except Exception as e:
        logger.error('Request failed: %s', e, exc_info=True)
        return JSONResponse({'error': INTERNAL_ERROR}, status_code=500)


# --- /compare/save-excel  /compare/save-pdf — auto-save formatted exports to volume ---

@router.post('/compare/save-excel', dependencies=[Depends(require_compare)], response_model=None)
async def save_excel_to_session(
    request: Request,
    session_path: str = Form(...),
    json_text: str = Form(...),
    file_type: str = Form(''),
    filename: str = Form('analysis'),
    image_pairs_json: str = Form(''),
) -> dict | JSONResponse:
    """Generate Excel from a structured JSON analysis and save it to the session volume directory.

    :param request: the incoming request, for the caller's identity: only the caller's own sessions are writable.
    :param session_path: folder of the saved session; it must be the caller's.
    :param json_text: the structured JSON analysis returned by the model.
    :param file_type: type of the compared files.
    :param filename: name of the workbook, without extension.
    :param image_pairs_json: JSON of the image pairs to embed, '' for none.
    :return: ``{'success': True, 'path'}``, or an error response (400 invalid request or volume not configured, 500 on a failed upload).
    """
    try:
        cfg = get_config()
    except ValueError as e:
        return JSONResponse({'error': str(e)}, status_code=400)

    volume_path = cfg['volume_path'].rstrip('/')
    if not volume_path or volume_path.startswith('TODO'):
        return JSONResponse({'error': 'volume_path not configured'}, status_code=400)
    if not await owns_session(request, volume_path, session_path):
        return JSONResponse({'error': 'Invalid session_path'}, status_code=403)

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
        return JSONResponse({'error': INTERNAL_ERROR}, status_code=500)

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
        return JSONResponse({'error': INTERNAL_ERROR}, status_code=500)


@router.post('/compare/save-pdf', dependencies=[Depends(require_compare)], response_model=None)
async def save_pdf_to_session(
    request: Request,
    session_path: str = Form(...),
    markdown_text: str = Form(None),   # optional — server generates PDF from markdown
    pdf_file: UploadFile = File(None),  # optional — client uploads a pre-rendered PDF
    filename: str = Form('analysis'),
    title: str = Form('Change summary'),
    old_name: str = Form(''),
    new_name: str = Form(''),
) -> dict | JSONResponse:
    """Save a PDF to volume. Can accept EITHER an uploaded client PDF, OR generate one server-side from markdown.

    :param request: the incoming request, for the caller's identity: only the caller's own sessions are writable.
    :param session_path: folder of the saved session; it must be the caller's.
    :param markdown_text: Markdown to render as the PDF, when no PDF is uploaded.
    :param pdf_file: a PDF rendered by the browser, saved as is.
    :param filename: name of the file, without extension.
    :param title: title of a PDF rendered from Markdown.
    :param old_name: name of the previous revision, printed in the header.
    :param new_name: name of the new revision, printed in the header.
    :return: ``{'success': True, 'path'}``, or an error response (400 invalid request or volume not configured, 500 on a failed upload).
    """
    try:
        cfg = get_config()
    except ValueError as e:
        return JSONResponse({'error': str(e)}, status_code=400)

    volume_path = cfg['volume_path'].rstrip('/')
    if not volume_path or volume_path.startswith('TODO'):
        return JSONResponse({'error': 'volume_path not configured'}, status_code=400)
    if not await owns_session(request, volume_path, session_path):
        return JSONResponse({'error': 'Invalid session_path'}, status_code=403)

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
        return JSONResponse({'error': INTERNAL_ERROR}, status_code=500)

    safe_name = _pdf_filename(filename, 'analysis')

    try:
        remote_path = f'{session_path}/{safe_name}'
        await asyncio.to_thread(_upload, remote_path, pdf_bytes)
        return {'success': True, 'path': remote_path}
    except Exception as e:
        logger.error('save-pdf: volume upload failed: %s', e, exc_info=True)
        store_error(endpoint='/api/compare/save-pdf', error_type=type(e).__name__, error_msg=str(e), exc=e)
        return JSONResponse({'error': INTERNAL_ERROR}, status_code=500)
