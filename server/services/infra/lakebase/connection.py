"""Connection pool of the Lakebase (autoscaling PostgreSQL) project, with its OAuth token refresh.

A background task rebuilds the pool before the token expires (lifetime ~1h).

Required env vars:
  LAKEBASE_PROJECT_ID   - Lakebase project ID (e.g. "latec-compare")
  LAKEBASE_DATABASE     - PostgreSQL database name (default: "doccompare")

Optional:
  LAKEBASE_BRANCH       - branch (default: "production")
  LAKEBASE_ENDPOINT     - endpoint (default: "primary")
"""

import asyncio
import logging
import os
import time
from typing import Optional

import asyncpg
from databricks.sdk import WorkspaceClient

from .schema import ensure_schema

logger = logging.getLogger(__name__)

_pool: Optional[asyncpg.Pool] = None
_refresh_task: Optional[asyncio.Task] = None

# Used only when the real expiry is unknown (fallback workspace token, see _get_host_and_token).
_DEFAULT_REFRESH_INTERVAL_S = 55 * 60  # 55 min — token lifetime is ~1h
_REFRESH_SAFETY_BUFFER_S = 5 * 60  # refresh this long before actual expiry
_MIN_REFRESH_INTERVAL_S = 60  # never refresh tighter than this

# Bounds the initial TCP connect (asyncpg waits 60 s by default): local dev may have port 5432 blocked, and the short
# timeout falls back to the no-history path quickly.
_CONNECT_TIMEOUT_S = float(os.getenv('LAKEBASE_CONNECT_TIMEOUT_S', '5'))


def app_version() -> str:
    return os.getenv('APP_VERSION', '2')


def _cfg() -> dict:
    return {
        'project_id': os.getenv('LAKEBASE_PROJECT_ID', ''),
        'branch': os.getenv('LAKEBASE_BRANCH', 'production'),
        'endpoint': os.getenv('LAKEBASE_ENDPOINT', 'primary'),
        'database': os.getenv('LAKEBASE_DATABASE', 'doccompare'),
    }


def _get_host_and_token(
    project_id: str, branch: str, endpoint: str
) -> tuple[str, str, str, Optional[float]]:
    """Fetch host and OAuth token from Databricks SDK (sync, run in thread).

    Lakebase OAuth auth uses the literal username "token"; the identity is
    carried inside the OAuth token itself, not in the PostgreSQL user field.

    Strategy:
      1. Try generate_database_credential (purpose-built Lakebase credential).
      2. Fall back to the workspace OAuth token already available in the SDK
         config (always present in a Databricks App environment).

    Returns the token's actual remaining lifetime in seconds (from the
    credential's expire_time) when known, so the refresh loop can schedule
    itself off ground truth instead of an assumed lifetime — the fallback
    workspace token carries no expiry info, so that path returns None.
    """
    w = WorkspaceClient()

    branch_path = f'projects/{project_id}/branches/{branch}'
    endpoint_path = f'{branch_path}/endpoints/{endpoint}'

    eps = list(w.postgres.list_endpoints(parent=branch_path))
    if not eps:
        raise RuntimeError(f'No endpoints found for {branch_path}')
    host = eps[0].status.hosts.host
    logger.debug(f'Lakebase host resolved: {host}')

    # PostgreSQL username = SP application ID (or user email for human users).
    # Lakebase OAuth maps the connection to the Databricks identity via this field.
    me = w.current_user.me()
    username = me.user_name or me.display_name or ''
    if not username:
        raise RuntimeError('Could not resolve current user identity for Lakebase auth')
    logger.debug(f'Lakebase connecting as: {username}')

    token: str | None = None
    expires_in_s: Optional[float] = None
    try:
        cred = w.postgres.generate_database_credential(endpoint=endpoint_path)
        token = cred.token or None
        if token:
            if cred.expire_time is not None:
                expires_in_s = cred.expire_time.ToSeconds() - time.time()
            logger.debug(
                f'generate_database_credential succeeded (token len={len(token)}, '
                f'expires_in={expires_in_s}s)'
            )
        else:
            logger.warning('generate_database_credential returned empty token — falling back to workspace token')
    except Exception as e:
        logger.warning(f'generate_database_credential failed ({e}) — falling back to workspace token')

    # Fallback: workspace OAuth token (always present in Databricks App env).
    # No expiry is exposed here, so the caller falls back to a fixed interval.
    if not token:
        token = w.config.token
        if token:
            logger.info('Using workspace OAuth token for Lakebase auth (fallback, unknown expiry)')
        else:
            raise RuntimeError('No authentication token available for Lakebase')

    return host, username, token, expires_in_s


