"""Chat API: WebSocket turn, feedback, conversation history/sharing and the list of the documents citing a code."""

from fastapi import APIRouter

from server.services.chat.config import TRANSLATE_BRIDGE_ENABLED
from server.routers.chat import citations, sessions, ws
from server.routers.chat.helpers import (
    CHAT_MAX_HISTORY_TOKENS,
    _apply_citation_markers,
    _detect_division,
    _stored_sources,
    _strip_division,
    _trim_history,
    _with_today_date,
)

router = APIRouter()
router.include_router(ws.router)
router.include_router(sessions.router)
router.include_router(citations.router)

# Imported by utils/evaluation to reproduce what a chat turn feeds the engine.
__all__ = [
    'CHAT_MAX_HISTORY_TOKENS',
    'TRANSLATE_BRIDGE_ENABLED',
    '_apply_citation_markers',
    '_detect_division',
    '_stored_sources',
    '_strip_division',
    '_trim_history',
    '_with_today_date',
    'router',
]
