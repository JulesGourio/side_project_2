# Databricks notebook source
# MAGIC %md
# MAGIC # Stop/start Databricks Apps (generic, parameterized)
# MAGIC
# MAGIC One notebook for every off-hours job (nightly and weekend) of the UAT and DEV
# MAGIC workspaces. `action` and `app_names` come from the job's `base_parameters`
# MAGIC (see `databricks.yml`).
# MAGIC
# MAGIC Runs under the job's own identity (`WorkspaceClient()`, no profile), which needs
# MAGIC CAN_MANAGE on every app in `app_names`.

# COMMAND ----------

dbutils.widgets.removeAll()
dbutils.widgets.dropdown("action", "stop", ["start", "stop"], "Action")
dbutils.widgets.text("app_names", "", "Comma-separated app names")

ACTION = dbutils.widgets.get("action")
APP_NAMES = [n.strip() for n in dbutils.widgets.get("app_names").split(",") if n.strip()]

if not APP_NAMES:
    raise ValueError("app_names is empty — pass a comma-separated list of Databricks App names.")

import logging

logger = logging.getLogger("stop_start_app_job")
logger.setLevel(logging.INFO)
if not logger.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S"))
    logger.addHandler(_handler)
    logger.propagate = False


logger.info(f"action={ACTION}  apps={APP_NAMES}")

# COMMAND ----------

from databricks.sdk import WorkspaceClient

w = WorkspaceClient()  # ambient auth — this job's own identity in this workspace

# States that already satisfy the requested action — skip the call rather
# than let the SDK reject a redundant stop/start.
ALREADY_DONE = {
    "stop": {"STOPPED", "STOPPING"},
    "start": {"ACTIVE", "STARTING"},
}[ACTION]

failures = []
for name in APP_NAMES:
    try:
        current_state = w.apps.get(name=name).compute_status.state.value
        if current_state in ALREADY_DONE:
            logger.info(f"  {name}: already {current_state.lower()} — skipping")
            continue
        if ACTION == "stop":
            w.apps.stop(name=name)
        else:
            w.apps.start(name=name)
        logger.info(f"  {name}: {ACTION} OK")
    except Exception as e:
        logger.info(f"  {name}: {ACTION} FAILED — {e}")
        failures.append(name)

if failures:
    raise RuntimeError(f"{ACTION} failed for: {', '.join(failures)}")

logger.info(f"Done — {len(APP_NAMES)} app(s) processed.")


# COMMAND ----------
