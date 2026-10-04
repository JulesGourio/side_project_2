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


class ImpactFeedbackRequest(BaseModel):
    vote: str          # "up" or "down"
    comment: Optional[str] = None
    impact_request_id: Optional[int] = None
    message_id: Optional[int] = None
    old_file_hash: Optional[str] = None
    new_file_hash: Optional[str] = None
    # Set for a vote on one document's verdict; empty for a vote on the whole result.
    ref: Optional[str] = None
    verdict_shown: Optional[str] = None


@router.post('/compare/impact/feedback')
async def submit_impact_feedback(body: ImpactFeedbackRequest, request: Request):
    if body.vote not in ('up', 'down'):
        return JSONResponse({'error': 'vote must be "up" or "down"'}, status_code=422)

    pool = get_pool()
    if not pool:
        return JSONResponse({'error': 'Feedback unavailable (database not configured)', 'available': False}, status_code=503)

    try:
        identity = await get_user_identity(request)
    except Exception:
        identity = {'user_id': '', 'workspace_id': None, 'email': None}

    try:
        async with pool.acquire() as conn:
            feedback_id = await conn.fetchval(
                '''
                INSERT INTO impact_feedbacks
                    (impact_request_id, message_id, user_id, workspace_id, workspace_url,
                     old_file_hash, new_file_hash, ref, verdict_shown, vote, comment)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
                RETURNING id
                ''',
                body.impact_request_id,
                body.message_id,
                identity['user_id'],
                identity.get('workspace_id'),
                get_workspace_url(),
                body.old_file_hash or None,
                body.new_file_hash or None,
                body.ref or None,
                body.verdict_shown or None,
                body.vote,
                (body.comment or '')[:2000] or None,
            )
        return {'success': True, 'id': feedback_id}
    except Exception as e:
        logger.error(f'Failed to save impact feedback: {e}')
        return JSONResponse({'error': str(e)}, status_code=500)
