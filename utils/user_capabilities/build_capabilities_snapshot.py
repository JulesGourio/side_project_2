"""Generate server/config/capabilities_snapshot.json from Databricks audit logs.

WHY THIS EXISTS: workspace SCIM /Me does NOT surface ACCOUNT-level group
memberships, so users provisioned at the account level (by the IdP) into
Role-Project-LEAP-End-users-Qualibot-* groups were wrongly denied chat/compare.
Until the app's service principal is granted SELECT on `system.access`, this
script resolves membership OFFLINE (via a CoreDev member's credentials — the
only principal granted that SELECT) and bakes the result into a static snapshot
the app merges into whatever /Me returns at startup.
See README.md#capabilities-snapshot-account-group-visibility for the full story.

This reads the audit log's net membership: for each (user, group) the most
recent addPrincipalToGroup / removePrincipalFromGroup event wins.

USAGE
-----
Re-run whenever group membership changes, then redeploy the app:

    python utils/user_capabilities/build_capabilities_snapshot.py --profile DEV

Requires the databricks-sdk and a CLI profile whose user is in CoreDev.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from databricks.sdk import WorkspaceClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # utils/ — shared config.py
from ops_config import WAREHOUSE_ID as DEFAULT_WAREHOUSE_ID, CAPS_ALL_GROUPS

GROUPS = sorted(CAPS_ALL_GROUPS)

SQL = """
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
SELECT user_id, email, group_name
FROM ev
WHERE rn = 1 AND action_name = 'addPrincipalToGroup'
ORDER BY email, group_name
""".format(groups=", ".join("'{}'".format(g) for g in GROUPS))

OUT_PATH = Path(__file__).resolve().parents[3] / "server" / "config" / "capabilities_snapshot.json"


import logging

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("build_capabilities_snapshot")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--profile", default="DEV", help="Databricks CLI profile (user must be in CoreDev)")
    ap.add_argument("--warehouse-id", default=DEFAULT_WAREHOUSE_ID)
    args = ap.parse_args()

    w = WorkspaceClient(profile=args.profile)
    resp = w.statement_execution.execute_statement(
        warehouse_id=args.warehouse_id, statement=SQL, wait_timeout="50s",
    )
    if resp.status and resp.status.state and resp.status.state.value != "SUCCEEDED":
        raise SystemExit(f"Query did not succeed: {resp.status.state} {getattr(resp.status, 'error', '')}")
    rows = (resp.result.data_array if resp.result else None) or []

    users: dict[str, dict] = {}
    for user_id, email, group_name in rows:
        if not user_id:
            continue
        entry = users.setdefault(user_id, {"email": email, "groups": []})
        if group_name not in entry["groups"]:
            entry["groups"].append(group_name)

    snapshot = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": "system.access.audit — net membership of capability groups",
        "groups_tracked": GROUPS,
        "users": dict(sorted(users.items(), key=lambda kv: kv[1].get("email") or kv[0])),
    }
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(snapshot, indent=2, ensure_ascii=False), encoding="utf-8")
    logger.info(f"Wrote {len(users)} users to {OUT_PATH}")


if __name__ == "__main__":
    main()
