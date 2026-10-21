"""POST /compare/analyze: stream the LLM analysis of two documents, with result cache and run audit."""

import asyncio
import copy
import hashlib
import json
import logging
import time
import traceback
from dataclasses import dataclass, field
from typing import Any, AsyncGenerator, Dict, List, Tuple

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import StreamingResponse

from server.services.compare.chunked_analysis import split_messages_for_chunking, stream_analysis_chunked
from server.services.compare.llm import TIRED_MESSAGE, stream_analysis
from server.services.compare.processors.factory import (
    diff_truncation_warnings, file_type_of, get_extension, get_processor,
)
from server.services.infra.credentials import get_credentials
from server.services.infra.lakebase import (
    get_cached_analysis, store_analysis_cache, store_error,
    store_llm_request,
    update_llm_request_run, update_llm_request_usage,
)
from server.services.infra.tasks import spawn
from server.services.infra.user import get_user_identity, rate_limit, require_compare
from server.routers.compare.config import APP_VERSION, MAX_CONCURRENT_ANALYSES, get_config
from server.routers.compare.common import _validate_file

logger = logging.getLogger(__name__)
router = APIRouter()


# Headers of an SSE response: no caching, and no proxy buffering, so events reach the browser as they are produced.
_SSE_HEADERS = {'Cache-Control': 'no-cache', 'Connection': 'keep-alive', 'X-Accel-Buffering': 'no'}


def _sanitize_messages(messages: list) -> list:
    """Replace base64 image data with readable placeholders for audit storage.

    :param messages: the chat messages sent to the model; image blocks hold ``data:`` URLs.
    :return: a deep copy where each ``data:`` URL is replaced by ``[IMAGE_PLACEHOLDER: <mime>, ~<size> KB]``.
    """
    result = []
    for msg in messages:
        m = copy.deepcopy(msg)
        content = m.get('content')
        if isinstance(content, list):
            for block in content:
                if block.get('type') == 'image_url':
                    url = block.get('image_url', {}).get('url', '')
                    if url.startswith('data:'):
                        mime = url.split(';')[0].replace('data:', '') if ';' in url else 'image'
                        b64 = url.split(',', 1)[-1] if ',' in url else ''
                        kb = len(b64) * 3 // (4 * 1024)
                        block['image_url']['url'] = f'[IMAGE_PLACEHOLDER: {mime}, ~{kb} KB]'
        result.append(m)
    return result


# Caps the analyses running at once: building the messages is CPU-bound and each run holds a model stream.
_analyze_semaphore = asyncio.Semaphore(MAX_CONCURRENT_ANALYSES)


def _public_error_chunk(event: Dict[str, Any]) -> str:
    """The error event without its technical ``detail`` (the browser gets the user message only).

    :param event: a parsed ``{'type': 'error', …}`` event of the model stream.
    :return: the SSE line to forward.
    """
    return f'data: {json.dumps({k: v for k, v in event.items() if k != "detail"})}\n\n'


async def _error_stream(message: str) -> AsyncGenerator[str, None]:
    """An SSE stream with one error event, then the end marker.

    :param message: the text the browser shows.
    """
    yield f'data: {json.dumps({"type": "error", "error": message})}\n\n'
    yield 'data: [DONE]\n\n'


def _analysis_cache_version(job_cfg: Dict[str, Any], processor: Any, processor_version: str) -> str:
    """Key part of the report cache: everything besides the files that shapes the report.

    The prompt, the model, the extraction method and the chunking and generation settings are part of it, so changing
    one of them never serves a report made with the old ones.

    :param job_cfg: the compare settings (``get_config()``)
    :param processor: the processor chosen for the file type, which carries the diff method
    :param processor_version: extraction method forced by the client, '' for the default
    :return: ``<APP_VERSION>:<hash of the settings>``
    """
    parts = [job_cfg['analysis_system_prompt'], job_cfg['analysis_endpoint'], str(getattr(processor, 'method', '')),
             processor_version, str(job_cfg['max_tokens']), str(job_cfg['thinking_budget']),
             str(job_cfg['temperature']), str(job_cfg['chunk_threshold_chars']), str(job_cfg['chunk_size_chars'])]
    return f'{APP_VERSION}:{hashlib.sha256(chr(31).join(parts).encode("utf-8")).hexdigest()[:16]}'


