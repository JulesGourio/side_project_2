"""Compare router — thin HTTP layer for document diff and impact analysis.

All heavy lifting (streaming, LLM calls, file processing) lives in services/.
"""

import asyncio
import base64
import copy
import hashlib
import io
import json
import logging
import mimetypes
import os
import posixpath
import re
import time
import traceback
from datetime import datetime
from typing import Any, Dict, List

from databricks.sdk import WorkspaceClient
from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

from ..services.chunked_analysis import split_messages_for_chunking, stream_analysis_chunked
from ..services.impact_queries import changes_to_queries
from ..services.lakebase import (
    get_cached_impact_result, get_cached_summary, get_pool, store_error,
    store_impact_cache, store_impact_request, store_llm_request,
    store_summary_cache, update_llm_request_usage,
)
from ..services.processors.factory import (
    EXTENSION_MAP, SUPPORTED_EXTENSIONS, diff_truncation_warnings, extract_document_text,
    file_type_of, get_extension, get_processor, prepare_image_content,
)
from ..services.streaming import stream_analysis
from ..services.summarize import summarize_image, summarize_text
from ..services.user import get_user_identity, require_compare
from ..services.vector_search import run_impact_search, sort_documents


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


APP_VERSION = os.getenv('APP_VERSION', '2')

_analyze_semaphore = asyncio.Semaphore(int(os.getenv('COMPARE_MAX_CONCURRENT', '5')))

# Default system prompt — for MODIFIED/REMOVED/ADDED inline markup format.
# Used when COMPARE_ANALYSIS_SYSTEM_PROMPT is not set.
_DEFAULT_ANALYSIS_PROMPT = """\
You are Qualibot, an expert document comparison analyst specialising in technical, regulatory, and quality-management documentation.

Report every substantive change. Discard everything that does not affect what someone must do, know, or comply with.

The diff is provided paragraph-by-paragraph:
- ADDED: Completely new text.
- REMOVED: Completely deleted text.
- MODIFIED: Text that changed. Pay close attention to the inline markup: ~~crossed out text~~ means it was removed, and **bold text** means it was added.

REPORT:
- Requirements, specifications, constraints, or obligations that are added, deleted, or changed in substance
- Numerical values that changed (tolerances, limits, thresholds, criteria, quantities, durations)
- Changes between shall/must/will/should/may/can
- Referenced standards whose revision changed, were added, or removed
- Scope changes: who is affected, what is covered, when or where it applies
- Changed procedures, test methods, inspection steps, safety notes

IGNORE:
- Whitespace, line breaks, punctuation, capitalisation with no meaning change
- Reference code formatting when same revision is cited (NAS 410 vs NAS410 = formatting only)
- Synonyms and restructuring that preserve identical meaning
- Document metadata (revision history, approval pages, dates, boilerplate)

OUTPUT: Markdown, ## per section, one bullet per change. Bold critical values. If nothing substantive changed: **No significant changes detected.**\
"""
# app.yaml declares MAX_COMPARE_PDF_MB (the historical name); both are honoured.
MAX_FILE_BYTES = int(os.getenv('MAX_COMPARE_FILE_MB') or os.getenv('MAX_COMPARE_PDF_MB') or '20') * 1024 * 1024


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def _get_config() -> Dict[str, Any]:
    if os.getenv('COMPARE_ENABLED', 'true').lower() != 'true':
        raise ValueError('Compare feature is not enabled')
    return {
        'analysis_endpoint':     os.getenv('COMPARE_ANALYSIS_ENDPOINT', ''),
        'analysis_system_prompt': os.getenv('COMPARE_ANALYSIS_SYSTEM_PROMPT', _DEFAULT_ANALYSIS_PROMPT),
        # Impact search: one Vector Search query per change, then one LLM call
        # per candidate document judges it from all of its retrieved passages.
        'impact_index':          os.getenv('COMPARE_IMPACT_INDEX', ''),
        'impact_endpoint':       os.getenv('COMPARE_IMPACT_ENDPOINT', os.getenv('COMPARE_ANALYSIS_ENDPOINT', '')),
        'impact_max_queries':    int(os.getenv('COMPARE_IMPACT_MAX_QUERIES', '30')),
        'impact_per_query_results': int(os.getenv('COMPARE_IMPACT_PER_QUERY_RESULTS', '40')),
        'impact_max_candidates': int(os.getenv('COMPARE_IMPACT_MAX_CANDIDATES', '15')),
        'impact_max_query_chars': int(os.getenv('COMPARE_IMPACT_MAX_QUERY_CHARS', '20000')),
        'impact_max_tokens':     int(os.getenv('COMPARE_IMPACT_MAX_TOKENS', '1500')),
        # Documents published before this date are badged "archive" in the UI.
        'impact_archive_before': os.getenv('COMPARE_IMPACT_ARCHIVE_BEFORE', '2018-01-01'),
        # Single-document summary: independent of the diff, a cheap model
        # summarizes whichever of the two uploaded files the user picks.
        # Images need a vision-capable model, hence the separate endpoint.
        'summary_endpoint':       os.getenv('COMPARE_SUMMARY_ENDPOINT', 'databricks-gpt-5-6-luna'),
        'summary_image_endpoint': os.getenv('COMPARE_SUMMARY_IMAGE_ENDPOINT', 'databricks-gpt-5-6-luna'),
        'summary_max_chars':  int(os.getenv('COMPARE_SUMMARY_MAX_CHARS', '300000')),
        'summary_max_tokens': int(os.getenv('COMPARE_SUMMARY_MAX_TOKENS', '800')),
        'volume_path':           os.getenv('COMPARE_VOLUME_PATH', ''),
        'max_tokens':            int(os.getenv('COMPARE_MAX_TOKENS', '8192')),
        'thinking_budget':       int(os.getenv('COMPARE_THINKING_BUDGET', '4000')),
        'temperature':           float(os.getenv('COMPARE_TEMPERATURE', '0.0')),
        # Map-reduce for very large diffs: split at section boundaries when the
        # TEXT CHANGES body exceeds the threshold; one LLM call per chunk.
        'chunk_threshold_chars': int(os.getenv('COMPARE_CHUNK_THRESHOLD_CHARS', '60000')),
        'chunk_size_chars':      int(os.getenv('COMPARE_CHUNK_SIZE_CHARS', '45000')),
    }


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

