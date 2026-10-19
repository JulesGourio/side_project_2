# Databricks notebook source
import json
import logging
import os
import sys

import psycopg2.extras
from databricks.sdk import WorkspaceClient

# Parameters come through widgets: serverless tasks have no spark_env_vars. Classic compute cannot reach
# the Lakebase endpoint.
dbutils.widgets.removeAll()
dbutils.widgets.text("LAKEBASE_PROJECT_ID", "qualibot")
dbutils.widgets.text("LAKEBASE_BRANCH", "production")
dbutils.widgets.text("LAKEBASE_ENDPOINT", "primary")
dbutils.widgets.text("LAKEBASE_DATABASE", "doccompare")
dbutils.widgets.text("OUTPUT_VOLUME_CATALOG", "")
dbutils.widgets.text("OUTPUT_VOLUME_SCHEMA", "qualibot")
dbutils.widgets.text("OUTPUT_VOLUME_NAME", "staging")
dbutils.widgets.text("TABLES_TO_SKIP", "")

LAKEBASE_PROJECT_ID = dbutils.widgets.get("LAKEBASE_PROJECT_ID")
LAKEBASE_BRANCH = dbutils.widgets.get("LAKEBASE_BRANCH")
LAKEBASE_ENDPOINT = dbutils.widgets.get("LAKEBASE_ENDPOINT")
LAKEBASE_DATABASE = dbutils.widgets.get("LAKEBASE_DATABASE")
OUTPUT_VOLUME_CATALOG = dbutils.widgets.get("OUTPUT_VOLUME_CATALOG")
OUTPUT_VOLUME_SCHEMA = dbutils.widgets.get("OUTPUT_VOLUME_SCHEMA")
OUTPUT_VOLUME_NAME = dbutils.widgets.get("OUTPUT_VOLUME_NAME")
if not OUTPUT_VOLUME_CATALOG:
    raise ValueError("The OUTPUT_VOLUME_CATALOG parameter is required")
TABLES_TO_SKIP = {
    table.strip()
    for table in dbutils.widgets.get("TABLES_TO_SKIP").split(",")
    if table.strip()
}

OUTPUT_DIR = f"/Volumes/{OUTPUT_VOLUME_CATALOG}/{OUTPUT_VOLUME_SCHEMA}/{OUTPUT_VOLUME_NAME}/lakebase_export"


sys.path.insert(0, os.path.dirname(os.getcwd()))
from ops_config import json_serializer, lakebase_connect_with

spark.sql(
    f"CREATE VOLUME IF NOT EXISTS `{OUTPUT_VOLUME_CATALOG}`.`{OUTPUT_VOLUME_SCHEMA}`.`{OUTPUT_VOLUME_NAME}`"
)
os.makedirs(OUTPUT_DIR, exist_ok=True)

conn = lakebase_connect_with(
    WorkspaceClient(), LAKEBASE_PROJECT_ID, LAKEBASE_BRANCH, LAKEBASE_ENDPOINT, LAKEBASE_DATABASE
)

logger = logging.getLogger("export_lakebase_to_volume_job")
logger.setLevel(logging.INFO)
if not logger.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S"))
    logger.addHandler(_handler)
    logger.propagate = False


try:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT table_schema, table_name
            FROM information_schema.tables
            WHERE table_schema NOT IN ('pg_catalog', 'information_schema')
              AND table_type = 'BASE TABLE'
            ORDER BY table_schema, table_name
            """
        )
        tables = cur.fetchall()

    logger.info(f"Export Lakebase {LAKEBASE_PROJECT_ID}/{LAKEBASE_BRANCH}/{LAKEBASE_DATABASE} -> {OUTPUT_DIR}")
    logger.info(f"{len(tables)} table(s) found")

    for schema, table in tables:
        if table in TABLES_TO_SKIP:
            logger.info(f"  {schema}.{table}... skipped (TABLES_TO_SKIP)")
            continue

        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(f'SELECT * FROM "{schema}"."{table}"')
            rows = [dict(row) for row in cur.fetchall()]

        out_file = f"{OUTPUT_DIR}/{table}.json"
        with open(out_file, "w", encoding="utf-8") as handle:
            for row in rows:
                handle.write(
                    json.dumps(row, ensure_ascii=False, default=json_serializer) + "\n"
                )

        logger.info(f"  {schema}.{table}: {len(rows)} row(s) -> {out_file}")
finally:
    conn.close()

logger.info(f"Done. Export available in {OUTPUT_DIR}")
