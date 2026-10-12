# Databricks notebook source
# MAGIC %md
# MAGIC # Grant Volume Access
# MAGIC
# MAGIC One-off admin utility: grants USE CATALOG + USE SCHEMA + READ VOLUME on a
# MAGIC single volume to a service principal. Mirrors the sps_rfq_analysis bundle's
# MAGIC `Grant_Reference_Access` job (job_id 538456350455869) — same pattern
# MAGIC (run_as a service principal that already has grant authority on the target
# MAGIC catalog), extended to volumes since that job only handles tables.
# MAGIC
# MAGIC Immediate use case: `job-runner-sa-uat` needs READ VOLUME on
# MAGIC `prod_landingzone.intraqual.intraqual_documents` to run
# MAGIC `utils/parsing_pipeline/2_Cleanup_Volume.py` (`dbutils.fs.ls` on that
# MAGIC volume) under `run_as` in the qualibot-uat/-test bundle targets.
# MAGIC
# MAGIC If `run_as` also lacks GRANT rights on the target catalog, this job fails
# MAGIC with the same PERMISSION_DENIED the target service principal hits — a job
# MAGIC can't manufacture a privilege `run_as` doesn't already have.

# COMMAND ----------

dbutils.widgets.text("service_principal", "", "Service principal client id (or account) to grant")
dbutils.widgets.text("catalog", "", "Catalog (e.g. prod_landingzone)")
dbutils.widgets.text("schema_name", "", "Schema (e.g. intraqual)")
dbutils.widgets.text("volume_name", "", "Volume (e.g. intraqual_documents)")
dbutils.widgets.text("volume_permission", "READ VOLUME", "READ VOLUME or WRITE VOLUME")

service_principal  = dbutils.widgets.get("service_principal").strip()
catalog            = dbutils.widgets.get("catalog").strip()
schema_name        = dbutils.widgets.get("schema_name").strip()
volume_name        = dbutils.widgets.get("volume_name").strip()
volume_permission  = dbutils.widgets.get("volume_permission").strip().upper()

assert service_principal, "service_principal widget is required"
assert catalog, "catalog widget is required"
assert schema_name, "schema_name widget is required"
assert volume_name, "volume_name widget is required"
assert volume_permission in ("READ VOLUME", "WRITE VOLUME"), "volume_permission must be READ VOLUME or WRITE VOLUME"

import logging

logger = logging.getLogger("grant_volume_access_job")
logger.setLevel(logging.INFO)
if not logger.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S"))
    logger.addHandler(_handler)
    logger.propagate = False

logger.info(f"Granting USE CATALOG + USE SCHEMA + {volume_permission} on "
      f"{catalog}.{schema_name}.{volume_name} to `{service_principal}`")

# COMMAND ----------

statements = [
    f"GRANT USE CATALOG ON CATALOG `{catalog}` TO `{service_principal}`",
    f"GRANT USE SCHEMA ON SCHEMA `{catalog}`.`{schema_name}` TO `{service_principal}`",
    f"GRANT {volume_permission} ON VOLUME `{catalog}`.`{schema_name}`.`{volume_name}` TO `{service_principal}`",
]

failed = []

for stmt in statements:
    try:
        spark.sql(stmt)
        logger.info(f"  OK: {stmt}")
    except Exception as exc:
        failed.append((stmt, str(exc).splitlines()[0][:300]))
        logger.warning(f"  FAILED: {stmt}\n    -> {str(exc).splitlines()[0][:300]}")

if failed:
    raise RuntimeError(
        f"{len(failed)}/{len(statements)} grant statement(s) failed — run_as lacks "
        f"GRANT rights on {catalog}.{schema_name}; a real Unity Catalog "
        f"metastore/catalog admin needs to run this instead."
    )

logger.info("All grants applied successfully.")
