"""POST /compare/impact: NDJSON stream of the documents impacted by a change list."""

import asyncio
import hashlib
import json
import logging
import time
import traceback
from dataclasses import dataclass, field
from typing import Any, AsyncGenerator, Dict, Iterator, List

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

from server.services.compare.impact_queries import changes_to_queries
from server.services.compare.prompts import load_prompt
from server.services.infra.credentials import get_credentials_async
from server.services.infra.lakebase import (
    get_cached_impact_result, store_error,
    store_impact_cache, store_impact_request,
)
from server.services.infra.tasks import spawn
from server.services.infra.user import get_user_identity, rate_limit, require_compare
from server.services.infra.vector_search import run_impact_search, sort_documents
from server.routers.compare.config import APP_VERSION, get_config

logger = logging.getLogger(__name__)
router = APIRouter()


class ImpactRequest(BaseModel):
    changes_text: str
    # File hashes of the comparison that produced changes_text: they link the impact_requests row to its messages row and key
    # this endpoint's result cache. Optional: a manual-mode search has none, so the cache is skipped.
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

    The file hashes alone do not determine an impact search: the change source (Change Table or Change Summary)
    and the impact index both change the result, so they are part of the key, as are the judge endpoint and prompt.

    :param changes_text: the change list the search starts from.
    :param cfg: the compare settings (``get_config()``).
    """
    parts = [changes_text.strip(), cfg['impact_index'], cfg['impact_endpoint'],
             str(cfg['impact_max_queries']), str(cfg['impact_per_query_results']),
             str(cfg['impact_max_candidates']), load_prompt('impact_judge')]
    return f'{APP_VERSION}:{hashlib.sha256(chr(31).join(parts).encode("utf-8")).hexdigest()[:16]}'


def _ndjson(event: Dict[str, Any]) -> str:
    """One NDJSON line.

    :param event: a JSON-serialisable event of the stream.
    """
    return json.dumps(event, ensure_ascii=False) + '\n'


def _replay_events(result: Dict[str, Any]) -> Iterator[str]:
    """Re-emit a cached result as the same event stream a fresh search produces.

    :param result: a result stored by ``store_impact_cache``.
    """
    plan = {k: v for k, v in result.items() if k not in ('documents', 'usage', 'duration_s', 'impact_request_id')}
    yield _ndjson({**plan, 'type': 'plan', 'cached': True})
    for doc in result.get('documents', []):
        yield _ndjson({'type': 'document', 'document': doc})
    yield _ndjson({'type': 'done', 'usage': result.get('usage', {}), 'duration_s': result.get('duration_s', 0.0),
                   'impact_request_id': result.get('impact_request_id'), 'cached': True})


@dataclass
class _ImpactSearch:
    """One impact search: what the request fixed before the stream starts, and the calls that depend on it."""
    body: ImpactRequest
    cfg: Dict[str, Any]
    host: str
    identity: Dict[str, Any]
    extracted: Dict[str, Any]        # changes_to_queries(): the queries to run and where they come from
    extract_ms: int
    old_hash: str
    new_hash: str
    # Filled by the judge calls: the endpoint(s) that answered, which may be a fallback.
    judge_info: Dict[str, Any] = field(default_factory=dict)

    @property
    def index_name(self) -> str:
        return self.cfg['impact_index']

    @property
    def llm_endpoint(self) -> str:
        return self.cfg['impact_endpoint']

    def search(self, token: str) -> AsyncGenerator[Dict[str, Any], None]:
        """The Vector Search + judge stream.

        :param token: the token the index is queried with.
        :return: the ``plan``, ``document`` and ``done`` events of ``run_impact_search``.
        """
        return run_impact_search(
            host=self.host, token=token, index_name=self.index_name, llm_endpoint=self.llm_endpoint,
            info=self.judge_info, extracted=self.extracted,
            num_results=self.cfg['impact_per_query_results'],
            max_candidates=self.cfg['impact_max_candidates'],
            max_changes_chars=self.cfg['impact_max_query_chars'],
            max_tokens=self.cfg['impact_max_tokens'],
            archive_before=self.cfg['impact_archive_before'],
            exclude_names=[self.body.old_file_name, self.body.new_file_name],
        )

    def judges(self) -> str:
        """The endpoints that judged, comma-joined ('' when none answered)."""
        return ','.join(self.judge_info.get('endpoints') or [])

    def audit(self, endpoint_name: str = '', **kwargs: Any) -> Any:
        """Save the ``impact_requests`` row of this search.

        :param endpoint_name: the endpoints that judged; the configured endpoint when none answered.
        :param kwargs: outcome, counts and timings passed to ``store_impact_request``.
        :return: the awaitable of ``store_impact_request`` (its result is the row id).
        """
        return store_impact_request(
            method='index_llm_per_doc',
            user_id=self.identity['user_id'], workspace_id=self.identity.get('workspace_id') or '',
            old_file_hash=self.old_hash, new_file_hash=self.new_hash,
            changes_chars=len(self.body.changes_text), endpoint_name=endpoint_name or self.llm_endpoint,
            index_name=self.index_name, queries_used=len(self.extracted['queries']), extract_ms=self.extract_ms,
            **kwargs,
        )

    def warn(self, error_type: str, stage: str, message: str, upstream: str, **context: Any) -> None:
        """Record a degraded search (it went on) as an errors row with severity 'warning'.

        :param error_type: short code of the problem.
        :param stage: ``search`` or ``judge``.
        :param message: what happened.
        :param upstream: the index or endpoint involved.
        :param context: extra fields kept with the row.
        """
        store_error(
            endpoint='/api/compare/impact', error_type=error_type, error_msg=message, severity='warning',
            stage=stage, upstream=upstream, user_id=self.identity['user_id'],
            workspace_id=self.identity.get('workspace_id') or '', context=context or None)


async def _impact_events(run: _ImpactSearch, request: Request, token: str,
                         cache_version: str) -> AsyncGenerator[str, None]:
    """The NDJSON stream of one search, with its audit row and its cache entry.

    :param run: the search to run.
    :param request: the incoming request; its forwarded user token is the retry when the service principal is denied.
    :param token: the service principal token the index is queried with first.
    :param cache_version: key of the result cache (see ``_impact_cache_version``).
    """
    extracted = run.extracted
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
    search_ms: int | None = None
    judge_start = 0.0

    def _counts() -> Dict[str, Any]:
        """What the search did so far — for the audit row, whatever the outcome."""
        return {'queries_failed': plan.get('queries_failed'), 'candidates': plan.get('candidates'),
                'not_judged': len(plan.get('not_judged') or []) if plan else None,
                'judge_failed': sum(1 for d in documents if d.get('status') == 'error'),
                'search_ms': search_ms,
                'judge_ms': int((time.monotonic() - judge_start) * 1000) if judge_start else None}

    try:
        stream = run.search(token)
        try:
            first = await stream.__anext__()
        except Exception as sp_err:
            fwd = request.headers.get('x-forwarded-access-token', '')
            if not (fwd and fwd != token and '403' in str(sp_err)):
                raise
            logger.info('impact: SP token denied on index — retrying with forwarded user token')
            stream = run.search(fwd)
            first = await stream.__anext__()
        search_ms = int((time.monotonic() - start) * 1000)
        judge_start = time.monotonic()
        plan = {k: v for k, v in first.items() if k != 'type'}
        if plan.get('queries_failed'):
            run.warn('VectorSearchPartial', 'search',
                     f"{plan['queries_failed']}/{plan.get('queries_used')} Vector Search queries failed",
                     run.index_name, queries_used=plan.get('queries_used'), queries_failed=plan['queries_failed'])
        left_out = extracted.get('left_out') or []
        if left_out:
            plan['changes_not_searched'] = left_out
            first = {**first, 'changes_not_searched': left_out}
            run.warn('ChangesNotSearched', 'search',
                     f'{len(left_out)} changes left out: the queries are full', run.index_name,
                     change_ids=left_out, max_queries=run.cfg['impact_max_queries'])
        yield _ndjson(first)
        async for event in stream:
            if event['type'] == 'document':
                doc = event['document']
                detail = doc.pop('error_detail', '')
                documents.append(doc)
                if doc.get('status') == 'error':
                    run.warn(doc.get('error_type') or 'JudgeError', 'judge', detail or doc.get('reason') or '',
                             run.llm_endpoint, ref=doc.get('ref'), judge_attempts=doc.get('judge_attempts'))
                yield _ndjson(event)
            elif event['type'] == 'done':
                usage = event['usage']
    except (asyncio.CancelledError, GeneratorExit):
        # The browser left (closed tab, new search): record the search as aborted.
        spawn(run.audit(endpoint_name=run.judges(), duration_s=time.monotonic() - start,
                                      status='aborted', num_documents=len(documents), **_counts()))
        raise
    except Exception as e:
        logger.error(f'Impact search failed: {e}', exc_info=True)
        stage = 'judge' if plan else 'search'
        store_error(
            endpoint='/api/compare/impact', error_type=type(e).__name__, error_msg=str(e), stage=stage,
            upstream=run.llm_endpoint if plan else run.index_name, user_id=run.identity['user_id'],
            workspace_id=run.identity.get('workspace_id') or '', stack_trace=traceback.format_exc())
        spawn(run.audit(endpoint_name=run.judges(), duration_s=time.monotonic() - start,
                                      http_status=502, status='error', error_type=type(e).__name__,
                                      error_msg=str(e), **_counts()))
        yield _ndjson({'type': 'error', 'error': 'Impact search failed'})
        return

    duration_s = round(time.monotonic() - start, 2)
    documents = sort_documents(documents)
    # Written before 'done': the client needs the id to attach feedback to this search.
    request_id = await run.audit(
        endpoint_name=run.judges(),
        chunks_returned=plan.get('chunks_returned', 0), num_documents=len(documents),
        duration_s=duration_s, http_status=200, status='ok',
        input_tokens=usage.get('input_tokens', 0), output_tokens=usage.get('output_tokens', 0),
        total_tokens=usage.get('total_tokens', 0), cost_eur=usage.get('cost_eur', 0.0),
        documents=documents, **_counts(),
    )
    yield _ndjson({'type': 'done', 'usage': usage, 'duration_s': duration_s, 'impact_request_id': request_id})

    # A result is kept only when it is complete: a failed query or judge call would be replayed to every later user.
    complete = not plan.get('queries_failed') and not any(d.get('status') == 'error' for d in documents)
    # Judgments by a fallback model are not stored under the key of the configured one.
    complete = complete and set(run.judge_info.get('endpoints') or []) <= {run.llm_endpoint}
    if run.old_hash and run.new_hash and complete:
        spawn(store_impact_cache(
            run.old_hash, run.new_hash, cache_version,
            {**plan, 'documents': documents, 'usage': usage, 'duration_s': duration_s,
             'impact_request_id': request_id},
        ))


@router.post('/compare/impact', dependencies=[Depends(require_compare), Depends(rate_limit)])
async def find_impacted_documents(body: ImpactRequest, request: Request) -> Response:
    """Stream NDJSON events: plan → document (one per judged candidate, as each finishes) → done | error.

    A cached result for the same files and changes is replayed as the same events.

    :param body: the change list, the hashes and names of the compared files, and ``force_refresh`` to skip the cache.
    :param request: the incoming request, for the Databricks credentials and the caller's identity.
    :return: the NDJSON stream, or a 400 JSON error when the settings or the credentials are missing.
    """
    try:
        cfg = get_config()
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
        # A cached result without a change list is not replayed.
        if cached is not None and 'changes' in cached:
            return StreamingResponse(_replay_events(cached), media_type='application/x-ndjson')

    host, token = await get_credentials_async(request)
    if not host or not token:
        return JSONResponse({'error': 'Missing Databricks credentials'}, status_code=400)

    identity = await get_user_identity(request)
    t_extract = time.monotonic()
    extracted = changes_to_queries(body.changes_text, max_queries=cfg['impact_max_queries'],
                                   max_query_chars=cfg['impact_max_query_chars'])
    extract_ms = int((time.monotonic() - t_extract) * 1000)

    run = _ImpactSearch(body=body, cfg=cfg, host=host, identity=identity, extracted=extracted,
                        extract_ms=extract_ms, old_hash=old_hash, new_hash=new_hash)
    return StreamingResponse(_impact_events(run, request, token, cache_version), media_type='application/x-ndjson')
