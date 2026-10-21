"""Result caches of /compare/analyze, /compare/impact and /compare/summarize, keyed on file hashes and a version."""

import json
import logging

from server.services.infra.lakebase import connection

logger = logging.getLogger(__name__)

async def get_cached_analysis(old_file_hash: str, new_file_hash: str, cache_version: str) -> dict | None:
    """Return the most recent cached /compare/analyze result for this file pair, or None.

    :param old_file_hash: hash of the old file
    :param new_file_hash: hash of the new file
    :param cache_version: version of everything that shapes the report (app, prompt, model, method)
    :return: ``{'analysis_text', 'file_type', 'processing_method', 'processor_version'}``, or None when there is none
    """
    if not old_file_hash or not new_file_hash:
        return None
    pool = connection.get_pool()
    if not pool:
        return None
    try:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                '''
                SELECT analysis_text, file_type, processing_method, processor_version FROM analysis_cache
                WHERE old_file_hash = $1 AND new_file_hash = $2 AND cache_version = $3
                ORDER BY created_at DESC
                LIMIT 1
                ''',
                old_file_hash, new_file_hash, cache_version,
            )
        return dict(row) if row else None
    except Exception as e:
        logger.debug('Analysis cache lookup failed: %s', e)
        return None


async def store_analysis_cache(old_file_hash: str, new_file_hash: str, cache_version: str, analysis_text: str,
                               file_type: str = '', processing_method: str = '', processor_version: str = '') -> None:
    """Persist a complete /compare/analyze report for later cache hits (best-effort, never raises).

    Only the server calls this, with a report it has just streamed: a client cannot choose what other users get back.

    :param old_file_hash: hash of the old file
    :param new_file_hash: hash of the new file
    :param cache_version: version of everything that shapes the report
    :param analysis_text: the full report
    :param file_type: type of the compared files
    :param processing_method: method that produced the report
    :param processor_version: extraction method forced by the client, empty for the default
    """
    if not old_file_hash or not new_file_hash or not analysis_text.strip():
        return
    pool = connection.get_pool()
    if not pool:
        return
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                '''
                INSERT INTO analysis_cache (old_file_hash, new_file_hash, cache_version, analysis_text, file_type,
                                            processing_method, processor_version)
                VALUES ($1, $2, $3, $4, $5, $6, $7)
                ''',
                old_file_hash, new_file_hash, cache_version, analysis_text, file_type or None,
                processing_method or None, processor_version or None,
            )
    except Exception as e:
        logger.debug('store_analysis_cache failed: %s', e)


async def get_cached_impact_result(old_file_hash: str, new_file_hash: str, app_version: str) -> dict | None:
    """Return the most recent cached /compare/impact result for this file pair, or None.

    :param old_file_hash: hash of the old file
    :param new_file_hash: hash of the new file
    :param app_version: app version that made the result
    :return: the cached result, or None when there is none
    """
    if not old_file_hash or not new_file_hash:
        return None
    pool = connection.get_pool()
    if not pool:
        return None
    try:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                '''
                SELECT result_json FROM impact_cache
                WHERE old_file_hash = $1 AND new_file_hash = $2 AND app_version = $3
                ORDER BY created_at DESC
                LIMIT 1
                ''',
                old_file_hash, new_file_hash, app_version,
            )
        return json.loads(row['result_json']) if row else None
    except Exception as e:
        logger.debug('Impact cache lookup failed: %s', e)
        return None


async def store_impact_cache(old_file_hash: str, new_file_hash: str, app_version: str, result: dict) -> None:
    """Persist a fresh /compare/impact result for later cache hits (best-effort, never raises).

    :param old_file_hash: hash of the old file
    :param new_file_hash: hash of the new file
    :param app_version: app version that made the result
    :param result: result to store, JSON-serialisable
    """
    if not old_file_hash or not new_file_hash:
        return
    pool = connection.get_pool()
    if not pool:
        return
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                '''
                INSERT INTO impact_cache (old_file_hash, new_file_hash, app_version, result_json)
                VALUES ($1, $2, $3, $4)
                ''',
                old_file_hash, new_file_hash, app_version, json.dumps(result),
            )
    except Exception as e:
        logger.debug('store_impact_cache failed: %s', e)


async def get_cached_summary(file_hash: str, app_version: str) -> dict | None:
    """Return the most recent cached /compare/summarize result for this file, or None.

    :param file_hash: hash of the file
    :param app_version: app version that made the summary
    :return: the cached summary, or None when there is none
    """
    if not file_hash:
        return None
    pool = connection.get_pool()
    if not pool:
        return None
    try:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                '''
                SELECT result_json FROM summary_cache
                WHERE file_hash = $1 AND app_version = $2
                ORDER BY created_at DESC
                LIMIT 1
                ''',
                file_hash, app_version,
            )
        return json.loads(row['result_json']) if row else None
    except Exception as e:
        logger.debug('Summary cache lookup failed: %s', e)
        return None


async def store_summary_cache(file_hash: str, app_version: str, result: dict, endpoint_name: str = '') -> None:
    """Persist a fresh /compare/summarize result for later cache hits (best-effort, never raises).

    :param file_hash: hash of the file
    :param app_version: app version that made the summary
    :param result: summary to store, JSON-serialisable
    :param endpoint_name: endpoint that wrote the summary, empty for none
    """
    if not file_hash:
        return
    pool = connection.get_pool()
    if not pool:
        return
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                '''
                INSERT INTO summary_cache (file_hash, app_version, result_json, endpoint_name)
                VALUES ($1, $2, $3, $4)
                ''',
                file_hash, app_version, json.dumps(result), endpoint_name or None,
            )
    except Exception as e:
        logger.debug('store_summary_cache failed: %s', e)
