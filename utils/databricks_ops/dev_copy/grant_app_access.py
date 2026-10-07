# Databricks notebook source
# MAGIC %md
# MAGIC # DEV copy — grant the app's service principal what it reads and writes
# MAGIC
# MAGIC Job `qualibot-grant-app-access-dev` (`databricks.yml`, target `dev`), run as
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
# MAGIC
# MAGIC USE CATALOG / USE SCHEMA on `catalog`.`schema_name` are always granted.

# COMMAND ----------

dbutils.widgets.text("app_service_principal", "8e411164-a7e8-46ff-8013-8c56af2c3656")
dbutils.widgets.text("catalog", "dev_landingzone")
dbutils.widgets.text("schema_name", "qualibot")
dbutils.widgets.text("volumes", "doc_compare,test")
dbutils.widgets.text("indexes", "chunks_index_v1,chunks_as_index_v1,chunks_is_index_v1")
dbutils.widgets.text("serving_endpoints", "databricks-claude-sonnet-4-6,databricks-gpt-5-6-luna")


def _list(name):
    return [v.strip() for v in dbutils.widgets.get(name).split(",") if v.strip()]


sp = dbutils.widgets.get("app_service_principal").strip()
catalog = dbutils.widgets.get("catalog").strip()
schema = dbutils.widgets.get("schema_name").strip()
assert sp, "app_service_principal is required"

# COMMAND ----------

# DBTITLE 1,Unity Catalog grants (catalog, schema, volumes, indexes)
statements = [
    f"GRANT USE CATALOG ON CATALOG `{catalog}` TO `{sp}`",
    f"GRANT USE SCHEMA ON SCHEMA `{catalog}`.`{schema}` TO `{sp}`",
]
statements += [f"GRANT READ VOLUME, WRITE VOLUME ON VOLUME `{catalog}`.`{schema}`.`{v}` TO `{sp}`" for v in _list("volumes")]
statements += [f"GRANT SELECT ON TABLE `{catalog}`.`{schema}`.`{i}` TO `{sp}`" for i in _list("indexes")]

failed = []
for stmt in statements:
    try:
        spark.sql(stmt)
        print(f"  OK: {stmt}")
    except Exception as exc:
        failed.append((stmt, str(exc).splitlines()[0][:300]))
        print(f"  FAILED: {stmt}\n    -> {str(exc).splitlines()[0][:300]}")

# COMMAND ----------

# DBTITLE 1,Serving endpoints (CAN_QUERY, merged into the existing ACL)
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
        print(f"  OK: {what}")
    except Exception as exc:
        # Typical cause: the job SP is not CAN_MANAGE on this endpoint (foundation
        # model endpoints are usually managed by a workspace admin) — ask one to run
        # the same grant, see operations_dev.md block V.
        failed.append((what, str(exc).splitlines()[0][:300]))
        print(f"  FAILED: {what}\n    -> {str(exc).splitlines()[0][:300]}")

# COMMAND ----------

total = len(statements) + len(_list("serving_endpoints"))
if failed:
    details = "\n".join(f"- {what}\n    -> {err}" for what, err in failed)
    raise RuntimeError(f"{len(failed)}/{total} grant(s) failed:\n{details}")
print(f"All {total} grants applied.")
