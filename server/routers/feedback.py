"""Feedback endpoint — save user votes and comments to Lakebase."""

import logging
from typing import Literal

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from server.services.infra.lakebase import get_pool
from server.services.infra.user import get_user_identity, get_workspace_url, require_compare

logger = logging.getLogger(__name__)
router = APIRouter()


class FeedbackRequest(BaseModel):
    vote: Literal['up', 'down']
    comment: str | None = None
    message_id: int | None = None


@router.post('/feedback', dependencies=[Depends(require_compare)], response_model=None)
async def submit_feedback(body: FeedbackRequest, request: Request) -> dict | JSONResponse:
    """Save a thumbs up / down on a comparison result.

    :param body: the vote (anything but ``up`` or ``down`` is refused with a 422 before the route runs), an optional
        comment and the message it is about.
    :param request: the incoming request, for the caller's identity.
    :return: ``{'success': True, 'id', 'vote'}``, or an error response (503 without database, 500 on a failed save).
    """
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
                INSERT INTO compare_feedbacks (message_id, user_id, workspace_id, workspace_url, vote, comment)
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
    vote: Literal['up', 'down']
    comment: str | None = None
    impact_request_id: int | None = None
    message_id: int | None = None
    old_file_hash: str | None = None
    new_file_hash: str | None = None
    # Set for a vote on one document's verdict; empty for a vote on the whole result.
    ref: str | None = None
    verdict_shown: str | None = None


@router.post('/compare/impact/feedback', dependencies=[Depends(require_compare)], response_model=None)
async def submit_impact_feedback(body: ImpactFeedbackRequest, request: Request) -> dict | JSONResponse:
    """Save a thumbs up / down on an impact search result, or on one document's verdict when ``body.ref`` is set.

    :param body: the vote (anything but ``up`` or ``down`` is refused with a 422 before the route runs), an optional
        comment (cut to 2000 characters) and what it is about: the impact request, the compared files, the document.
    :param request: the incoming request, for the caller's identity.
    :return: ``{'success': True, 'id'}``, or an error response (503 without database, 500 on a failed save).
    """
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