async def _cached_stream(cached: dict) -> AsyncGenerator[str, None]:
    """Stream a cached analysis result (chunked for UX consistency).

    The event carries nobody's identifiers: the cached report belongs to no user, so the caller saves it as a new
    result of their own.

    :param cached: a row of ``analysis_cache``.
    """
    meta = json.dumps({
        'type': 'metadata',
        'file_type': cached.get('file_type') or '',
        'method': cached.get('processing_method') or '',
        'processor_version': cached.get('processor_version') or '',
        'cached': True,
    })
    yield f'data: {meta}\n\n'
    text: str = cached['analysis_text']
    chunk_size = 150
    for i in range(0, len(text), chunk_size):
        yield f'data: {json.dumps({"type": "response.output_text.delta", "delta": text[i:i + chunk_size]})}\n\n'
    yield 'data: [DONE]\n\n'


# --- /compare/analyze ---

@dataclass
class _AnalysisJob:
    """Everything one analysis stream needs, fixed before the stream starts."""
    req_id: str                       # short id (hash prefix) in the log lines of this request
    host: str
    token: str
    endpoint: str                     # analysis model endpoint
    cfg: Dict[str, Any]               # get_config()
    processor: Any                    # file processor chosen from the file type
    processor_version: str            # method override asked by the client, '' for the default
    old_name: str
    new_name: str
    old_bytes: bytes
    new_bytes: bytes
    old_hash: str
    new_hash: str
    cache_version: str                # key of the report cache
    user_id: str = ''                 # who asked, stored with the audit row


@dataclass
class _Report:
    """The report as it is streamed: its text, and whether it may be kept for the next user with the same files."""
    parts: List[str] = field(default_factory=list)
    cacheable: bool = True


def _error_response(message: str) -> StreamingResponse:
    """An SSE response that carries one error, for a request refused before the stream starts.

    :param message: the text the browser shows.
    :return: the response.
    """
    return StreamingResponse(_error_stream(message), media_type='text/event-stream')


async def _read_uploads(old_file: UploadFile, new_file: UploadFile) -> Tuple[bytes, bytes]:
    """Read and validate both uploads before the stream starts, so a refusal is fast and early.

    :param old_file: the previous revision.
    :param new_file: the new revision.
    :return: the content of both files.
    :raises ValueError: when a file is empty, too large or of an unsupported type.
    """
    old_bytes = await old_file.read()
    _validate_file(old_file, old_bytes)
    new_bytes = await new_file.read()
    _validate_file(new_file, new_bytes)
    return old_bytes, new_bytes


async def _acquire_slot(req_id: str) -> AsyncGenerator[str, None]:
    """Wait for a free analysis slot, yielding a keepalive every 15 s so the gateway keeps the connection open.

    The slot is held when the generator ends; the caller releases ``_analyze_semaphore``.

    :param req_id: short request id for the log.
    """
    wait_start = asyncio.get_event_loop().time()
    while True:
        try:
            await asyncio.wait_for(_analyze_semaphore.acquire(), timeout=15.0)
            return
        except asyncio.TimeoutError:
            waited = asyncio.get_event_loop().time() - wait_start
            logger.warning(f'[{req_id}] Semaphore wait {waited:.0f}s — all slots busy')
            yield ': keepalive\n\n'


def _release_slot_when_idle(build_task: asyncio.Future | None) -> None:
    """Free the analysis slot, once the thread that builds the messages has ended.

    A browser that leaves does not stop that thread, so the slot stays taken until it returns.

    :param build_task: the message-building task, None when it never started.
    """
    if build_task is None or build_task.done():
        _analyze_semaphore.release()
        return

    def _release(task: asyncio.Future) -> None:
        """Release the slot and mark the task's error as seen.

        :param task: the finished building task.
        """
        if not task.cancelled():
            task.exception()
        _analyze_semaphore.release()

    build_task.add_done_callback(_release)


async def _keepalive_until_done(task: asyncio.Future, req_id: str) -> AsyncGenerator[str, None]:
    """Yield a keepalive every 10 s until ``task`` (the CPU-bound message building) is done.

    :param task: the running build task; its result is read by the caller.
    :param req_id: short request id for the log.
    """
    build_start = asyncio.get_event_loop().time()
    while True:
        done, _ = await asyncio.wait({task}, timeout=10.0)
        if done:
            return
        logger.info(f'[{req_id}] Building messages… {asyncio.get_event_loop().time() - build_start:.0f}s')
        yield ': keepalive\n\n'


