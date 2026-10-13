"""Pure helpers of the chat routes: division scope, history window, citation markers, stored sources."""

import json
import os
import re
from datetime import datetime
from typing import List, Optional

from ...services.infra.user import get_user_identity


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
    full "Nous sommes le lundi ..." sentence here was found to
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

_SOURCE_FIELDS = ('title', 'url', 'n', 'doc_title', 'revision', 'doc_date')


def _stored_sources(raw: Optional[str]) -> list:
    """The source chips of a saved assistant message (chat_messages.sources_json)."""
    if not raw:
        return []
    try:
        return [{'title': s.get('title'), 'url': s.get('url'), 'n': s.get('n'),
                 **{k: s[k] for k in _SOURCE_FIELDS[3:] if s.get(k)}} for s in json.loads(raw)]
    except (json.JSONDecodeError, AttributeError, TypeError):
        return []


async def _identity_or_anonymous(request) -> dict:
    """The caller's identity; an anonymous one when it cannot be resolved (the route then scopes to nobody)."""
    try:
        return await get_user_identity(request)
    except Exception:
        return {'user_id': '', 'workspace_id': None, 'email': None}
