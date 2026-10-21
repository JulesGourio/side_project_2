"""History endpoints — save and retrieve comparison records from Lakebase."""

import logging
from datetime import datetime, timezone
from typing import Any, Mapping

from fastapi import APIRouter, Body, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from server.services.infra.lakebase import get_pool, upsert_user
from server.services.infra.user import get_user_identity, get_workspace_url, reject_anonymous, require_compare
# One definition: a row written with another default than the one the cache
# lookup reads with would never be found again.
from server.routers.compare.config import APP_VERSION
from server.routers.errors import INTERNAL_ERROR

# What a JSON response can hold; `_serialize` stringifies anything else.
JsonScalar = str | int | float | bool | None

logger = logging.getLogger(__name__)
router = APIRouter()

def _unavailable() -> JSONResponse:
    """Build the response returned when the history store is not configured.

    :return: a new 503 response (responses are not shared between requests)
    """
    return JSONResponse(
        {'error': 'History not available (LAKEBASE_PROJECT_ID not configured)', 'available': False},
        status_code=503,
    )


async def _caller(request: Request) -> dict:
    """The caller's identity, refused with a 401 when there is none (outside development).

    :param request: the incoming request.
    :return: ``{'user_id', 'workspace_id', 'email'}``; an empty ``user_id`` only in development.
    """
    try:
        identity = dict(await get_user_identity(request))
    except Exception:
        identity = {'user_id': '', 'workspace_id': None, 'email': None}
    reject_anonymous(identity)
    return identity


class SaveComparisonRequest(BaseModel):
    old_filename: str
    new_filename: str
    old_file_hash: str | None = None
    new_file_hash: str | None = None
    analysis_text: str | None = None
    impact_text: str | None = None
    volume_session_path: str | None = None
    file_type: str | None = None
    processing_method: str | None = None
    processor_version: str | None = None
    ttft_s: float | None = None
    generation_s: float | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    thinking_tokens: int | None = None
    total_tokens: int | None = None
    cost_eur: float | None = None
    llm_request_id: int | None = None


def _serialize(row: Mapping[str, Any]) -> dict[str, JsonScalar]:
    """A database row as a JSON-ready dict.

    :param row: a ``compare_messages`` record (any column set).
    :return: the same keys; datetimes as ISO strings (UTC when naive), other non-scalar values as ``str``.
    """
    d = dict(row)
    result: dict[str, JsonScalar] = {}
    for k, v in d.items():
        if isinstance(v, datetime):
            # Ensure timezone info is present — asyncpg may return naive UTC datetimes.
            # Without it, JS reads the string as local time.
            if v.tzinfo is None:
                v = v.replace(tzinfo=timezone.utc)
            result[k] = v.isoformat()  # "YYYY-MM-DDTHH:MM:SS+00:00"
        elif isinstance(v, (str, int, float, bool, type(None))):
            result[k] = v
        else:
            result[k] = str(v)
    return result


