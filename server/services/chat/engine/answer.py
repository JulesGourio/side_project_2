"""The prompt, the answer stream and the turn log of the answer step."""

import asyncio
import hashlib
import json
import logging
import time
from typing import Any, AsyncGenerator, Dict, List, Optional, Tuple


from .. import llm as chat_llm, prompts
from ..llm import sse_event
from ...infra.doc_catalog import document_info
from ..turn_log import TurnLog
from .citations import CitationStreamParser
from .retrieval import _clean_history, group_documents, retrieve_for_turn
from .search import ChatVsiError
from .config import answer_max_tokens, index_name, llm_endpoint, load_instructions, normalize_division, settings

_STATUS_AFTER_S = 8.0          # a search or rewrite slower than this shows 'still searching'

logger = logging.getLogger(__name__)

_OPERATION = 'Chat'              # label used in user-facing error messages

LANGUAGE_REMINDER = prompts.read('language_reminder')
NAMED_LANGUAGE_REMINDER = prompts.read('language_reminder_named')


# --- Prompt and answer ---

def with_language_reminder(messages: List[Dict[str, str]], language: Optional[str] = None) -> List[Dict[str, str]]:
    """``messages`` with the language reminder after the last user turn (a copy)."""
    line = NAMED_LANGUAGE_REMINDER.format(language=language) if language else LANGUAGE_REMINDER
    out = [dict(m) for m in messages]
    out[-1]['content'] = f'{out[-1]["content"]}\n\n{line}'
    return out


def revision_note(ref: str) -> str:
    """' (current revision B, published YYYY-MM-DD)' from the catalog, '' when it has neither: the
    passages themselves never carry the revision, and every document of the index is the current one."""
    info = document_info(ref)
    parts = ([f"current revision {info['revision']}"] if info.get('revision') else []) + \
            ([f"published {info['doc_date']}"] if info.get('doc_date') else [])
    return f" ({', '.join(parts)})" if parts else ''


def build_prompt(division: str, conversation: List[Dict[str, str]], documents: List[Tuple[str, Dict[str, Any]]],
                 answer_language: Optional[str] = None) -> List[Dict[str, str]]:
    """System instructions, then the conversation; the last turn carries the numbered documents
    before the question and the language reminder after it."""
    context = '\n\n'.join(f'[{i}] Document {ref}{revision_note(ref)}\n' + '\n\n'.join(d['passages'])
                          for i, (ref, d) in enumerate(documents, 1))
    turns = [dict(m) for m in conversation]
    turns[-1]['content'] = f'Documents:\n\n{context}\n\n---\n\n{turns[-1]["content"]}'
    return with_language_reminder([{'role': 'system', 'content': load_instructions(division)}] + turns,
                                  answer_language)


def _error_events(message: str, error_type: str, http_status: int = 0) -> List[str]:
    return [sse_event({'type': 'error', 'error': message, 'error_type': error_type, 'http_status': http_status}),
            'data: [DONE]\n\n']


def _record_prompt(log: TurnLog, division: str, documents: List[Tuple[str, Dict[str, Any]]],
                   prompt: List[Dict[str, str]]) -> None:
    """Prompt facts on the turn, and each kept passage's place in the prompt."""
    number = {ref: n for n, (ref, _) in enumerate(documents, 1)}
    kept = [p for p in log.passages if p['kept']]
    for rank, p in enumerate(sorted(kept, key=lambda p: (number.get(p['ref'], 0), p['position']))):
        p['prompt_rank'], p['doc_number'] = rank, number.get(p['ref'])
    log.data.update({
        'documents_sent': len(documents),
        'prompt_chars': sum(len(m['content']) for m in prompt),
        'instructions_sha': hashlib.sha256(load_instructions(division).encode('utf-8')).hexdigest()[:16],
    })


def _record_answer(log: TurnLog, chain: List[str], llm: Dict[str, Any], usage: Dict[str, Any],
                   parser: CitationStreamParser) -> None:
    """What the answer model did, and which documents the answer cites."""
    cited = [s['title'] for s in parser.sources]
    for p in log.passages:
        p['cited'] = p['kept'] and p['ref'] in cited
    attempts = llm.get('attempts') or []
    log.timings_ms['queue_wait'] = llm.get('queue_wait_ms') or 0
    log.data.update({
        'answer_endpoint': llm.get('endpoint'), 'llm_fallback': bool(llm.get('fallback')), 'llm_attempts': attempts,
        'truncated': bool(llm.get('truncated')), 'continuations': llm.get('continuations') or 0,
        'input_tokens': usage.get('input_tokens'), 'output_tokens': usage.get('output_tokens'),
        'thinking_tokens': usage.get('thinking_tokens'), 'cost_eur': usage.get('cost_eur'),
        'answer_chars': parser.emitted_chars, 'citations_count': len(parser.citations), 'cited_refs': cited,
    })
    failed = [a for a in attempts if a.get('outcome') not in ('ok', 'continued')]
    context = {'expected': chain[0] if chain else None, 'chain': chain, 'attempts': attempts}
    if llm.get('fallback'):
        log.issue('llm_fallback', 'llm', f"answered by {llm.get('endpoint')} instead of {chain[0]}",
                  upstream=llm.get('endpoint') or '', context=context)
    elif failed and llm.get('endpoint'):
        log.issue('llm_retried', 'llm', f'{len(failed)} failed call(s) before the answer',
                  upstream=llm.get('endpoint') or '', context=context)
    if llm.get('interrupted'):
        log.issue('answer_interrupted', 'llm', 'part of the answer was sent, no model could finish it',
                  context=context)
    elif llm.get('continuations'):
        log.issue('answer_continued', 'llm', f"answer resumed {llm['continuations']} time(s) after a cut",
                  context=context)


