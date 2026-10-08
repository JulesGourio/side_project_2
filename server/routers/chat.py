"""Chat router — conversational interface answered by the Vector Search engine (services/chat_vsi.py)."""

import asyncio
import json
import logging
import os
import re
import secrets
import traceback
import uuid
from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from ..services.chat_vsi import normalize_division, stream_chat_vsi
from ..services.doc_catalog import augment_sources
from ..services.lakebase import get_pool, store_error, upsert_user
from ..services.translation_bridge import (
    ENABLED as TRANSLATE_BRIDGE_ENABLED,
    answer_language as answer_language_for,
    translate_answer_back,
    translate_question_to_en,
)
from ..services.user import get_capabilities, get_user_identity, get_workspace_url, require_chat

logger = logging.getLogger(__name__)
router = APIRouter()

CHAT_ENABLED = os.getenv('CHAT_ENABLED', 'true').lower() == 'true'
# Cap how many past messages are replayed to the endpoint each turn, so the
# prompt stays bounded as a conversation grows (this is a RAG assistant — each
# answer is grounded in retrieval, so long conversational memory matters less).
# The latest user question is always the last message and is never dropped.
CHAT_MAX_HISTORY = int(os.getenv('CHAT_MAX_HISTORY', '10'))

# Matches the division-scoping directive the client prepends to a question
# (see client/src/components/chat/division.tsx). Stripped before deriving the
# session name so the directive never leaks into the thread title.
_DIVISION_PREFIX_RE = re.compile(r'^\[Division: (?:AS|IS)\][\s\S]*?\n\n')
_DIVISION_DETECT_RE = re.compile(r'^\[Division: (AS|IS)\]')


def _strip_division(text: str) -> str:
    return _DIVISION_PREFIX_RE.sub('', text)


def _detect_division(text: str) -> str:
    """Return the division scope the client tagged the question with: 'AS', 'IS'
    or 'ALL' (no directive prefix)."""
    m = _DIVISION_DETECT_RE.match(text or '')
    return m.group(1) if m else 'ALL'


# Inline-citation markers are written into the answer text as ``⟦n⟧`` (rare
# math brackets, U+27E6/U+27E7 — practically never present in source docs) and
# rendered by the client as a superscript link to source #n. Storing them in
# the content means a reloaded conversation shows the same inline citations.
_CITE_MARKER_TMPL = '⟦{n}⟧'

# Regex to protect links to be splitted by databricks agent citations.
_PROTECTED_SPAN_RE = re.compile(r'\[[^\]]+\]\([^)]+\)|https?://[^\s)\]\'">]+')

def _apply_citation_markers(text: str, citations: List[dict]) -> str:
    """Insert ``⟦n⟧`` markers into ``text`` at each citation's character offset.
    
    If the provided offset falls inside a Markdown link or a raw URL, the marker
    is pushed to the end of that link so it doesn't break the hyper-reference.
    """
    if not text or not citations:
        return text
    
    n_chars = len(text)
    
    # Link spans of the text: a citation position falling inside one is moved to its end.
    protected_spans = [(m.start(), m.end()) for m in _PROTECTED_SPAN_RE.finditer(text)]
    
    seen: set = set()
    valid: list = []
    
    for c in citations:
        try:
            n = int(c['n'])
            pos = int(c['pos'])
        except (KeyError, TypeError, ValueError):
            continue
            
        pos = max(0, min(pos, n_chars))
        
        for start, end in protected_spans:
            if start < pos < end:
                pos = end
                break  # On sort de la boucle, la position est corrigée
                
        if (n, pos) in seen:
            continue
        seen.add((n, pos))
        valid.append((pos, n))
        
    # Insert right-to-left; for markers at the same offset keep ascending order
    # of n once rendered (so a fact cited by 2 and 3 reads "[2][3]").
    valid.sort(key=lambda t: (t[0], t[1]))
    
    out = text
    for pos, n in reversed(valid):
        out = out[:pos] + _CITE_MARKER_TMPL.format(n=n) + out[pos:]
        
    return out


