"""User service — resolve the current user's identity from request headers."""

import asyncio
import logging
import os
import time
from collections import deque
from typing import Any, Mapping, TypedDict

from fastapi import HTTPException, Request

from server.routers.errors import TOO_MANY_REQUESTS
from server.services.infra.config import RATE_LIMIT_PER_MINUTE


class UserIdentity(TypedDict):
    user_id: str                 # Numeric part only — e.g. "78655966095635"
    workspace_id: str | None  # Workspace code (part after '@') — e.g. "2865348338307293"
    email: str | None         # Real email if x-forwarded-user is one, otherwise None


logger = logging.getLogger(__name__)

_dev_user_cache: str | None = None
_CAPS_TTL_S = int(os.getenv('CAPS_TTL_S', '600'))
_CAPS_CACHE_MAX = 5000
_caps_cache: dict[str, tuple[float, dict]] = {}

# Allowed groups per feature; the sync job (utils/lakebase_sync/user_capabilities.py) tracks the same sets.
_CAPS_CHAT_GROUPS = {'Role-Project-LEAP-End-users-Qualibot-ChatBot', 'Role-Project-LEAP-CoreDev', 'Role-Project-LEAP-CoreAdmin'}
_CAPS_COMPARE_GROUPS = {'Role-Project-LEAP-End-users-Qualibot-DocCompare', 'Role-Project-LEAP-CoreDev', 'Role-Project-LEAP-CoreAdmin'}
_CAPS_ALL_GROUPS = _CAPS_CHAT_GROUPS | _CAPS_COMPARE_GROUPS

# DEV only: CAPS_BYPASS=true grants chat and compare to every visitor.
# tests/test_deploy_config.py guards that it never reaches uat or prod.
_CAPS_BYPASS = os.getenv('CAPS_BYPASS', 'false').strip().lower() == 'true'


def _is_dev() -> bool:
    """Whether the app runs in development (``ENV=development``).

    :return: True in development.
    """
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


def reject_anonymous(identity: Mapping[str, Any]) -> None:
    """Refuse a caller with no user id, so no two anonymous callers share one history. Development, and the DEV
    bypass, keep working without an identity header.

    :param identity: ``get_user_identity`` result
    :raises HTTPException: 401 when ``user_id`` is empty outside development
    """
    if not identity.get('user_id') and not (_is_dev() or _CAPS_BYPASS):
        raise HTTPException(status_code=401, detail='User not identified')


_OPEN_CAPS = {'can_chat': True, 'can_compare': True, 'groups': []}
# A visitor the app permission lets in without any allowed group (members of an IdP group nested in ChatBot are in
# neither source) gets the chat only: Compare stays with the groups that name it.
_DEFAULT_CAPS = {'can_chat': True, 'can_compare': False, 'groups': []}


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
    :return: the stored groups, or None when the job never computed them
    :raises RuntimeError: when Lakebase is unavailable
    """
    from server.services.infra import lakebase
    pool = lakebase.get_pool()
    if not pool:
        raise RuntimeError('Lakebase pool unavailable')
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


def _remember_caps(key: str, now: float, caps: dict) -> None:
    """Cache the capabilities of a visitor, dropping expired entries and then the oldest ones past the size cap.

    :param key: cache key of the visitor
    :param now: ``time.monotonic()`` reading
    :param caps: capabilities to keep for ``CAPS_TTL_S``
    """
    _caps_cache.pop(key, None)
    if len(_caps_cache) >= _CAPS_CACHE_MAX:
        for stale in [k for k, (expires, _) in _caps_cache.items() if expires <= now]:
            del _caps_cache[stale]
    while len(_caps_cache) >= _CAPS_CACHE_MAX:
        del _caps_cache[next(iter(_caps_cache))]
    _caps_cache[key] = (now + _CAPS_TTL_S, caps)


async def get_capabilities(request: Request) -> dict:
    """Resolve {can_chat, can_compare, groups} for the current user.

    The groups are the union of the users table, written by the sync job from the account audit log (account-level
    groups), and SCIM /Me on the visitor's own token (groups attached to the workspace). No allowed group found grants
    the chat only: the Databricks app permission already decides who reaches the app, and a visitor in a group only
    through another group is in neither source. The result is cached for CAPS_TTL_S, except when the users table could
    not be read: a visitor with no allowed group in SCIM gets the chat only, uncached, so an outage opens Compare to nobody.

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
    table_read = True
    try:
        stored = await _stored_groups(forwarded.split('@', 1)[0], header_email)
    except Exception as e:
        stored = None
        table_read = False
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
    elif not table_read:
        logger.warning('Capabilities %s: no allowed group found and users table not read (%s) — chat only, uncached',
                       key, '+'.join(sources) or 'no source')
        await _remember_identity(forwarded, email or header_email, display_name)
        return dict(_DEFAULT_CAPS)
    else:
        caps = dict(_DEFAULT_CAPS)
        logger.info('Capabilities %s: no allowed group found (%s) — chat only', key, '+'.join(sources))
    _remember_caps(key, now, caps)
    await _remember_identity(forwarded, email or header_email, display_name)
    return caps


_rate_hits: dict[str, deque[float]] = {}
_RATE_WINDOW_S = 60.0


def _rate_limited(user_key: str, now: float, limit: int) -> bool:
    """Count one request of a user in the last minute and say whether it exceeds the limit.

    :param user_key: who is asking
    :param now: ``time.monotonic()`` reading
    :param limit: requests accepted per minute
    :return: True when the request must be refused (a refused request is not counted)
    """
    hits = _rate_hits.setdefault(user_key, deque())
    while hits and hits[0] <= now - _RATE_WINDOW_S:
        hits.popleft()
    if len(hits) >= limit:
        return True
    hits.append(now)
    if len(_rate_hits) > _CAPS_CACHE_MAX:
        for other in [k for k, v in _rate_hits.items() if not v or v[-1] <= now - _RATE_WINDOW_S]:
            del _rate_hits[other]
    return False


def check_rate_limit(user_key: str) -> bool:
    """Count one model request of a user against ``RATE_LIMIT_PER_MINUTE``.

    :param user_key: the proxy's user header value, empty for an unidentified caller.
    :return: True when the request exceeds the limit and must be refused.
    """
    limit = RATE_LIMIT_PER_MINUTE
    if limit <= 0:
        return False
    return _rate_limited(user_key.strip() or 'anon', time.monotonic(), limit)


async def rate_limit(request: Request) -> None:
    """FastAPI dependency: 429 when the user sent more than ``RATE_LIMIT_PER_MINUTE`` model requests in the last minute.

    :param request: incoming request
    """
    if check_rate_limit(request.headers.get('x-forwarded-user', '')):
        raise HTTPException(status_code=429, detail=TOO_MANY_REQUESTS)


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
