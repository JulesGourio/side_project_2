"""User service — resolve the current user's identity from request headers."""

import asyncio
import logging
import os
import time
from typing import Optional, TypedDict

from fastapi import HTTPException, Request


class UserIdentity(TypedDict):
    user_id: str                 # Numeric part only — e.g. "78655966095635"
    workspace_id: Optional[str]  # Workspace code (part after '@') — e.g. "2865348338307293"
    email: Optional[str]         # Real email if x-forwarded-user is one, otherwise None


logger = logging.getLogger(__name__)

_dev_user_cache: Optional[str] = None
_CAPS_TTL_S = int(os.getenv('CAPS_TTL_S', '600'))
_caps_cache: dict[str, tuple[float, dict]] = {}

# Group check: is_account_group_member() run on behalf of the visitor, the only source of capabilities.
_CAPS_WAREHOUSE_ID = os.getenv('CAPS_WAREHOUSE_ID', '').strip()
_CAPS_CHAT_GROUPS = {'Role-Project-LEAP-End-users-Qualibot-ChatBot', 'Role-Project-LEAP-CoreDev', 'Role-Project-LEAP-CoreAdmin'}
_CAPS_COMPARE_GROUPS = {'Role-Project-LEAP-End-users-Qualibot-DocCompare', 'Role-Project-LEAP-CoreDev', 'Role-Project-LEAP-CoreAdmin'}

# CAPS_BYPASS=true grants chat and compare to every visitor (DEV only, set in utils/deploy/target_env.json);
# tests/test_deploy_config.py guards that it never reaches uat, uat-test or prod.
_CAPS_BYPASS = os.getenv('CAPS_BYPASS', 'false').strip().lower() == 'true'


def _is_dev() -> bool:
    return os.getenv('ENV', 'development') == 'development'


def _is_real_email(value: str) -> bool:
    """True if value looks like a real email (not a numeric Databricks workspace-scoped ID)."""
    if '@' not in value:
        return False
    local, _, domain = value.partition('@')
    return not (local.isdigit() and domain.isdigit())


def get_workspace_url() -> str:
    """Return the Databricks workspace base URL."""
    host = os.getenv('DATABRICKS_HOST', '')
    if not host:
        try:
            from databricks.sdk import WorkspaceClient
            host = WorkspaceClient().config.host or ''
        except Exception:
            pass
    if not host:
        return ''
    host = host.rstrip('/')
    if not host.startswith('http'):
        host = f'https://{host}'
    return host


async def get_current_user(request: Request) -> str:
    """Return the authenticated user's email or numeric user_id.

    Resolution order:
    1. x-forwarded-user — real email → use directly
    2. x-forwarded-user — numeric Databricks ID → return the numeric part
    3. WorkspaceClient.current_user.me() — local development only
    """
    forwarded = request.headers.get('x-forwarded-user', '').strip()
    if forwarded and _is_real_email(forwarded):
        return forwarded
    if forwarded:
        return forwarded.split('@', 1)[0]
    if _is_dev():
        return await _dev_user()
    raise ValueError('No user identity found in request headers.')


async def get_user_identity(request: Request) -> UserIdentity:
    """Return the split user_id, workspace_id, and email from the request headers."""
    forwarded = request.headers.get('x-forwarded-user', '').strip()

    if not forwarded:
        if _is_dev():
            user = await _dev_user()
            return UserIdentity(user_id=user, workspace_id=None, email=user)
        logger.warning('x-forwarded-user header is absent — cannot identify user')
        return UserIdentity(user_id='', workspace_id=None, email=None)

    if _is_real_email(forwarded):
        return UserIdentity(user_id=forwarded, workspace_id=None, email=forwarded)

    parts = forwarded.split('@', 1)
    clean_uid = parts[0]
    workspace_id: Optional[str] = parts[1] if len(parts) > 1 else None
    return UserIdentity(user_id=clean_uid, workspace_id=workspace_id, email=None)


