"""Sync user capabilities (can_chat, can_compare, groups) from the audit log to Lakebase.

Merge rule: for each (user, group) pair seen in system.access.audit (~1 year of
retention), the audit wins (add = member, remove = removed). Pairs absent from
the audit keep whatever is already in `users.groups`, so no access is lost as
long as this runs at least once a year.

USAGE
-----
    python utils/databricks_ops/user_capabilities/sync_user_capabilities.py           # -> DEV (default)
    python utils/databricks_ops/user_capabilities/sync_user_capabilities.py --env UAT
    python utils/databricks_ops/user_capabilities/sync_user_capabilities.py --env DEV UAT
    python utils/databricks_ops/user_capabilities/sync_user_capabilities.py --dry-run   # CSV preview, no write
"""
from __future__ import annotations

import argparse
import csv
import os
import sys
from pathlib import Path

from databricks.sdk import WorkspaceClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # utils/databricks_ops/ — shared config.py
from config import (
    lakebase_connect,
    WAREHOUSE_ID,
    PROFILE_DEV,
    CAPS_CHAT_GROUPS,
    CAPS_COMPARE_GROUPS,
    CAPS_ALL_GROUPS,
    LAKEBASE,
)

# Returns the latest event per (user, group) pair — both add AND remove included.
_SQL = """
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
)
SELECT user_id, email, group_name, action_name
FROM ev
WHERE rn = 1
ORDER BY email, group_name
""".format(groups=", ".join(f"'{g}'" for g in sorted(CAPS_ALL_GROUPS)))


_SQL_LIST_GROUPS = """
SELECT
    request_params['targetGroupName'] AS group_name,
    COUNT(DISTINCT request_params['targetUserId']) AS nb_users,
    SUM(CASE WHEN action_name = 'addPrincipalToGroup'    THEN 1 ELSE 0 END) AS adds,
    SUM(CASE WHEN action_name = 'removePrincipalFromGroup' THEN 1 ELSE 0 END) AS removes
FROM system.access.audit
WHERE action_name IN ('addPrincipalToGroup', 'removePrincipalFromGroup')
  AND request_params['targetGroupName'] LIKE 'Role-Project-LEAP%'
GROUP BY 1
ORDER BY 1
"""


def list_leap_groups(profile: str) -> None:
    """Prints all LEAP groups found in the audit log."""
    w = WorkspaceClient(profile=profile)
    resp = w.statement_execution.execute_statement(
        warehouse_id=WAREHOUSE_ID, statement=_SQL_LIST_GROUPS, wait_timeout="30s",
    )
    if resp.status and resp.status.state and resp.status.state.value != "SUCCEEDED":
        raise SystemExit(f"Query failed: {resp.status.state}")

    rows = (resp.result.data_array if resp.result else None) or []
    tracked = CAPS_ALL_GROUPS

    print(f"\n{'Group':<65} {'Users':>6} {'Add':>5} {'Rmv':>5}  {'Status'}")
    print("-" * 95)
    for group_name, nb_users, adds, removes in rows:
        status = "tracked" if group_name in tracked else "not tracked"
        print(f"{group_name:<65} {nb_users:>6} {adds:>5} {removes:>5}  {status}")
    print(f"\n{len(rows)} LEAP group(s) found. "
          f"{sum(1 for r in rows if r[0] not in tracked)} not tracked.")


def fetch_audit_events(profile: str) -> dict[tuple[str, str], dict]:
    """Returns {(user_id, group): {is_member, email}} — latest event per pair."""
    w = WorkspaceClient(profile=profile)
    resp = w.statement_execution.execute_statement(
        warehouse_id=WAREHOUSE_ID, statement=_SQL, wait_timeout="50s",
    )
    if resp.status and resp.status.state and resp.status.state.value != "SUCCEEDED":
        raise SystemExit(f"Audit query failed: {resp.status.state} {getattr(resp.status, 'error', '')}")

    rows = (resp.result.data_array if resp.result else None) or []
    result: dict[tuple[str, str], dict] = {}
    for user_id, email, group_name, action_name in rows:
        if not user_id:
            continue
        result[(user_id, group_name)] = {
            'is_member': action_name == 'addPrincipalToGroup',
            'email': email,
        }
    return result