def _get_credentials(request: Request):
    host = os.environ.get('DATABRICKS_HOST', '').rstrip('/')
    if host and not host.startswith('http'):
        host = f'https://{host}'
    token = os.environ.get('DATABRICKS_TOKEN', '')
    if not host or not token:
        try:
            from databricks.sdk.core import Config
            cfg = Config()
            if not host:
                host = (cfg.host or '').rstrip('/')
            if not token:
                auth_value = cfg.authenticate().get('Authorization', '')
                if auth_value.startswith('Bearer '):
                    token = auth_value[len('Bearer '):]
        except Exception as e:
            logger.debug(f'SDK auth unavailable: {e}')
    if not token:
        token = request.headers.get('x-forwarded-access-token', '')
    return host, token


# ---------------------------------------------------------------------------
# File helpers
# ---------------------------------------------------------------------------

def _sanitize_filename(filename: str, fallback: str = 'document') -> str:
    base = os.path.basename((filename or '').strip())
    if not base:
        return fallback
    safe = re.sub(r'[^A-Za-z0-9._-]+', '_', base).strip('._')
    return safe or fallback


def _validate_file(file: UploadFile, data: bytes) -> None:
    if not data:
        raise ValueError(f'File "{file.filename or "unknown"}" is empty')
    if len(data) > MAX_FILE_BYTES:
        raise ValueError(f'File "{file.filename or "unknown"}" exceeds {MAX_FILE_BYTES // (1024*1024)} MB limit')
    ext = get_extension(file.filename or '')
    if ext not in SUPPORTED_EXTENSIONS:
        raise ValueError(
            f'Unsupported extension "{ext}". Supported: {", ".join(sorted(SUPPORTED_EXTENSIONS))}'
        )


