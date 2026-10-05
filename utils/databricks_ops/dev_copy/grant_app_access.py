# Databricks notebook source
# MAGIC %md
# MAGIC # DEV copy — grant the app's service principal what it reads and writes
# MAGIC
# MAGIC **Description:**
# MAGIC Job `qualibot-grant-app-access-dev` (`databricks.yml`, target `dev`), run as
# MAGIC the DEV job SP, which holds grant rights on `dev_landingzone`. Replaces the
# MAGIC UC bindings of the app resource, which the human deployer can't apply (needs
# MAGIC MANAGE on the catalog). Idempotent — GRANT on an existing privilege is a no-op.
# MAGIC
# MAGIC What the app reads/writes as its own SP (`server/routers/compare.py`,
# MAGIC `server/services/vector_search.py`, `soffice.py`):
# MAGIC - `doc_compare` / `test` volumes: comparison files, LibreOffice archive (READ + WRITE)
# MAGIC - the 3 `_v1` Vector Search indexes: impact search (SELECT)

# COMMAND ----------

dbutils.widgets.text("app_service_principal", "")
dbutils.widgets.text("catalog", "dev_landingzone")
dbutils.widgets.text("schema_name", "qualibot")
dbutils.widgets.text("volumes", "doc_compare,test")
dbutils.widgets.text("indexes", "chunks_index_v1,chunks_as_index_v1,chunks_is_index_v1")

sp = dbutils.widgets.get("app_service_principal").strip()
catalog = dbutils.widgets.get("catalog").strip()
schema = dbutils.widgets.get("schema_name").strip()
volumes = [v.strip() for v in dbutils.widgets.get("volumes").split(",") if v.strip()]
indexes = [i.strip() for i in dbutils.widgets.get("indexes").split(",") if i.strip()]
assert sp, "app_service_principal is required"

statements = [
    f"GRANT USE CATALOG ON CATALOG `{catalog}` TO `{sp}`",
    f"GRANT USE SCHEMA ON SCHEMA `{catalog}`.`{schema}` TO `{sp}`",
]
statements += [f"GRANT READ VOLUME, WRITE VOLUME ON VOLUME `{catalog}`.`{schema}`.`{v}` TO `{sp}`" for v in volumes]
statements += [f"GRANT SELECT ON TABLE `{catalog}`.`{schema}`.`{i}` TO `{sp}`" for i in indexes]

# COMMAND ----------

failed = []
for stmt in statements:
    try:
        spark.sql(stmt)
        print(f"  OK: {stmt}")
    except Exception as exc:
        failed.append((stmt, str(exc).splitlines()[0][:300]))
        print(f"  FAILED: {stmt}\n    -> {str(exc).splitlines()[0][:300]}")

if failed:
    raise RuntimeError(f"{len(failed)}/{len(statements)} grant(s) failed — see above.")
print("All grants applied.")