def _number_sources(sources: List[dict], citations: List[dict]) -> List[dict]:
    """Tag each source that is referenced by an inline citation with its 1-based
    citation number (``n``); leave prose-only sources (re-surfaced from the
    catalog, never cited inline) without a number.

    The streaming layer numbers citations as the position of their source in the
    annotation order (1-based), and those annotation sources are always the
    first entries of ``sources`` — so a source at index ``i`` is cited inline iff
    ``i + 1`` is among the citation numbers. The chip then shows that same number
    as the inline ``[n]`` marker; catalog-only sources show just their REF.
    """
    cited = {c.get('n') for c in (citations or []) if isinstance(c.get('n'), int)}
    for i, s in enumerate(sources or []):
        if (i + 1) in cited:
            s['n'] = i + 1
        else:
            s.pop('n', None)
    return sources


def _trim_history(messages: List[dict]) -> List[dict]:
    """Keep only the most recent CHAT_MAX_HISTORY messages for the endpoint."""
    if CHAT_MAX_HISTORY <= 0 or len(messages) <= CHAT_MAX_HISTORY:
        return messages
    trimmed = messages[-CHAT_MAX_HISTORY:]
    # Don't open the window on an assistant turn — keep role coherence.
    while len(trimmed) > 1 and trimmed[0].get('role') != 'user':
        trimmed = trimmed[1:]
    return trimmed


def _with_today_date(messages: List[dict]) -> List[dict]:
    """Return a COPY of messages with today's date prepended to the last user
    turn. The answer model has no way to know the current
    date on its own (it can't call a clock), so date-relative questions
    ("les documents publiés cette année", "diffusés depuis 2024"...) fail
    silently otherwise. Injected into the user turn itself (not a 'system'
    role message): the engine puts its own system message first.

    Kept language-neutral (plain ISO date, no French/English sentence) —a
    full "Nous sommes le lundi ..." sentence here was found (2026-07-07) to
    bias the model toward answering in French even when the user asked in
    English, since it's the first text the model reads in the turn.

    Returns a new list — callers must keep using the ORIGINAL `messages` for
    logging/persistence, so stored chat history doesn't carry this prefix.
    """
    if not messages:
        return messages
    date_str = datetime.now().strftime('%Y-%m-%d')
    out = [dict(m) for m in messages]
    for i in range(len(out) - 1, -1, -1):
        if out[i].get('role') == 'user':
            out[i]['content'] = f"[Date: {date_str}]\n\n{out[i]['content']}"
            break
    return out


def _get_chat_credentials(request: Request) -> tuple[str, str]:
    """Get Databricks host + token for calling the chat serving endpoint."""
    host = os.environ.get('DATABRICKS_HOST', '').rstrip('/')
    if host and not host.startswith('http'):
        host = f'https://{host}'
    token = os.environ.get('DATABRICKS_TOKEN', '')
    if not host or not token:
        try:
            from databricks.sdk.core import Config
            cfg = Config()
            if not host:
                host = (cfg.host or '').rstrip('/')
            if not token:
                auth_value = cfg.authenticate().get('Authorization', '')
                if auth_value.startswith('Bearer '):
                    token = auth_value[len('Bearer '):]
        except Exception as exc:
            logger.debug('SDK auth unavailable: %s', exc)
    if not token:
        token = request.headers.get('x-forwarded-access-token', '')
    return host, token


# --- Pydantic models ---


class ChatFeedbackRequest(BaseModel):
    vote: str
    comment: Optional[str] = None
    message_id: Optional[int] = None
    session_id: Optional[str] = None


# --- DB helpers ---