async def stream_chat(host: str, token: str, division: str, messages: List[Dict[str, str]],
                          answer_language: Optional[str] = None,
                          log: Optional[TurnLog] = None) -> AsyncGenerator[str, None]:
    """Answer the last user turn of ``messages`` (trimmed history, last turn prefixed with
    ``[Date: …]``, translated to English by the bridge when needed). ``answer_language`` (e.g.
    "French", from the chat route) is named in the language reminder. ``log`` (the turn's ``TurnLog``,
    from the chat route) receives every step; its ``trace_id`` is the one in the ``metadata`` event."""
    yield ': keepalive\n\n'                      # first byte out, before the slow steps
    log = log if log is not None else TurnLog()
    trace_id = log.trace_id
    div = normalize_division(division)
    log.data.update({'division': div, 'index_name': index_name(), 'config': settings()})
    log.prompts = prompts.keys_for_turn(div, bool(answer_language))
    conversation = _clean_history(messages)
    if not conversation or conversation[-1]['role'] != 'user':
        log.fail('input', 'InputError', 'the last message must be a user question')
        for e in _error_events('Chat: the last message must be a user question.', 'InputError'):
            yield e
        return
    search = asyncio.ensure_future(retrieve_for_turn(host, token, div, conversation, log=log))
    while not search.done():
        await asyncio.wait({search}, timeout=_STATUS_AFTER_S)
        if not search.done():
            yield sse_event({'type': 'status', 'status': 'still_searching'})
    try:
        found = search.result()
    except ChatVsiError as exc:
        log.fail('search', exc.error_type, exc.message, http_status=exc.http_status, upstream=index_name(),
                 context={'vs_calls_expected': log.data.get('vs_calls_expected'),
                          'vs_calls_ok': log.data.get('vs_calls_ok')}, exc=exc.__cause__ or exc)
        for e in _error_events(exc.message, exc.error_type, exc.http_status):
            yield e
        return
    except Exception as exc:
        log.fail('search', type(exc).__name__, str(exc)[:300], exc=exc)
        for e in _error_events('Chat: the search failed.', type(exc).__name__):
            yield e
        return
    rows = found['rows']
    documents = group_documents(rows)
    logger.info('chat: division=%s index=%s fr_query=%r en_query=%r passages=%d documents=%d trace_id=%s',
                div, found['index'], found['fr_query'], found['en_query'], len(rows), len(documents), trace_id)

    parser = CitationStreamParser(documents)
    usage: Dict[str, Any] = {}
    llm: Dict[str, Any] = {}
    chain = chat_llm.answer_chain(llm_endpoint())
    prompt = build_prompt(div, conversation, documents, answer_language)
    _record_prompt(log, div, documents, prompt)
    llm_started = time.monotonic()
    log.starts_ms['answer'] = log.elapsed_ms()

    def _generation_done() -> None:
        llm_ms = int((time.monotonic() - llm_started) * 1000)
        log.timings_ms['generation'] = max(0, llm_ms - (llm.get('queue_wait_ms') or 0))
        _record_answer(log, chain, llm, usage, parser)

    async for chunk in chat_llm.stream_answer(host, token, chain, prompt, answer_max_tokens(), _OPERATION):
        if not chunk.startswith('data: '):
            yield chunk                          # keepalive comments
            continue
        data = chunk[6:].strip()
        if data == '[DONE]':
            break
        try:
            event = json.loads(data)
        except json.JSONDecodeError:
            continue
        kind = event.get('type')
        if kind == 'response.output_text.delta':
            clean = parser.feed(event.get('delta', ''))
            if clean:
                log.mark('first_token')
                yield sse_event({'type': 'response.output_text.delta', 'delta': clean})
        elif kind == 'error':
            logger.error('chat: generation failed: %s', event.get('error'))
            _generation_done()
            log.fail('llm', event.get('error_type') or 'LLMError', event.get('error') or 'Answer generation failed.',
                     http_status=event.get('http_status') or 0, upstream=','.join(chain),
                     context={'attempts': llm.get('attempts')})
            for e in _error_events(event.get('error') or 'Answer generation failed.',
                                   event.get('error_type') or 'LLMError', event.get('http_status') or 0):
                yield e
            return
        elif kind == 'warning':
            logger.warning('chat: %s', event.get('detail') or event)
            log.issue(event.get('warning') or 'llm_warning', 'llm', event.get('detail') or '',
                      upstream=llm.get('endpoint') or '')
        elif kind == 'usage':
            usage = {k: event.get(k) for k in ('input_tokens', 'output_tokens', 'thinking_tokens', 'cost_eur')}
        elif kind == 'llm':
            llm = event

    tail = parser.flush()
    if tail:
        log.mark('first_token')
        yield sse_event({'type': 'response.output_text.delta', 'delta': tail})
    _generation_done()
    if parser.sources or parser.citations:
        yield sse_event({'type': 'sources', 'sources': parser.sources, 'citations': parser.citations})
    yield sse_event({'type': 'metadata', 'trace_id': trace_id})
    yield 'data: [DONE]\n\n'