def _track_llm_event(chunk: str, job: _AnalysisJob, run: Dict[str, Any], llm_request_id: int | None,
                     t_llm: float, report: _Report) -> str:
    """Record what an SSE chunk from the model says (first token, usage, error) and return the chunk to forward.

    :param chunk: one SSE line from the model stream.
    :param job: the request being served.
    :param run: outcome and timings of the run, updated in place (``status``, ``first_token_ms``).
    :param llm_request_id: id of the ``llm_requests`` row, None if it was not saved.
    :param t_llm: ``time.monotonic()`` when the model stream started.
    :param report: the report being streamed, which collects the text and notes whether it can be cached.
    :return: ``chunk``, or its public version (without the technical ``detail``) for an error event.
    """
    if not chunk.startswith('data: ') or '[DONE]' in chunk:
        return chunk
    try:
        event = json.loads(chunk[6:].strip())
        etype = event.get('type')
        if etype == 'response.output_text.delta':
            report.parts.append(event.get('delta', ''))
            if 'first_token_ms' not in run:
                run['first_token_ms'] = _elapsed_ms(t_llm)
        elif etype == 'warning':
            report.cacheable = False  # a cut or partial report is never replayed
        elif etype == 'usage':
            spawn(update_llm_request_usage(
                llm_request_id,
                input_tokens=event.get('input_tokens', 0),
                output_tokens=event.get('output_tokens', 0),
                thinking_tokens=event.get('thinking_tokens', 0),
                total_tokens=event.get('total_tokens', 0),
                cost_eur=event.get('cost_eur', 0.0),
                http_status=200,
            ))
        elif etype == 'error':
            report.cacheable = False
            run['status'] = 'error'
            detail = event.get('detail') or event.get('error', '')
            chunk = _public_error_chunk(event)
            store_error(
                endpoint='/api/compare/analyze',
                error_type=event.get('error_type', 'LLMError'),
                error_msg=detail,
                old_filename=job.old_name,
                new_filename=job.new_name,
                file_type=get_extension(job.old_name),
                llm_request_id=llm_request_id,
                stage='llm',
                http_status=event.get('http_status') or None,
                upstream=job.endpoint,
                context={'chunked_parts': run['chunked_parts'], 'elapsed_ms': _elapsed_ms(t_llm)},
            )
            spawn(update_llm_request_usage(
                llm_request_id,
                http_status=event.get('http_status', 0),
                error_type=event.get('error_type', 'LLMError'),
                error_msg=detail,
            ))
    except Exception:
        pass
    return chunk


def _elapsed_ms(t0: float) -> int:
    """Milliseconds since ``t0``.

    :param t0: a ``time.monotonic()`` reading.
    """
    return int((time.monotonic() - t0) * 1000)


