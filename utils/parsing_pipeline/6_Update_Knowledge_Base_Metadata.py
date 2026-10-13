# Databricks notebook source
# MAGIC %md
# MAGIC # 06 — Update Knowledge Base metadata (Lakebase)
# MAGIC
# MAGIC **Description:**
# MAGIC Last link of the daily chain. Bumps `knowledge_base_metadata.documents_as_of`
# MAGIC (today's date) and `updated_at` (now) in Lakebase — the "documents as of"
# MAGIC date shown in the chat UI toolbar (`GET /config/knowledge-base-date`).
# MAGIC
# MAGIC **Highlighted complexities:**
# MAGIC Lakebase is a Postgres endpoint, not Unity Catalog — unreachable from
# MAGIC classic job compute over its private endpoint. This task has no cluster
# MAGIC spec on purpose (serverless `environment_key`), same as `5_sync_index` above.
# MAGIC
# MAGIC Depends on `5_sync_index`, not `4_describe_images`: the date should only
# MAGIC advance once the chunks are actually queryable, not just written to Delta.
# MAGIC
# MAGIC **Intended Pipeline**
# MAGIC - Qualibot Parsing Pipeline — Daily (task `6_update_kb_metadata`)
# MAGIC
# MAGIC **Input Tables Pipeline**
# MAGIC - *(none — Postgres, not a UC table read)*
# MAGIC
# MAGIC **Inputs Reference Data**
# MAGIC - *(none)*
# MAGIC
# MAGIC **Output Tables (Pipeline)**
# MAGIC - `{LAKEBASE_DATABASE}.knowledge_base_metadata` (Lakebase/Postgres, id=1 single row) — empty `LAKEBASE_DATABASE` = skip (DEV has no Qualibot app/Lakebase database in service)

# COMMAND ----------

# MAGIC %md
# MAGIC # Technical Debt
# MAGIC
# MAGIC None.

# COMMAND ----------

# MAGIC %md
# MAGIC # Configuration

# COMMAND ----------

dbutils.widgets.text("LAKEBASE_PROJECT_ID", "qualibot")
dbutils.widgets.text("LAKEBASE_BRANCH", "production")
dbutils.widgets.text("LAKEBASE_ENDPOINT", "primary")
dbutils.widgets.text("LAKEBASE_DATABASE", "")

LAKEBASE_PROJECT_ID = dbutils.widgets.get("LAKEBASE_PROJECT_ID")
LAKEBASE_BRANCH = dbutils.widgets.get("LAKEBASE_BRANCH")
LAKEBASE_ENDPOINT = dbutils.widgets.get("LAKEBASE_ENDPOINT")
LAKEBASE_DATABASE = dbutils.widgets.get("LAKEBASE_DATABASE").strip()

if not LAKEBASE_DATABASE:
    print("No LAKEBASE_DATABASE param (DEV case, no Qualibot app in service) — nothing to do.")
    dbutils.notebook.exit("no_lakebase_database")

print(f"Target: {LAKEBASE_PROJECT_ID}/{LAKEBASE_BRANCH}/{LAKEBASE_DATABASE}")

# COMMAND ----------

# MAGIC %md
# MAGIC # Outputs

# COMMAND ----------

# MAGIC %md
# MAGIC ## Connect to Lakebase (ambient identity, no profile)

# COMMAND ----------

import psycopg2
from databricks.sdk import WorkspaceClient

w = WorkspaceClient()  # ambient auth — this notebook's own identity in this workspace

branch_path = f"projects/{LAKEBASE_PROJECT_ID}/branches/{LAKEBASE_BRANCH}"
endpoint_path = f"{branch_path}/endpoints/{LAKEBASE_ENDPOINT}"

endpoints = list(w.postgres.list_endpoints(parent=branch_path))
if not endpoints:
    raise RuntimeError(f"No endpoint found for {branch_path}")
lakebase_host = endpoints[0].status.hosts.host

me = w.current_user.me()
username = me.user_name or me.display_name
if not username:
    raise RuntimeError("Could not resolve the current identity.")

credential = w.postgres.generate_database_credential(endpoint=endpoint_path)
if not credential.token:
    raise RuntimeError("generate_database_credential returned an empty token.")

conn = psycopg2.connect(
    host=lakebase_host,
    port=5432,
    database=LAKEBASE_DATABASE,
    user=username,
    password=credential.token,
    sslmode="require",
)
print(f"Connected to {lakebase_host}/{LAKEBASE_DATABASE} as {username}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Bump documents_as_of / updated_at
# MAGIC
# MAGIC `id=1` always exists once the app has started at least once (created by
# MAGIC `server/services/lakebase.py`'s startup migration) — `ON CONFLICT DO UPDATE`
# MAGIC just makes this idempotent rather than assuming that ordering.

# COMMAND ----------

try:
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO knowledge_base_metadata (id, documents_as_of, updated_at)
            VALUES (1, CURRENT_DATE, NOW())
            ON CONFLICT (id) DO UPDATE SET
                documents_as_of = EXCLUDED.documents_as_of,
                updated_at      = EXCLUDED.updated_at
        """)
    conn.commit()
finally:
    conn.close()

print(f"knowledge_base_metadata updated in {LAKEBASE_DATABASE} (documents_as_of=today).")

# COMMAND ----------
