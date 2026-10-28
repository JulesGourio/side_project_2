"""The prompt, the answer stream and the turn log of the answer step."""

import asyncio
import hashlib
import json
import logging
import time
from typing import AsyncGenerator, Dict, List, Tuple

from server.services.chat import llm as chat_llm, prompts
from server.services.chat.llm import sse_event
from server.services.infra.doc_catalog import document_info
from server.services.chat.turn_log import TurnLog
from server.services.chat.engine.citations import CitationStreamParser
from server.services.chat.engine.references import exact_citations_block
from server.services.chat.engine.references import exact_citations_block
from server.services.chat.engine.retrieval import _clean_history, group_documents, retrieve_for_turn
from server.services.chat.engine.search import ChatVsiError
from server.services.chat.engine.types import AnswerFacts, AnswerUsage, DocumentPassages
from server.services.chat.engine.config import answer_max_tokens, index_name, llm_endpoint, load_instructions, normalize_division, settings

_STATUS_AFTER_S = 15.0         # a search or rewrite slower than this shows 'still searching'

logger = logging.getLogger(__name__)


LANGUAGE_REMINDER = prompts.read('language_reminder')


# --- Prompt and answer ---

def with_language_reminder(messages: List[Dict[str, str]], language: str | None = None) -> List[Dict[str, str]]:
    """``messages`` with the language reminder after the last user turn (a copy).

    :param messages: the conversation, oldest first; its last item is the user turn
    :param language: name of the answer language (e.g. ``French``), or None: the language of the question
    :return: a new list of message dicts, the input is not modified
    """
    line = LANGUAGE_REMINDER.format(language=language or 'the language of the question above')
    out = [dict(m) for m in messages]
    out[-1]['content'] = f'{out[-1]["content"]}\n\n{line}'
    return out


def revision_note(ref: str, division: str | None = None) -> str:
    """' "Title" (current revision B, published YYYY-MM-DD; division AS)' from the catalog and the index row,
    with only the parts it has: the passages themselves never carry them, and every document of the index is the
    current one.

    :param ref: REF of the document, as in the search rows
    :param division: division code of the document (``AS`` or ``IS``), or None when unknown
    :return: the note starting with a space: the quoted title, then the known parts in parentheses; empty when the
        catalog knows nothing
    """
    info = document_info(ref)
    parts = ([f"current revision {info['revision']}"] if info.get('revision') else []) + \
            ([f"published {info['doc_date']}"] if info.get('doc_date') else []) + \
            ([f"division {division}"] if division else [])
    title = f' "{info["title"]}"' if info.get('title') else ''
    return f"{title} ({', '.join(parts)})" if parts else title


def build_prompt(division: str, conversation: List[Dict[str, str]], documents: List[Tuple[str, DocumentPassages]],
                 answer_language: str | None = None, exact_citations: str = '') -> List[Dict[str, str]]:
    """System instructions, then the conversation; the last turn carries the numbered documents (and, for a "which
    documents cite X?" question, the exact citation index) before the question and the language reminder after it.

    :param division: normalised division code: ``ALL``, ``AS`` or ``IS``
    :param conversation: cleaned conversation whose last item is the user question
    :param documents: passages grouped by document, as returned by ``group_documents``
    :param answer_language: name of the language the answer must use, or None
    :param exact_citations: block of ``references.exact_citations_block``, empty for none
    :return: the messages for the answer model: system instructions, then the conversation
    """
    context = '\n\n'.join(f'[{i}] Document {ref}{revision_note(ref, d.get("division"))}\n' + '\n\n'.join(d['passages'])
                          for i, (ref, d) in enumerate(documents, 1))
    if exact_citations:
        context = f'{context}\n\n{exact_citations}'
    turns = [dict(m) for m in conversation]
    turns[-1]['content'] = f'Documents:\n\n{context}\n\n---\n\n{turns[-1]["content"]}'
    return with_language_reminder([{'role': 'system', 'content': load_instructions(division)}] + turns,
                                  answer_language)


