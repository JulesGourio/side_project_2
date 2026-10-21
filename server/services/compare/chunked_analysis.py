"""Chunked (map-reduce) LLM analysis for very large diffs.

One LLM call over a 100k+ char diff loses recall and can hit the max_tokens output ceiling, silently dropping the end
of the report. Above a threshold the TEXT CHANGES body is split at section boundaries, one focused call runs per part,
and the results are merged server-side into the SSE stream the frontend consumes (structured: one JSON array;
standard: Markdown parts in document order). The input cost is unchanged: the diff is split, not repeated.
"""

import asyncio
import json
import logging
import os
import re
from typing import Any, AsyncGenerator, Dict, List, Tuple

from server.services.compare.impact_queries import _NO_CHANGES_RE, _strip_markdown
from server.services.compare.llm import stream_analysis

logger = logging.getLogger(__name__)

_TEXT_MARKER = '--- TEXT CHANGES ---'
_VISUAL_MARKER = '--- VISUAL CHANGES ---'
# Parts run concurrently (bounded): sequential parts would push the SSE response past the gateway's hard duration limit.
# The wall clock is then about the slowest part, not the sum of all parts.
_CHUNK_PARALLELISM = int(os.getenv('COMPARE_CHUNK_PARALLELISM', '3'))
_EXCEL_OPENER = '```diff\n'
_EXCEL_CLOSER = '\n```'
_HEADING_RE = re.compile(r'^#{1,6}\s+\S.*$', re.M)


# --- Splitting ---

def _split_body(body: str, chunk_chars: int) -> List[str]:
    """Split the diff body into parts <= chunk_chars, preferring '\\n## ' section
    boundaries; oversized single sections are hard-split at line boundaries.

    :param body: diff body text, the TEXT CHANGES part
    :param chunk_chars: maximum length of a part, in characters
    :return: the parts, stripped of their surrounding newlines, empty ones dropped
    """
    sections = re.split(r'(?=\n## )', body)

    pieces: List[str] = []
    for sec in sections:
        if len(sec) <= chunk_chars:
            pieces.append(sec)
            continue
        lines = sec.split('\n')
        cur = ''
        for line in lines:
            while len(line) > chunk_chars:  # pathological single line — hard cut
                if cur:
                    pieces.append(cur)
                    cur = ''
                pieces.append(line[:chunk_chars])
                line = line[chunk_chars:]
            if cur and len(cur) + len(line) + 1 > chunk_chars:
                pieces.append(cur)
                cur = line
            else:
                cur = f'{cur}\n{line}' if cur else line
        if cur:
            pieces.append(cur)

    parts: List[str] = []
    cur = ''
    for piece in pieces:
        if cur and len(cur) + len(piece) > chunk_chars:
            parts.append(cur)
            cur = piece
        else:
            # Pieces cut inside a section carry no leading newline: without one a heading would stick to the previous line.
            cur = f'{cur}\n{piece}' if cur and not piece.startswith('\n') else cur + piece
    if cur:
        parts.append(cur)
    return [p for p in (p.strip('\n') for p in parts) if p]


def _locate_body(first_text: str) -> Tuple[str, str, str, str, str, str] | None:
    """Find the diff body in the first text block of a processor message.

    :param first_text: text of the first block of the user message
    :return: ``(before, opener, body, closer, tail_last, tail_other)``: the text before the body, the opener and closer
        that frame it in every part, the text that follows the closer in the last part and in the others;
        None when the layout has no splittable diff body
    """
    if _TEXT_MARKER in first_text:
        head, _, rest = first_text.partition(_TEXT_MARKER)
        body, _, tail = rest.partition(_VISUAL_MARKER)
        return (head, f'{_TEXT_MARKER}\n', body, f'\n\n{_VISUAL_MARKER}', tail,
                '\nNo embedded images in this part.')
    start = first_text.find(_EXCEL_OPENER)
    end = first_text.rfind(_EXCEL_CLOSER)
    if first_text.startswith('Excel spreadsheet comparison') and 0 <= start < end:
        body_start = start + len(_EXCEL_OPENER)
        return first_text[:start], _EXCEL_OPENER, first_text[body_start:end], _EXCEL_CLOSER, '', ''
    return None


def _last_heading(body: str) -> str:
    """The last Markdown heading line of a text.

    :param body: body of a part
    :return: the heading, empty when there is none
    """
    found = _HEADING_RE.findall(body)
    return found[-1].strip() if found else ''


def _carried_heading(in_effect: str, body: str) -> str:
    """Heading under which a part starts when it does not start with its own.

    :param in_effect: heading in effect at the end of the previous parts, empty when none
    :param body: body of this part
    :return: ``in_effect``, empty when this part opens with a heading
    """
    first = body.lstrip('\n').split('\n', 1)[0]
    return '' if _HEADING_RE.match(first) else in_effect


def _is_no_change_part(text: str) -> bool:
    """Whether the output of a part only says that nothing changed.

    :param text: Markdown output of one part
    :return: True for a short "no significant changes" statement
    """
    return bool(_NO_CHANGES_RE.search(text)) and len(_strip_markdown(text)) < 120