async def _analysis_events(job: _AnalysisJob, request: Request) -> AsyncGenerator[str, None]:
    """The SSE stream of one analysis: keepalives while it waits and builds, then metadata, warnings and the model's
    answer. The outcome and the step durations are saved on the ``llm_requests`` row at the end.

    :param job: the request being served.
    :param request: the incoming request, to notice a browser that left.
    """
    req_id = job.req_id
    logger.info(f'[{req_id}] SSE stream start: {job.old_name!r} vs {job.new_name!r}')

    yield ': keepalive\n\n'

    # Outcome and step durations, saved on the llm_requests row at the end (finally).
    run: Dict[str, Any] = {'status': 'aborted', 'chunked_parts': 0}
    # Filled by the stream: the endpoint(s) that answered, which may be a fallback.
    answered: Dict[str, Any] = {}
    t_start = time.monotonic()

    async for keepalive in _acquire_slot(req_id):
        yield keepalive
    run['queue_wait_ms'] = _elapsed_ms(t_start)

    llm_request_id = None
    report = _Report()
    build_task: asyncio.Future | None = None

    try:
        # Building the messages is CPU-bound: it runs in a thread while keepalives hold the connection.
        build_task = asyncio.ensure_future(
            asyncio.to_thread(
                job.processor.build_messages,
                job.old_bytes, job.old_name, job.new_bytes, job.new_name, job.cfg['analysis_system_prompt'],
            )
        )
        t_build = time.monotonic()
        async for keepalive in _keepalive_until_done(build_task, req_id):
            yield keepalive

        run['build_ms'] = _elapsed_ms(t_build)
        try:
            result = build_task.result()
        except Exception as e:
            logger.error(f'[{req_id}] Processor failed: {e}', exc_info=True)
            run['status'] = 'error'
            store_error(
                endpoint='/api/compare/analyze',
                error_type=type(e).__name__,
                error_msg=f'File processing failed: {e}',
                old_filename=job.old_name, new_filename=job.new_name,
                file_type=get_extension(job.old_name),
                stack_trace=traceback.format_exc(),
                llm_request_id=llm_request_id,
                stage='build',
                context={'processor_version': job.processor_version, 'build_ms': run['build_ms']},
            )
            yield f'data: {json.dumps({"type": "error", "error": f"File processing failed: {e}"})}\n\n'
            yield 'data: [DONE]\n\n'
            return

        # Store sanitized LLM request for audit; await to get the ID for error linkage
        llm_request_id = await store_llm_request(
            old_file_hash=job.old_hash.strip(),
            new_file_hash=job.new_hash.strip(),
            file_type=result.metadata.file_type,
            endpoint_name=job.endpoint,
            messages_json=json.dumps(_sanitize_messages(result.messages), ensure_ascii=False),
            user_id=job.user_id,
        )

        # Large diffs are split into per-section chunks (map-reduce); one model call per chunk.
        message_parts = None
        if result.metadata.method in ('standard', 'structured'):
            message_parts = split_messages_for_chunking(
                result.messages, job.cfg['chunk_threshold_chars'], job.cfg['chunk_size_chars'],
            )

        meta_event = json.dumps({
            'type': 'metadata',
            'file_type': result.metadata.file_type,
            'method': result.metadata.method,
            'processor_version': job.processor_version,
            'llm_request_id': llm_request_id,
            'chunked': len(message_parts) if message_parts else 0,
        })
        yield f'data: {meta_event}\n\n'

        # Extraction-quality warnings (e.g. scanned PDF with no text layer)
        # — without this the user gets a silent, misleading "no changes".
        warnings = result.warnings + diff_truncation_warnings(result.messages)
        report.cacheable = not warnings  # the warning is not part of the text: a replay would lose it
        for w in warnings:
            yield f'data: {json.dumps({"type": "warning", "warning": "extraction", "detail": w})}\n\n'

        if result.image_pairs:
            yield f'data: {json.dumps({"type": "image_context", "images": result.image_pairs})}\n\n'

        run['chunked_parts'] = len(message_parts) if message_parts else 0
        t_llm = time.monotonic()
        if message_parts:
            logger.info(f'[{req_id}] LLM streaming start (chunked ×{len(message_parts)})')
            llm_stream = stream_analysis_chunked(
                job.host, job.token, job.endpoint, message_parts,
                job.cfg['max_tokens'], job.cfg['thinking_budget'], job.cfg['temperature'],
                structured=(result.metadata.method == 'structured'), info=answered,
            )
        else:
            logger.info(f'[{req_id}] LLM streaming start')
            llm_stream = stream_analysis(
                job.host, job.token, job.endpoint, result.messages,
                job.cfg['max_tokens'], job.cfg['thinking_budget'], job.cfg['temperature'], info=answered,
            )
        run['status'] = 'ok'
        async for chunk in llm_stream:
            if await request.is_disconnected():
                logger.warning(f'[{req_id}] Client disconnected during LLM streaming — aborting')
                run['status'] = 'aborted'
                return
            yield _track_llm_event(chunk, job, run, llm_request_id, t_llm, report)

        run['generation_ms'] = _elapsed_ms(t_llm)
        if run['status'] == 'ok' and report.cacheable and report.parts:
            spawn(store_analysis_cache(
                job.old_hash, job.new_hash, job.cache_version, ''.join(report.parts), result.metadata.file_type,
                result.metadata.method, job.processor_version))
        logger.info(f'[{req_id}] SSE stream complete')

    except (asyncio.CancelledError, GeneratorExit):
        run['status'] = 'aborted'      # the browser left
        raise
    except Exception as e:
        logger.error(f'[{req_id}] Unhandled error in SSE stream: {e}', exc_info=True)
        run['status'] = 'error'
        store_error(
            endpoint='/api/compare/analyze',
            error_type=type(e).__name__,
            error_msg=str(e),
            old_filename=job.old_name, new_filename=job.new_name,
            file_type=get_extension(job.old_name),
            stack_trace=traceback.format_exc(),
            llm_request_id=llm_request_id,
            stage='stream',
        )
        try:
            yield f'data: {json.dumps({"type": "error", "error": TIRED_MESSAGE})}\n\n'
            yield 'data: [DONE]\n\n'
        except Exception:
            pass  # client already gone
    finally:
        _release_slot_when_idle(build_task)
        if llm_request_id:
            spawn(update_llm_request_run(llm_request_id, total_ms=_elapsed_ms(t_start), **run))
            if answered.get('endpoint'):
                spawn(update_llm_request_usage(llm_request_id, endpoint_name=answered['endpoint']))


