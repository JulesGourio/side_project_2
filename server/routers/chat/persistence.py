"""Saving a chat turn: session, user and assistant messages with their source chips."""

import json
import logging
from typing import List, Tuple

import asyncpg

from server.services.chat.turn_log import TurnLog
from server.services.infra.lakebase import upsert_user
from server.routers.chat.helpers import _detect_division, _strip_division

logger = logging.getLogger(__name__)


async def _upsert_session(conn: asyncpg.Connection, session_id: str, user_id: str, workspace_id: str | None,
                          workspace_url: str, name: str, touch: bool = True) -> bool:
    """Create the chat session row, or bump its ``updated_at`` when it exists and belongs to ``user_id``.

    A session id that already belongs to someone else is left untouched: the caller cannot write into it. The share
    token is created only when the owner shares the conversation.

    :param conn: an acquired Lakebase connection.
    :param session_id: id of the conversation (the browser creates it).
    :param user_id: owner of the conversation.
    :param workspace_id: workspace of the owner, None when unknown.
    :param workspace_url: workspace URL of the owner.
    :param name: thread title, used only when the row is created.
    :param touch: False keeps the thread's date for a hidden turn (error, aborted): the sidebar sorts by updated_at.
    :return: True when the session is the caller's (created or updated), False when another user owns that id.
    """
    status = await conn.execute(
        '''
        INSERT INTO chat_sessions (id, user_id, workspace_id, workspace_url, name)
        VALUES ($1, $2, $3, $4, $5)
        ON CONFLICT (id) DO UPDATE SET updated_at = CASE WHEN $6 THEN NOW() ELSE chat_sessions.updated_at END
        WHERE chat_sessions.user_id = EXCLUDED.user_id
        ''',
        session_id, user_id, workspace_id, workspace_url, name, touch,
    )
    return status.endswith(' 1')


async def session_of_another_user(pool: asyncpg.Pool | None, session_id: str, user_id: str) -> bool:
    """Whether the conversation id already belongs to another user.

    :param pool: the Lakebase pool, None when Lakebase is unavailable.
    :param session_id: id of the conversation sent by the browser.
    :param user_id: the caller.
    :return: True when a session of that id exists with another owner; False when it is the caller's, does not exist,
        or cannot be read (the save then checks the owner again and writes nothing into another user's session).
    """
    if pool is None:
        return False
    try:
        async with pool.acquire() as conn:
            owner = await conn.fetchval('SELECT user_id FROM chat_sessions WHERE id = $1', session_id)
    except Exception as exc:
        logger.warning('chat: owner of session %s not read: %s', session_id, exc)
        return False
    return owner is not None and owner != user_id


async def _save_turn(
    pool: asyncpg.Pool,
    session_id: str,
    user_id: str,
    workspace_id: str | None,
    workspace_url: str,
    user_content: str,
    assistant_content: str,
    sources: List[dict] | None = None,
    trace_id: str = '',
    email: str | None = None,
    endpoint_name: str = '',
    status: str = 'ok',
    error_msg: str = '',
    division: str = 'ALL',
    question_lang: str = '',
    log: TurnLog | None = None,
) -> Tuple[int | None, int | None]:
    """Persist the user and assistant messages with their sources.

    A failed or aborted turn is saved too, so the question and the reason it broke stay traceable. Only 'ok' turns
    are shown in the history. The retrieval itself is saved by store_chat_turn (chat_turns), under the same trace_id.

    :param pool: the Lakebase connection pool.
    :param session_id: id of the conversation.
    :param user_id: author of the question.
    :param workspace_id: workspace of the author, None when unknown.
    :param workspace_url: workspace URL of the author.
    :param user_content: the question as sent, ``[Division: …]`` prefix included.
    :param assistant_content: the answer (empty for a failed turn).
    :param sources: the source chips shown under the answer.
    :param trace_id: ``chat-…`` id shared with ``chat_turns``.
    :param email: e-mail of the author, used to register the user.
    :param endpoint_name: model endpoint that wrote the answer.
    :param status: ``ok``, ``error`` or ``aborted``.
    :param error_msg: reason of a failed turn (cut to 2000 characters).
    :param division: ``AS``, ``IS`` or ``ALL``; ``ALL`` falls back to the prefix of the question.
    :param question_lang: ISO code of the question's language, when detected.
    :param log: the turn log, which receives a ``persist_failed`` issue if the save fails.
    :return: (user message id, assistant message id), or (None, None) when the save failed.
    """
    # Thread title from the user's question, without the [Division: …] prefix.
    clean_content = _strip_division(user_content)
    name = clean_content[:60] + ('…' if len(clean_content) > 60 else '')
    # Division comes from the client selector; fall back to the [Division: …] prefix of the question.
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
            # Register the identity in `users` so chat-only users are not orphaned. A failure here must not lose the
            # conversation: the messages are saved either way.
            try:
                await upsert_user(
                    conn,
                    user_id=user_id,
                    workspace_id=workspace_id,
                    email=email,
                    workspace_url=workspace_url or None,
                )
            except Exception as exc:
                logger.warning('Chat save: user not registered: %s', exc)
                if log is not None:
                    log.issue('user_upsert_failed', 'persist', f'users not updated: {exc}', error_type=type(exc).__name__)
            if not await _upsert_session(conn, session_id, user_id, workspace_id, workspace_url, name,
                                         touch=(status or 'ok') == 'ok'):
                logger.warning('Chat save refused: session %s belongs to another user', session_id)
                if log is not None:
                    log.issue('session_not_owned', 'persist', 'the session id belongs to another user; nothing saved')
                return None, None
            # One transaction: a question is never saved without its answer, or the reverse.
            async with conn.transaction():
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