async def _upsert_session(conn, session_id: str, user_id: str, workspace_id: Optional[str], workspace_url: str, name: str) -> None:
    # share_token is generated up front so developers can browse any conversation in the database; it stays unused
    # until the owner clicks "Share".
    await conn.execute(
        '''
        INSERT INTO chat_sessions (id, user_id, workspace_id, workspace_url, name, share_token)
        VALUES ($1, $2, $3, $4, $5, $6)
        ON CONFLICT (id) DO UPDATE SET updated_at = NOW()
        ''',
        session_id, user_id, workspace_id, workspace_url, name, secrets.token_urlsafe(20),
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
    tool_name: str = '',
    tool_query: str = '',
    tool_result: str = '',
    reasoning_steps: Optional[list] = None,
    email: Optional[str] = None,
    endpoint_name: str = '',
    status: str = 'ok',
    error_msg: str = '',
    division: str = 'ALL',
    question_lang: str = '',
) -> Optional[int]:
    """Persist user + assistant messages and retrieval sources; return assistant message DB id.

    A turn is saved even when it failed (``status='error'``) so the user's
    question — and the reason it broke — is always traceable in the database.
    """
    # Thread title from the user's question; _strip_division only matters for legacy turns carrying a [Division: …]
    # prefix.
    clean_content = _strip_division(user_content)
    name = clean_content[:60] + ('…' if len(clean_content) > 60 else '')
    # Division comes from the client selector; fall back to a legacy prefix.
    division = (division or 'ALL').upper()
    if division == 'ALL':
        division = _detect_division(user_content)
    reasoning_str = '\n---\n'.join(reasoning_steps) if reasoning_steps else None
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
            await _upsert_session(conn, session_id, user_id, workspace_id, workspace_url, name)
            await conn.execute(
                '''
                INSERT INTO chat_messages (session_id, user_id, workspace_id, workspace_url, role, content, status, division, question_lang)
                VALUES ($1, $2, $3, $4, 'user', $5, $6, $7, $8)
                ''',
                session_id, user_id, workspace_id, workspace_url, user_content, status or 'ok', division, question_lang or None,
            )
            row = await conn.fetchrow(
                '''
                INSERT INTO chat_messages
                    (session_id, user_id, workspace_id, workspace_url, role, content,
                     trace_id, tool_name, tool_query, tool_result, reasoning_steps,
                     endpoint_name, sources_json, status, error_msg, division, question_lang)
                VALUES ($1, $2, $3, $4, 'assistant', $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16)
                RETURNING id
                ''',
                session_id, user_id, workspace_id, workspace_url, assistant_content,
                trace_id or None, tool_name or None, tool_query or None,
                tool_result or None, reasoning_str, endpoint_name or None, sources_json,
                status or 'ok', (error_msg or None) and error_msg[:2000], division, question_lang or None,
            )
            msg_id = row['id'] if row else None

        return msg_id
    except Exception as exc:
        logger.warning('Chat DB save failed: %s', exc)
        return None


# --- Endpoints ---


