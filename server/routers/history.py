"""History endpoints — save and retrieve comparison records from Lakebase."""

import logging
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from ..services.lakebase import get_pool, upsert_user
from ..services.user import get_user_identity, get_workspace_url, require_compare
# One definition: a row written with another default than the one the cache
# lookup reads with would never be found again.
from .compare import APP_VERSION

logger = logging.getLogger(__name__)
router = APIRouter()

_UNAVAILABLE = JSONResponse(
    {'error': 'History not available (LAKEBASE_PROJECT_ID not configured)', 'available': False},
    status_code=503,
)


class SaveComparisonRequest(BaseModel):
    old_filename: str
    new_filename: str
    old_file_hash: Optional[str] = None
    new_file_hash: Optional[str] = None
    analysis_text: Optional[str] = None
    impact_text: Optional[str] = None
    volume_session_path: Optional[str] = None
    # Processor selection
    file_type: Optional[str] = None
    processing_method: Optional[str] = None
    processor_version: Optional[str] = None
    # Token / cost tracking
    ttft_s: Optional[float] = None
    generation_s: Optional[float] = None
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    thinking_tokens: Optional[int] = None
    total_tokens: Optional[int] = None
    cost_eur: Optional[float] = None
    # Link to llm_requests audit row
    llm_request_id: Optional[int] = None


def _serialize(row) -> dict:
    d = dict(row)
    result = {}
    for k, v in d.items():
        if isinstance(v, datetime):
            # Ensure timezone info is present — asyncpg may return naive UTC datetimes.
            # Without it, JS treats the string as local time and introduces a 2h offset.
            if v.tzinfo is None:
                v = v.replace(tzinfo=timezone.utc)
            result[k] = v.isoformat()  # "2025-05-11T10:34:56+00:00"
        elif isinstance(v, (str, int, float, bool, type(None))):
            result[k] = v
        else:
            result[k] = str(v)
    return result


@router.post('/history', dependencies=[Depends(require_compare)])
async def save_comparison(body: SaveComparisonRequest, request: Request):
    pool = get_pool()
    if not pool:
        return _UNAVAILABLE

    try:
        identity = await get_user_identity(request)
    except Exception:
        identity = {'user_id': '', 'email': None}
    user_id = identity['user_id']
    workspace_url = get_workspace_url()

    try:
        async with pool.acquire() as conn:
            # Denormalize the model used from llm_requests (via llm_request_id)
            # so `messages` alone shows it — no join needed to answer "which
            # model produced this comparison".
            endpoint_name = None
            if body.llm_request_id is not None:
                endpoint_row = await conn.fetchrow(
                    'SELECT endpoint_name FROM llm_requests WHERE id = $1',
                    body.llm_request_id,
                )
                endpoint_name = endpoint_row['endpoint_name'] if endpoint_row else None

            # Insert into messages (primary table — comparisons no longer written)
            row = await conn.fetchrow(
                '''
                INSERT INTO messages
                    (user_id, workspace_id, workspace_url,
                     old_filename, new_filename, old_file_hash, new_file_hash,
                     analysis_text, impact_text, volume_session_path,
                     file_type, processing_method, processor_version,
                     ttft_s, generation_s,
                     input_tokens, output_tokens, thinking_tokens, total_tokens, cost_eur,
                     app_version, llm_request_id, endpoint_name)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10,
                        $11, $12, $13, $14, $15, $16, $17, $18, $19, $20, $21, $22, $23)
                RETURNING id, created_at, old_filename, new_filename, volume_session_path
                ''',
                user_id,
                identity.get('workspace_id'),
                workspace_url,
                body.old_filename,
                body.new_filename,
                body.old_file_hash,
                body.new_file_hash,
                body.analysis_text,
                body.impact_text,
                body.volume_session_path,
                body.file_type,
                body.processing_method,
                body.processor_version,
                body.ttft_s,
                body.generation_s,
                body.input_tokens,
                body.output_tokens,
                body.thinking_tokens,
                body.total_tokens,
                body.cost_eur,
                APP_VERSION,
                body.llm_request_id,
                endpoint_name,
            )

            # Register (or update) the user in the shared `users` table.
            # Same helper is used by the chat flow so both surfaces populate
            # `users` consistently — see services/lakebase.upsert_user.
            await upsert_user(
                conn,
                user_id=identity['user_id'],
                workspace_id=identity.get('workspace_id'),
                email=identity.get('email'),
                workspace_url=workspace_url or None,
            )

        result = _serialize(row)
        result['messages_id'] = row['id']
        return result
    except Exception as e:
        logger.error(f'Failed to save comparison: {e}')
        return JSONResponse({'error': str(e)}, status_code=500)


class SaveImpactRequest(BaseModel):
    # The impact search result as displayed (JSON), kept with the comparison it
    # was run on so reopening the history entry shows it again.
    impact_json: str


@router.put('/history/{comparison_id}/impact', dependencies=[Depends(require_compare)])
async def save_comparison_impact(comparison_id: int, body: SaveImpactRequest, request: Request):
    pool = get_pool()
    if not pool:
        return _UNAVAILABLE
    try:
        identity = await get_user_identity(request)
    except Exception:
        identity = {'user_id': '', 'email': None}
    try:
        async with pool.acquire() as conn:
            updated = await conn.fetchval(
                'UPDATE messages SET impact_text = $1 WHERE id = $2 AND user_id = $3 RETURNING id',
                body.impact_json, comparison_id, identity['user_id'],
            )
        if updated is None:
            return JSONResponse({'error': 'Comparison not found'}, status_code=404)
        return {'success': True}
    except Exception as e:
        logger.error(f'Failed to save impact result: {e}')
        return JSONResponse({'error': str(e)}, status_code=500)


@router.get('/history', dependencies=[Depends(require_compare)])
async def list_comparisons(request: Request, limit: int = 20, offset: int = 0):
    pool = get_pool()
    if not pool:
        return {'comparisons': [], 'total': 0, 'available': False}
    try:
        identity = await get_user_identity(request)
    except Exception:
        identity = {'user_id': '', 'email': None}
    user_id = identity['user_id']
    limit, offset = max(1, min(limit, 100)), max(0, offset)
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                '''
                SELECT id, created_at, old_filename, new_filename, volume_session_path
                FROM messages
                WHERE user_id = $1
                ORDER BY created_at DESC
                LIMIT $2 OFFSET $3
                ''',
                user_id,
                limit,
                offset,
            )
            total = await conn.fetchval(
                'SELECT COUNT(*) FROM messages WHERE user_id = $1',
                user_id,
            )
        return {'comparisons': [_serialize(r) for r in rows], 'total': total, 'available': True}
    except Exception as e:
        logger.error(f'Failed to list comparisons: {e}')
        return JSONResponse({'error': str(e)}, status_code=500)


@router.get('/history/{comparison_id}', dependencies=[Depends(require_compare)])
async def get_comparison(comparison_id: int, request: Request):
    pool = get_pool()
    if not pool:
        return _UNAVAILABLE
    try:
        identity = await get_user_identity(request)
    except Exception:
        identity = {'user_id': '', 'email': None}
    user_id = identity['user_id']
    try:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                'SELECT * FROM messages WHERE id = $1 AND user_id = $2',
                comparison_id,
                user_id,
            )
        if not row:
            return JSONResponse({'error': 'Not found'}, status_code=404)
        return _serialize(row)
    except Exception as e:
        logger.error(f'Failed to get comparison {comparison_id}: {e}')
        return JSONResponse({'error': str(e)}, status_code=500)
