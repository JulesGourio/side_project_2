"""POST /compare/analyze: stream the LLM analysis of two documents, with result cache and run audit."""

import asyncio
import copy
import hashlib
import io
import json
import logging
import os
import time
import traceback
from datetime import datetime
from typing import Any, Dict

from databricks.sdk import WorkspaceClient
from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import StreamingResponse

from ...services.compare.chunked_analysis import split_messages_for_chunking, stream_analysis_chunked
from ...services.compare.processors.factory import (
    diff_truncation_warnings, file_type_of, get_extension, get_processor,
)
from ...services.infra.credentials import get_credentials
from ...services.infra.lakebase import (
    get_pool, store_error,
    store_llm_request,
    update_llm_request_run, update_llm_request_usage,
)
from ...services.infra.streaming import stream_analysis
from ...services.infra.user import require_compare
from .common import (
    APP_VERSION, _get_config, _sanitize_filename, _validate_file,
)

logger = logging.getLogger(__name__)
router = APIRouter()


def _sanitize_messages(messages: list) -> list:
    """Replace base64 image data with readable placeholders for audit storage."""
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

_analyze_semaphore = asyncio.Semaphore(int(os.getenv('COMPARE_MAX_CONCURRENT', '5')))


async def _error_stream(message: str):
    yield f'data: {json.dumps({"type": "error", "error": message})}\n\n'
    yield 'data: [DONE]\n\n'


async def _get_cached_analysis(old_hash: str, new_hash: str, processor_version: str = '') -> dict | None:
    """Return an existing messages row if hashes, app_version, and processor_version match."""
    if not old_hash or not new_hash:
        return None
    pool = get_pool()
    if not pool:
        return None
    try:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                '''
                SELECT id, analysis_text, file_type, processing_method, processor_version,
                       volume_session_path
                FROM messages
                WHERE old_file_hash     = $1
                  AND new_file_hash     = $2
                  AND app_version       = $3
                  AND COALESCE(processor_version, '') = $4
                  AND analysis_text IS NOT NULL
                  AND analysis_text != ''
                ORDER BY created_at DESC
                LIMIT 1
                ''',
                old_hash, new_hash, APP_VERSION, processor_version,
            )
        return dict(row) if row else None
    except Exception as e:
        logger.debug(f'Cache lookup failed: {e}')
        return None


async def _cached_stream(cached: dict):
    """Stream a cached analysis result (chunked for UX consistency)."""
    meta = json.dumps({
        'type': 'metadata',
        'file_type': cached.get('file_type') or '',
        'method': cached.get('processing_method') or '',
        'processor_version': cached.get('processor_version') or '',
        'cached': True,
        'message_id': cached['id'],
        'session_path': cached.get('volume_session_path') or '',
    })
    yield f'data: {meta}\n\n'
    text: str = cached['analysis_text']
    chunk_size = 150
    for i in range(0, len(text), chunk_size):
        yield f'data: {json.dumps({"type": "response.output_text.delta", "delta": text[i:i + chunk_size]})}\n\n'
    yield 'data: [DONE]\n\n'


# --- /compare/analyze ---

