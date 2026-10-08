# Databricks notebook source
# MAGIC %md
# MAGIC # Stop/start Databricks Apps (generic, parameterized)
# MAGIC
# MAGIC One reusable notebook for every "off-hours" job (nightly 21h-7h, weekend)
# MAGIC across the UAT and DEV workspaces. `action` and `app_names` come from job
# MAGIC parameters (see `databricks.yml`) — this file is never edited per-schedule,
# MAGIC only the job's `base_parameters` change.
# MAGIC
# MAGIC Uses the job's own ambient identity (`WorkspaceClient()`, no profile) —
# MAGIC whoever the job's `run_as` is set to must have CAN_MANAGE on every app
# MAGIC named in `app_names` for that workspace.
# MAGIC
# MAGIC See README.md#app-stop-start-idempotency — SDK doesn't tolerate a redundant stop/start.

# COMMAND ----------

dbutils.widgets.dropdown("action", "stop", ["start", "stop"], "Action")
dbutils.widgets.text("app_names", "", "Comma-separated app names")

ACTION = dbutils.widgets.get("action")
APP_NAMES = [n.strip() for n in dbutils.widgets.get("app_names").split(",") if n.strip()]

if not APP_NAMES:
    raise ValueError("app_names is empty — pass a comma-separated list of Databricks App names.")

print(f"action={ACTION}  apps={APP_NAMES}")

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
            print(f"  {name}: already {current_state.lower()} — skipping")
            continue
        if ACTION == "stop":
            w.apps.stop(name=name)
        else:
            w.apps.start(name=name)
        print(f"  {name}: {ACTION} OK")
    except Exception as e:
        print(f"  {name}: {ACTION} FAILED — {e}")
        failures.append(name)

if failures:
    raise RuntimeError(f"{ACTION} failed for: {', '.join(failures)}")

print(f"\nDone — {len(APP_NAMES)} app(s) processed.")


# COMMAND ----------