_OPEN_CAPS = {'can_chat': True, 'can_compare': True, 'groups': []}
_CLOSED_CAPS = {'can_chat': False, 'can_compare': False, 'groups': []}


def _live_caps_via_sql(token: str) -> Optional[dict]:
    """is_account_group_member() on behalf of the requesting user. None = not usable, caller should fall back."""
    if not _CAPS_WAREHOUSE_ID:
        return None
    from databricks.sdk import WorkspaceClient
    chat_expr = ' OR '.join(f"is_account_group_member('{g}')" for g in _CAPS_CHAT_GROUPS)
    compare_expr = ' OR '.join(f"is_account_group_member('{g}')" for g in _CAPS_COMPARE_GROUPS)
    # auth_type='pat' forces token-only auth: the app's ambient OAuth env vars (DATABRICKS_CLIENT_ID/SECRET) would clash with
    # the explicit user token ('more than one authorization method configured').
    w = WorkspaceClient(host=get_workspace_url(), token=token, auth_type='pat')
    resp = w.statement_execution.execute_statement(
        warehouse_id=_CAPS_WAREHOUSE_ID,
        statement=f'SELECT ({chat_expr}) AS can_chat, ({compare_expr}) AS can_compare',
        wait_timeout='30s',
    )
    if resp.status.state.value != 'SUCCEEDED':
        raise RuntimeError(f'statement did not succeed: {resp.status}')
    row = resp.result.data_array[0]
    return {'can_chat': row[0] == 'true', 'can_compare': row[1] == 'true', 'groups': []}


async def get_capabilities(request: Request) -> dict:
    """Resolve {can_chat, can_compare, groups} for the current user.

    The visitor's own on-behalf-of token runs is_account_group_member(), so the
    answer is live and no job or table holds a copy of the groups. Fail-closed:
    a missing token, a missing warehouse or a failing check grants nothing, and
    a failure is not cached.

    CAPS_BYPASS=true short-circuits and grants everything (DEV only).
    """
    if _CAPS_BYPASS:
        return dict(_OPEN_CAPS)

    forwarded = request.headers.get('x-forwarded-user', '').strip()
    if _is_dev() and not forwarded:
        return dict(_OPEN_CAPS)

    key = forwarded or 'anon'
    now = time.monotonic()
    hit = _caps_cache.get(key)
    if hit and hit[0] > now:
        return hit[1]

    obo_token = request.headers.get('x-forwarded-access-token', '').strip()
    if not obo_token or not _CAPS_WAREHOUSE_ID:
        logger.error('Capabilities %s: no on-behalf-of token or CAPS_WAREHOUSE_ID — access denied', key)
        return dict(_CLOSED_CAPS)

    try:
        caps = await asyncio.to_thread(_live_caps_via_sql, obo_token)
    except Exception as e:
        logger.error('Capabilities %s: live group check failed (%s) — access denied', key, e)
        return dict(_CLOSED_CAPS)

    logger.info('Capabilities %s: can_chat=%s can_compare=%s', key, caps['can_chat'], caps['can_compare'])
    _caps_cache[key] = (now + _CAPS_TTL_S, caps)
    return caps


async def require_chat(request: Request) -> None:
    """FastAPI dependency: 403 unless the user may use the chatbot."""
    caps = await get_capabilities(request)
    if not caps['can_chat']:
        raise HTTPException(status_code=403, detail='Chat access not granted')


async def require_compare(request: Request) -> None:
    """FastAPI dependency: 403 unless the user may use document comparison."""
    caps = await get_capabilities(request)
    if not caps['can_compare']:
        raise HTTPException(status_code=403, detail='Document comparison access not granted')


async def _dev_user() -> str:
    global _dev_user_cache
    if _dev_user_cache:
        return _dev_user_cache
    from databricks.sdk import WorkspaceClient
    try:
        me = await asyncio.to_thread(lambda: WorkspaceClient().current_user.me())
        _dev_user_cache = me.user_name or me.display_name or ''
    except Exception as e:
        raise ValueError(f'Could not determine current user: {e}') from e
    return _dev_user_cache
