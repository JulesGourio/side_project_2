"""Fonts, palette, page geometry and the report data model."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

FONT_DIR = Path(__file__).resolve().parents[3] / 'assets' / 'fonts'
_FONT_FILES = {
    '': 'Inter-Regular.ttf',
    'B': 'Inter-SemiBold.ttf',
    'I': 'Inter-Italic.ttf',
    'BI': 'Inter-SemiBoldItalic.ttf',
}
FONT = 'Inter'

KIND_LABELS = {
    'chat_response': 'Chatbot answer',
    'chat_session': 'Chatbot conversation',
    'change_summary': 'Change summary',
    'document_summary': 'Document summary',
    'report': 'Report',
}

# ── Palette (RGB) — accent is the app's --color-accent-primary (#0055A4) ─────
ACCENT = (0, 85, 164)
ACCENT_DARK = (0, 58, 112)
ACCENT_TINT = (238, 244, 251)
INK = (15, 23, 42)          # titles
TEXT = (51, 65, 85)         # body
MUTED = (100, 116, 139)     # labels, footer
FAINT = (148, 163, 184)
RULE = (226, 232, 240)
ZEBRA = (248, 250, 252)
CODE_BG = (243, 245, 248)

# ── Geometry (mm) ────────────────────────────────────────────────────────────
PAGE_W, PAGE_H = 210, 297
MARGIN_X = 20
TOP = 22            # body top on pages 2+ (below the running header)
BOTTOM = 20
W = PAGE_W - 2 * MARGIN_X

BODY_SIZE = 10
LINE_H = 5.4


@dataclass
class Source:
    title: str              # REF
    url: str | None = None
    n: int | None = None    # citation number, None for an uncited source
    detail: str = ''        # Intraqual title, revision and date


@dataclass
class Section:
    markdown: str
    question: str | None = None
    sources: list[Source] = field(default_factory=list)

