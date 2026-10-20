"""User service — resolve the current user's identity from request headers."""

import asyncio
import logging
import os
import time
from typing import Tuple, TypedDict

from fastapi import HTTPException, Request


class UserIdentity(TypedDict):
    user_id: str                 # Numeric part only — e.g. "78655966095635"
    workspace_id: str | None  # Workspace code (part after '@') — e.g. "2865348338307293"
    email: str | None         # Real email if x-forwarded-user is one, otherwise None


logger = logging.getLogger(__name__)

_dev_user_cache: str | None = None
_CAPS_TTL_S = int(os.getenv('CAPS_TTL_S', '600'))
_caps_cache: dict[str, tuple[float, dict]] = {}

# Group check on behalf of the visitor, the only source of capabilities: the visitor's own groups from SCIM, then
# is_account_group_member() on this warehouse when SCIM fails or shows no group (the visitor then needs CAN_USE on it).
_CAPS_WAREHOUSE_ID = os.getenv('CAPS_WAREHOUSE_ID', '').strip()
_CAPS_CHAT_GROUPS = {'Role-Project-LEAP-End-users-Qualibot-ChatBot', 'Role-Project-LEAP-CoreDev', 'Role-Project-LEAP-CoreAdmin'}
_CAPS_COMPARE_GROUPS = {'Role-Project-LEAP-End-users-Qualibot-DocCompare', 'Role-Project-LEAP-CoreDev', 'Role-Project-LEAP-CoreAdmin'}

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


def _live_caps_via_sql(token: str) -> dict:
    """is_account_group_member() for each allowed group, run on behalf of the visitor.

    It sees account-level groups and nested membership, which SCIM /Me does not list.

    :param token: access token of the visitor
    :return: the capabilities, ``groups`` being the allowed groups the visitor belongs to
    """
    from databricks.sdk import WorkspaceClient
    groups = sorted(_CAPS_CHAT_GROUPS | _CAPS_COMPARE_GROUPS)
    # auth_type='pat' forces token-only auth: the app's ambient OAuth env vars (DATABRICKS_CLIENT_ID/SECRET) would clash with
    # the explicit user token ('more than one authorization method configured').
    w = WorkspaceClient(host=get_workspace_url(), token=token, auth_type='pat')
    resp = w.statement_execution.execute_statement(
        warehouse_id=_CAPS_WAREHOUSE_ID,
        statement='SELECT ' + ', '.join(f"is_account_group_member('{g}')" for g in groups),
        wait_timeout='30s',
    )
    if resp.status.state.value != 'SUCCEEDED':
        raise RuntimeError(f'statement did not succeed: {resp.status}')
    matched = {g for g, v in zip(groups, resp.result.data_array[0]) if v == 'true'}
    return {'can_chat': bool(matched & _CAPS_CHAT_GROUPS), 'can_compare': bool(matched & _CAPS_COMPARE_GROUPS),
            'groups': sorted(matched)}


def _live_caps_via_scim(token: str) -> dict:
    """The visitor's groups from SCIM /Me with their own token: no warehouse permission and no SQL scope needed.

    :param token: access token of the visitor
    :return: the capabilities from the visitor's groups
    """
    from databricks.sdk import WorkspaceClient
    w = WorkspaceClient(host=get_workspace_url(), token=token, auth_type='pat')
    me = w.current_user.me()
    names = {g.display for g in (me.groups or []) if g.display}
    matched = names & (_CAPS_CHAT_GROUPS | _CAPS_COMPARE_GROUPS)
    return {'can_chat': bool(names & _CAPS_CHAT_GROUPS), 'can_compare': bool(names & _CAPS_COMPARE_GROUPS),
            'groups': sorted(matched), 'email': me.user_name or '', 'display_name': me.display_name or ''}


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


def _resolve_caps(token: str) -> Tuple[dict, str]:
    """(capabilities, source). The warehouse check when a warehouse is set, SCIM when it is not or when it fails.

    Raises when no source could answer, or when the warehouse check failed and SCIM shows no allowed group: SCIM cannot
    see account-level or nested groups, so that answer is not a refusal.

    :param token: access token of the visitor
    :return: ``(capabilities, source)``, the source being ``SQL`` or ``SCIM``
    """
    sql_error: Exception | None = None
    if _CAPS_WAREHOUSE_ID:
        try:
            return _live_caps_via_sql(token), 'SQL'
        except Exception as e:
            sql_error = e
            logger.warning('Capabilities: warehouse check failed (%s) — trying SCIM', e)
    scim = _live_caps_via_scim(token)
    if sql_error is not None and not (scim['can_chat'] or scim['can_compare']):
        raise RuntimeError(f'SCIM shows no allowed group and the warehouse check failed ({sql_error})')
    return scim, 'SCIM'


async def get_capabilities(request: Request) -> dict:
    """Resolve {can_chat, can_compare, groups} for the current user.

    The visitor's own on-behalf-of token asks is_account_group_member() on the warehouse
    (SCIM /Me when no warehouse is set or the warehouse fails), so the answer is live
    and no job or table holds a copy of them. A check that shows no allowed group
    denies. A check that cannot be made (no token, every source failing) grants
    everything and is not cached: the Databricks app permission already decides who
    reaches the app, the groups only narrow it.

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

    obo_token = request.headers.get('x-forwarded-access-token', '').strip()
    if not obo_token:
        logger.warning('Capabilities %s: no on-behalf-of token — granting all (fail-open)', key)
        return dict(_OPEN_CAPS)

    try:
        caps, source = await asyncio.to_thread(_resolve_caps, obo_token)
    except Exception as e:
        logger.warning('Capabilities %s: live group check failed (%s) — granting all (fail-open)', key, e)
        return dict(_OPEN_CAPS)

    email, display_name = caps.pop('email', ''), caps.pop('display_name', '')
    logger.info('Capabilities %s: can_chat=%s can_compare=%s (%s) groups=%s', key, caps['can_chat'], caps['can_compare'],
                source, caps['groups'])
    _caps_cache[key] = (now + _CAPS_TTL_S, caps)
    await _remember_identity(forwarded, email, display_name)
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
