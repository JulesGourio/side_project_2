"""Feedback endpoint — save user votes and comments to Lakebase."""

import logging
from typing import Optional

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from ..services.lakebase import get_pool
from ..services.user import get_user_identity, get_workspace_url

logger = logging.getLogger(__name__)
router = APIRouter()


class FeedbackRequest(BaseModel):
    vote: str          # "up" or "down"
    comment: Optional[str] = None
    message_id: Optional[int] = None


@router.post('/feedback')
async def submit_feedback(body: FeedbackRequest, request: Request):
    if body.vote not in ('up', 'down'):
        return JSONResponse({'error': 'vote must be "up" or "down"'}, status_code=422)

    pool = get_pool()
    if not pool:
        return JSONResponse({'error': 'Feedback unavailable (database not configured)', 'available': False}, status_code=503)

    try:
        identity = await get_user_identity(request)
    except Exception:
        identity = {'user_id': '', 'workspace_id': None, 'email': None}
    user_id = identity['user_id']
    workspace_id = identity.get('workspace_id')
    workspace_url = get_workspace_url()

    try:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                '''
                INSERT INTO feedbacks (message_id, user_id, workspace_id, workspace_url, vote, comment)
                VALUES ($1, $2, $3, $4, $5, $6)
                RETURNING id, created_at, vote
                ''',
                body.message_id,
                user_id,
                workspace_id,
                workspace_url,
                body.vote,
                body.comment or None,
            )
        return {'success': True, 'id': row['id'], 'vote': row['vote']}
    except Exception as e:
        logger.error(f'Failed to save feedback: {e}')
        return JSONResponse({'error': str(e)}, status_code=500)
