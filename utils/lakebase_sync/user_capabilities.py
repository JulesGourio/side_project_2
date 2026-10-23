"""Group membership of the app's visitors, rebuilt from the account audit log and written to the Lakebase ``users`` table.

The app reads ``users.groups`` for the visitors whose ``groups_synced_at`` is set. The audit log is the only source
that sees the account-level groups ``Qualibot-ChatBot`` and ``Qualibot-DocCompare``: they are not attached to the
workspaces, so SCIM does not list them, and the job identity cannot ask ``is_account_group_member()`` for someone else.
"""
from __future__ import annotations

import logging
from typing import Any, Iterable

logger = logging.getLogger(__name__)

CAPS_CHAT_GROUPS = frozenset({
    'Role-Project-LEAP-End-users-Qualibot-ChatBot', 'Role-LATECOERE-User-Internal',
    'Role-Project-LEAP-CoreDev', 'Role-Project-LEAP-CoreAdmin',
})
CAPS_COMPARE_GROUPS = frozenset({
    'Role-Project-LEAP-End-users-Qualibot-DocCompare', 'Role-Project-LEAP-CoreDev', 'Role-Project-LEAP-CoreAdmin',
})
CAPS_ALL_GROUPS = CAPS_CHAT_GROUPS | CAPS_COMPARE_GROUPS

# Latest add/remove event per (user, tracked group). The audit keeps about a year: older pairs keep the stored value.
AUDIT_SQL = """
WITH ev AS (
  SELECT
    request_params['targetUserId']    AS user_id,
    request_params['targetUserName']  AS email,
    request_params['targetGroupName'] AS group_name,
    action_name,
    ROW_NUMBER() OVER (
      PARTITION BY request_params['targetUserId'], request_params['targetGroupName']
      ORDER BY event_time DESC
    ) AS rn
  FROM system.access.audit
  WHERE action_name IN ('addPrincipalToGroup', 'removePrincipalFromGroup')
    AND request_params['targetGroupName'] IN ({groups})
    AND request_params['targetUserId'] IS NOT NULL
)
SELECT user_id, email, group_name, action_name = 'addPrincipalToGroup' AS is_member
FROM ev
WHERE rn = 1
""".format(groups=', '.join(f"'{g}'" for g in sorted(CAPS_ALL_GROUPS)))


# Columns the app's schema bootstrap adds to users (server/services/infra/lakebase/schema.py).
USER_GROUP_COLUMNS = (
    ('can_chat', 'BOOLEAN NOT NULL DEFAULT TRUE'),
    ('can_compare', 'BOOLEAN NOT NULL DEFAULT TRUE'),
    ('groups', "TEXT[] NOT NULL DEFAULT '{}'"),
    ('groups_synced_at', 'TIMESTAMPTZ'),
)


def merge_groups(events: Iterable[tuple[str, str | None, str, bool]],
                 stored: dict[str, set[str]]) -> dict[str, dict[str, Any]]:
    """Groups of every visitor the audit mentions, the audit winning over the stored value for the pairs it holds.

    :param events: ``(user_id, email, group, is_member)``, the latest audit event per (user, tracked group)
    :param stored: ``users.groups`` already stored, by user id
    :return: by user id, ``{email, groups, can_chat, can_compare}``; ``groups`` holds tracked groups only, sorted
    """
    result: dict[str, dict[str, Any]] = {}
    for user_id, email, group, is_member in events:
        if group not in CAPS_ALL_GROUPS or not user_id:
            continue
        entry = result.setdefault(user_id, {'email': None, 'groups': set(stored.get(user_id, set())) & CAPS_ALL_GROUPS})
        entry['email'] = entry['email'] or (email or None)
        (entry['groups'].add if is_member else entry['groups'].discard)(group)
    for entry in result.values():
        groups = entry['groups']
        entry['can_chat'] = bool(groups & CAPS_CHAT_GROUPS)
        entry['can_compare'] = bool(groups & CAPS_COMPARE_GROUPS)
        entry['groups'] = sorted(groups)
    return result


def read_stored_groups(conn: Any) -> dict[str, set[str]]:
    """``users.groups`` of every row whose groups the job already computed.

    :param conn: open psycopg2 connection to the app's Lakebase database
    :return: tracked groups by user id; empty when the group columns do not exist yet
    """
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM information_schema.columns WHERE table_name = 'users' AND column_name = 'groups_synced_at'")
        if not cur.fetchone():
            return {}
        cur.execute('SELECT user_id, groups FROM users WHERE groups_synced_at IS NOT NULL')
        return {user_id: set(groups or []) for user_id, groups in cur.fetchall()}


def write_groups(conn: Any, merged: dict[str, dict[str, Any]]) -> int:
    """Write the groups and capabilities of each visitor, matched on user id or email, then commit.

    A visitor with no row yet gets one, so the app finds the groups on the first visit.

    :param conn: open psycopg2 connection to the app's Lakebase database
    :param merged: result of :func:`merge_groups`
    :return: number of visitors written
    """
    with conn.cursor() as cur:
        for user_id, entry in merged.items():
            values = (entry['groups'], entry['can_chat'], entry['can_compare'])
            cur.execute(
                'UPDATE users SET groups = %s, can_chat = %s, can_compare = %s, groups_synced_at = NOW(),'
                ' updated_at = NOW() WHERE user_id = %s OR email = %s',
                (*values, user_id, entry['email']),
            )
            if cur.rowcount:
                continue
            cur.execute(
                'INSERT INTO users (user_id, email, groups, can_chat, can_compare, groups_synced_at)'
                ' VALUES (%s, %s, %s, %s, %s, NOW())'
                ' ON CONFLICT (user_id) DO UPDATE SET groups = EXCLUDED.groups, can_chat = EXCLUDED.can_chat,'
                ' can_compare = EXCLUDED.can_compare, groups_synced_at = NOW(), updated_at = NOW()',
                (user_id, entry['email'], *values),
            )
    conn.commit()
    return len(merged)


def check_columns(conn: Any) -> None:
    """Fail when ``users`` lacks a group column; the app, which owns the table, adds them at startup.

    The job identity may write rows but not alter a table it does not own.

    :param conn: open psycopg2 connection to the app's Lakebase database
    """
    with conn.cursor() as cur:
        cur.execute("SELECT column_name FROM information_schema.columns WHERE table_name = 'users'")
        present = {row[0] for row in cur.fetchall()}
    missing = [col for col, _ in USER_GROUP_COLUMNS if col not in present]
    if missing:
        raise RuntimeError(f'users lacks {missing}: start the app with this code version, it adds them')

