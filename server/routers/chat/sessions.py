"""Chat feedback and the session routes: list, read, delete, share and duplicate."""

import asyncio
import logging
import secrets
import uuid
from typing import Optional

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from ...services.chat import mlflow_turns
from ...services.infra.lakebase import get_pool, upsert_user
from ...services.infra.user import get_workspace_url, require_chat
from .helpers import (
    _identity_or_anonymous, _stored_sources,
)
from .persistence import _upsert_session

logger = logging.getLogger(__name__)
router = APIRouter()


class ChatFeedbackRequest(BaseModel):
    vote: str
    comment: Optional[str] = None
    message_id: Optional[int] = None
    session_id: Optional[str] = None



async def _turn_trace_id(conn, message_id: int) -> str:
    """MLflow trace id of the turn that produced assistant message ``message_id`` ('' when unknown)."""
    try:
        return await conn.fetchval('SELECT mlflow_trace_id FROM chat_turns WHERE assistant_message_id = $1 '
                                   'ORDER BY id DESC LIMIT 1', message_id) or ''
    except Exception as exc:  # noqa: BLE001 — the vote is saved either way
        logger.warning('chat feedback: MLflow trace id lookup failed: %s', exc)
        return ''


@router.post('/chat/feedback', dependencies=[Depends(require_chat)])
async def chat_feedback(body: ChatFeedbackRequest, request: Request):
    if body.vote not in ('up', 'down'):
        return JSONResponse({'error': 'vote must be "up" or "down"'}, status_code=422)

    pool = get_pool()
    if not pool:
        return JSONResponse(
            {'error': 'Feedback unavailable (database not configured)', 'available': False},
            status_code=503,
        )

    identity = await _identity_or_anonymous(request)
    user_id = identity['user_id']
    workspace_id = identity.get('workspace_id')
    workspace_url = get_workspace_url()

    try:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                '''
                INSERT INTO chat_feedbacks (message_id, session_id, user_id, workspace_id, workspace_url, vote, comment)
                VALUES ($1, $2, $3, $4, $5, $6, $7)
                RETURNING id, vote
                ''',
                body.message_id,
                body.session_id,
                user_id,
                workspace_id,
                workspace_url,
                body.vote,
                body.comment or None,
            )
            mlflow_trace_id = await _turn_trace_id(conn, body.message_id)
        if mlflow_trace_id:
            asyncio.get_running_loop().create_task(asyncio.to_thread(
                mlflow_turns.log_vote, mlflow_trace_id, body.vote, body.comment or '', user_id))
        return {'success': True, 'id': row['id'], 'vote': row['vote']}
    except Exception as exc:
        logger.error('Chat feedback save failed: %s', exc)
        return JSONResponse({'error': str(exc)}, status_code=500)


@router.get('/chat/sessions', dependencies=[Depends(require_chat)])
async def list_sessions(request: Request):
    pool = get_pool()
    if not pool:
        return {'sessions': [], 'available': False}

    identity = await _identity_or_anonymous(request)
    user_id = identity['user_id']

    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                '''
                SELECT s.id, s.name, s.created_at, s.updated_at,
                       COUNT(m.id) AS message_count
                FROM chat_sessions s
                LEFT JOIN chat_messages m ON m.session_id = s.id AND m.deleted = FALSE AND m.status = 'ok'
                WHERE s.user_id = $1 AND s.deleted = FALSE
                GROUP BY s.id, s.name, s.created_at, s.updated_at
                ORDER BY s.updated_at DESC
                LIMIT 50
                ''',
                user_id,
            )
        return {
            'sessions': [
                {
                    'id': r['id'],
                    'name': r['name'],
                    'created_at': r['created_at'].isoformat(),
                    'updated_at': r['updated_at'].isoformat(),
                    'message_count': int(r['message_count']),
                }
                for r in rows
            ],
            'available': True,
        }
    except Exception as exc:
        logger.error('List sessions failed: %s', exc)
        return {'sessions': [], 'available': False}


