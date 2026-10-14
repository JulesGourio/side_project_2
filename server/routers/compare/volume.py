"""POST /compare/save and GET /compare/load: persist and restore a comparison session on the volume."""

import asyncio
import base64
import io
import logging
import mimetypes
import os
from datetime import datetime
from typing import List

from databricks.sdk import WorkspaceClient
from fastapi import APIRouter, Depends, File, Form, UploadFile
from fastapi.responses import JSONResponse

from ...services.infra.user import require_compare
from .common import (
    _get_config, _is_within_volume, _sanitize_filename, _validate_file,
)

logger = logging.getLogger(__name__)
router = APIRouter()


@router.post('/compare/save', dependencies=[Depends(require_compare)])
async def save_to_volume(
    old_file: UploadFile = File(...),
    new_file: UploadFile = File(...),
    analysis_text: str = Form(''),
    impact_text: str = Form(''),
):
    try:
        cfg = _get_config()
    except ValueError as e:
        return JSONResponse({'error': str(e)}, status_code=400)

    volume_path = cfg['volume_path'].rstrip('/')
    if not volume_path or volume_path.startswith('TODO'):
        logger.warning('save: COMPARE_VOLUME_PATH is not configured — cannot persist session files. '
                       'Set it in databricks.yml (resources.apps.doc-compare.env) or app.yaml.')
        return JSONResponse({'error': 'COMPARE_VOLUME_PATH not configured'}, status_code=400)

    try:
        old_bytes = await old_file.read()
        new_bytes = await new_file.read()
        _validate_file(old_file, old_bytes)
        _validate_file(new_file, new_bytes)
    except Exception as e:
        return JSONResponse({'error': str(e)}, status_code=400)

    timestamp = datetime.now().strftime('%Y-%m-%d_%H%M%S')
    session_path = f'{volume_path}/{timestamp}'
    saved_files: List[str] = []

    def _save() -> None:
        w = WorkspaceClient()

        def _ensure_dir(path: str):
            try:
                w.files.create_directory(path)
            except Exception as e:
                if 'already exists' not in str(e).lower():
                    raise

        def _upload(path: str, data: bytes):
            w.files.upload(path, io.BytesIO(data), overwrite=True)
            saved_files.append(path)

        _ensure_dir(volume_path)
        _ensure_dir(session_path)
        _upload(f'{session_path}/old_{_sanitize_filename(old_file.filename or "old")}', old_bytes)
        _upload(f'{session_path}/new_{_sanitize_filename(new_file.filename or "new")}', new_bytes)
        if analysis_text.strip():
            _upload(f'{session_path}/analysis.md', analysis_text.encode('utf-8'))
        if impact_text.strip():
            _upload(f'{session_path}/impact.md', impact_text.encode('utf-8'))

    try:
        # The SDK calls block: they must not run on the event loop.
        await asyncio.to_thread(_save)
        return {'success': True, 'session_path': session_path, 'saved_files': saved_files}
    except Exception as e:
        logger.error(f'Failed to save to volume: {e}')
        return JSONResponse({'error': str(e), 'saved_files': saved_files}, status_code=500)


# --- /compare/load — restore files from a saved volume session ---

@router.get('/compare/load', dependencies=[Depends(require_compare)])
async def load_session_files(session_path: str, old_filename: str = '', new_filename: str = ''):
    """Retrieve the two documents stored in a volume session back as base64.

    Called when the user loads a history entry so the files are restored
    in the UI and can be previewed or re-analyzed.
    """

    # session_path comes from the DB (trusted) — security check only when volume_path is configured
    volume_path = os.getenv('COMPARE_VOLUME_PATH', '').rstrip('/')
    if volume_path and not volume_path.startswith('TODO'):
        if not _is_within_volume(session_path, volume_path):
            logger.warning(f'load: session_path {session_path!r} outside configured volume {volume_path!r}')
            return JSONResponse({'error': 'Invalid session_path'}, status_code=403)
    else:
        # No volume configured — allow only UC Volume paths (starts with /Volumes/)
        if not _is_within_volume(session_path, '/Volumes'):
            logger.warning(f'load: session_path {session_path!r} does not look like a UC Volume path')
            return JSONResponse({'error': 'Invalid session_path'}, status_code=403)
        logger.info(f'load: COMPARE_VOLUME_PATH not set, attempting load from trusted DB path {session_path!r}')

    def _read_file(w, path: str) -> bytes:
        resp = w.files.download(path)
        c = resp.contents
        return c.read() if hasattr(c, 'read') else bytes(c)

    def _load() -> dict:
        w = WorkspaceClient()
        result: dict = {}

        for prefix, original_name in (('old', old_filename), ('new', new_filename)):
            safe = _sanitize_filename(original_name) if original_name else None
            file_bytes = None
            found_name = original_name or f'{prefix}_document'

            if safe:
                try:
                    file_bytes = _read_file(w, f'{session_path}/{prefix}_{safe}')
                    found_name = original_name
                except Exception:
                    pass

            # Fall back to directory listing
            if file_bytes is None:
                try:
                    entries = list(w.files.list_directory_contents(session_path))
                    for entry in entries:
                        name = getattr(entry, 'name', '') or ''
                        if name.startswith(f'{prefix}_') and not name.endswith('.md'):
                            file_bytes = _read_file(w, f'{session_path}/{name}')
                            found_name = name[len(prefix) + 1:]  # strip prefix_
                            break
                except Exception:
                    pass

            if file_bytes is not None:
                mime = mimetypes.guess_type(found_name)[0] or 'application/octet-stream'
                result[prefix] = {
                    'name': found_name,
                    'size': len(file_bytes),
                    'data': base64.b64encode(file_bytes).decode('ascii'),
                    'mimeType': mime,
                }

        return result

    try:
        return await asyncio.to_thread(_load)
    except Exception as e:
        logger.error(f'Failed to load session files: {e}')
        return JSONResponse({'error': str(e)}, status_code=500)