def _is_within_volume(session_path: str, volume_path: str) -> bool:
    """True when session_path is the volume root or a path under it.

    A bare startswith() accepted '<volume>/../../other' and '<volume>_other':
    both leave the configured volume.
    """
    root = posixpath.normpath(volume_path)
    path = posixpath.normpath(session_path)
    return '..' not in session_path.split('/') and (path == root or path.startswith(root + '/'))


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


# ---------------------------------------------------------------------------
# /compare/analyze
# ---------------------------------------------------------------------------

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

    host, token = _get_credentials(request)
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

    # The processor is picked from the old file alone: a PDF against a DOCX used
    # to fail deep inside the extractor with an unreadable parser error.
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

    # Background upload to UC Volume
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

    # ── Return StreamingResponse immediately so the SSE connection is established
    # before any slow work (semaphore wait, CPU-bound processing, LLM call).
    # Keepalive SSE comments (':keepalive') keep the connection alive through
    # Databricks Apps' HTTP gateway during long waits.
    _req_id = (old_file_hash or '')[:8] or 'nohash'

    async def _stream():
        logger.info(f'[{_req_id}] SSE stream start: {old_name!r} vs {new_name!r}')

        # Establish SSE connection immediately — gateway sees first byte, won't time out
        yield ': keepalive\n\n'

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
            while True:
                done, _ = await asyncio.wait({build_task}, timeout=10.0)
                if done:
                    break
                elapsed = asyncio.get_event_loop().time() - _build_start
                logger.info(f'[{_req_id}] Building messages… {elapsed:.0f}s')
                yield ': keepalive\n\n'

            try:
                result = build_task.result()
            except Exception as e:
                logger.error(f'[{_req_id}] Processor failed: {e}', exc_info=True)
                asyncio.create_task(store_error(
                    endpoint='/api/compare/analyze',
                    error_type=type(e).__name__,
                    error_msg=f'File processing failed: {e}',
                    old_filename=old_name, new_filename=new_name,
                    file_type=get_extension(old_name),
                    stack_trace=traceback.format_exc(),
                    llm_request_id=llm_request_id,
                ))
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
            async for chunk in llm_stream:
                if await request.is_disconnected():
                    logger.warning(f'[{_req_id}] Client disconnected during LLM streaming — aborting')
                    return
                # Intercept usage and error events to track in DB
                if chunk.startswith('data: ') and '[DONE]' not in chunk:
                    try:
                        event = json.loads(chunk[6:].strip())
                        etype = event.get('type')
                        if etype == 'usage':
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
                            asyncio.create_task(store_error(
                                endpoint='/api/compare/analyze',
                                error_type=event.get('error_type', 'LLMError'),
                                error_msg=event.get('error', ''),
                                old_filename=old_name,
                                new_filename=new_name,
                                file_type=get_extension(old_name),
                                llm_request_id=llm_request_id,
                            ))
                            asyncio.create_task(update_llm_request_usage(
                                llm_request_id,
                                http_status=event.get('http_status', 0),
                                error_type=event.get('error_type', 'LLMError'),
                                error_msg=event.get('error', ''),
                            ))
                    except Exception:
                        pass
                yield chunk

            logger.info(f'[{_req_id}] SSE stream complete')

        except Exception as e:
            logger.error(f'[{_req_id}] Unhandled error in SSE stream: {e}', exc_info=True)
            asyncio.create_task(store_error(
                endpoint='/api/compare/analyze',
                error_type=type(e).__name__,
                error_msg=str(e),
                old_filename=old_name, new_filename=new_name,
                file_type=get_extension(old_name),
                stack_trace=traceback.format_exc(),
                llm_request_id=llm_request_id,
            ))
            try:
                yield f'data: {json.dumps({"type": "error", "error": f"Internal error: {e}"})}\n\n'
                yield 'data: [DONE]\n\n'
            except Exception:
                pass  # client already gone
        finally:
            _analyze_semaphore.release()

    return StreamingResponse(
        _stream(),
        media_type='text/event-stream',
        headers={'Cache-Control': 'no-cache', 'Connection': 'keep-alive', 'X-Accel-Buffering': 'no'},
    )


# ---------------------------------------------------------------------------
# /compare/impact
# ---------------------------------------------------------------------------

