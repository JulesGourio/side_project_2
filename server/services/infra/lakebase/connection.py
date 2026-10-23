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

import asyncpg
from databricks.sdk import WorkspaceClient

from server.services.infra.lakebase.schema import ensure_schema

logger = logging.getLogger(__name__)

_pool: asyncpg.Pool | None = None
_refresh_task: asyncio.Task | None = None
_reconnect_task: asyncio.Task | None = None

# Used only when the real expiry is unknown (fallback workspace token, see _get_host_and_token).
_DEFAULT_REFRESH_INTERVAL_S = 55 * 60  # 55 min — token lifetime is ~1h
_REFRESH_SAFETY_BUFFER_S = 5 * 60  # refresh this long before actual expiry
_MIN_REFRESH_INTERVAL_S = 60  # never refresh tighter than this

# Wait between two connection attempts while Lakebase is unreachable at startup: doubles from the minimum to the maximum.
_RECONNECT_MIN_S = 10.0
_RECONNECT_MAX_S = 300.0

# A replaced pool is closed after this grace period (in-flight requests), then terminated if the close hangs.
_RETIRE_GRACE_S = 30.0
_RETIRE_CLOSE_TIMEOUT_S = 30.0
# Bounds the initial TCP connect (asyncpg waits 60 s by default): local dev may have port 5432 blocked, and the short
# timeout falls back to the no-history path quickly.
_CONNECT_TIMEOUT_S = float(os.getenv('LAKEBASE_CONNECT_TIMEOUT_S', '5'))


def app_version() -> str:
    return os.getenv('APP_VERSION', '1.4.1')


def _cfg() -> dict:
    return {
        'project_id': os.getenv('LAKEBASE_PROJECT_ID', ''),
        'branch': os.getenv('LAKEBASE_BRANCH', 'production'),
        'endpoint': os.getenv('LAKEBASE_ENDPOINT', 'primary'),
        'database': os.getenv('LAKEBASE_DATABASE', 'doccompare'),
    }


def _get_host_and_token(
    project_id: str, branch: str, endpoint: str
) -> tuple[str, str, str, float | None]:
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

    :param project_id: Lakebase project id
    :param branch: branch name
    :param endpoint: endpoint name
    :return: ``(host, username, token, expires_in_s)``; ``expires_in_s`` is None when the expiry is unknown
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
    expires_in_s: float | None = None
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


def _next_refresh_delay(expires_in_s: float | None) -> float:
    """Delay before the next credential refresh, based on real expiry when known.

    :param expires_in_s: remaining lifetime of the token in seconds, None when unknown
    :return: the seconds until the next refresh
    """
    if expires_in_s is None:
        return _DEFAULT_REFRESH_INTERVAL_S
    return max(expires_in_s - _REFRESH_SAFETY_BUFFER_S, _MIN_REFRESH_INTERVAL_S)


async def _build_pool(host: str, username: str, token: str, database: str) -> asyncpg.Pool:
    """Creates the connection pool of the database, over TLS.

    :param host: Lakebase host
    :param username: PostgreSQL user, the identity of the OAuth token
    :param token: OAuth token, used as the password
    :param database: database name
    :return: the pool
    """
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

    :param host: Lakebase host
    :param username: PostgreSQL user
    :param token: OAuth token, used as the password
    :param database: database to create when it is missing
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


async def _retire_pool(pool: asyncpg.Pool, grace_s: float = _RETIRE_GRACE_S,
                       close_timeout_s: float = _RETIRE_CLOSE_TIMEOUT_S) -> None:
    """Closes a replaced pool once its in-flight requests are done; connections still busy after the timeout are cut.

    :param pool: the pool that was replaced
    :param grace_s: seconds to wait for requests that already took the pool through ``get_pool()``
    :param close_timeout_s: seconds a graceful close may take before the connections are terminated
    """
    await asyncio.sleep(grace_s)
    try:
        await asyncio.wait_for(pool.close(), timeout=close_timeout_s)
    except asyncio.TimeoutError:
        logger.warning('Lakebase: old pool still busy after %.0fs, terminating it', close_timeout_s)
        pool.terminate()