@router.get('/chat/sessions/{session_id}', dependencies=[Depends(require_chat)])
async def get_session(session_id: str, request: Request):
    pool = get_pool()
    if not pool:
        return JSONResponse({'error': 'Database not configured'}, status_code=503)

    identity = await _identity_or_anonymous(request)
    user_id = identity['user_id']

    try:
        async with pool.acquire() as conn:
            # Scoped to the caller's own sessions; shared conversations are served through the share_token below.
            session = await conn.fetchrow(
                'SELECT id, name, created_at FROM chat_sessions WHERE id = $1 AND user_id = $2',
                session_id, user_id,
            )
            if not session:
                return JSONResponse({'error': 'Session not found'}, status_code=404)
            msgs = await conn.fetch(
                '''
                SELECT id, role, content, created_at, sources_json
                FROM chat_messages
                WHERE session_id = $1 AND deleted = FALSE AND status = 'ok'
                ORDER BY created_at ASC
                ''',
                session_id,
            )

        return {
            'id': session['id'],
            'name': session['name'],
            'created_at': session['created_at'].isoformat(),
            'messages': [
                {
                    'id': m['id'],
                    'role': m['role'],
                    'content': m['content'],
                    'created_at': m['created_at'].isoformat(),
                    'sources': _stored_sources(m['sources_json']),
                }
                for m in msgs
            ],
        }
    except Exception as exc:
        logger.error('Get session failed: %s', exc)
        return JSONResponse({'error': str(exc)}, status_code=500)


@router.delete('/chat/sessions/{session_id}', dependencies=[Depends(require_chat)])
async def delete_session(session_id: str, request: Request):
    pool = get_pool()
    if not pool:
        return JSONResponse({'error': 'Database not configured'}, status_code=503)

    identity = await _identity_or_anonymous(request)
    user_id = identity['user_id']

    try:
        async with pool.acquire() as conn:
            # Scoped to the caller's own sessions: a shared session_id must never let anyone else delete it.
            owned = await conn.fetchval(
                'SELECT 1 FROM chat_sessions WHERE id = $1 AND user_id = $2',
                session_id, user_id,
            )
            if not owned:
                return JSONResponse({'error': 'Session not found'}, status_code=404)
            # Soft-delete: flag the session and its messages so a trace is kept for audit.
            await conn.execute(
                'UPDATE chat_messages SET deleted = TRUE, deleted_at = NOW()'
                ' WHERE session_id = $1 AND deleted = FALSE',
                session_id,
            )
            await conn.execute(
                'UPDATE chat_sessions SET deleted = TRUE, deleted_at = NOW()'
                ' WHERE id = $1 AND deleted = FALSE',
                session_id,
            )
        return {'success': True}
    except Exception as exc:
        logger.error('Delete session failed: %s', exc)
        return JSONResponse({'error': str(exc)}, status_code=500)


@router.post('/chat/sessions/{session_id}/share', dependencies=[Depends(require_chat)])
async def share_session(session_id: str, request: Request):
    """Grant read-only access to this conversation to anyone with the link.

    Idempotent: returns the existing token rather than rotating it, so a link already distributed keeps working.
    """
    pool = get_pool()
    if not pool:
        return JSONResponse({'error': 'Database not configured'}, status_code=503)

    identity = await _identity_or_anonymous(request)
    user_id = identity['user_id']

    try:
        async with pool.acquire() as conn:
            session = await conn.fetchrow(
                'SELECT share_token FROM chat_sessions WHERE id = $1 AND user_id = $2 AND deleted = FALSE',
                session_id, user_id,
            )
            if not session:
                return JSONResponse({'error': 'Session not found'}, status_code=404)
            token = session['share_token']
            if not token:
                token = secrets.token_urlsafe(20)
                await conn.execute(
                    'UPDATE chat_sessions SET share_token = $2 WHERE id = $1',
                    session_id, token,
                )
        return {'share_token': token}
    except Exception as exc:
        logger.error('Share session failed: %s', exc)
        return JSONResponse({'error': str(exc)}, status_code=500)


