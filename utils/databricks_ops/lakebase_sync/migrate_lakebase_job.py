# Databricks notebook source
# DBTITLE 1,Migrate Lakebase schema (job — runs on Databricks compute, no local 5432 needed)
import psycopg2
from databricks.sdk import WorkspaceClient

# Serverless job tasks have no cluster spec, so spark_env_vars isn't
# available — job parameters come through as notebook widgets instead.
# dbutils.widgets.text() also supplies the default when run interactively
# (no job context), same role os.getenv()'s default used to play.
dbutils.widgets.text("LAKEBASE_PROJECT_ID", "qualibot")
dbutils.widgets.text("LAKEBASE_BRANCH", "production")
dbutils.widgets.text("LAKEBASE_ENDPOINT", "primary")
# Safe-by-default: this job is meant for the disposable qualibot-uat-test
# database. It must NEVER default to "doccompare" (the real UAT data) —
# always pass LAKEBASE_DATABASE explicitly for any other target.
dbutils.widgets.text("LAKEBASE_DATABASE", "doccompare_test")

LAKEBASE_PROJECT_ID = dbutils.widgets.get("LAKEBASE_PROJECT_ID")
LAKEBASE_BRANCH = dbutils.widgets.get("LAKEBASE_BRANCH")
LAKEBASE_ENDPOINT = dbutils.widgets.get("LAKEBASE_ENDPOINT")
LAKEBASE_DATABASE = dbutils.widgets.get("LAKEBASE_DATABASE")

# Databricks already puts this notebook's own directory on sys.path (same
# reason sibling `from config import *` needs no setup in utils/parsing_pipeline/),
# so this same-directory import needs no sys.path change either.
from migrations import MIGRATIONS, apply_migrations

if LAKEBASE_DATABASE == "doccompare":
    raise RuntimeError(
        "LAKEBASE_DATABASE='doccompare' rejected on this job — that's the real UAT database. "
        "This job is reserved for doccompare_test until the migration is validated."
    )

w = WorkspaceClient()
branch_path = f"projects/{LAKEBASE_PROJECT_ID}/branches/{LAKEBASE_BRANCH}"
endpoint_path = f"{branch_path}/endpoints/{LAKEBASE_ENDPOINT}"

endpoints = list(w.postgres.list_endpoints(parent=branch_path))
if not endpoints:
    raise RuntimeError(f"No endpoint found for {branch_path}")

endpoint = next(
    (ep for ep in endpoints if getattr(ep, "name", None) == LAKEBASE_ENDPOINT),
    endpoints[0],
)
lakebase_host = endpoint.status.hosts.host

me = w.current_user.me()
username = me.user_name or me.display_name
if not username:
    raise RuntimeError("Could not resolve the current identity.")

credential = w.postgres.generate_database_credential(endpoint=endpoint_path)
if not credential.token:
    raise RuntimeError("generate_database_credential returned an empty token.")

print(f"Connecting to Lakebase {LAKEBASE_PROJECT_ID}/{LAKEBASE_BRANCH}/{LAKEBASE_DATABASE}...")
conn = psycopg2.connect(
    host=lakebase_host,
    port=5432,
    database=LAKEBASE_DATABASE,
    user=username,
    password=credential.token,
    sslmode="require",
)

try:
    applied, failed = apply_migrations(conn, MIGRATIONS)
    print(f"\nDone — {len(applied)}/{len(MIGRATIONS)} migration(s) applied to {LAKEBASE_DATABASE}.")
    if failed:
        print(f"Failed ({len(failed)}):")
        for name, err in failed:
            print(f"  [{name}] {err.strip()}")
finally:
    conn.close()


# COMMAND ----------