class ImpactRequest(BaseModel):
    changes_text: str
    # File hashes of the comparison that produced changes_text — link an
    # impact_requests row back to its messages row, and double as the cache
    # key for this endpoint's own result cache. Optional: a manual-mode search
    # has none (cache is then skipped).
    old_file_hash: str = ''
    new_file_hash: str = ''
    # Names of the compared files: the compared document itself is always the
    # top hit, so it is dropped from the candidates.
    old_file_name: str = ''
    new_file_name: str = ''
    # "Re-run" sets this to bypass the cache and force a fresh judgment.
    force_refresh: bool = False


def _impact_cache_version(changes_text: str, cfg: Dict[str, Any]) -> str:
    """Cache discriminator stored in impact_cache.app_version.

    The file hashes alone are not the input of an impact search: the same pair
    gives a different change list from the Change Table than from the Change
    Summary, and a different result once COMPARE_IMPACT_INDEX points at another
    index. Keyed on the hashes only, the first search was replayed for all of
    them (2026-10-04).
    """
    parts = [changes_text.strip(), cfg['impact_index'], cfg['impact_endpoint'],
             str(cfg['impact_max_queries']), str(cfg['impact_per_query_results']),
             str(cfg['impact_max_candidates'])]
    return f'{APP_VERSION}:{hashlib.sha256(chr(31).join(parts).encode("utf-8")).hexdigest()[:16]}'


def _ndjson(event: Dict[str, Any]) -> str:
    return json.dumps(event, ensure_ascii=False) + '\n'


def _replay_events(result: Dict[str, Any]):
    """Re-emit a cached result as the same event stream a fresh search produces."""
    plan = {k: v for k, v in result.items() if k not in ('documents', 'usage', 'duration_s', 'impact_request_id')}
    yield _ndjson({**plan, 'type': 'plan', 'cached': True})
    for doc in result.get('documents', []):
        yield _ndjson({'type': 'document', 'document': doc})
    yield _ndjson({'type': 'done', 'usage': result.get('usage', {}), 'duration_s': result.get('duration_s', 0.0),
                   'impact_request_id': result.get('impact_request_id'), 'cached': True})


