# Databricks notebook source
# MAGIC %md
# MAGIC # Export Lakebase UAT -> volume UC (compute UAT)
# MAGIC
# MAGIC Run this on a **UAT** cluster: the local machine can't reach Lakebase
# MAGIC directly (port 5432 is blocked by the corporate network), but Databricks
# MAGIC compute has native network access to its own Lakebase.
# MAGIC
# MAGIC 100% implicit auth (the cluster's identity, same as the app at runtime) —
# MAGIC no token to enter. Writes one JSON Lines file per table to the UAT staging
# MAGIC volume; fetch it afterwards from the local machine via
# MAGIC `databricks fs cp --profile UAT` (HTTPS, not blocked), then push it to
# MAGIC DEV with `copy_Lakebase_tables.py`.

# COMMAND ----------
# MAGIC %pip install psycopg2-binary "databricks-sdk>=0.102.0"
# COMMAND ----------
dbutils.library.restartPython()

# COMMAND ----------
import datetime
import json
import os
from decimal import Decimal

import psycopg2
import psycopg2.extras
from databricks.sdk import WorkspaceClient

LAKEBASE_PROJECT = {
    "project_id": "qualibot",
    "branch": "production",
    "endpoint": "primary",
    "database": "doccompare",
}

STAGING_VOLUME = "/Volumes/uat_landingzone/qualibot/staging"
OUTPUT_DIR = f"{STAGING_VOLUME}/lakebase_export"
TABLES_TO_SKIP = {"llm_requests"}


def json_serializer(obj):
    if isinstance(obj, (datetime.date, datetime.datetime)):
        return obj.isoformat()
    if isinstance(obj, Decimal):
        return float(obj)
    if isinstance(obj, (bytes, memoryview)):
        return obj.hex() if isinstance(obj, bytes) else bytes(obj).hex()
    return str(obj)


# COMMAND ----------
spark.sql("CREATE VOLUME IF NOT EXISTS `uat_landingzone`.`qualibot`.`staging`")
os.makedirs(OUTPUT_DIR, exist_ok=True)

# COMMAND ----------
# Implicit auth: the cluster is already in the UAT workspace, no profile/token needed.
w = WorkspaceClient()

branch_path = f"projects/{LAKEBASE_PROJECT['project_id']}/branches/{LAKEBASE_PROJECT['branch']}"
endpoint_path = f"{branch_path}/endpoints/{LAKEBASE_PROJECT['endpoint']}"

eps = list(w.postgres.list_endpoints(parent=branch_path))
if not eps:
    raise RuntimeError(f"No endpoint found for {branch_path}")
lakebase_host = eps[0].status.hosts.host

me = w.current_user.me()
username = me.user_name or me.display_name
if not username:
    raise RuntimeError("Could not resolve the current identity.")

cred = w.postgres.generate_database_credential(endpoint=endpoint_path)
if not cred.token:
    raise RuntimeError("generate_database_credential returned an empty token.")

conn = psycopg2.connect(
    host=lakebase_host,
    port=5432,
    database=LAKEBASE_PROJECT["database"],
    user=username,
    password=cred.token,
    sslmode="require",
)
import logging

logger = logging.getLogger("export_lakebase_uat_to_volume")
logger.setLevel(logging.INFO)
if not logger.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S"))
    logger.addHandler(_handler)
    logger.propagate = False


logger.info(f"Connected to Lakebase UAT ({lakebase_host}) as {username}.")

# COMMAND ----------
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

logger.info(f"{len(tables)} table(s) found: {[f'{s}.{t}' for s, t in tables]}")

# COMMAND ----------
for schema, table in tables:
    if table in TABLES_TO_SKIP:
        logger.info(f"  {table}... skipped (TABLES_TO_SKIP)")
        continue

    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(f'SELECT * FROM "{schema}"."{table}"')
        rows = [dict(r) for r in cur.fetchall()]

    out_file = f"{OUTPUT_DIR}/{table}.json"
    with open(out_file, "w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False, default=json_serializer) + "\n")

    logger.info(f"  {table}: {len(rows)} row(s) -> {out_file}")

conn.close()
logger.info(f"Done. Export at {OUTPUT_DIR}")
