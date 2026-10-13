"""POST /compare/summarize: single-document summary, independent of the diff."""

import asyncio
import hashlib
import logging
import time
from typing import Any, Dict, List

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import JSONResponse

from ...services.compare.processors.factory import (
    EXTENSION_MAP, extract_document_text,
    get_extension, prepare_image_content,
)
from ...services.compare.summarize import summarize_image, summarize_text
from ...services.infra.lakebase import (
    get_cached_summary, store_error,
    store_summary_cache,
)
from ...services.infra.user import get_user_identity, require_compare
from .common import (
    APP_VERSION, _get_config, _get_credentials, _validate_file,
)

logger = logging.getLogger(__name__)
router = APIRouter()


@router.post('/compare/summarize', dependencies=[Depends(require_compare)])
async def summarize_document(
    request: Request,
    file: UploadFile = File(...),
    file_hash: str = Form(''),
    force_refresh: str = Form('false'),
):
    try:
        cfg = _get_config()
    except ValueError as e:
        return JSONResponse({'error': str(e)}, status_code=400)

    force = force_refresh.lower() == 'true'

    data = await file.read()
    try:
        _validate_file(file, data)
    except ValueError as e:
        return JSONResponse({'error': str(e)}, status_code=400)

    # Same rule as /compare/analyze: the cache key is the hash of the bytes
    # actually received, not the one the client claims.
    file_hash = hashlib.sha256(data).hexdigest()

    if not force:
        cached = await get_cached_summary(file_hash, APP_VERSION)
        if cached is not None:
            return {**cached, 'cached': True}

    is_image = EXTENSION_MAP.get(get_extension(file.filename or '')) == 'image'
    endpoint = cfg['summary_image_endpoint'] if is_image else cfg['summary_endpoint']
    endpoint_var = 'COMPARE_SUMMARY_IMAGE_ENDPOINT' if is_image else 'COMPARE_SUMMARY_ENDPOINT'
    if not endpoint or endpoint.startswith('TODO'):
        return JSONResponse({'error': f'{endpoint_var} not configured in app.yaml'}, status_code=400)

    text = ''
    image_content: List[Dict[str, Any]] = []
    try:
        if is_image:
            image_content = await asyncio.to_thread(prepare_image_content, file.filename or '', data)
        else:
            text = await asyncio.to_thread(extract_document_text, file.filename or '', data)
    except ValueError as e:
        return JSONResponse({'error': str(e)}, status_code=400)
    except Exception as e:
        logger.error(f'Summary content extraction failed: {e}', exc_info=True)
        return JSONResponse({'error': f'Could not process "{file.filename}": {e}'}, status_code=502)

    if not is_image and not text.strip():
        return {
            'summary': '', 'no_content': True, 'truncated': False, 'duration_s': 0.0,
            'usage': {'input_tokens': 0, 'output_tokens': 0, 'total_tokens': 0, 'cost_eur': 0.0},
        }

    host, token = _get_credentials(request)
    if not host or not token:
        return JSONResponse({'error': 'Missing Databricks credentials'}, status_code=400)

    identity = await get_user_identity(request)
    start = time.monotonic()
    try:
        if is_image:
            result = await summarize_image(host, token, endpoint, image_content, max_tokens=cfg['summary_max_tokens'])
        else:
            result = await summarize_text(
                host, token, endpoint, text,
                max_chars=cfg['summary_max_chars'],
                max_tokens=cfg['summary_max_tokens'],
            )
    except Exception as e:
        logger.error(f'Summarization failed: {e}', exc_info=True)
        store_error(
            endpoint='/api/compare/summarize',
            error_type=type(e).__name__,
            error_msg=str(e),
            user_id=identity['user_id'], workspace_id=identity.get('workspace_id') or '',
            file_type=get_extension(file.filename or ''),
            stage='llm', upstream=endpoint, exc=e,
        )
        return JSONResponse({'error': f'Summarization failed: {e}'}, status_code=502)

    result['duration_s'] = round(time.monotonic() - start, 2)
    if file_hash:
        asyncio.create_task(store_summary_cache(file_hash, APP_VERSION, result, endpoint_name=endpoint))
    return result
