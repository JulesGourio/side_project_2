"""User service — resolve the current user's identity from request headers."""

import asyncio
import logging
import os
import time
from typing import TypedDict

from fastapi import HTTPException, Request


class UserIdentity(TypedDict):
    user_id: str                 # Numeric part only — e.g. "78655966095635"
    workspace_id: str | None  # Workspace code (part after '@') — e.g. "2865348338307293"
    email: str | None         # Real email if x-forwarded-user is one, otherwise None


logger = logging.getLogger(__name__)

_dev_user_cache: str | None = None
_CAPS_TTL_S = int(os.getenv('CAPS_TTL_S', '600'))
_caps_cache: dict[str, tuple[float, dict]] = {}

# Allowed groups per feature; the sync job (utils/lakebase_sync/user_capabilities.py) tracks the same sets.
_CAPS_CHAT_GROUPS = {'Role-Project-LEAP-End-users-Qualibot-ChatBot', 'Role-Project-LEAP-CoreDev', 'Role-Project-LEAP-CoreAdmin'}
_CAPS_COMPARE_GROUPS = {'Role-Project-LEAP-End-users-Qualibot-DocCompare', 'Role-Project-LEAP-CoreDev', 'Role-Project-LEAP-CoreAdmin'}
_CAPS_ALL_GROUPS = _CAPS_CHAT_GROUPS | _CAPS_COMPARE_GROUPS

# DEV only: CAPS_BYPASS=true grants chat and compare to every visitor.
# tests/test_deploy_config.py guards that it never reaches uat or prod.
_CAPS_BYPASS = os.getenv('CAPS_BYPASS', 'false').strip().lower() == 'true'


def _is_dev() -> bool:
    return os.getenv('ENV', 'production') == 'development'


def _is_real_email(value: str) -> bool:
    """True if value looks like a real email (not a numeric Databricks workspace-scoped ID).

    :param value: string to test, usually the ``x-forwarded-user`` header
    :return: True when it is an email and not a numeric workspace-scoped id
    """
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

    :param request: incoming request, read for its headers
    :return: the email, or the numeric user id
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
    """Return the split user_id, workspace_id, and email from the request headers.

    :param request: incoming request, read for its headers
    :return: the user id, the workspace id and the email, parsed from the headers
    """
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
    workspace_id: str | None = parts[1] if len(parts) > 1 else None
    return UserIdentity(user_id=clean_uid, workspace_id=workspace_id, email=None)


_OPEN_CAPS = {'can_chat': True, 'can_compare': True, 'groups': []}


def _caps_from_groups(groups: set[str]) -> dict:
    """Capabilities granted by a set of group names.

    :param groups: names of the groups the visitor belongs to, any group included
    :return: ``{can_chat, can_compare, groups}``, ``groups`` being the allowed groups found, sorted
    """
    matched = groups & _CAPS_ALL_GROUPS
    return {'can_chat': bool(matched & _CAPS_CHAT_GROUPS), 'can_compare': bool(matched & _CAPS_COMPARE_GROUPS),
            'groups': sorted(matched)}


def _scim_identity(token: str) -> tuple[set[str], str, str]:
    """The visitor's groups, e-mail and name from SCIM /Me with their own token.

    SCIM lists only the groups attached to the workspace; the account-level groups come from the users table.

    :param token: access token of the visitor
    :return: ``(group names, email, display name)``, the last two empty when unknown
    """
    from databricks.sdk import WorkspaceClient
    # auth_type='pat' forces token-only auth: the app's ambient OAuth env vars (DATABRICKS_CLIENT_ID/SECRET) would clash with
    # the explicit user token ('more than one authorization method configured').
    w = WorkspaceClient(host=get_workspace_url(), token=token, auth_type='pat')
    me = w.current_user.me()
    return {g.display for g in (me.groups or []) if g.display}, me.user_name or '', me.display_name or ''