@router.post('/compare/analyze', dependencies=[Depends(require_compare)])
async def analyze_documents(
    request: Request,
    old_file: UploadFile = File(...),
    new_file: UploadFile = File(...),
    old_file_hash: str = Form(''),
    new_file_hash: str = Form(''),
    force_refresh: str = Form('false'),
    processor_version: str = Form(''),
):
    try:
        cfg = _get_config()
    except ValueError as e:
        return StreamingResponse(_error_stream(str(e)), media_type='text/event-stream')

    endpoint = cfg['analysis_endpoint']
    if not endpoint:
        return StreamingResponse(
            _error_stream('COMPARE_ANALYSIS_ENDPOINT not configured in app.yaml'),
            media_type='text/event-stream',
        )

    host, token = get_credentials(request)
    if not host or not token:
        return StreamingResponse(
            _error_stream('Missing Databricks credentials'),
            media_type='text/event-stream',
        )

    # ── Read & validate files before starting the generator (fast, early error) ──
    try:
        old_bytes = await old_file.read()
        _validate_file(old_file, old_bytes)
        new_bytes = await new_file.read()
        _validate_file(new_file, new_bytes)
    except Exception as e:
        return StreamingResponse(_error_stream(str(e)), media_type='text/event-stream')

    old_name = old_file.filename or 'old_document'
    new_name = new_file.filename or 'new_document'

    # A mixed pair (PDF against DOCX) is rejected here, before any extraction starts.
    if file_type_of(old_name) != file_type_of(new_name):
        return StreamingResponse(
            _error_stream(
                f'The two documents must be of the same type ("{get_extension(old_name)}" vs '
                f'"{get_extension(new_name)}"). Convert one of them first.'
            ),
            media_type='text/event-stream',
        )

    # Hashes are computed here from the uploaded bytes (same SHA-256 hex the
    # client sends) instead of trusting the form fields: a stale or wrong
    # client hash would replay another file pair's cached analysis.
    old_file_hash = hashlib.sha256(old_bytes).hexdigest()
    new_file_hash = hashlib.sha256(new_bytes).hexdigest()

    # ── Cache hit: return stored analysis if both hashes match ────────────────
    _pv = processor_version.strip()
    cached = None if force_refresh.lower() == 'true' else await _get_cached_analysis(old_file_hash, new_file_hash, _pv)
    if cached:
        logger.info(f'Cache hit for hashes {old_file_hash[:12]}…/{new_file_hash[:12]}… → messages.id={cached["id"]}')
        return StreamingResponse(
            _cached_stream(cached),
            media_type='text/event-stream',
            headers={'Cache-Control': 'no-cache', 'Connection': 'keep-alive', 'X-Accel-Buffering': 'no'},
        )

    processor = get_processor(old_name, method_override=_pv or None)
    if not processor:
        return StreamingResponse(
            _error_stream(f'Unsupported file type: "{get_extension(old_name)}"'),
            media_type='text/event-stream',
        )

    volume_path = cfg['volume_path'].rstrip('/')
    if volume_path and not volume_path.startswith('TODO'):
        session_id = datetime.now().strftime('%Y%m%d_%H%M%S%f')
        upload_dir = f'{volume_path}/.tmp/{session_id}'

        async def _upload(ob: bytes, nb: bytes) -> None:
            try:
                w = WorkspaceClient()
                await asyncio.to_thread(w.files.create_directory, upload_dir)
                await asyncio.to_thread(w.files.upload, f'{upload_dir}/old_{_sanitize_filename(old_name)}', io.BytesIO(ob), overwrite=True)
                await asyncio.to_thread(w.files.upload, f'{upload_dir}/new_{_sanitize_filename(new_name)}', io.BytesIO(nb), overwrite=True)
            except Exception as upload_err:
                logger.warning(f'Background volume upload failed: {upload_err}')

        asyncio.create_task(_upload(old_bytes, new_bytes))

    # Return the StreamingResponse immediately so the SSE connection exists before any slow work; keepalive comments
    # (':keepalive') carry it through the gateway during long waits.
    _req_id = (old_file_hash or '')[:8] or 'nohash'

    async def _stream():
        logger.info(f'[{_req_id}] SSE stream start: {old_name!r} vs {new_name!r}')

        yield ': keepalive\n\n'

        # Outcome and step durations, saved on the llm_requests row at the end (finally).
        run: Dict[str, Any] = {'status': 'aborted', 'chunked_parts': 0}
        t_start = time.monotonic()

        def _ms(t0: float) -> int:
            return int((time.monotonic() - t0) * 1000)

        # ── Wait for a processing slot ────────────────────────────────────────
        _wait_start = asyncio.get_event_loop().time()
        while True:
            try:
                await asyncio.wait_for(_analyze_semaphore.acquire(), timeout=15.0)
                break
            except asyncio.TimeoutError:
                waited = asyncio.get_event_loop().time() - _wait_start
                logger.warning(f'[{_req_id}] Semaphore wait {waited:.0f}s — all slots busy')
                yield ': keepalive\n\n'
        run['queue_wait_ms'] = _ms(t_start)

        llm_request_id = None

        try:
            # ── Build messages (CPU-bound) ────────────────────────────────────
            build_task = asyncio.ensure_future(
                asyncio.to_thread(
                    processor.build_messages,
                    old_bytes, old_name, new_bytes, new_name, cfg['analysis_system_prompt'],
                )
            )
            _build_start = asyncio.get_event_loop().time()
            t_build = time.monotonic()
            while True:
                done, _ = await asyncio.wait({build_task}, timeout=10.0)
                if done:
                    break
                elapsed = asyncio.get_event_loop().time() - _build_start
                logger.info(f'[{_req_id}] Building messages… {elapsed:.0f}s')
                yield ': keepalive\n\n'

            run['build_ms'] = _ms(t_build)
            try:
                result = build_task.result()
            except Exception as e:
                logger.error(f'[{_req_id}] Processor failed: {e}', exc_info=True)
                run['status'] = 'error'
                store_error(
                    endpoint='/api/compare/analyze',
                    error_type=type(e).__name__,
                    error_msg=f'File processing failed: {e}',
                    old_filename=old_name, new_filename=new_name,
                    file_type=get_extension(old_name),
                    stack_trace=traceback.format_exc(),
                    llm_request_id=llm_request_id,
                    stage='build',
                    context={'processor_version': _pv, 'build_ms': run['build_ms']},
                )
                yield f'data: {json.dumps({"type": "error", "error": f"File processing failed: {e}"})}\n\n'
                yield 'data: [DONE]\n\n'
                return

            # Store sanitized LLM request for audit; await to get the ID for error linkage
            llm_request_id = await store_llm_request(
                old_file_hash=old_file_hash.strip(),
                new_file_hash=new_file_hash.strip(),
                file_type=result.metadata.file_type,
                endpoint_name=endpoint,
                messages_json=json.dumps(_sanitize_messages(result.messages), ensure_ascii=False),
            )

            # ── Large diffs: split into per-section chunks (map-reduce) ──────
            message_parts = None
            if result.metadata.method in ('standard', 'structured'):
                message_parts = split_messages_for_chunking(
                    result.messages, cfg['chunk_threshold_chars'], cfg['chunk_size_chars'],
                )

            meta_event = json.dumps({
                'type': 'metadata',
                'file_type': result.metadata.file_type,
                'method': result.metadata.method,
                'processor_version': _pv,
                'llm_request_id': llm_request_id,
                'chunked': len(message_parts) if message_parts else 0,
            })
            yield f'data: {meta_event}\n\n'

            # Extraction-quality warnings (e.g. scanned PDF with no text layer)
            # — without this the user gets a silent, misleading "no changes".
            for w in result.warnings + diff_truncation_warnings(result.messages):
                yield f'data: {json.dumps({"type": "warning", "warning": "extraction", "detail": w})}\n\n'

            if result.image_pairs:
                yield f'data: {json.dumps({"type": "image_context", "images": result.image_pairs})}\n\n'

            # ── Stream LLM response ───────────────────────────────────────────
            run['chunked_parts'] = len(message_parts) if message_parts else 0
            t_llm = time.monotonic()
            if message_parts:
                logger.info(f'[{_req_id}] LLM streaming start (chunked ×{len(message_parts)})')
                llm_stream = stream_analysis_chunked(
                    host, token, endpoint, message_parts,
                    cfg['max_tokens'], cfg['thinking_budget'], cfg['temperature'],
                    structured=(result.metadata.method == 'structured'),
                )
            else:
                logger.info(f'[{_req_id}] LLM streaming start')
                llm_stream = stream_analysis(
                    host, token, endpoint, result.messages,
                    cfg['max_tokens'], cfg['thinking_budget'], cfg['temperature'],
                )
            run['status'] = 'ok'
            async for chunk in llm_stream:
                if await request.is_disconnected():
                    logger.warning(f'[{_req_id}] Client disconnected during LLM streaming — aborting')
                    run['status'] = 'aborted'
                    return
                # Intercept usage and error events to track in DB
                if chunk.startswith('data: ') and '[DONE]' not in chunk:
                    try:
                        event = json.loads(chunk[6:].strip())
                        etype = event.get('type')
                        if etype == 'response.output_text.delta' and 'first_token_ms' not in run:
                            run['first_token_ms'] = _ms(t_llm)
                        elif etype == 'usage':
                            asyncio.create_task(update_llm_request_usage(
                                llm_request_id,
                                input_tokens=event.get('input_tokens', 0),
                                output_tokens=event.get('output_tokens', 0),
                                thinking_tokens=event.get('thinking_tokens', 0),
                                total_tokens=event.get('total_tokens', 0),
                                cost_eur=event.get('cost_eur', 0.0),
                                http_status=200,
                            ))
                        elif etype == 'error':
                            run['status'] = 'error'
                            store_error(
                                endpoint='/api/compare/analyze',
                                error_type=event.get('error_type', 'LLMError'),
                                error_msg=event.get('error', ''),
                                old_filename=old_name,
                                new_filename=new_name,
                                file_type=get_extension(old_name),
                                llm_request_id=llm_request_id,
                                stage='llm',
                                http_status=event.get('http_status') or None,
                                upstream=endpoint,
                                context={'chunked_parts': run['chunked_parts'],
                                         'elapsed_ms': _ms(t_llm)},
                            )
                            asyncio.create_task(update_llm_request_usage(
                                llm_request_id,
                                http_status=event.get('http_status', 0),
                                error_type=event.get('error_type', 'LLMError'),
                                error_msg=event.get('error', ''),
                            ))
                    except Exception:
                        pass
                yield chunk

            run['generation_ms'] = _ms(t_llm)
            logger.info(f'[{_req_id}] SSE stream complete')

        except (asyncio.CancelledError, GeneratorExit):
            run['status'] = 'aborted'      # the browser left
            raise
        except Exception as e:
            logger.error(f'[{_req_id}] Unhandled error in SSE stream: {e}', exc_info=True)
            run['status'] = 'error'
            store_error(
                endpoint='/api/compare/analyze',
                error_type=type(e).__name__,
                error_msg=str(e),
                old_filename=old_name, new_filename=new_name,
                file_type=get_extension(old_name),
                stack_trace=traceback.format_exc(),
                llm_request_id=llm_request_id,
                stage='stream',
            )
            try:
                yield f'data: {json.dumps({"type": "error", "error": f"Internal error: {e}"})}\n\n'
                yield 'data: [DONE]\n\n'
            except Exception:
                pass  # client already gone
        finally:
            _analyze_semaphore.release()
            if llm_request_id:
                asyncio.create_task(update_llm_request_run(llm_request_id, total_ms=_ms(t_start), **run))

    return StreamingResponse(
        _stream(),
        media_type='text/event-stream',
        headers={'Cache-Control': 'no-cache', 'Connection': 'keep-alive', 'X-Accel-Buffering': 'no'},
    )