def sync_to_lakebase(audit: dict[tuple[str, str], dict], env: str) -> int:
    """Merges the audit with the existing users.groups, writes groups/can_chat/can_compare."""
    conn = lakebase_connect(env)
    updated = 0
    try:
        with conn.cursor() as cur:
            # ── Check whether the optional columns exist ──────────────────────
            cur.execute("""
                SELECT EXISTS (
                    SELECT 1 FROM information_schema.columns
                    WHERE table_name='users' AND column_name='groups'
                )
            """)
            has_groups = cur.fetchone()[0]

            cur.execute("""
                SELECT EXISTS (
                    SELECT 1 FROM information_schema.columns
                    WHERE table_name='users' AND column_name='is_manual'
                )
            """)
            has_is_manual = cur.fetchone()[0]

            # ── Read current state ─────────────────────────────────────────────
            if has_groups:
                extra = ", is_manual" if has_is_manual else ""
                cur.execute(f"SELECT user_id, email, groups{extra} FROM users")
                db_rows = {
                    row[0]: {
                        'email': row[1],
                        'groups': set(row[2] or []),
                        'is_manual': bool(row[3]) if has_is_manual else False,
                    }
                    for row in cur.fetchall()
                }
            else:
                extra = ", is_manual" if has_is_manual else ""
                cur.execute(f"SELECT user_id, email{extra} FROM users")
                db_rows = {
                    row[0]: {
                        'email': row[1],
                        'groups': set(),
                        'is_manual': bool(row[2]) if has_is_manual else False,
                    }
                    for row in cur.fetchall()
                }

            # ── Collect all user_ids involved (audit + DB) ────────────────────
            audit_users: dict[str, dict] = {}
            for (uid, grp), info in audit.items():
                entry = audit_users.setdefault(uid, {'email': info['email'], 'groups': set()})
                entry['email'] = entry['email'] or info['email']
                if info['is_member']:
                    entry['groups'].add(grp)

            all_uids = set(audit_users) | set(db_rows)

            for uid in all_uids:
                db      = db_rows.get(uid, {'email': None, 'groups': set(), 'is_manual': False})
                if db['is_manual']:
                    continue
                audit_u = audit_users.get(uid, {'email': None, 'groups': set()})

                # Merge: for each tracked group, the audit is authoritative if
                # a (uid, grp) pair appears in it; otherwise keep the DB value.
                merged_groups = set(db['groups'])
                for grp in CAPS_ALL_GROUPS:
                    if (uid, grp) in audit:
                        if audit[(uid, grp)]['is_member']:
                            merged_groups.add(grp)
                        else:
                            merged_groups.discard(grp)
                # Groups outside CAPS_ALL_GROUPS: kept as-is from the DB.

                email       = db['email'] or audit_u['email']
                can_chat    = bool(merged_groups & CAPS_CHAT_GROUPS)
                can_compare = bool(merged_groups & CAPS_COMPARE_GROUPS)
                groups_list = sorted(merged_groups)

                # Optional columns/values assembled dynamically to avoid
                # duplicating the 4 query variants.
                extra_cols: list[tuple[str, object]] = []
                if has_groups:
                    extra_cols.append(("groups", groups_list))

                if email:
                    set_extra = "".join(f", {c}=%s" for c, _ in extra_cols)
                    cur.execute(
                        f"UPDATE users SET can_chat=%s, can_compare=%s{set_extra}, updated_at=NOW() WHERE email=%s",
                        (can_chat, can_compare, *[v for _, v in extra_cols], email),
                    )
                    if cur.rowcount:
                        updated += 1
                        continue

                cols = "".join(f", {c}" for c, _ in extra_cols)
                placeholders = ", %s" * len(extra_cols)
                conflict_extra = "".join(f", {c} = EXCLUDED.{c}" for c, _ in extra_cols)
                cur.execute(
                    f"""INSERT INTO users (user_id, email, can_chat, can_compare{cols})
                        VALUES (%s, %s, %s, %s{placeholders})
                        ON CONFLICT (user_id) DO UPDATE SET
                            email       = COALESCE(EXCLUDED.email, users.email),
                            can_chat    = EXCLUDED.can_chat,
                            can_compare = EXCLUDED.can_compare{conflict_extra},
                            updated_at  = NOW()""",
                    (uid, email, can_chat, can_compare, *[v for _, v in extra_cols]),
                )
                updated += 1

        conn.commit()
    finally:
        conn.close()
    return updated


def preview_to_csv(audit: dict[tuple[str, str], dict], path: str) -> int:
    """Generates a CSV with the rights computed from the audit, without writing to Lakebase."""
    users: dict[str, dict] = {}
    for (uid, grp), info in audit.items():
        entry = users.setdefault(uid, {'email': info['email'], 'groups': set()})
        entry['email'] = entry['email'] or info['email']
        if info['is_member']:
            entry['groups'].add(grp)

    rows = []
    for uid, u in sorted(users.items(), key=lambda x: (x[1]['email'] or x[0])):
        rows.append({
            'user_id':       uid,
            'email':         u['email'] or '',
            'can_chat':      bool(u['groups'] & CAPS_CHAT_GROUPS),
            'can_compare':   bool(u['groups'] & CAPS_COMPARE_GROUPS),
            'groups':        '|'.join(sorted(u['groups'])),
        })

    with open(path, 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=['user_id', 'email', 'can_chat', 'can_compare', 'groups'])
        writer.writeheader()
        writer.writerows(rows)
    return len(rows)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument(
        "--env", nargs="+", choices=list(LAKEBASE), default=["DEV"],
        metavar="ENV",
        help=f"Target Lakebase environment(s). Choices: {list(LAKEBASE)}. Default: DEV",
    )
    ap.add_argument(
        "--audit-profile", default=PROFILE_DEV,
        help="CLI profile to read the audit log (CoreDev). Default: DEV",
    )
    ap.add_argument(
        "--list-groups", action="store_true",
        help="List all LEAP groups in the audit log and exit.",
    )
    ap.add_argument(
        "--dry-run", action="store_true",
        help="Preview only: generates a local CSV without writing to Lakebase.",
    )
    args = ap.parse_args()

    if args.list_groups:
        list_leap_groups(args.audit_profile)
        return

    print(f"[1/2] Reading the audit log (profile={args.audit_profile})...")
    audit = fetch_audit_events(args.audit_profile)
    adds    = sum(1 for v in audit.values() if v['is_member'])
    removes = sum(1 for v in audit.values() if not v['is_member'])
    print(f"      {len(audit)} (user, group) pair(s) — {adds} active, {removes} removed\n")

    if args.dry_run:
        out = os.path.join(os.path.dirname(__file__), "users_preview.csv")
        n = preview_to_csv(audit, out)
        print(f"[dry-run] {n} user(s) computed -> {os.path.abspath(out)}")
        print("Nothing written to the database. Re-run without --dry-run to push.")
        return

    for env in args.env:
        print(f"[2/2] Syncing to Lakebase {env}...")
        n = sync_to_lakebase(audit, env)
        print(f"      {n} user(s) updated (groups / can_chat / can_compare).")

    print("\nDone.")


if __name__ == "__main__":
    main()
