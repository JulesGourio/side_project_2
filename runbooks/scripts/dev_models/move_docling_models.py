# Databricks notebook source
# MAGIC %md
# MAGIC # One-shot: move the DEV Docling models up one level
# MAGIC
# MAGIC The parsing pipeline reads the offline models from `/Volumes/<env>_proj/qualibot/docling_models` in every environment.
# MAGIC On DEV the files sat one folder deeper (`docling_models/docling_models`). This moves every entry up, refuses to
# MAGIC overwrite anything, and removes the emptied folder. Dry run by default: it only lists what would move.

# COMMAND ----------

dbutils.widgets.removeAll()
dbutils.widgets.text("dry_run", "true", "true: list only, false: move")
DRY_RUN = dbutils.widgets.get("dry_run").strip().lower() == "true"

SOURCE = "/Volumes/dev_proj/qualibot/docling_models/docling_models"
TARGET = "/Volumes/dev_proj/qualibot/docling_models"

# COMMAND ----------

def list_entries(folder: str) -> list:
    """List the direct entries of a volume folder.

    :param folder: Absolute volume path of the folder.
    :return: the ``FileInfo`` entries of the folder (empty when the folder does not exist).
    """
    try:
        return dbutils.fs.ls(folder)
    except Exception:
        return []

# COMMAND ----------

entries = list_entries(SOURCE)
collisions = [e.name for e in entries if any(t.name == e.name for t in list_entries(TARGET))]
print(f"{len(entries)} entrie(s) under {SOURCE}; collisions with {TARGET}: {collisions or 'none'}")
for entry in entries:
    print(f"  {'would move' if DRY_RUN else 'moving'}: {entry.name}")

if collisions:
    raise RuntimeError(f"Refusing to move: {', '.join(collisions)} already exist in {TARGET}")

# COMMAND ----------

if DRY_RUN:
    print("Dry run: nothing moved. Re-run with dry_run = false to move.")
else:
    for entry in entries:
        dbutils.fs.mv(entry.path, f"{TARGET}/{entry.name}", recurse=True)
    if not list_entries(SOURCE):
        dbutils.fs.rm(SOURCE, recurse=True)
    print(f"Moved {len(entries)} entrie(s) to {TARGET}.")
