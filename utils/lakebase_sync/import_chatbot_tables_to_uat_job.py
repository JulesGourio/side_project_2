# Databricks notebook source
# Loads every JSON export of the UAT staging volume (one file per Lakebase table: chat, compare, impact search, users,
# caches, errors, request logs) into a Delta table of the same name in uat_proj.qualibot. Runs as a second task in the
# lakebase_export_uat_to_volume job, right after the export task that produces the JSON files. The notebook keeps its
# first name, from when it loaded the chat tables only.
#
# Consumers: the "Qualibot Usage Tracking" Lakeview dashboard (UAT workspace), the SQL alerts and the chat scoring
# (chat_messages, chat_sessions, chat_feedbacks, chat_turns, chat_retrieved_chunks, app_errors), and anyone querying the
# history of the app in SQL.
import os
from pathlib import PurePosixPath


SOURCE_EXPORT_DIR = os.getenv(
    "SOURCE_EXPORT_DIR",
    "/Volumes/uat_proj/qualibot/staging/lakebase_export",
)
TARGET_CATALOG = os.getenv("TARGET_CATALOG", "uat_proj")
TARGET_SCHEMA = os.getenv("TARGET_SCHEMA", "qualibot")
# Tables this job loaded before it loaded them all: they exist without the marker property below.
FIRST_LOADED_TABLES = {"chat_feedbacks", "chat_messages", "chat_sessions", "chat_turns", "chat_retrieved_chunks",
                       "app_errors"}
# Set on every table the job writes: only a table that carries it (or one of the first six) is dropped and rewritten,
# so a table of the parsing pipeline or anyone else that happens to share a Lakebase table name is never overwritten.
OWNER_PROPERTY = "lakebase_export"


def to_dbfs_path(path: str) -> str:
    if path.startswith("dbfs:/"):
        return path
    if path.startswith("/Volumes/"):
        return f"dbfs:{path}"
    return path


def iter_export_files(source_dir: str):
    entries = sorted(
        dbutils.fs.ls(to_dbfs_path(source_dir)), key=lambda entry: entry.name
    )
    return [entry for entry in entries if entry.path.endswith(".json")]


spark.sql(f"CREATE SCHEMA IF NOT EXISTS `{TARGET_CATALOG}`.`{TARGET_SCHEMA}`")

source_files = iter_export_files(SOURCE_EXPORT_DIR)
if not source_files:
    raise RuntimeError(f"No JSON file found in {SOURCE_EXPORT_DIR}")

import logging

logger = logging.getLogger("import_chatbot_tables_to_uat_job")
logger.setLevel(logging.INFO)
if not logger.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S"))
    logger.addHandler(_handler)
    logger.propagate = False


logger.info(f"Import volume {SOURCE_EXPORT_DIR} -> {TARGET_CATALOG}.{TARGET_SCHEMA}")
logger.info(f"Tables to load: {sorted(PurePosixPath(entry.path).stem for entry in source_files)}")


def is_ours(target_table: str, table: str) -> bool:
    """True if the Delta table does not exist yet, or was written by this job."""
    if not spark.catalog.tableExists(target_table):
        return True
    if table in FIRST_LOADED_TABLES:
        return True
    properties = {row["key"]: row["value"] for row in spark.sql(f"SHOW TBLPROPERTIES {target_table}").collect()}
    return properties.get(OWNER_PROPERTY) == "true"


loaded_tables = 0
refused_tables = []
for entry in source_files:
    table = PurePosixPath(entry.path).stem
    target_table = f"`{TARGET_CATALOG}`.`{TARGET_SCHEMA}`.`{table}`"
    if not is_ours(target_table, table):
        refused_tables.append(table)
        logger.error(f"  {table}: a Delta table of that name exists and was not written by this job, left untouched")
        continue

    df = spark.read.json(entry.path)
    row_count = df.count()
    if row_count == 0:
        logger.info(f"  {table}... skipped (0 rows)")
        continue

    # Drop and recreate: overwriteSchema alone fails when a column is all NULL in one export and typed in the next
    # (DELTA_FAILED_TO_MERGE_FIELDS).
    spark.sql(f"DROP TABLE IF EXISTS {target_table}")
    df.write.format("delta").mode("overwrite").option(
        "overwriteSchema", "true"
    ).saveAsTable(target_table)
    spark.sql(f"ALTER TABLE {target_table} SET TBLPROPERTIES ('{OWNER_PROPERTY}' = 'true')")
    loaded_tables += 1
    logger.info(f"  {table}: {row_count} row(s) -> {TARGET_CATALOG}.{TARGET_SCHEMA}.{table}")

logger.info(f"Done. {loaded_tables} table(s) loaded into {TARGET_CATALOG}.{TARGET_SCHEMA}")
if refused_tables:
    raise RuntimeError(f"Tables left untouched, a Delta table of that name was not written by this job: {refused_tables}")
