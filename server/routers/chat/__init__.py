"""Chat API: WebSocket turn, feedback and conversation history/sharing."""

from fastapi import APIRouter

from ...services.chat.translation_bridge import ENABLED as TRANSLATE_BRIDGE_ENABLED
from . import sessions, ws
from .helpers import (
    CHAT_MAX_HISTORY,
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

# Imported by utils/evaluation to reproduce what a chat turn feeds the engine.
__all__ = [
    'CHAT_MAX_HISTORY',
    'TRANSLATE_BRIDGE_ENABLED',
    '_apply_citation_markers',
    '_detect_division',
    '_stored_sources',
    '_strip_division',
    '_trim_history',
    '_with_today_date',
    'router',
]