@router.post('/compare/impact', dependencies=[Depends(require_compare)])
async def find_impacted_documents(body: ImpactRequest, request: Request):
    """Stream NDJSON events: plan → document (one per judged candidate, as each finishes) → done | error."""
    try:
        cfg = _get_config()
    except ValueError as e:
        return JSONResponse({'error': str(e)}, status_code=400)

    index_name = cfg['impact_index']
    llm_endpoint = cfg['impact_endpoint']
    if not index_name or index_name.startswith('TODO'):
        return JSONResponse({'error': 'COMPARE_IMPACT_INDEX not configured in app.yaml'}, status_code=400)
    if not llm_endpoint or llm_endpoint.startswith('TODO'):
        return JSONResponse({'error': 'COMPARE_IMPACT_ENDPOINT not configured in app.yaml'}, status_code=400)

    old_hash = body.old_file_hash.strip()
    new_hash = body.new_file_hash.strip()

    cache_version = _impact_cache_version(body.changes_text, cfg)

    if not body.force_refresh:
        cached = await get_cached_impact_result(old_hash, new_hash, cache_version)
        # Results cached before the per-document redesign have no change list.
        if cached is not None and 'changes' in cached:
            return StreamingResponse(_replay_events(cached), media_type='application/x-ndjson')

    host, token = _get_credentials(request)
    if not host or not token:
        return JSONResponse({'error': 'Missing Databricks credentials'}, status_code=400)

    identity = await get_user_identity(request)
    extracted = changes_to_queries(body.changes_text, max_queries=cfg['impact_max_queries'])

    def _search(search_token: str):
        return run_impact_search(
            host=host, token=search_token, index_name=index_name, llm_endpoint=llm_endpoint,
            extracted=extracted,
            num_results=cfg['impact_per_query_results'],
            max_candidates=cfg['impact_max_candidates'],
            max_changes_chars=cfg['impact_max_query_chars'],
            max_tokens=cfg['impact_max_tokens'],
            archive_before=cfg['impact_archive_before'],
            exclude_names=[body.old_file_name, body.new_file_name],
        )

    def _audit(**kwargs):
        return store_impact_request(
            method='index_llm_per_doc',
            user_id=identity['user_id'], workspace_id=identity.get('workspace_id') or '',
            old_file_hash=old_hash, new_file_hash=new_hash,
            changes_chars=len(body.changes_text), endpoint_name=llm_endpoint, **kwargs,
        )

    async def _events():
        start = time.monotonic()
        if not extracted['queries']:
            yield _ndjson({'type': 'plan', 'changes': extracted['changes'], 'source': extracted['source'],
                           'queries_used': 0, 'queries_failed': 0, 'chunks_returned': 0, 'candidates': 0,
                           'excluded_refs': [], 'not_judged': [], 'no_changes': True})
            yield _ndjson({'type': 'done', 'duration_s': 0.0,
                           'usage': {'input_tokens': 0, 'output_tokens': 0, 'total_tokens': 0, 'cost_eur': 0.0}})
            return

        plan: Dict[str, Any] = {}
        documents: List[Dict[str, Any]] = []
        usage: Dict[str, Any] = {}
        try:
            stream = _search(token)
            try:
                first = await stream.__anext__()
            except Exception as sp_err:
                fwd = request.headers.get('x-forwarded-access-token', '')
                if not (fwd and fwd != token and '403' in str(sp_err)):
                    raise
                logger.info('impact: SP token denied on index — retrying with forwarded user token')
                stream = _search(fwd)
                first = await stream.__anext__()
            plan = {k: v for k, v in first.items() if k != 'type'}
            yield _ndjson(first)
            async for event in stream:
                if event['type'] == 'document':
                    documents.append(event['document'])
                    yield _ndjson(event)
                elif event['type'] == 'done':
                    usage = event['usage']
        except Exception as e:
            logger.error(f'Impact search failed: {e}', exc_info=True)
            asyncio.create_task(store_error(endpoint='/api/compare/impact', error_type=type(e).__name__, error_msg=str(e)))
            asyncio.create_task(_audit(duration_s=time.monotonic() - start, http_status=502,
                                       error_type=type(e).__name__, error_msg=str(e)))
            yield _ndjson({'type': 'error', 'error': f'Impact search failed: {e}'})
            return

        duration_s = round(time.monotonic() - start, 2)
        documents = sort_documents(documents)
        # Written before 'done' (a few ms): the client needs the id to attach feedback to this search.
        request_id = await _audit(
            chunks_returned=plan.get('chunks_returned', 0), num_documents=len(documents),
            duration_s=duration_s, http_status=200,
            input_tokens=usage.get('input_tokens', 0), output_tokens=usage.get('output_tokens', 0),
            total_tokens=usage.get('total_tokens', 0), cost_eur=usage.get('cost_eur', 0.0),
            documents=documents,
        )
        yield _ndjson({'type': 'done', 'usage': usage, 'duration_s': duration_s, 'impact_request_id': request_id})

        if old_hash and new_hash and not any(d.get('status') == 'error' for d in documents):
            asyncio.create_task(store_impact_cache(
                old_hash, new_hash, cache_version,
                {**plan, 'documents': documents, 'usage': usage, 'duration_s': duration_s,
                 'impact_request_id': request_id},
            ))

    return StreamingResponse(_events(), media_type='application/x-ndjson')


# ---------------------------------------------------------------------------
# /compare/summarize — single-document summary, independent of the diff
# ---------------------------------------------------------------------------