def _next_refresh_delay(expires_in_s: Optional[float]) -> float:
    """Delay before the next credential refresh, based on real expiry when known."""
    if expires_in_s is None:
        return _DEFAULT_REFRESH_INTERVAL_S
    return max(expires_in_s - _REFRESH_SAFETY_BUFFER_S, _MIN_REFRESH_INTERVAL_S)


async def _build_pool(host: str, username: str, token: str, database: str) -> asyncpg.Pool:
    return await asyncpg.create_pool(
        host=host,
        port=5432,
        database=database,
        user=username,
        password=token,
        ssl='require',
        min_size=1,
        max_size=10,
        server_settings={'timezone': 'UTC'},
        timeout=_CONNECT_TIMEOUT_S,
    )


async def _ensure_database(host: str, username: str, token: str, database: str) -> None:
    """Create the PostgreSQL database if it does not exist.

    Connects to the default 'postgres' system database first (always present),
    then issues CREATE DATABASE if needed.  Requires CREATE DB privilege on the
    service principal.
    """
    conn = await asyncpg.connect(
        host=host, port=5432, database='postgres',
        user=username, password=token, ssl='require',
        timeout=_CONNECT_TIMEOUT_S,
    )
    try:
        exists = await conn.fetchval(
            "SELECT 1 FROM pg_database WHERE datname = $1", database
        )
        if not exists:
            # CREATE DATABASE cannot run in a transaction (asyncpg autocommits outside explicit ones), and the identifier cannot be
            # a parameter: embedded quotes are escaped so a hostile LAKEBASE_DATABASE value cannot break out.
            safe_db = database.replace('"', '""')
            await conn.execute(f'CREATE DATABASE "{safe_db}"')
            logger.info(f'Database "{database}" created')
        else:
            logger.debug(f'Database "{database}" already exists')
    finally:
        await conn.close()


async def _token_refresh_loop(
    project_id: str, branch: str, endpoint: str, database: str, delay: float
) -> None:
    global _pool
    while True:
        await asyncio.sleep(delay)
        try:
            host, username, token, expires_in_s = await asyncio.to_thread(
                _get_host_and_token, project_id, branch, endpoint
            )
            new_pool = await _build_pool(host, username, token, database)
            old_pool = _pool
            _pool = new_pool
            if old_pool:
                # Grace period for in-flight requests that already grabbed the old pool via get_pool().
                await asyncio.sleep(30)
                await old_pool.close()
            delay = _next_refresh_delay(expires_in_s)
            logger.info(f'Lakebase token refreshed (next refresh in {delay:.0f}s)')
        except Exception as e:
            # The current token keeps expiring regardless: retry soon instead of waiting for the next cycle.
            delay = 60
            logger.error(f'Lakebase token refresh failed (retrying in {delay}s): {e}')


# --- Public API ---


async def init_lakebase() -> None:
    global _pool, _refresh_task
    cfg = _cfg()
    project_id = cfg['project_id']
    if not project_id:
        logger.warning('LAKEBASE_PROJECT_ID not set — history feature disabled')
        return
    try:
        host, username, token, expires_in_s = await asyncio.to_thread(
            _get_host_and_token, project_id, cfg['branch'], cfg['endpoint']
        )
        await _ensure_database(host, username, token, cfg['database'])
        _pool = await _build_pool(host, username, token, cfg['database'])
        try:
            await ensure_schema(_pool)
        except Exception as e:
            # A failing schema migration must never skip the refresh loop below: the live pool's token expires in ~1h.
            logger.error(f'Lakebase schema ensure failed — continuing with existing schema: {e}')
        _refresh_task = asyncio.create_task(
            _token_refresh_loop(
                project_id, cfg['branch'], cfg['endpoint'], cfg['database'],
                _next_refresh_delay(expires_in_s),
            )
        )
        logger.info(f'Lakebase ready ({host})')
    except Exception as e:
        logger.error(f'Lakebase init failed — history disabled: {e}')


async def shutdown_lakebase() -> None:
    if _refresh_task:
        _refresh_task.cancel()
        try:
            await _refresh_task
        except asyncio.CancelledError:
            pass
    if _pool:
        await _pool.close()
    logger.info('Lakebase shut down')


def get_pool() -> Optional[asyncpg.Pool]:
    return _pool