@router.websocket('/chat/ws')
async def chat_ws(websocket: WebSocket):
    """One chat turn over a WebSocket (avoids Databricks Apps proxy buffering), answered by
    the Vector Search engine (``stream_chat_vsi``); then citation markers, catalog sources,
    translation back and persistence."""
    await websocket.accept()
    route = '/api/chat/ws'

    if not CHAT_ENABLED:
        await websocket.send_json({'type': 'error', 'error': 'Chat is disabled'})
        await websocket.close()
        return

    host, token = _get_chat_credentials(websocket)  # type: ignore[arg-type]
    if not host or not token:
        await websocket.send_json({'type': 'error', 'error': 'Missing Databricks credentials'})
        await websocket.close()
        return

    # Chat access control — WebSockets can't return an HTTP 403, so check here.
    try:
        caps = await get_capabilities(websocket)  # type: ignore[arg-type]
    except Exception:
        caps = {'can_chat': True}
    if not caps.get('can_chat', True):
        await websocket.send_json({'type': 'error', 'error': 'Chat access not granted'})
        await websocket.close()
        return

    try:
        data = await websocket.receive_json()
    except Exception:
        await websocket.close()
        return

    messages = _trim_history([{'role': m['role'], 'content': m['content']} for m in data.get('messages', [])])
    session_id = data.get('session_id') or str(uuid.uuid4())
    division = (data.get('division') or 'ALL').upper()
    # Recorded as endpoint_name (older turns carry ka-… names).
    endpoint = f'vsi-{normalize_division(division).lower()}'

    try:
        identity = await get_user_identity(websocket)  # type: ignore[arg-type]
    except Exception:
        identity = {'user_id': '', 'workspace_id': None, 'email': None}
    user_id = identity['user_id']
    workspace_id = identity.get('workspace_id')
    workspace_url = get_workspace_url()

    user_content = next((m['content'] for m in reversed(messages) if m['role'] == 'user'), '')
    logger.info('chat turn [ws]: division=%s endpoint=%s session=%s user=%s msgs=%d q=%r',
                division, endpoint, session_id, identity.get('email') or user_id,
                len(messages), user_content[:160])

    accumulated: list[str] = []
    collected_sources: list = []
    collected_citations: list = []
    collected_meta: dict = {}
    error_occurred = False
    error_text = ''
    translate_ctx = None
    messages_for_engine = messages
    if TRANSLATE_BRIDGE_ENABLED and user_content:
        en_question, translate_ctx = await translate_question_to_en(user_content, host, token)
        if translate_ctx.needs_translation:
            messages_for_engine = [dict(m) for m in messages]
            for i in range(len(messages_for_engine) - 1, -1, -1):
                if messages_for_engine[i]['role'] == 'user':
                    messages_for_engine[i]['content'] = en_question
                    break
            logger.info('chat translate-bridge [ws]: lang=%s en_q=%r', translate_ctx.lang_code, en_question[:160])

    # The language the answer must be in, named in the prompt's language reminder: English
    # when the bridge translated the question (it translates the answer back), else the
    # detected French/English, else a local guess.
    answer_language = answer_language_for(translate_ctx, user_content) if user_content else None
    answer_stream = stream_chat_vsi(host, token, division, _with_today_date(messages_for_engine),
                                    answer_language=answer_language)

    async def _persist(status: str, content: str) -> Optional[int]:
        """Persist the turn (ok or error). The question is saved even on
        failure, so it stays traceable. Best-effort."""
        pool = get_pool()
        if not (pool and user_content):
            return None
        return await _save_turn(
            pool, session_id, user_id, workspace_id, workspace_url,
            user_content, content,
            collected_sources,
            collected_meta.get('trace_id', ''),
            collected_meta.get('tool_name', ''),
            collected_meta.get('tool_query', ''),
            collected_meta.get('tool_result', ''),
            collected_meta.get('reasoning_steps', []),
            email=identity.get('email'),
            endpoint_name=endpoint,
            status=status,
            error_msg=error_text,
            division=division,
            question_lang=translate_ctx.lang_code if translate_ctx else '',
        )

    try:
        async for chunk in answer_stream:
            if chunk.startswith('data: [DONE]'):
                if not error_occurred and user_content and accumulated:
                    final_content = _apply_citation_markers(''.join(accumulated), collected_citations)
                    # Re-surface documents the answer names in prose but the
                    # endpoint never annotated, so they become clickable chips.
                    collected_sources = augment_sources(''.join(accumulated), collected_sources)
                    # Number only the inline-cited sources; prose-only chips
                    # (catalog-resurfaced) stay numberless.
                    _number_sources(collected_sources, collected_citations)
                    if translate_ctx:
                        final_content = await translate_answer_back(final_content, translate_ctx, host, token)
                    msg_id = await _persist('ok', final_content)
                    logger.info('chat turn done [ws]: division=%s endpoint=%s sources=%d citations=%d',
                                division, endpoint, len(collected_sources), len(collected_citations))
                    await websocket.send_json({
                        'type': 'done',
                        'session_id': session_id,
                        'message_id': msg_id,
                        'content': final_content,
                        'sources': collected_sources,
                    })
                elif not error_occurred:
                    # The engine finished without any text and without an error: tell the browser the turn failed, or
                    # it stays on "Thinking".
                    error_text = 'No answer was produced. Please try again.'
                    logger.warning('chat turn empty [ws]: division=%s endpoint=%s',
                                   division, endpoint)
                    asyncio.create_task(store_error(
                        endpoint=route,
                        error_type='EmptyAnswer',
                        error_msg=f'{endpoint}: stream ended with no text',
                        user_id=user_id,
                        workspace_id=workspace_id or '',
                    ))
                    await _persist('error', '')
                    await websocket.send_json({'type': 'error', 'error': error_text})
                break

            if not chunk.startswith('data: '):
                # Keepalive comment lines (": keepalive") from the engine: forward a
                # no-op ping so a proxy idle timeout (~30 s) never closes the socket
                # while the model is still thinking.
                if chunk.startswith(': '):
                    try:
                        await websocket.send_json({'type': 'ping'})
                    except Exception:
                        pass
                continue

            raw = chunk[6:].strip()
            try:
                parsed = json.loads(raw)
                t = parsed.get('type', '')
                if t == 'response.output_text.delta':
                    delta = parsed.get('delta', '')
                    accumulated.append(delta)
                    if translate_ctx and translate_ctx.needs_translation:
                        # Never forward raw English to the client — the
                        # client shows "Thinking" until the full answer is
                        # translated in one pass and sent in the 'done' event.
                        pass
                    else:
                        await websocket.send_json({'type': 'delta', 'delta': delta})
                elif t == 'sources':
                    srcs = parsed.get('sources')
                    cits = parsed.get('citations')
                    collected_sources = srcs if isinstance(srcs, list) else []
                    collected_citations = cits if isinstance(cits, list) else []
                elif t == 'metadata':
                    collected_meta = parsed
                elif t == 'error':
                    error_occurred = True
                    error_text = parsed.get('error', '')
                    asyncio.create_task(store_error(
                        endpoint=route,
                        error_type=parsed.get('error_type', 'ChatLLMError'),
                        error_msg=error_text,
                        user_id=user_id,
                        workspace_id=workspace_id or '',
                    ))
                    await _persist('error', ''.join(accumulated))
                    await websocket.send_json({'type': 'error', 'error': parsed.get('error', 'Unknown error')})
                    break
            except json.JSONDecodeError:
                pass

    except WebSocketDisconnect:
        logger.info('Chat WebSocket disconnected during stream')
    except Exception as exc:
        logger.error('Chat WebSocket error: %s', exc, exc_info=True)
        error_occurred = True
        error_text = str(exc)
        asyncio.create_task(store_error(
            endpoint=route,
            error_type=type(exc).__name__,
            error_msg=error_text,
            user_id=user_id,
            workspace_id=workspace_id or '',
            stack_trace=traceback.format_exc(),
        ))
        await _persist('error', ''.join(accumulated))
        try:
            await websocket.send_json({'type': 'error', 'error': str(exc)})
        except Exception:
            pass
    finally:
        try:
            await websocket.close()
        except Exception:
            pass


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
        return {'success': True, 'id': row['id'], 'vote': row['vote']}
    except Exception as exc:
        logger.error('Chat feedback save failed: %s', exc)
        return JSONResponse({'error': str(exc)}, status_code=500)


