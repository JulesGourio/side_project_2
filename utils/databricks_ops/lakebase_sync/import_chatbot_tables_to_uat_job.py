# Databricks notebook source
# DBTITLE 1,Import chatbot JSON exports into UAT Delta tables
# Loads only the chatbot-related JSON exports (chat_feedbacks, chat_messages,
# chat_sessions) from the UAT staging volume into uat_landingzone.qualibot
# Delta tables.  Runs as a second task in the lakebase_export_uat_to_volume
# job, right after the export task that produces the JSON files.
#
# Consumer: the "Qualibot Usage Tracking" Lakeview dashboard (UAT workspace),
# which reads uat_landingzone.qualibot.{chat_messages,chat_sessions,
# chat_feedbacks}.
import os
from pathlib import PurePosixPath


SOURCE_EXPORT_DIR = os.getenv(
    "SOURCE_EXPORT_DIR",
    "/Volumes/uat_landingzone/qualibot/staging/lakebase_export",
)
TARGET_CATALOG = os.getenv("TARGET_CATALOG", "uat_landingzone")
TARGET_SCHEMA = os.getenv("TARGET_SCHEMA", "qualibot")
# Only the three chatbot tables — the rest are either unused in UAT or
# already served directly from Lakebase by the app.
CHATBOT_TABLES = {"chat_feedbacks", "chat_messages", "chat_sessions"}


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

print(f"Import volume {SOURCE_EXPORT_DIR} -> {TARGET_CATALOG}.{TARGET_SCHEMA}")
print(f"Chatbot tables to load: {sorted(CHATBOT_TABLES)}")

loaded_tables = 0
for entry in source_files:
    table = PurePosixPath(entry.path).stem
    if table not in CHATBOT_TABLES:
        continue

    df = spark.read.json(entry.path)
    row_count = df.count()
    if row_count == 0:
        print(f"  {table}... skipped (0 rows)")
        continue

    target_table = f"`{TARGET_CATALOG}`.`{TARGET_SCHEMA}`.`{table}`"
    # DROP + recreate rather than rely on overwriteSchema: a previously-all-NULL
    # column (e.g. llm_request_id before any row had a value) gets inferred by
    # spark.read.json as an incompatible type versus a later run where real
    # values appear, and overwriteSchema alone hits DELTA_FAILED_TO_MERGE_FIELDS
    # on that column even in "overwrite" mode.  Each run replaces the table
    # wholesale anyway, so a clean drop sidesteps the merge entirely.
    spark.sql(f"DROP TABLE IF EXISTS {target_table}")
    df.write.format("delta").mode("overwrite").option(
        "overwriteSchema", "true"
    ).saveAsTable(target_table)
    loaded_tables += 1
    print(
        f"  {table}: {row_count} row(s) -> {TARGET_CATALOG}.{TARGET_SCHEMA}.{table}"
    )

print(f"Done. {loaded_tables} table(s) loaded into {TARGET_CATALOG}.{TARGET_SCHEMA}")