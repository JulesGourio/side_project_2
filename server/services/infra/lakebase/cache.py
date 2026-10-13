"""Result caches of /compare/impact and /compare/summarize, keyed on file hashes and app version."""

import json
import logging
from typing import Optional

from . import connection

logger = logging.getLogger(__name__)

async def get_cached_impact_result(old_file_hash: str, new_file_hash: str, app_version: str) -> Optional[dict]:
    """Return the most recent cached /compare/impact result for this file pair, or None."""
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
    """Persist a fresh /compare/impact result for later cache hits (best-effort, never raises)."""
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


async def get_cached_summary(file_hash: str, app_version: str) -> Optional[dict]:
    """Return the most recent cached /compare/summarize result for this file, or None."""
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
    """Persist a fresh /compare/summarize result for later cache hits (best-effort, never raises)."""
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