@router.get('/chat/shared/{share_token}', dependencies=[Depends(require_chat)])
async def get_shared_session(share_token: str, request: Request):
    """Read-only view of a shared conversation — any authenticated Qualibot
    user with the link can view it, not just its owner."""
    pool = get_pool()
    if not pool:
        return JSONResponse({'error': 'Database not configured'}, status_code=503)

    try:
        async with pool.acquire() as conn:
            session = await conn.fetchrow(
                'SELECT id, name, created_at FROM chat_sessions'
                ' WHERE share_token = $1 AND deleted = FALSE',
                share_token,
            )
            if not session:
                return JSONResponse({'error': 'Shared conversation not found'}, status_code=404)
            msgs = await conn.fetch(
                '''
                SELECT m.id, m.role, m.content, m.created_at, m.sources_json,
                       f.vote AS feedback_vote, f.comment AS feedback_comment
                FROM chat_messages m
                LEFT JOIN LATERAL (
                    SELECT vote, comment FROM chat_feedbacks
                    WHERE message_id = m.id ORDER BY created_at DESC LIMIT 1
                ) f ON TRUE
                WHERE m.session_id = $1 AND m.deleted = FALSE AND m.status = 'ok'
                ORDER BY m.created_at ASC
                ''',
                session['id'],
            )

        def _feedback_for(m) -> Optional[dict]:
            if not m['feedback_vote']:
                return None
            return {'vote': m['feedback_vote'], 'comment': m['feedback_comment']}

        return {
            'id': session['id'],
            'name': session['name'],
            'created_at': session['created_at'].isoformat(),
            'messages': [
                {
                    'id': m['id'],
                    'role': m['role'],
                    'content': m['content'],
                    'created_at': m['created_at'].isoformat(),
                    'sources': _stored_sources(m['sources_json']),
                    'feedback': _feedback_for(m),
                }
                for m in msgs
            ],
        }
    except Exception as exc:
        logger.error('Get shared session failed: %s', exc)
        return JSONResponse({'error': str(exc)}, status_code=500)


@router.post('/chat/shared/{share_token}/duplicate', dependencies=[Depends(require_chat)])
async def duplicate_shared_session(share_token: str, request: Request):
    """Copy a shared conversation into a new session owned by the caller, so
    they can keep asking questions without ever writing into someone else's
    conversation."""
    pool = get_pool()
    if not pool:
        return JSONResponse({'error': 'Database not configured'}, status_code=503)

    identity = await _identity_or_anonymous(request)
    user_id = identity['user_id']
    workspace_id = identity.get('workspace_id')
    workspace_url = get_workspace_url()

    try:
        async with pool.acquire() as conn:
            session = await conn.fetchrow(
                'SELECT id, name FROM chat_sessions WHERE share_token = $1 AND deleted = FALSE',
                share_token,
            )
            if not session:
                return JSONResponse({'error': 'Shared conversation not found'}, status_code=404)
            msgs = await conn.fetch(
                '''
                SELECT role, content, sources_json
                FROM chat_messages
                WHERE session_id = $1 AND deleted = FALSE AND status = 'ok'
                ORDER BY created_at ASC
                ''',
                session['id'],
            )

            new_session_id = str(uuid.uuid4())
            await upsert_user(
                conn, user_id=user_id, workspace_id=workspace_id,
                email=identity.get('email'), workspace_url=workspace_url or None,
            )
            await _upsert_session(conn, new_session_id, user_id, workspace_id, workspace_url, session['name'])
            for m in msgs:
                await conn.execute(
                    '''
                    INSERT INTO chat_messages
                        (session_id, user_id, workspace_id, workspace_url, role, content, sources_json, status)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, 'ok')
                    ''',
                    new_session_id, user_id, workspace_id, workspace_url,
                    m['role'], m['content'], m['sources_json'],
                )
        return {'session_id': new_session_id}
    except Exception as exc:
        logger.error('Duplicate shared session failed: %s', exc)
        return JSONResponse({'error': str(exc)}, status_code=500)
