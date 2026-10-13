"""POST /compare/impact: NDJSON stream of the documents impacted by a change list."""

import asyncio
import hashlib
import json
import logging
import time
import traceback
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

from ...services.compare.impact_queries import changes_to_queries
from ...services.infra.lakebase import (
    get_cached_impact_result, store_error,
    store_impact_cache, store_impact_request,
)
from ...services.infra.user import get_user_identity, require_compare
from ...services.infra.vector_search import run_impact_search, sort_documents
from .common import (
    APP_VERSION, _get_config, _get_credentials,
)

logger = logging.getLogger(__name__)
router = APIRouter()


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
    them.
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
    t_extract = time.monotonic()
    extracted = changes_to_queries(body.changes_text, max_queries=cfg['impact_max_queries'])
    extract_ms = int((time.monotonic() - t_extract) * 1000)

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
            changes_chars=len(body.changes_text), endpoint_name=llm_endpoint, index_name=index_name,
            queries_used=len(extracted['queries']), extract_ms=extract_ms, **kwargs,
        )

    def _warn(error_type: str, stage: str, message: str, upstream: str, **context):
        """A degraded search (it went on): errors row with severity 'warning'."""
        store_error(
            endpoint='/api/compare/impact', error_type=error_type, error_msg=message, severity='warning',
            stage=stage, upstream=upstream, user_id=identity['user_id'],
            workspace_id=identity.get('workspace_id') or '', context=context or None)

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
        search_ms: Optional[int] = None
        judge_start = 0.0

        def _counts() -> Dict[str, Any]:
            """What the search did so far — for the audit row, whatever the outcome."""
            return {'queries_failed': plan.get('queries_failed'), 'candidates': plan.get('candidates'),
                    'not_judged': len(plan.get('not_judged') or []) if plan else None,
                    'judge_failed': sum(1 for d in documents if d.get('status') == 'error'),
                    'search_ms': search_ms,
                    'judge_ms': int((time.monotonic() - judge_start) * 1000) if judge_start else None}

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
            search_ms = int((time.monotonic() - start) * 1000)
            judge_start = time.monotonic()
            plan = {k: v for k, v in first.items() if k != 'type'}
            if plan.get('queries_failed'):
                _warn('VectorSearchPartial', 'search',
                      f"{plan['queries_failed']}/{plan.get('queries_used')} Vector Search queries failed",
                      index_name, queries_used=plan.get('queries_used'), queries_failed=plan['queries_failed'])
            yield _ndjson(first)
            async for event in stream:
                if event['type'] == 'document':
                    doc = event['document']
                    documents.append(doc)
                    if doc.get('status') == 'error':
                        _warn(doc.get('error_type') or 'JudgeError', 'judge', doc.get('reason') or '', llm_endpoint,
                              ref=doc.get('ref'), judge_attempts=doc.get('judge_attempts'))
                    yield _ndjson(event)
                elif event['type'] == 'done':
                    usage = event['usage']
        except (asyncio.CancelledError, GeneratorExit):
            # The browser left (closed tab, new search): record the search as aborted.
            asyncio.create_task(_audit(duration_s=time.monotonic() - start, status='aborted',
                                       num_documents=len(documents), **_counts()))
            raise
        except Exception as e:
            logger.error(f'Impact search failed: {e}', exc_info=True)
            stage = 'judge' if plan else 'search'
            store_error(
                endpoint='/api/compare/impact', error_type=type(e).__name__, error_msg=str(e), stage=stage,
                upstream=llm_endpoint if plan else index_name, user_id=identity['user_id'],
                workspace_id=identity.get('workspace_id') or '', stack_trace=traceback.format_exc())
            asyncio.create_task(_audit(duration_s=time.monotonic() - start, http_status=502, status='error',
                                       error_type=type(e).__name__, error_msg=str(e), **_counts()))
            yield _ndjson({'type': 'error', 'error': f'Impact search failed: {e}'})
            return

        duration_s = round(time.monotonic() - start, 2)
        documents = sort_documents(documents)
        # Written before 'done' (a few ms): the client needs the id to attach feedback to this search.
        request_id = await _audit(
            chunks_returned=plan.get('chunks_returned', 0), num_documents=len(documents),
            duration_s=duration_s, http_status=200, status='ok',
            input_tokens=usage.get('input_tokens', 0), output_tokens=usage.get('output_tokens', 0),
            total_tokens=usage.get('total_tokens', 0), cost_eur=usage.get('cost_eur', 0.0),
            documents=documents, **_counts(),
        )
        yield _ndjson({'type': 'done', 'usage': usage, 'duration_s': duration_s, 'impact_request_id': request_id})

        if old_hash and new_hash and not any(d.get('status') == 'error' for d in documents):
            asyncio.create_task(store_impact_cache(
                old_hash, new_hash, cache_version,
                {**plan, 'documents': documents, 'usage': usage, 'duration_s': duration_s,
                 'impact_request_id': request_id},
            ))

    return StreamingResponse(_events(), media_type='application/x-ndjson')
