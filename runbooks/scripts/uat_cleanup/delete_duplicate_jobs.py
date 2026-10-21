# Databricks notebook source
# One-shot, submitted as the UAT job SP: deletes the bundle jobs left over from the former bundle root. Each id is deleted only if its name matches and its bundle metadata still points at that root, so a job of the current root (/Workspace/.bundle/qualibot/qualibot-uat) can never be hit.
import logging

from databricks.sdk import WorkspaceClient
from databricks.sdk.errors import NotFound

logger = logging.getLogger("delete_duplicate_jobs")
logging.basicConfig(level=logging.INFO)

OLD_ROOT = "/Workspace/Shared/.bundle/qualibot/qualibot-uat/"
ORPHANS = {
    779339891964836: "D_1_Qualibot_Lakebase_Export_To_Volume",
    783022027143685: "W_1_Qualibot_Stop_Weekend_Uat",
    1090642136386557: "D_1_Qualibot_Parsing_Pipeline",
    342815175501077: "W_1_Qualibot_Start_Weekend_Uat",
    824967477595237: "Z_2_Qualibot_Create_Chat_Experiment",
    687448690184801: "D_2_Qualibot_Score_Chat_Traces",
    931830613336591: "Z_3_Qualibot_Register_Chat_Prompts",
}

client = WorkspaceClient()
failures = []

for job_id, expected_name in ORPHANS.items():
    try:
        job = client.jobs.get(job_id=job_id)
    except NotFound:
        logger.info("job %s: absent, nothing to delete", job_id)
        continue
    name = job.settings.name
    root = job.settings.deployment.metadata_file_path if job.settings.deployment else ""
    if name != expected_name or not (root or "").startswith(OLD_ROOT):
        failures.append(job_id)
        logger.error("job %s refused: name=%s metadata=%s", job_id, name, root)
        continue
    try:
        client.jobs.delete(job_id=job_id)
        logger.info("job %s deleted: %s", job_id, name)
    except Exception as error:
        failures.append(job_id)
        logger.error("job %s not deleted: %s", job_id, error)

if failures:
    raise RuntimeError(f"jobs not deleted: {failures}")
logger.info("Done")