@router.get('/chat/sessions', dependencies=[Depends(require_chat)])
async def list_sessions(request: Request):
    pool = get_pool()
    if not pool:
        return {'sessions': [], 'available': False}

    try:
        identity = await get_user_identity(request)
    except Exception:
        identity = {'user_id': '', 'workspace_id': None, 'email': None}
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

    try:
        identity = await get_user_identity(request)
    except Exception:
        identity = {'user_id': '', 'workspace_id': None, 'email': None}
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

        def _sources_for(m) -> list:
            raw = m['sources_json']
            if not raw:
                return []
            try:
                return [
                    {'title': s.get('title'), 'url': s.get('url'), 'n': s.get('n')}
                    for s in json.loads(raw)
                ]
            except (json.JSONDecodeError, AttributeError, TypeError):
                return []

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
                    'sources': _sources_for(m),
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

    try:
        identity = await get_user_identity(request)
    except Exception:
        identity = {'user_id': '', 'workspace_id': None, 'email': None}
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

    Idempotent: returns the existing token if the session was already shared,
    rather than rotating it — a previously distributed link keeps working.
    """
    pool = get_pool()
    if not pool:
        return JSONResponse({'error': 'Database not configured'}, status_code=503)

    try:
        identity = await get_user_identity(request)
    except Exception:
        identity = {'user_id': '', 'workspace_id': None, 'email': None}
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

        def _sources_for(m) -> list:
            raw = m['sources_json']
            if not raw:
                return []
            try:
                return [
                    {'title': s.get('title'), 'url': s.get('url'), 'n': s.get('n')}
                    for s in json.loads(raw)
                ]
            except (json.JSONDecodeError, AttributeError, TypeError):
                return []

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
                    'sources': _sources_for(m),
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

    try:
        identity = await get_user_identity(request)
    except Exception:
        identity = {'user_id': '', 'workspace_id': None, 'email': None}
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
