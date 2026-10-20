"""Pure helpers of the chat routes: division scope, history window, citation markers, stored sources."""

import json
import os
import re
from datetime import datetime
from typing import List

from server.services.infra.user import get_user_identity


# Token budget of the past messages replayed to the engine each turn (answers are grounded in retrieval, so long memory
# matters less). 0 = no trimming.
CHAT_MAX_HISTORY_TOKENS = int(os.getenv('CHAT_MAX_HISTORY_TOKENS', '8000'))
# Rough size of a token in characters, for French/English text: enough to cap a window, no tokenizer call per turn.
_CHARS_PER_TOKEN = 4

# Division-scoping directive the client may prepend to a question; stripped before deriving the thread title.
_DIVISION_PREFIX_RE = re.compile(r'^\[Division: (?:AS|IS)\][\s\S]*?\n\n')
_DIVISION_DETECT_RE = re.compile(r'^\[Division: (AS|IS)\]')

# Inline citations are stored in the answer as ``⟦n⟧`` (U+27E6/U+27E7, absent from source documents) and rendered by
# the client as a link to source n, so a reloaded conversation shows them too.
_CITE_MARKER_TMPL = '⟦{n}⟧'

# Keeps Markdown links and raw URLs whole when citation markers are inserted.
_PROTECTED_SPAN_RE = re.compile(r'\[[^\]]+\]\([^)]+\)|https?://[^\s)\]\'">]+')

# Fields of a stored source chip; the first three are always sent, the others only when set.
_SOURCE_FIELDS = ('title', 'url', 'n', 'doc_title', 'revision', 'doc_date')


def _strip_division(text: str) -> str:
    """The question without the ``[Division: …]`` directive the client may have prepended.

    :param text: the user message as sent by the browser.
    :return: ``text`` without the directive line(s).
    """
    return _DIVISION_PREFIX_RE.sub('', text)


def _detect_division(text: str) -> str:
    """The division scope the client tagged the question with.

    :param text: the user message as sent by the browser.
    :return: ``'AS'`` or ``'IS'``, or ``'ALL'`` when there is no directive prefix.
    """
    m = _DIVISION_DETECT_RE.match(text or '')
    return m.group(1) if m else 'ALL'


def _apply_citation_markers(text: str, citations: List[dict]) -> str:
    """Insert ``⟦n⟧`` markers into ``text`` at each citation's character offset.

    An offset inside a Markdown link or a raw URL is pushed to the end of that link so the link keeps working.

    :param text: the answer as streamed by the model.
    :param citations: ``{'n': source number, 'pos': character offset in text}``; malformed entries are skipped.
    :return: ``text`` with the markers, or ``text`` itself when there is nothing to mark.
    """
    if not text or not citations:
        return text
    n_chars = len(text)
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
                break
        if (n, pos) not in seen:
            seen.add((n, pos))
            valid.append((pos, n))
    # Insert right-to-left; markers at the same offset read in ascending order of n ("[2][3]").
    valid.sort()
    out = text
    for pos, n in reversed(valid):
        out = out[:pos] + _CITE_MARKER_TMPL.format(n=n) + out[pos:]
    return out


def _number_sources(sources: List[dict], citations: List[dict]) -> List[dict]:
    """Give each inline-cited source its 1-based citation number ``n``; prose-only sources (re-surfaced from the
    catalog) get none. The cited sources are the first entries of ``sources``: index ``i`` is cited iff ``i + 1`` is
    among the citation numbers.

    :param sources: the source chips of the answer, modified in place.
    :param citations: the inline citations of the answer (``{'n': …, 'pos': …}``).
    :return: ``sources``.
    """
    cited = {c.get('n') for c in (citations or []) if isinstance(c.get('n'), int)}
    for i, s in enumerate(sources or []):
        if (i + 1) in cited:
            s['n'] = i + 1
        else:
            s.pop('n', None)
    return sources


def _estimate_tokens(message: dict) -> int:
    """Rough token count of a message (see ``_CHARS_PER_TOKEN``).

    :param message: a ``{'role': …, 'content': …}`` message.
    :return: at least 1.
    """
    return len(message.get('content') or '') // _CHARS_PER_TOKEN + 1


def _trim_history(messages: List[dict]) -> List[dict]:
    """The most recent messages that fit in ``CHAT_MAX_HISTORY_TOKENS``, for the endpoint.

    The latest message is always kept, even alone above the budget. The window never opens on an assistant turn.

    :param messages: the whole conversation, oldest first.
    :return: the tail of ``messages`` that fits the budget (``messages`` itself when it all fits).
    """
    if CHAT_MAX_HISTORY_TOKENS <= 0 or sum(_estimate_tokens(m) for m in messages) <= CHAT_MAX_HISTORY_TOKENS:
        return messages
    kept, used = 0, 0
    for message in reversed(messages):
        used += _estimate_tokens(message)
        if kept and used > CHAT_MAX_HISTORY_TOKENS:
            break
        kept += 1
    trimmed = messages[-kept:]
    # Don't open the window on an assistant turn — keep role coherence.
    while len(trimmed) > 1 and trimmed[0].get('role') != 'user':
        trimmed = trimmed[1:]
    return trimmed


def _with_today_date(messages: List[dict]) -> List[dict]:
    """A COPY of messages with today's date prepended to the last user turn, so date-relative questions work.

    The engine puts its own system message first, hence the user turn. A plain ISO date, no sentence: a French
    sentence pushes the model toward French answers. Callers keep the ORIGINAL `messages` for logging and persistence.

    :param messages: the conversation sent to the engine, oldest first.
    :return: a copy of ``messages`` with ``[Date: YYYY-MM-DD]`` before the last user message.
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


def _stored_sources(raw: str | None) -> list:
    """The source chips of a saved assistant message (chat_messages.sources_json).

    :param raw: the JSON column, or None for a message without sources.
    :return: the chips; an empty list when ``raw`` is empty or unreadable.
    """
    if not raw:
        return []
    try:
        return [{'title': s.get('title'), 'url': s.get('url'), 'n': s.get('n'),
                 **{k: s[k] for k in _SOURCE_FIELDS[3:] if s.get(k)}} for s in json.loads(raw)]
    except (json.JSONDecodeError, AttributeError, TypeError):
        return []


async def _identity_or_anonymous(request) -> dict:
    """The caller's identity; an anonymous one when it cannot be resolved (the route then scopes to nobody).

    :param request: the incoming FastAPI request.
    :return: ``{'user_id', 'workspace_id', 'email'}``.
    """
    try:
        return await get_user_identity(request)
    except Exception:
        return {'user_id': '', 'workspace_id': None, 'email': None}