def split_messages_for_chunking(
    messages: List[Dict[str, Any]],
    threshold_chars: int,
    chunk_chars: int,
) -> List[List[Dict[str, Any]]] | None:
    """Split a processor-built message list into per-part message lists.

    Returns None when the diff is small enough for a single call (the normal
    case) or when the message shape isn't the standard-processor layout.

    :param messages: message list built by the processor
    :param threshold_chars: length of the diff body above which it is split
    :param chunk_chars: maximum length of the body of one part
    :return: one message list per part, or None when no split is needed
    """
    user_idx = next((i for i, m in enumerate(messages) if m.get('role') == 'user'), None)
    if user_idx is None:
        return None
    content = messages[user_idx].get('content')
    if not isinstance(content, list) or not content or content[0].get('type') != 'text':
        return None

    first_text = content[0].get('text', '')
    located = _locate_body(first_text)
    if located is None:
        return None
    before, opener, body, closer, tail_last, tail_other = located
    if len(body) <= threshold_chars:
        return None

    part_bodies = _split_body(body, chunk_chars)
    if len(part_bodies) <= 1:
        return None

    trailing_blocks = content[1:]  # image blocks — attached to the last part only
    n = len(part_bodies)
    parts: List[List[Dict[str, Any]]] = []
    in_effect = ''
    for i, part_body in enumerate(part_bodies):
        is_last = i == n - 1
        note = (
            f'[Large document — diff split into {n} parts, this is part {i + 1}/{n}. '
            'Report ONLY the changes appearing in this part.]\n\n'
        )
        carried = _carried_heading(in_effect, part_body)
        in_effect = _last_heading(part_body) or in_effect
        if carried:
            note += (f'[This part continues the section "{carried}": report its changes under that '
                     'section until a new heading appears.]\n\n')
        text = f'{before}{note}{opener}{part_body}{closer}'
        if is_last:
            blocks: List[Dict[str, Any]] = [{'type': 'text', 'text': f'{text}{tail_last}' if tail_last else text}]
            blocks += trailing_blocks
        else:
            blocks = [{'type': 'text', 'text': f'{text}{tail_other}'}]

        part_messages = [dict(m) for m in messages]
        part_messages[user_idx] = {'role': 'user', 'content': blocks}
        parts.append(part_messages)

    logger.info('Diff of %d chars split into %d LLM calls (chunk size %d)', len(body), n, chunk_chars)
    return parts


# --- JSON object extraction (string/escape-aware brace scanner) ---

def extract_json_objects(text: str) -> List[Dict[str, Any]]:
    """Extract every complete top-level JSON object from free-form text.

    :param text: free text that may contain JSON objects
    :return: the top-level objects that parse, in order of appearance
    """
    objs: List[Dict[str, Any]] = []
    depth = 0
    start = -1
    in_str = False
    esc = False
    for i, ch in enumerate(text):
        if in_str:
            if esc:
                esc = False
            elif ch == '\\':
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            if depth > 0:
                in_str = True
        elif ch == '{':
            if depth == 0:
                start = i
            depth += 1
        elif ch == '}':
            if depth > 0:
                depth -= 1
                if depth == 0 and start != -1:
                    try:
                        obj = json.loads(text[start:i + 1])
                        if isinstance(obj, dict):
                            objs.append(obj)
                    except json.JSONDecodeError:
                        pass
                    start = -1
    return objs


# --- Chunked streaming — same SSE protocol as stream_analysis ---

