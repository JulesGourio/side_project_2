"""Saving a chat turn: session, user and assistant messages with their source chips."""

import json
import logging
import secrets
from typing import Optional, Tuple

from ...services.chat.turn_log import TurnLog
from ...services.infra.lakebase import upsert_user
from .helpers import _detect_division, _strip_division

logger = logging.getLogger(__name__)


async def _upsert_session(conn, session_id: str, user_id: str, workspace_id: Optional[str], workspace_url: str, name: str,
                          touch: bool = True) -> None:
    # share_token is generated up front so developers can browse any conversation in the database; it stays unused
    # until the owner clicks "Share". touch=False (failed or aborted turn, hidden from the thread) keeps the
    # thread's date: the sidebar sorts and dates threads by updated_at.
    await conn.execute(
        '''
        INSERT INTO chat_sessions (id, user_id, workspace_id, workspace_url, name, share_token)
        VALUES ($1, $2, $3, $4, $5, $6)
        ON CONFLICT (id) DO UPDATE SET updated_at = CASE WHEN $7 THEN NOW() ELSE chat_sessions.updated_at END
        ''',
        session_id, user_id, workspace_id, workspace_url, name, secrets.token_urlsafe(20), touch,
    )


async def _save_turn(
    pool,
    session_id: str,
    user_id: str,
    workspace_id: Optional[str],
    workspace_url: str,
    user_content: str,
    assistant_content: str,
    sources: Optional[list] = None,
    trace_id: str = '',
    email: Optional[str] = None,
    endpoint_name: str = '',
    status: str = 'ok',
    error_msg: str = '',
    division: str = 'ALL',
    question_lang: str = '',
    log: Optional[TurnLog] = None,
) -> Tuple[Optional[int], Optional[int]]:
    """Persist user + assistant messages and retrieval sources; return their ids
    (user message, assistant message), (None, None) when the save failed.

    A turn is saved even when it failed (``status='error'``) or the browser left
    (``'aborted'``) so the user's question — and the reason it broke — is always
    traceable in the database. Only ``'ok'`` turns are shown in the history.
    The retrieval itself (queries, passages, models, durations) is saved by
    ``store_chat_turn`` in chat_turns, under the same ``trace_id``.
    """
    # Thread title from the user's question; _strip_division only matters for legacy turns carrying a [Division: …]
    # prefix.
    clean_content = _strip_division(user_content)
    name = clean_content[:60] + ('…' if len(clean_content) > 60 else '')
    # Division comes from the client selector; fall back to a legacy prefix.
    division = (division or 'ALL').upper()
    if division == 'ALL':
        division = _detect_division(user_content)
    # Consulted documents are stored as one compact JSON blob on the assistant message (rank, title, url), de-
    # duplicated by title in order.
    sources_payload: list[dict] = []
    seen_titles: set = set()
    for rank, src in enumerate(sources or []):
        title = (src.get('title') or '').strip()
        if not title or title in seen_titles:
            continue
        seen_titles.add(title)
        sources_payload.append({
            'rank': len(sources_payload),
            'title': title,
            'url': src.get('url') or None,
            # 1-based inline-citation number; absent for prose-only sources.
            'n': src.get('n'),
            # Catalog title, revision and date at answer time (the revision may change later).
            **{k: src[k] for k in ('doc_title', 'revision', 'doc_date') if src.get(k)},
        })
    sources_json = json.dumps(sources_payload, ensure_ascii=False) if sources_payload else None

    try:
        async with pool.acquire() as conn:
            # Register the identity in `users` so chat-only users are not orphaned.
            await upsert_user(
                conn,
                user_id=user_id,
                workspace_id=workspace_id,
                email=email,
                workspace_url=workspace_url or None,
            )
            await _upsert_session(conn, session_id, user_id, workspace_id, workspace_url, name,
                                  touch=(status or 'ok') == 'ok')
            user_msg_id = await conn.fetchval(
                '''
                INSERT INTO chat_messages
                    (session_id, user_id, workspace_id, workspace_url, role, content, trace_id,
                     status, division, question_lang)
                VALUES ($1, $2, $3, $4, 'user', $5, $6, $7, $8, $9)
                RETURNING id
                ''',
                session_id, user_id, workspace_id, workspace_url, user_content, trace_id or None,
                status or 'ok', division, question_lang or None,
            )
            assistant_msg_id = await conn.fetchval(
                '''
                INSERT INTO chat_messages
                    (session_id, user_id, workspace_id, workspace_url, role, content, trace_id,
                     endpoint_name, sources_json, status, error_msg, division, question_lang)
                VALUES ($1, $2, $3, $4, 'assistant', $5, $6, $7, $8, $9, $10, $11, $12)
                RETURNING id
                ''',
                session_id, user_id, workspace_id, workspace_url, assistant_content, trace_id or None,
                endpoint_name or None, sources_json, status or 'ok',
                (error_msg or None) and error_msg[:2000], division, question_lang or None,
            )
        return user_msg_id, assistant_msg_id
    except Exception as exc:
        logger.warning('Chat DB save failed: %s', exc)
        if log is not None:
            log.issue('persist_failed', 'persist', f'chat_messages not saved: {exc}', error_type=type(exc).__name__)
        return None, None