async def _token_refresh_loop(
    project_id: str, branch: str, endpoint: str, database: str, delay: float
) -> None:
    """Rebuilds the pool before its token expires; a failed refresh is retried after 60 seconds.

    :param project_id: Lakebase project id
    :param branch: branch name
    :param endpoint: endpoint name
    :param database: database name
    :param delay: seconds to wait before the first refresh
    """
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
                await _retire_pool(old_pool)
            delay = _next_refresh_delay(expires_in_s)
            logger.info(f'Lakebase token refreshed (next refresh in {delay:.0f}s)')
        except Exception as e:
            # The current token keeps expiring regardless: retry soon instead of waiting for the next cycle.
            delay = 60
            logger.error(f'Lakebase token refresh failed (retrying in {delay}s): {e}')


# --- Public API ---


async def _connect(cfg: dict) -> float | None:
    """Connect to Lakebase, make sure the database and the schema exist, and set the pool.

    :param cfg: the Lakebase settings from ``_cfg()``
    :return: seconds until the token expires, None when unknown
    :raises Exception: when the endpoint cannot be reached or refuses the connection
    """
    global _pool
    host, username, token, expires_in_s = await asyncio.to_thread(
        _get_host_and_token, cfg['project_id'], cfg['branch'], cfg['endpoint']
    )
    await _ensure_database(host, username, token, cfg['database'])
    _pool = await _build_pool(host, username, token, cfg['database'])
    try:
        await ensure_schema(_pool)
    except Exception as e:
        # A failing schema migration must never skip the refresh loop: the live pool's token expires in ~1h.
        logger.error(f'Lakebase schema ensure failed — continuing with existing schema: {e}')
    logger.info(f'Lakebase ready ({host})')
    return expires_in_s


def _start_refresh(cfg: dict, expires_in_s: float | None) -> None:
    """Start the task that rebuilds the pool before its token expires.

    :param cfg: the Lakebase settings from ``_cfg()``
    :param expires_in_s: seconds until the current token expires, None when unknown
    """
    global _refresh_task
    _refresh_task = asyncio.create_task(
        _token_refresh_loop(
            cfg['project_id'], cfg['branch'], cfg['endpoint'], cfg['database'], _next_refresh_delay(expires_in_s),
        )
    )


async def _connect_until_ready(cfg: dict) -> None:
    """Retry the connection with a growing wait until it works, then start the token refresh.

    Lakebase can still be waking from scale-to-zero when the app starts: without this loop the pool would stay empty
    until the next restart.

    :param cfg: the Lakebase settings from ``_cfg()``
    """
    wait = _RECONNECT_MIN_S
    while True:
        await asyncio.sleep(wait)
        try:
            _start_refresh(cfg, await _connect(cfg))
            return
        except Exception as e:
            logger.error(f'Lakebase still unreachable (retrying in {min(wait * 2, _RECONNECT_MAX_S):.0f}s): {e}')
            wait = min(wait * 2, _RECONNECT_MAX_S)


def status() -> str:
    """The state of the Lakebase connection, for the health check.

    :return: ``disabled`` (no project configured), ``ready`` (pool open) or ``connecting`` (not reachable yet)
    """
    if not _cfg()['project_id']:
        return 'disabled'
    return 'ready' if _pool else 'connecting'


async def init_lakebase() -> None:
    """Open the Lakebase pool at startup; when Lakebase is not reachable, keep trying in the background.

    Nothing here raises: the app runs without history until the connection works.
    """
    global _reconnect_task
    cfg = _cfg()
    if not cfg['project_id']:
        logger.warning('LAKEBASE_PROJECT_ID not set — history feature disabled')
        return
    try:
        _start_refresh(cfg, await _connect(cfg))
    except Exception as e:
        logger.error(f'Lakebase init failed — history disabled until it connects: {e}')
        _reconnect_task = asyncio.create_task(_connect_until_ready(cfg))


async def shutdown_lakebase() -> None:
    """Stop the background tasks and close the pool."""
    for task in (_refresh_task, _reconnect_task):
        if task:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
    if _pool:
        await _pool.close()
    logger.info('Lakebase shut down')


def get_pool() -> asyncpg.Pool | None:
    return _pool
