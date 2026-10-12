# Databricks notebook source
# MAGIC %md
# MAGIC # DEV copy — grant the app's and the pipeline's service principals what they use
# MAGIC
# MAGIC Job `Z_1_Qualibot_Grant_App_Access_dev` (`databricks.yml`, target `dev`), run as
# MAGIC the DEV job SP, which holds grant rights on `dev_landingzone`. Idempotent:
# MAGIC re-running it never removes anything, an existing grant is a no-op.
# MAGIC
# MAGIC **To add or remove a grant, edit only the lists below** (the job passes the
# MAGIC same values from `databricks.yml` → `grant_app_access_dev` → `base_parameters`;
# MAGIC a one-off run can also override them with "Run now with different parameters").
# MAGIC Comma-separated names, no catalog/schema prefix for volumes and indexes.
# MAGIC
# MAGIC | List | Grant | Used by |
# MAGIC |---|---|---|
# MAGIC | `volumes` | READ + WRITE VOLUME | Compare files, LibreOffice archive |
# MAGIC | `indexes` | SELECT | Impact search, Chat VSI |
# MAGIC | `serving_endpoints` | CAN_QUERY | Chat VSI, Compare, impact judge, translation |
# MAGIC | `pipeline_tables` | SELECT + MODIFY, to `pipeline_service_principal` | Parsing pipeline jobs (run as the DEV SP) on tables created by a person: copy of the UAT corpus, `chunks` (`operations_dev.md` S3/S4) — e.g. task `6_update_kb_metadata` reads `parse_manifest` and `chunks` |
# MAGIC
# MAGIC USE CATALOG / USE SCHEMA on `catalog`.`schema_name` are always granted to both.

# COMMAND ----------

dbutils.widgets.text("app_service_principal", "8e411164-a7e8-46ff-8013-8c56af2c3656")
dbutils.widgets.text("catalog", "dev_landingzone")
dbutils.widgets.text("schema_name", "qualibot")
dbutils.widgets.text("volumes", "doc_compare,test")
dbutils.widgets.text("indexes", "chunks_index")
dbutils.widgets.text("serving_endpoints", "databricks-claude-sonnet-4-6,databricks-gpt-5-6-luna,databricks-gpt-6-luna")
dbutils.widgets.text("pipeline_service_principal", "fde6ff28-739f-4a41-b61e-604a298c8478")
dbutils.widgets.text("pipeline_tables", "_pipeline_checkpoint,processed_files,image_metadata,parse_manifest,category_reference,chunks")


def _list(name):
    return [v.strip() for v in dbutils.widgets.get(name).split(",") if v.strip()]


sp = dbutils.widgets.get("app_service_principal").strip()
catalog = dbutils.widgets.get("catalog").strip()
schema = dbutils.widgets.get("schema_name").strip()
pipeline_sp = dbutils.widgets.get("pipeline_service_principal").strip()
assert sp, "app_service_principal is required"

# COMMAND ----------

statements = [
    f"GRANT USE CATALOG ON CATALOG `{catalog}` TO `{sp}`",
    f"GRANT USE SCHEMA ON SCHEMA `{catalog}`.`{schema}` TO `{sp}`",
]
statements += [f"GRANT READ VOLUME, WRITE VOLUME ON VOLUME `{catalog}`.`{schema}`.`{v}` TO `{sp}`" for v in _list("volumes")]
statements += [f"GRANT SELECT ON TABLE `{catalog}`.`{schema}`.`{i}` TO `{sp}`" for i in _list("indexes")]
if pipeline_sp:
    statements += [
        f"GRANT USE CATALOG ON CATALOG `{catalog}` TO `{pipeline_sp}`",
        f"GRANT USE SCHEMA ON SCHEMA `{catalog}`.`{schema}` TO `{pipeline_sp}`",
    ]
    statements += [f"GRANT SELECT, MODIFY ON TABLE `{catalog}`.`{schema}`.`{t}` TO `{pipeline_sp}`"
                   for t in _list("pipeline_tables")]

failed = []
import logging

logger = logging.getLogger("grant_app_access")
logger.setLevel(logging.INFO)
if not logger.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S"))
    logger.addHandler(_handler)
    logger.propagate = False


for stmt in statements:
    try:
        spark.sql(stmt)
        logger.info(f"  OK: {stmt}")
    except Exception as exc:
        failed.append((stmt, str(exc).splitlines()[0][:300]))
        logger.warning(f"  FAILED: {stmt}\n    -> {str(exc).splitlines()[0][:300]}")

# COMMAND ----------

from databricks.sdk import WorkspaceClient
from databricks.sdk.service.serving import ServingEndpointAccessControlRequest, ServingEndpointPermissionLevel

w = WorkspaceClient()
for name in _list("serving_endpoints"):
    what = f"CAN_QUERY on serving endpoint {name}"
    try:
        endpoint_id = w.serving_endpoints.get(name).id
        w.serving_endpoints.update_permissions(endpoint_id, access_control_list=[
            ServingEndpointAccessControlRequest(service_principal_name=sp,
                                                permission_level=ServingEndpointPermissionLevel.CAN_QUERY),
        ])
        logger.info(f"  OK: {what}")
    except Exception as exc:
        # Typical cause: the job SP is not CAN_MANAGE on this endpoint (foundation
        # model endpoints are usually managed by a workspace admin) — ask one to run
        # the same grant, see operations_dev.md block V.
        failed.append((what, str(exc).splitlines()[0][:300]))
        logger.warning(f"  FAILED: {what}\n    -> {str(exc).splitlines()[0][:300]}")

# COMMAND ----------

total = len(statements) + len(_list("serving_endpoints"))
if failed:
    details = "\n".join(f"- {what}\n    -> {err}" for what, err in failed)
    raise RuntimeError(f"{len(failed)}/{total} grant(s) failed:\n{details}")
logger.info(f"All {total} grants applied.")
