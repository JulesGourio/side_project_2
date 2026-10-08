"""Deploy/update the qualibot-sync-user-capabilities-uat job OUTSIDE the bundle.

WHY THIS EXISTS: the bundle's own deployer (job-runner-sa-uat) lacks USE
SCHEMA on system.access (a metastore-admin grant, still pending) and a
non-admin deployer can only set a job's run_as to itself, never to a human
user — so this job can never succeed when deployed through the pipeline/
bundle. Role-Project-LEAP-CoreDev members already have that grant, and a job
created directly by a human (no run_as set) runs as its own creator — so this
script must be run by a CoreDev human directly, never by the pipeline.

USAGE (run by a Role-Project-LEAP-CoreDev member, personal login):
    python utils/deploy/deploy_sync_user_capabilities_uat_personal.py [--profile UAT]

Idempotent: looks up the job by name and resets it in place if found,
otherwise creates it. Safe to re-run any time this script or the underlying
notebook changes.
"""
from __future__ import annotations

import argparse

from databricks.sdk import WorkspaceClient
from databricks.sdk.service import jobs
from databricks.sdk.service.compute import Environment

JOB_NAME = "qualibot-sync-user-capabilities-uat"
NOTEBOOK_PATH = "/Workspace/Shared/.bundle/qualibot/qualibot-uat/files/utils/user_capabilities/sync_user_capabilities_job.py"


def build_job_kwargs() -> dict:
    """Kwargs for jobs.create() — SDK objects, not pre-serialized dicts (create() serializes them itself)."""
    return dict(
        name=JOB_NAME,
        description=(
            "Sync user group membership (system.access.audit) -> "
            "users.groups/can_chat/can_compare, on doccompare (real UAT data). "
            "Deployed directly by a CoreDev human — see "
            "utils/deploy/deploy_sync_user_capabilities_uat_personal.py."
        ),
        max_concurrent_runs=1,
        tags={"project": "Qualibot"},
        tasks=[
            jobs.Task(
                task_key="sync_user_capabilities",
                notebook_task=jobs.NotebookTask(
                    notebook_path=NOTEBOOK_PATH,
                    source=jobs.Source.WORKSPACE,
                    base_parameters={
                        "dry_run": "false",
                        "LAKEBASE_PROJECT_ID": "qualibot",
                        "LAKEBASE_BRANCH": "production",
                        "LAKEBASE_ENDPOINT": "primary",
                        "LAKEBASE_DATABASE": "doccompare",
                    },
                ),
                environment_key="default",
            )
        ],
        environments=[
            jobs.JobEnvironment(
                environment_key="default",
                spec=Environment(
                    environment_version="2",
                    dependencies=["psycopg2-binary", "databricks-sdk>=0.118.0"],
                ),
            )
        ],
        # Same cadence as before: 15 min ahead of lakebase_export_uat_to_volume
        # (02:00/14:00) so a same-day group change is reflected before that
        # snapshot is taken.
        schedule=jobs.CronSchedule(
            quartz_cron_expression="0 45 1,13 * * ?",
            timezone_id="Europe/Paris",
            pause_status=jobs.PauseStatus.UNPAUSED,
        ),
        # run_as deliberately omitted: defaults to the identity running this
        # script, which is the whole point.
    )


import logging

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("deploy_sync_user_capabilities_uat_personal")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--profile", default="UAT", help="~/.databrickscfg profile to deploy under (default: UAT)")
    args = ap.parse_args()

    w = WorkspaceClient(profile=args.profile)
    me = w.current_user.me()
    logger.info(f"Deploying '{JOB_NAME}' as {me.user_name or me.display_name} (profile={args.profile})...")

    existing = [j for j in w.jobs.list(name=JOB_NAME)]

    for j in existing:
        # reset()/update() won't clear an existing run_as pinned to another
        # identity (job-runner-sa-uat) — delete + recreate so the new job
        # genuinely has no run_as override and defaults to its creator.
        prior_run_as = w.jobs.get(j.job_id).settings.run_as
        logger.info(f"Deleting existing job_id={j.job_id} (run_as was {prior_run_as})...")
        w.jobs.delete(job_id=j.job_id)

    created = w.jobs.create(**build_job_kwargs())
    job_id = created.job_id
    logger.info(f"Created new job_id={job_id}.")

    logger.info(f"Done. Job runs as its creator/deployer ({me.user_name or me.display_name}) — "
          f"re-run this script any time the notebook or schedule changes.")


if __name__ == "__main__":
    main()