@router.post('/compare/analyze', dependencies=[Depends(require_compare), Depends(rate_limit)])
async def analyze_documents(
    request: Request,
    old_file: UploadFile = File(...),
    new_file: UploadFile = File(...),
    old_file_hash: str = Form(''),
    new_file_hash: str = Form(''),
    force_refresh: str = Form('false'),
    processor_version: str = Form(''),
) -> StreamingResponse:
    """Stream the analysis of two revisions of a document as server-sent events.

    A request that cannot start (settings, credentials, files, file types) is refused with one SSE error. A pair already
    analysed is replayed from the history; otherwise the files are processed and the model's answer is streamed.

    :param request: the incoming request, for the Databricks credentials and to notice a browser that left.
    :param old_file: the previous revision.
    :param new_file: the new revision.
    :param old_file_hash: ignored, kept for older clients: the hashes are computed from the uploaded bytes.
    :param new_file_hash: ignored, kept for older clients.
    :param force_refresh: ``'true'`` to skip the cached analysis.
    :param processor_version: extraction method forced by the client, '' for the default.
    :return: the SSE response.
    """
    try:
        cfg = get_config()
    except ValueError as e:
        return _error_response(str(e))

    endpoint = cfg['analysis_endpoint']
    if not endpoint:
        return _error_response('COMPARE_ANALYSIS_ENDPOINT not configured in app.yaml')

    host, token = get_credentials(request)
    if not host or not token:
        return _error_response('Missing Databricks credentials')

    try:
        old_bytes, new_bytes = await _read_uploads(old_file, new_file)
    except Exception as e:
        return _error_response(str(e))

    old_name = old_file.filename or 'old_document'
    new_name = new_file.filename or 'new_document'

    # A mixed pair (PDF against DOCX) is rejected here, before any extraction starts.
    if file_type_of(old_name) != file_type_of(new_name):
        return _error_response(
            f'The two documents must be of the same type ("{get_extension(old_name)}" vs '
            f'"{get_extension(new_name)}"). Convert one of them first.'
        )

    # Hashes come from the uploaded bytes, not the form fields: a stale client hash would replay another pair's cached analysis.
    old_hash = hashlib.sha256(old_bytes).hexdigest()
    new_hash = hashlib.sha256(new_bytes).hexdigest()

    pv = processor_version.strip()
    processor = get_processor(old_name, method_override=pv or None)
    if not processor:
        return _error_response(f'Unsupported file type: "{get_extension(old_name)}"')

    cache_version = _analysis_cache_version(cfg, processor, pv)
    cached = None if force_refresh.lower() == 'true' else await get_cached_analysis(old_hash, new_hash, cache_version)
    if cached:
        logger.info(f'Cache hit for hashes {old_hash[:12]}…/{new_hash[:12]}…')
        return StreamingResponse(_cached_stream(cached), media_type='text/event-stream', headers=_SSE_HEADERS)

    job = _AnalysisJob(
        req_id=old_hash[:8] or 'nohash', host=host, token=token, endpoint=endpoint, cfg=cfg, processor=processor,
        processor_version=pv, old_name=old_name, new_name=new_name, old_bytes=old_bytes, new_bytes=new_bytes,
        old_hash=old_hash, new_hash=new_hash, cache_version=cache_version,
        user_id=(await get_user_identity(request))['user_id'] or '',
    )
    # The response is returned at once so the SSE connection exists before any slow work; keepalive comments
    # (':keepalive') carry it through the gateway during long waits.
    return StreamingResponse(_analysis_events(job, request), media_type='text/event-stream', headers=_SSE_HEADERS)