def _error_events(message: str, error_type: str, http_status: int = 0) -> List[str]:
    """The SSE events that report a failed answer and end the stream.

    :param message: text shown to the user
    :param error_type: name of the error, sent in the event
    :param http_status: HTTP status of the failed upstream call, 0 when there is none
    :return: the error event followed by the end-of-stream marker
    """
    return [sse_event({'type': 'error', 'error': message, 'error_type': error_type, 'http_status': http_status}),
            'data: [DONE]\n\n']


def _record_prompt(log: TurnLog, division: str, documents: List[Tuple[str, DocumentPassages]],
                   prompt: List[Dict[str, str]]) -> None:
    """Prompt facts on the turn, and each kept passage's place in the prompt.

    :param log: turn log to fill
    :param division: normalised division code, used to fingerprint the instructions
    :param documents: documents sent to the model, in their numbering order
    :param prompt: messages sent to the model, used for the prompt size
    """
    number = {ref: n for n, (ref, _) in enumerate(documents, 1)}
    kept = [p for p in log.passages if p['kept']]
    for rank, p in enumerate(sorted(kept, key=lambda p: (number.get(p['ref'], 0), p['position']))):
        p['prompt_rank'], p['doc_number'] = rank, number.get(p['ref'])
    log.data.update({
        'documents_sent': len(documents),
        'prompt_chars': sum(len(m['content']) for m in prompt),
        'instructions_sha': hashlib.sha256(load_instructions(division).encode('utf-8')).hexdigest()[:16],
    })


def _record_answer(log: TurnLog, chain: List[str], llm: AnswerFacts, usage: AnswerUsage,
                   parser: CitationStreamParser) -> None:
    """What the answer model did, and which documents the answer cites.

    :param log: turn log to fill
    :param chain: answer endpoints in order, primary first
    :param llm: facts of the answer call from its ``llm`` event: endpoint used, fallback, attempts, queue wait,
        truncation, continuations, interruption
    :param usage: token counts and cost of the answer (``input_tokens``, ``output_tokens``, ``thinking_tokens``,
        ``cost_eur``)
    :param parser: citation parser of the answer, after its last chunk
    """
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
                          answer_language: str | None = None,
                          log: TurnLog | None = None) -> AsyncGenerator[str, None]:
    """Answer the last user turn of ``messages`` (trimmed history, last turn prefixed with
    ``[Date: …]``, translated to English by the bridge when needed). ``answer_language`` (e.g.
    "French", from the chat route) is named in the language reminder. ``log`` (the turn's ``TurnLog``,
    from the chat route) receives every step; its ``trace_id`` is the one in the ``metadata`` event.

    :param host: Databricks workspace URL
    :param token: Databricks access token used for the model and Vector Search calls
    :param division: division of the chat, any case (normalised here)
    :param messages: chat history ending with the user question
    :param answer_language: name of the answer language (e.g. ``French``), or None
    :param log: turn log to fill, created when None
    :return: the SSE lines of the answer: status, text deltas, sources, metadata and the end-of-stream marker
    """
    yield ': keepalive\n\n'                      # first byte out, before the slow steps
    log = log if log is not None else TurnLog()
    trace_id = log.trace_id
    div = normalize_division(division)
    log.data.update({'division': div, 'index_name': index_name(), 'config': settings()})
    log.prompts = prompts.keys_for_turn(div)
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
    usage: AnswerUsage = {}
    llm: AnswerFacts = {}
    chain = chat_llm.answer_chain(llm_endpoint())
    exact = exact_citations_block(found.get('exact_citations') or {}, div)
    if exact:
        log.prompts.append('exact_citations')
    prompt = build_prompt(div, conversation, documents, answer_language, exact)
    _record_prompt(log, div, documents, prompt)
    log.model_io['answer'] = {'messages': prompt}
    llm_started = time.monotonic()
    log.starts_ms['answer'] = log.elapsed_ms()

    def _generation_done() -> None:
        llm_ms = int((time.monotonic() - llm_started) * 1000)
        log.timings_ms['generation'] = max(0, llm_ms - (llm.get('queue_wait_ms') or 0))
        _record_answer(log, chain, llm, usage, parser)

    async for chunk in chat_llm.stream_answer(host, token, chain, prompt, answer_max_tokens()):
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
