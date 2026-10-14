# Databricks notebook source
# MAGIC %md
# MAGIC # DEV copy — grant the app's and the pipeline's service principals what they use
# MAGIC
# MAGIC Job `Z_1_Qualibot_Grant_App_Access_dev` (`databricks.yml`, target `dev`), run as
# MAGIC the DEV job SP, which holds grant rights on the target catalogs. Idempotent:
# MAGIC re-running it never removes anything, an existing grant is a no-op.
# MAGIC
# MAGIC **To add or remove a grant, edit only the lists below** (the job passes the
# MAGIC same values from `databricks.yml` → `grant_app_access_dev` → `base_parameters`;
# MAGIC a one-off run can also override them with "Run now with different parameters").
# MAGIC Comma-separated names, no catalog/schema prefix.
# MAGIC
# MAGIC | List | Schema | Grant | Used by |
# MAGIC |---|---|---|---|
# MAGIC | `volumes` | `catalog`.`schema_name` | READ + WRITE VOLUME | Compare files, LibreOffice archive |
# MAGIC | `indexes` | `proj_catalog`.`proj_schema` | SELECT | Impact search, Chat VSI |
# MAGIC | `pipeline_tables` | `proj_catalog`.`proj_schema` | SELECT + MODIFY, to `pipeline_service_principal` | Parsing pipeline jobs (run as the DEV SP) on the tables of the project schema, e.g. task `6_update_kb_metadata` reads `parse_manifest` and `chunks` |
# MAGIC
# MAGIC The app SP gets USE CATALOG / USE SCHEMA on `catalog`.`schema_name` (volumes) and on `proj_catalog`.`proj_schema` (indexes).
# MAGIC The pipeline SP gets USE CATALOG / USE SCHEMA on both schemas.
# MAGIC
# MAGIC **Project schema** (`proj_catalog`.`proj_schema`, e.g. `dev_proj.qualibot`: tables, evaluation tables, quality scores, MLflow prompt
# MAGIC registry): the job SP gets what the evaluation / scoring / prompt-registration jobs need (tables and prompts: create, read,
# MAGIC write, execute). The app SP never reads the registry (prompt versions reach it as experiment tags).

# COMMAND ----------

dbutils.widgets.removeAll()
dbutils.widgets.text("app_service_principal", "8e411164-a7e8-46ff-8013-8c56af2c3656")
dbutils.widgets.text("catalog", "dev_proj")
dbutils.widgets.text("schema_name", "qualibot")
dbutils.widgets.text("volumes", "doc_compare,test")
dbutils.widgets.text("indexes", "chunks_index")
dbutils.widgets.text("proj_catalog", "dev_proj")
dbutils.widgets.text("proj_schema", "qualibot")
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

proj_catalog = dbutils.widgets.get("proj_catalog").strip()
proj_schema = dbutils.widgets.get("proj_schema").strip()
assert proj_catalog and proj_schema, "proj_catalog and proj_schema are required: the tables and indexes live there"
proj = f"`{proj_catalog}`.`{proj_schema}`"
statements += [
    f"GRANT USE CATALOG ON CATALOG `{proj_catalog}` TO `{sp}`",
    f"GRANT USE SCHEMA ON SCHEMA {proj} TO `{sp}`",
]
statements += [f"GRANT SELECT ON TABLE {proj}.`{i}` TO `{sp}`" for i in _list("indexes")]
if pipeline_sp:
    statements += [
        f"GRANT USE CATALOG ON CATALOG `{catalog}` TO `{pipeline_sp}`",
        f"GRANT USE SCHEMA ON SCHEMA `{catalog}`.`{schema}` TO `{pipeline_sp}`",
        f"GRANT USE CATALOG ON CATALOG `{proj_catalog}` TO `{pipeline_sp}`",
        f"GRANT USE SCHEMA, CREATE TABLE, CREATE FUNCTION, EXECUTE, SELECT, MODIFY ON SCHEMA {proj} TO `{pipeline_sp}`",
    ]
    statements += [f"GRANT SELECT, MODIFY ON TABLE {proj}.`{t}` TO `{pipeline_sp}`" for t in _list("pipeline_tables")]

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

total = len(statements)
if failed:
    details = "\n".join(f"- {what}\n    -> {err}" for what, err in failed)
    raise RuntimeError(f"{len(failed)}/{total} grant(s) failed:\n{details}")
logger.info(f"All {total} grants applied.")