@router.post('/compare/summarize', dependencies=[Depends(require_compare)])
async def summarize_document(
    request: Request,
    file: UploadFile = File(...),
    file_hash: str = Form(''),
    force_refresh: str = Form('false'),
):
    try:
        cfg = _get_config()
    except ValueError as e:
        return JSONResponse({'error': str(e)}, status_code=400)

    force = force_refresh.lower() == 'true'

    data = await file.read()
    try:
        _validate_file(file, data)
    except ValueError as e:
        return JSONResponse({'error': str(e)}, status_code=400)

    # Same rule as /compare/analyze: the cache key is the hash of the bytes
    # actually received, not the one the client claims.
    file_hash = hashlib.sha256(data).hexdigest()

    if not force:
        cached = await get_cached_summary(file_hash, APP_VERSION)
        if cached is not None:
            return {**cached, 'cached': True}

    is_image = EXTENSION_MAP.get(get_extension(file.filename or '')) == 'image'
    endpoint = cfg['summary_image_endpoint'] if is_image else cfg['summary_endpoint']
    endpoint_var = 'COMPARE_SUMMARY_IMAGE_ENDPOINT' if is_image else 'COMPARE_SUMMARY_ENDPOINT'
    if not endpoint or endpoint.startswith('TODO'):
        return JSONResponse({'error': f'{endpoint_var} not configured in app.yaml'}, status_code=400)

    text = ''
    image_content: List[Dict[str, Any]] = []
    try:
        if is_image:
            image_content = await asyncio.to_thread(prepare_image_content, file.filename or '', data)
        else:
            text = await asyncio.to_thread(extract_document_text, file.filename or '', data)
    except ValueError as e:
        return JSONResponse({'error': str(e)}, status_code=400)
    except Exception as e:
        logger.error(f'Summary content extraction failed: {e}', exc_info=True)
        return JSONResponse({'error': f'Could not process "{file.filename}": {e}'}, status_code=502)

    if not is_image and not text.strip():
        return {
            'summary': '', 'no_content': True, 'truncated': False, 'duration_s': 0.0,
            'usage': {'input_tokens': 0, 'output_tokens': 0, 'total_tokens': 0, 'cost_eur': 0.0},
        }

    host, token = _get_credentials(request)
    if not host or not token:
        return JSONResponse({'error': 'Missing Databricks credentials'}, status_code=400)

    identity = await get_user_identity(request)
    start = time.monotonic()
    try:
        if is_image:
            result = await summarize_image(host, token, endpoint, image_content, max_tokens=cfg['summary_max_tokens'])
        else:
            result = await summarize_text(
                host, token, endpoint, text,
                max_chars=cfg['summary_max_chars'],
                max_tokens=cfg['summary_max_tokens'],
            )
    except Exception as e:
        logger.error(f'Summarization failed: {e}', exc_info=True)
        asyncio.create_task(store_error(
            endpoint='/api/compare/summarize',
            error_type=type(e).__name__,
            error_msg=str(e),
            user_id=identity['user_id'], workspace_id=identity.get('workspace_id') or '',
            file_type=get_extension(file.filename or ''),
        ))
        return JSONResponse({'error': f'Summarization failed: {e}'}, status_code=502)

    result['duration_s'] = round(time.monotonic() - start, 2)
    if file_hash:
        asyncio.create_task(store_summary_cache(file_hash, APP_VERSION, result, endpoint_name=endpoint))
    return result


# ---------------------------------------------------------------------------
# /compare/save
# ---------------------------------------------------------------------------

@router.post('/compare/save', dependencies=[Depends(require_compare)])
async def save_to_volume(
    old_file: UploadFile = File(...),
    new_file: UploadFile = File(...),
    analysis_text: str = Form(''),
    impact_text: str = Form(''),
):
    try:
        cfg = _get_config()
    except ValueError as e:
        return JSONResponse({'error': str(e)}, status_code=400)

    volume_path = cfg['volume_path'].rstrip('/')
    if not volume_path or volume_path.startswith('TODO'):
        logger.warning('save: COMPARE_VOLUME_PATH is not configured — cannot persist session files. '
                       'Set it in databricks.yml (resources.apps.doc-compare.env) or app.yaml.')
        return JSONResponse({'error': 'COMPARE_VOLUME_PATH not configured'}, status_code=400)

    try:
        old_bytes = await old_file.read()
        new_bytes = await new_file.read()
        _validate_file(old_file, old_bytes)
        _validate_file(new_file, new_bytes)
    except Exception as e:
        return JSONResponse({'error': str(e)}, status_code=400)

    timestamp = datetime.now().strftime('%Y-%m-%d_%H%M%S')
    session_path = f'{volume_path}/{timestamp}'
    saved_files: List[str] = []

    def _save() -> None:
        w = WorkspaceClient()

        def _ensure_dir(path: str):
            try:
                w.files.create_directory(path)
            except Exception as e:
                if 'already exists' not in str(e).lower():
                    raise

        def _upload(path: str, data: bytes):
            w.files.upload(path, io.BytesIO(data), overwrite=True)
            saved_files.append(path)

        _ensure_dir(volume_path)
        _ensure_dir(session_path)
        _upload(f'{session_path}/old_{_sanitize_filename(old_file.filename or "old")}', old_bytes)
        _upload(f'{session_path}/new_{_sanitize_filename(new_file.filename or "new")}', new_bytes)
        if analysis_text.strip():
            _upload(f'{session_path}/analysis.md', analysis_text.encode('utf-8'))
        if impact_text.strip():
            _upload(f'{session_path}/impact.md', impact_text.encode('utf-8'))

    try:
        # The SDK calls are blocking: on the event loop they froze every open
        # analysis stream for the duration of two 20 MB uploads.
        await asyncio.to_thread(_save)
        return {'success': True, 'session_path': session_path, 'saved_files': saved_files}
    except Exception as e:
        logger.error(f'Failed to save to volume: {e}')
        return JSONResponse({'error': str(e), 'saved_files': saved_files}, status_code=500)


