# Databricks notebook source
# One-shot, submitted as the PROD job service principal: deletes the legacy jobs and serving endpoints of the former
# Knowledge Assistant setup. The names are written out one by one: nothing else can be deleted. Dry run by default.

import logging

from databricks.sdk import WorkspaceClient

logger = logging.getLogger("drop_prod_legacy")
logging.basicConfig(level=logging.INFO)

dbutils.widgets.removeAll()
dbutils.widgets.dropdown("dry_run", "true", ["true", "false"], "Dry run (list only)")
DRY_RUN = dbutils.widgets.get("dry_run") == "true"

JOBS = [
    "qualibot-provision-knowledge-assistant-qualibot-prod",
    "D_1_qualibot-parsing-pipeline-qualibot-prod",
]
ENDPOINTS = [
    "ka-3315943a-endpoint",
    "ka-5d35f4d5-endpoint",
    "ka-88a4da35-endpoint",
    "ka-c9dbc4db-endpoint",
]

w = WorkspaceClient()
failures = []

for name in JOBS:
    matches = [job for job in w.jobs.list(name=name) if job.settings and job.settings.name == name]
    logger.info("job %s: %d match(es) %s", name, len(matches), [job.job_id for job in matches])
    for job in matches:
        if DRY_RUN:
            continue
        try:
            w.jobs.delete(job_id=job.job_id)
            logger.info("job deleted: %s (%s)", name, job.job_id)
        except Exception as error:
            failures.append(f"job {name}")
            logger.error("job not deleted: %s: %s", name, error)

existing = {endpoint.name for endpoint in w.serving_endpoints.list()}
for name in ENDPOINTS:
    logger.info("endpoint %s: %s", name, "present" if name in existing else "absent")
    if name in existing and not DRY_RUN:
        try:
            w.serving_endpoints.delete(name=name)
            logger.info("endpoint deleted: %s", name)
        except Exception as error:
            failures.append(f"endpoint {name}")
            logger.error("endpoint not deleted: %s: %s", name, error)

if failures:
    raise RuntimeError(f"not done: {failures}")