@router.post('/history', dependencies=[Depends(require_compare)], response_model=None)
async def save_comparison(body: SaveComparisonRequest, request: Request) -> dict | JSONResponse:
    """Save a finished comparison in the caller's history.

    :param body: the compared files, the analysis and the timings of the run; the token counts and the cost it may carry
        are ignored, they come from the ``llm_requests`` row of the caller.
    :param request: the incoming request, for the caller's identity.
    :return: the saved row's id (``messages_id``), or an error response (503 without database, 500 on a failed save).
    """
    pool = get_pool()
    if not pool:
        return _unavailable()

    identity = await _caller(request)
    user_id = identity['user_id']
    workspace_url = get_workspace_url()

    try:
        async with pool.acquire() as conn:
            # Denormalized from llm_requests, so messages alone shows the model that produced the comparison.
            # Usage numbers come from the caller's own llm_requests row, never from the request body.
            endpoint_name = None
            usage: Mapping[str, Any] = {}
            llm_request_id = None
            if body.llm_request_id is not None:
                usage_row = await conn.fetchrow(
                    'SELECT endpoint_name, input_tokens, output_tokens, thinking_tokens, total_tokens, cost_eur '
                    'FROM llm_requests WHERE id = $1 AND user_id = $2',
                    body.llm_request_id, user_id,
                )
                if usage_row:
                    usage = usage_row
                    endpoint_name = usage_row['endpoint_name']
                    llm_request_id = body.llm_request_id

            row = await conn.fetchrow(
                '''
                INSERT INTO compare_messages
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
                usage.get('input_tokens'),
                usage.get('output_tokens'),
                usage.get('thinking_tokens'),
                usage.get('total_tokens'),
                usage.get('cost_eur'),
                APP_VERSION,
                llm_request_id,
                endpoint_name,
            )

            # Registers the user in the shared users table (same helper as the chat flow).
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
        return JSONResponse({'error': INTERNAL_ERROR}, status_code=500)


@router.put('/history/{comparison_id}/impact', dependencies=[Depends(require_compare)], response_model=None)
async def save_comparison_impact(
    comparison_id: int,
    request: Request,
    # The impact search result as displayed (JSON), kept with the comparison it was run on so reopening the history
    # entry shows it again. Sent as {"impact_json": "..."}.
    impact_json: str = Body(..., embed=True),
) -> dict | JSONResponse:
    """Attach the impact search result to a saved comparison.

    :param comparison_id: id of the comparison in ``compare_messages``, which must belong to the caller.
    :param request: the incoming request, for the caller's identity.
    :param impact_json: the result as displayed, serialised to JSON.
    :return: ``{'success': True}``, or a 404 when the comparison is not the caller's.
    """
    pool = get_pool()
    if not pool:
        return _unavailable()
    identity = await _caller(request)
    try:
        async with pool.acquire() as conn:
            updated = await conn.fetchval(
                'UPDATE compare_messages SET impact_text = $1 WHERE id = $2 AND user_id = $3 RETURNING id',
                impact_json, comparison_id, identity['user_id'],
            )
        if updated is None:
            return JSONResponse({'error': 'Comparison not found'}, status_code=404)
        return {'success': True}
    except Exception as e:
        logger.error(f'Failed to save impact result: {e}')
        return JSONResponse({'error': INTERNAL_ERROR}, status_code=500)


@router.get('/history', dependencies=[Depends(require_compare)], response_model=None)
async def list_comparisons(request: Request, limit: int = 20, offset: int = 0) -> dict | JSONResponse:
    """The caller's saved comparisons, newest first.

    :param request: the incoming request, for the caller's identity.
    :param limit: page size, kept between 1 and 100.
    :param offset: rows to skip, at least 0.
    :return: ``{'comparisons': [...], 'total', 'available'}``.
    """
    pool = get_pool()
    if not pool:
        return {'comparisons': [], 'total': 0, 'available': False}
    identity = await _caller(request)
    user_id = identity['user_id']
    limit, offset = max(1, min(limit, 100)), max(0, offset)
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                '''
                SELECT id, created_at, old_filename, new_filename, volume_session_path
                FROM compare_messages
                WHERE user_id = $1
                ORDER BY created_at DESC
                LIMIT $2 OFFSET $3
                ''',
                user_id,
                limit,
                offset,
            )
            total = await conn.fetchval(
                'SELECT COUNT(*) FROM compare_messages WHERE user_id = $1',
                user_id,
            )
        return {'comparisons': [_serialize(r) for r in rows], 'total': total, 'available': True}
    except Exception as e:
        logger.error(f'Failed to list comparisons: {e}')
        return JSONResponse({'error': INTERNAL_ERROR}, status_code=500)


@router.get('/history/{comparison_id}', dependencies=[Depends(require_compare)], response_model=None)
async def get_comparison(comparison_id: int, request: Request) -> dict | JSONResponse:
    """One of the caller's saved comparisons, in full.

    :param comparison_id: id of the comparison in ``compare_messages``.
    :param request: the incoming request, for the caller's identity.
    :return: the row, or a 404 when it is not the caller's.
    """
    pool = get_pool()
    if not pool:
        return _unavailable()
    identity = await _caller(request)
    user_id = identity['user_id']
    try:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                'SELECT * FROM compare_messages WHERE id = $1 AND user_id = $2',
                comparison_id,
                user_id,
            )
        if not row:
            return JSONResponse({'error': 'Not found'}, status_code=404)
        return _serialize(row)
    except Exception as e:
        logger.error(f'Failed to get comparison {comparison_id}: {e}')
        return JSONResponse({'error': INTERNAL_ERROR}, status_code=500)