# ---------------------------------------------------------------------------
# /compare/load — restore files from a saved volume session
# ---------------------------------------------------------------------------

@router.get('/compare/load', dependencies=[Depends(require_compare)])
async def load_session_files(session_path: str, old_filename: str = '', new_filename: str = ''):
    """Retrieve the two documents stored in a volume session back as base64.

    Called when the user loads a history entry so the files are restored
    in the UI and can be previewed or re-analyzed.
    """

    # session_path comes from the DB (trusted) — security check only when volume_path is configured
    volume_path = os.getenv('COMPARE_VOLUME_PATH', '').rstrip('/')
    if volume_path and not volume_path.startswith('TODO'):
        if not _is_within_volume(session_path, volume_path):
            logger.warning(f'load: session_path {session_path!r} outside configured volume {volume_path!r}')
            return JSONResponse({'error': 'Invalid session_path'}, status_code=403)
    else:
        # No volume configured — allow only UC Volume paths (starts with /Volumes/)
        if not _is_within_volume(session_path, '/Volumes'):
            logger.warning(f'load: session_path {session_path!r} does not look like a UC Volume path')
            return JSONResponse({'error': 'Invalid session_path'}, status_code=403)
        logger.info(f'load: COMPARE_VOLUME_PATH not set, attempting load from trusted DB path {session_path!r}')

    def _read_file(w, path: str) -> bytes:
        resp = w.files.download(path)
        c = resp.contents
        return c.read() if hasattr(c, 'read') else bytes(c)

    def _load() -> dict:
        w = WorkspaceClient()
        result: dict = {}

        for prefix, original_name in (('old', old_filename), ('new', new_filename)):
            safe = _sanitize_filename(original_name) if original_name else None
            file_bytes = None
            found_name = original_name or f'{prefix}_document'

            if safe:
                try:
                    file_bytes = _read_file(w, f'{session_path}/{prefix}_{safe}')
                    found_name = original_name
                except Exception:
                    pass

            # Fall back to directory listing
            if file_bytes is None:
                try:
                    entries = list(w.files.list_directory_contents(session_path))
                    for entry in entries:
                        name = getattr(entry, 'name', '') or ''
                        if name.startswith(f'{prefix}_') and not name.endswith('.md'):
                            file_bytes = _read_file(w, f'{session_path}/{name}')
                            found_name = name[len(prefix) + 1:]  # strip prefix_
                            break
                except Exception:
                    pass

            if file_bytes is not None:
                mime = mimetypes.guess_type(found_name)[0] or 'application/octet-stream'
                result[prefix] = {
                    'name': found_name,
                    'size': len(file_bytes),
                    'data': base64.b64encode(file_bytes).decode('ascii'),
                    'mimeType': mime,
                }

        return result

    try:
        return await asyncio.to_thread(_load)
    except Exception as e:
        logger.error(f'Failed to load session files: {e}')
        return JSONResponse({'error': str(e)}, status_code=500)