async def _stored_groups(user_id: str, email: str) -> set[str] | None:
    """Groups written to ``users`` by the sync job for the visitor, matched on user id or e-mail.

    :param user_id: numeric Databricks id of the visitor
    :param email: e-mail of the visitor, empty when unknown
    :return: the stored groups, or None when the job never computed them or Lakebase is unavailable
    """
    from server.services.infra import lakebase
    pool = lakebase.get_pool()
    if not pool:
        return None
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            'SELECT groups FROM users WHERE groups_synced_at IS NOT NULL AND (user_id = $1 OR email = $2)',
            user_id, email or None,
        )
    if not rows:
        return None
    return {g for row in rows for g in (row['groups'] or [])}


async def _remember_identity(forwarded: str, email: str, display_name: str) -> None:
    """Record the visitor's email and name in ``users``; reports read the email from there.

    Databricks Apps sends the visitor as a numeric id only, so the email is not otherwise known.
    Never raises.

    :param forwarded: visitor id as sent by Databricks Apps, ``<user id>@<workspace id>``
    :param email: e-mail of the visitor, ignored unless it contains ``@``
    :param display_name: name of the visitor, stored as NULL when empty
    """
    if not (forwarded and email and '@' in email) or _is_real_email(forwarded):
        return
    try:
        from server.services.infra import lakebase
        pool = lakebase.get_pool()
        if not pool:
            return
        user_id, _, workspace_id = forwarded.partition('@')
        async with pool.acquire() as conn:
            await lakebase.upsert_user(conn, user_id=user_id, workspace_id=workspace_id or None, email=email,
                                       display_name=display_name or None)
    except Exception as e:  # noqa: BLE001 — a report column, never worth a failed request
        logger.warning('Identity of %s not recorded: %s', forwarded, e)


async def get_capabilities(request: Request) -> dict:
    """Resolve {can_chat, can_compare, groups} for the current user.

    The groups are the union of the users table, written by the sync job from the account audit log (account-level
    groups), and SCIM /Me on the visitor's own token (groups attached to the workspace). No allowed group found grants
    everything: the Databricks app permission already decides who reaches the app, the groups only narrow it, and a
    visitor in a group only through another group is in neither source. The result is cached for CAPS_TTL_S.

    CAPS_BYPASS=true short-circuits and grants everything (DEV only).

    :param request: incoming request, read for its headers
    :return: ``{can_chat, can_compare, groups}`` for the current user
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

    header_email = request.headers.get('x-forwarded-email', '').strip()
    groups: set[str] = set()
    sources: list[str] = []
    try:
        stored = await _stored_groups(forwarded.split('@', 1)[0], header_email)
    except Exception as e:
        stored = None
        logger.warning('Capabilities %s: users table not read (%s)', key, e)
    if stored is not None:
        groups |= stored
        sources.append('TABLE')

    email = display_name = ''
    obo_token = request.headers.get('x-forwarded-access-token', '').strip()
    if obo_token:
        try:
            scim_groups, email, display_name = await asyncio.to_thread(_scim_identity, obo_token)
            groups |= scim_groups
            sources.append('SCIM')
        except Exception as e:
            logger.info('Capabilities %s: SCIM not read (%s)', key, e)

    caps = _caps_from_groups(groups)
    if caps['groups']:
        logger.info('Capabilities %s: can_chat=%s can_compare=%s (%s) groups=%s', key, caps['can_chat'],
                    caps['can_compare'], '+'.join(sources), caps['groups'])
    else:
        caps = dict(_OPEN_CAPS)
        logger.warning('Capabilities %s: no allowed group found (%s) — granting all (fail-open)', key,
                       '+'.join(sources) or 'no source')
    _caps_cache[key] = (now + _CAPS_TTL_S, caps)
    await _remember_identity(forwarded, email or header_email, display_name)
    return caps


async def require_chat(request: Request) -> None:
    """FastAPI dependency: 403 unless the user may use the chatbot.

    :param request: incoming request
    """
    caps = await get_capabilities(request)
    if not caps['can_chat']:
        raise HTTPException(status_code=403, detail='Chat access not granted')


async def require_compare(request: Request) -> None:
    """FastAPI dependency: 403 unless the user may use document comparison.

    :param request: incoming request
    """
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