async def _run_part(
    host: str, token: str, endpoint_name: str,
    part_messages: List[Dict[str, Any]],
    max_tokens: int, thinking_budget: int, temperature: float,
    info: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    """Consume one stream_analysis call fully; return its buffered outcome.

    :param host: Databricks workspace URL
    :param token: Databricks access token
    :param endpoint_name: serving endpoint name
    :param part_messages: messages of this part
    :param max_tokens: output budget
    :param thinking_budget: reasoning token budget
    :param temperature: sampling temperature
    :param info: dict that receives the endpoint used, None to skip
    :return: dict with the text of the part, its usage event, the error event if any, and the other events in order
    """
    text = ''
    usage: Dict[str, Any] = {}
    error_chunk: str | None = None
    passthrough: List[str] = []  # warning events etc.

    async for chunk in stream_analysis(
        host, token, endpoint_name, part_messages,
        max_tokens, thinking_budget, temperature, info=info,
    ):
        if not chunk.startswith('data: '):
            continue  # inner keepalives — outer generator emits its own
        data = chunk[6:].strip()
        if data == '[DONE]':
            break
        try:
            event = json.loads(data)
        except json.JSONDecodeError:
            continue
        etype = event.get('type')
        if etype == 'response.output_text.delta':
            text += event.get('delta', '')
        elif etype == 'usage':
            usage = event
        elif etype == 'error':
            error_chunk = chunk
            break
        else:
            passthrough.append(chunk)
    return {'text': text, 'usage': usage, 'error': error_chunk, 'passthrough': passthrough}


async def stream_analysis_chunked(
    host: str,
    token: str,
    endpoint_name: str,
    message_parts: List[List[Dict[str, Any]]],
    max_tokens: int,
    thinking_budget: int,
    temperature: float,
    structured: bool,
    info: Dict[str, Any] | None = None,
) -> AsyncGenerator[str, None]:
    """Run the per-part LLM calls concurrently (bounded) and merge them into a
    single SSE stream, emitting parts in document order as they complete.

    Emits the same events as stream_analysis: text deltas, at most one usage
    event (summed over parts), warning/error events, then [DONE]. Keepalives
    are emitted while waiting so the gateway never sees an idle connection.
    ``info['endpoint']``, when given, lists the endpoints that answered the parts,
    comma-joined in document order without repeats (set even when the stream stops early).

    :param host: Databricks workspace URL
    :param token: Databricks access token
    :param endpoint_name: serving endpoint name
    :param message_parts: one message list per part, from ``split_messages_for_chunking``
    :param max_tokens: output budget of each part
    :param thinking_budget: reasoning token budget of each part
    :param temperature: sampling temperature
    :param structured: True when the output is one JSON array
    :param info: dict that receives ``endpoint`` when given, None to skip
    :return: the SSE lines of the merged analysis
    """
    total_usage = {'input_tokens': 0, 'output_tokens': 0, 'thinking_tokens': 0,
                   'total_tokens': 0, 'cost_eur': 0.0}
    emitted_objects = 0
    emitted_text = False
    no_change_text = ''
    n = len(message_parts)
    sem = asyncio.Semaphore(_CHUNK_PARALLELISM)
    part_infos: List[Dict[str, Any]] = [{} for _ in message_parts]

    def _delta_event(text: str) -> str:
        """One SSE text delta event.

        :param text: text to send
        :return: the ``data:`` line
        """
        return f'data: {json.dumps({"type": "response.output_text.delta", "delta": text})}\n\n'

    async def _guarded(idx: int, msgs: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Runs the call of one part once a slot is free.

        :param idx: index of the part, from 0
        :param msgs: messages of the part
        :return: the buffered outcome of ``_run_part``
        """
        async with sem:
            logger.info('Chunked analysis: part %d/%d start', idx + 1, n)
            return await _run_part(host, token, endpoint_name, msgs,
                                   max_tokens, thinking_budget, temperature, info=part_infos[idx])

    tasks = [asyncio.create_task(_guarded(i, m)) for i, m in enumerate(message_parts)]

    try:
        for part_idx, task in enumerate(tasks):
            # Emit keepalives while this part (and the ones running with it)
            # is still generating.
            while True:
                done, _ = await asyncio.wait({task}, timeout=10.0)
                if done:
                    break
                yield ': keepalive\n\n'
            res = task.result()

            for chunk in res['passthrough']:
                yield chunk
            if res['error']:
                # Rows already sent stay a valid JSON array: the client keeps the
                # parts that succeeded instead of an unterminated "[ {...}, {...}".
                if structured and emitted_objects:
                    yield _delta_event('\n]')
                yield res['error']
                yield 'data: [DONE]\n\n'
                return

            u = res['usage']
            for k in ('input_tokens', 'output_tokens', 'thinking_tokens', 'total_tokens'):
                total_usage[k] += u.get(k, 0) or 0
            total_usage['cost_eur'] += u.get('cost_eur', 0.0) or 0.0

            if structured:
                objects = extract_json_objects(res['text'])
                logger.info('Chunked analysis: part %d/%d done — %d rows', part_idx + 1, n, len(objects))
                if objects:
                    payload = ',\n'.join(json.dumps(o, ensure_ascii=False) for o in objects)
                    prefix = '[\n' if emitted_objects == 0 else ',\n'
                    yield _delta_event(f'{prefix}{payload}')
                    emitted_objects += len(objects)
            else:
                logger.info('Chunked analysis: part %d/%d done — %d chars', part_idx + 1, n, len(res['text']))
                if res['text'] and _is_no_change_part(res['text']):
                    # Kept only if no other part reports a change; between real changes it would read as one.
                    no_change_text = no_change_text or res['text']
                elif res['text']:
                    yield _delta_event(('\n\n' if emitted_text else '') + res['text'])
                    emitted_text = True
    finally:
        for t in tasks:
            if not t.done():
                t.cancel()
        if info is not None:
            used = list(dict.fromkeys(p['endpoint'] for p in part_infos if p.get('endpoint')))
            if used:
                info['endpoint'] = ','.join(used)

    if structured:
        yield _delta_event('\n]' if emitted_objects else '[]')
    elif no_change_text and not emitted_text:
        yield _delta_event(no_change_text)

    usage_event = {'type': 'usage', **total_usage}
    usage_event['cost_eur'] = round(usage_event['cost_eur'], 6)
    yield f'data: {json.dumps(usage_event)}\n\n'
    yield 'data: [DONE]\n\n'
