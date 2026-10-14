"""WebSocket route: one chat turn answered by the Vector Search engine, then persisted and logged."""

import json
import logging
import os
import traceback
import uuid
from typing import Optional, Tuple

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from ...services.chat import mlflow_turns
from ...services.chat.engine import normalize_division, stream_chat
from ...services.chat.llm import TIRED_MESSAGE
from ...services.chat.translation_bridge import (
    ENABLED as TRANSLATE_BRIDGE_ENABLED,
    answer_language as answer_language_for,
    translate_answer_back,
    translate_question_to_en,
)
from ...services.chat.turn_log import INFO, TurnLog
from ...services.infra.credentials import get_credentials
from ...services.infra.doc_catalog import augment_sources, with_document_info
from ...services.infra.lakebase import get_pool, store_chat_turn, store_error
from ...services.infra.user import get_capabilities, get_workspace_url
from .helpers import (
    _apply_citation_markers, _identity_or_anonymous, _number_sources,
    _trim_history, _with_today_date,
)
from .persistence import _save_turn

logger = logging.getLogger(__name__)
router = APIRouter()


CHAT_ENABLED = os.getenv('CHAT_ENABLED', 'true').lower() == 'true'


@router.websocket('/chat/ws')
async def chat_ws(websocket: WebSocket):
    """One chat turn over a WebSocket (avoids Databricks Apps proxy buffering), answered by
    the Vector Search engine (``stream_chat``); then citation markers, catalog sources,
    translation back and persistence. Every turn that reached the engine — answered, failed or
    left by the browser — is logged in chat_turns (+ chat_retrieved_chunks, errors) when the turn ends."""
    await websocket.accept()
    route = '/api/chat/ws'

    if not CHAT_ENABLED:
        await websocket.send_json({'type': 'error', 'error': 'Chat is disabled'})
        await websocket.close()
        return

    host, token = get_credentials(websocket)  # type: ignore[arg-type]
    if not host or not token:
        logger.error('chat [ws]: no Databricks host/token for the app')
        store_error(endpoint=route, error_type='MissingCredentials', stage='ws',
                    error_msg='no Databricks host or token for the chat engine', user_message=TIRED_MESSAGE)
        await websocket.send_json({'type': 'error', 'error': TIRED_MESSAGE})
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

    log = TurnLog()
    messages = _trim_history([{'role': m['role'], 'content': m['content']} for m in data.get('messages', [])])
    session_id = data.get('session_id') or str(uuid.uuid4())
    division = (data.get('division') or 'ALL').upper()
    endpoint = f'chat-{normalize_division(division).lower()}'

    identity = await _identity_or_anonymous(websocket)
    user_id = identity['user_id']
    workspace_id = identity.get('workspace_id')
    workspace_url = get_workspace_url()

    user_content = next((m['content'] for m in reversed(messages) if m['role'] == 'user'), '')
    logger.info('chat turn [ws]: division=%s endpoint=%s session=%s user=%s msgs=%d q=%r trace_id=%s',
                division, endpoint, session_id, identity.get('email') or user_id,
                len(messages), user_content[:160], log.trace_id)
    log.data.update({'session_id': session_id, 'user_id': user_id, 'workspace_id': workspace_id,
                     'division': normalize_division(division),
                     'question': user_content, 'history_messages': len(messages)})

    accumulated: list[str] = []
    collected_sources: list = []
    collected_citations: list = []
    error_text = ''
    translate_ctx = None
    saved: Tuple[Optional[int], Optional[int]] = (None, None)
    answer_stream = None
    outcome = 'error'                            # 'ok' once the answer is saved; 'aborted' if the browser left

    async def _send_tired() -> None:
        """The only failure message a user sees, once every retry is spent; the cause is in the turn log."""
        log.data['user_message'] = TIRED_MESSAGE
        await websocket.send_json({'type': 'error', 'error': TIRED_MESSAGE})

    async def _persist(status: str, content: str) -> Optional[int]:
        """Persist the turn's messages (ok, error or aborted) and return the
        assistant message id. The question is saved even on failure, so it
        stays traceable. Best-effort."""
        nonlocal saved
        pool = get_pool()
        if not (pool and user_content):
            return None
        with log.timed('persist'):
            saved = await _save_turn(
                pool, session_id, user_id, workspace_id, workspace_url,
                user_content, content,
                collected_sources,
                log.trace_id,
                email=identity.get('email'),
                endpoint_name=endpoint,
                status=status,
                error_msg=error_text,
                division=division,
                question_lang=translate_ctx.lang_code if translate_ctx else '',
                log=log,
            )
        return saved[1]

    try:
        messages_for_engine = messages
        if TRANSLATE_BRIDGE_ENABLED and user_content:
            with log.timed('translate_in'):
                en_question, translate_ctx = await translate_question_to_en(user_content, host, token)
            log.data['question_lang'] = translate_ctx.lang_code
            if translate_ctx.error:
                log.issue('translate_in_failed', 'translate_in', translate_ctx.error)
            if translate_ctx.needs_translation:
                log.data['question_en'] = en_question
                messages_for_engine = [dict(m) for m in messages]
                for i in range(len(messages_for_engine) - 1, -1, -1):
                    if messages_for_engine[i]['role'] == 'user':
                        messages_for_engine[i]['content'] = en_question
                        break
                logger.info('chat translate-bridge [ws]: lang=%s en_q=%r', translate_ctx.lang_code, en_question[:160])

        # Named in the language reminder: English when the bridge translated the question (it translates the answer back),
        # else the detected French/English, else a local guess.
        answer_language = answer_language_for(translate_ctx, user_content) if user_content else None
        answer_stream = stream_chat(host, token, division, _with_today_date(messages_for_engine),
                                        answer_language=answer_language, log=log)

        async for chunk in answer_stream:
            if chunk.startswith('data: [DONE]'):
                if user_content and accumulated:
                    final_content = _apply_citation_markers(''.join(accumulated), collected_citations)
                    # Re-surface documents the answer names in prose but the
                    # endpoint never annotated, so they become clickable chips.
                    collected_sources = augment_sources(''.join(accumulated), collected_sources)
                    # Number only the inline-cited sources; prose-only chips
                    # (catalog-resurfaced) stay numberless.
                    _number_sources(collected_sources, collected_citations)
                    with_document_info(collected_sources)
                    if translate_ctx:
                        with log.timed('translate_back'):
                            final_content = await translate_answer_back(final_content, translate_ctx, host, token)
                        log.data['answer_translated'] = translate_ctx.answer_translated
                        if translate_ctx.answer_error:
                            log.issue('translate_back_failed', 'translate_back', translate_ctx.answer_error)
                    log.data['sources_count'] = len(collected_sources)
                    log.answer = final_content
                    msg_id = await _persist('ok', final_content)
                    outcome = 'ok'
                    logger.info('chat turn done [ws]: division=%s endpoint=%s sources=%d citations=%d',
                                division, endpoint, len(collected_sources), len(collected_citations))
                    await websocket.send_json({
                        'type': 'done',
                        'session_id': session_id,
                        'message_id': msg_id,
                        'content': final_content,
                        'sources': collected_sources,
                    })
                else:
                    # The engine finished without any text and without an error: tell the browser the turn failed, or
                    # it stays on "Thinking".
                    error_text = f'{endpoint}: stream ended with no text'
                    logger.warning('chat turn empty [ws]: division=%s endpoint=%s',
                                   division, endpoint)
                    log.fail('llm', 'EmptyAnswer', error_text)
                    await _persist('error', '')
                    await _send_tired()
                break

            if not chunk.startswith('data: '):
                # Keepalive lines from the engine are forwarded as no-op pings, so a proxy idle timeout (~30 s)
                # does not close the socket while the model is still thinking.
                if chunk.startswith(': '):
                    try:
                        await websocket.send_json({'type': 'ping'})
                    except Exception:
                        pass
                continue

            raw = chunk[6:].strip()
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                continue
            t = parsed.get('type', '')
            if t == 'response.output_text.delta':
                delta = parsed.get('delta', '')
                accumulated.append(delta)
                if translate_ctx and translate_ctx.needs_translation:
                    # Raw English never reaches the client: 'Thinking' stays until the full answer is translated and sent in 'done'.
                    pass
                else:
                    await websocket.send_json({'type': 'delta', 'delta': delta})
            elif t == 'status':
                await websocket.send_json({'type': 'status', 'status': parsed.get('status', '')})
            elif t == 'sources':
                srcs = parsed.get('sources')
                cits = parsed.get('citations')
                collected_sources = srcs if isinstance(srcs, list) else []
                collected_citations = cits if isinstance(cits, list) else []
            elif t == 'error':
                error_text = parsed.get('error', '')
                # The engine records its own failures; this covers any other.
                if not log.error:
                    log.fail('engine', parsed.get('error_type', 'ChatLLMError'), error_text,
                             http_status=parsed.get('http_status') or 0)
                await _persist('error', ''.join(accumulated))
                await _send_tired()
                break

    except WebSocketDisconnect:
        logger.info('Chat WebSocket disconnected during stream')
        if outcome == 'ok':
            log.issue('client_disconnected', 'ws', 'the browser left after the answer was saved', severity=INFO)
        else:
            outcome = 'aborted'
            log.issue('client_disconnected', 'ws', 'the browser left before the end of the turn', severity=INFO)
            await _persist('aborted', ''.join(accumulated))
    except Exception as exc:
        logger.error('Chat WebSocket error: %s', exc, exc_info=True)
        if outcome == 'ok':
            # The answer is saved; only sending it failed (browser gone).
            log.issue('send_failed', 'ws', f'{type(exc).__name__}: {exc}', severity=INFO)
        else:
            error_text = f'{type(exc).__name__}: {exc}'
            log.fail('ws', type(exc).__name__, str(exc), stack_trace=traceback.format_exc(), exc=exc)
            await _persist('error', ''.join(accumulated))
            try:
                await _send_tired()
            except Exception:
                pass
    finally:
        if answer_stream is not None:
            # Frees the engine's answer slot now, not when the generator is garbage-collected.
            try:
                await answer_stream.aclose()
            except Exception:
                pass
        if user_content:
            # After 'done' was sent: the user does not wait for this write.
            log.finish(outcome)
            await mlflow_turns.export_turn(log)
            await store_chat_turn(log, endpoint=route, user_message_id=saved[0], assistant_message_id=saved[1])
        try:
            await websocket.close()
        except Exception:
            pass
