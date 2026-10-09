# Databricks notebook source
# MAGIC %md
# MAGIC # 06 — Update Knowledge Base metadata (Lakebase)
# MAGIC
# MAGIC **Description:**
# MAGIC Last link of the daily chain. Two writes to Lakebase:
# MAGIC 1. `doc_catalog` rewritten from `parse_manifest` (every document in scope: REF, title,
# MAGIC    division, link, current revision and its date) and `chunks` (`in_chat` = the document
# MAGIC    has passages in the chat index). The app reads it every 30 min
# MAGIC    (`server/services/doc_catalog.py`): links of the REFs cited in an answer, titles for the
# MAGIC    title lookup of the chat, revision and date given to the answer model with each document,
# MAGIC    titles in impact search.
# MAGIC 2. `knowledge_base_metadata.documents_as_of` (today's date) and `updated_at` (now) —
# MAGIC    the "documents as of" date shown in the chat UI toolbar (`GET /config/knowledge-base-date`).
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
# MAGIC - `{CATALOG_SCHEMA}.parse_manifest{TABLE_SUFFIX}`, `{CATALOG_SCHEMA}.chunks{TABLE_SUFFIX}`
# MAGIC
# MAGIC **Inputs Reference Data**
# MAGIC - *(none)*
# MAGIC
# MAGIC **Output Tables (Pipeline)**
# MAGIC - `{LAKEBASE_DATABASE}.doc_catalog` (Lakebase/Postgres, full rewrite in one transaction)
# MAGIC - `{LAKEBASE_DATABASE}.knowledge_base_metadata` (Lakebase/Postgres, id=1 single row)
# MAGIC
# MAGIC `LAKEBASE_DATABASE` may list several databases, comma-separated (e.g. the app's and a
# MAGIC test app's); empty = skip.

# COMMAND ----------

# MAGIC %md
# MAGIC # Technical debt
# MAGIC - The REF link is built here and in `utils.intraqual_ref_url` (the `url` column of the chunks); `tests/test_doc_catalog.py` keeps the two equal, but the Intraqual host is written in both.
# MAGIC - The catalog groups on `trim(ref)` while `in_chat` joins the chunks on the untrimmed `ref`: a REF with surrounding spaces in the manifest would be reported as not in the chat index.
# MAGIC - With several target databases the rewrite is one transaction per database, not one across all of them: a failure on the second leaves the first already updated.
# MAGIC - The shrink guard (`MIN_KEEP_RATIO`) needs the current row count of the target, so it runs in `# Outputs` rather than in `# Quality Checks`.

# COMMAND ----------

# MAGIC %md
# MAGIC # Configuration
# MAGIC ## Config logger and widgets
# MAGIC This task is serverless, like `5_sync_index`: no `PARSING_*` environment variables, so the Lakebase target and the parsing tables come from the job parameters. `LAKEBASE_DATABASE` may list several databases, comma-separated (the app's and a test app's); empty means nothing to do.

# COMMAND ----------

import logging

logger = logging.getLogger("parsing_pipeline.kb_metadata")
logger.setLevel(logging.INFO)
if not logger.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S"))
    logger.addHandler(_handler)
    logger.propagate = False

dbutils.widgets.text("LAKEBASE_PROJECT_ID", "qualibot")
dbutils.widgets.text("LAKEBASE_BRANCH", "production")
dbutils.widgets.text("LAKEBASE_ENDPOINT", "primary")
dbutils.widgets.text("LAKEBASE_DATABASE", "")
dbutils.widgets.text("CATALOG_SCHEMA", "")
dbutils.widgets.text("TABLE_SUFFIX", "")

LAKEBASE_PROJECT_ID = dbutils.widgets.get("LAKEBASE_PROJECT_ID")
LAKEBASE_BRANCH = dbutils.widgets.get("LAKEBASE_BRANCH")
LAKEBASE_ENDPOINT = dbutils.widgets.get("LAKEBASE_ENDPOINT")
LAKEBASE_DATABASES = [d.strip() for d in dbutils.widgets.get("LAKEBASE_DATABASE").split(",") if d.strip()]
CATALOG_SCHEMA = dbutils.widgets.get("CATALOG_SCHEMA").strip()
TABLE_SUFFIX = dbutils.widgets.get("TABLE_SUFFIX").strip()

if not LAKEBASE_DATABASES:
    logger.info("No LAKEBASE_DATABASE param — nothing to do.")
    dbutils.notebook.exit("no_lakebase_database")
if not CATALOG_SCHEMA:
    raise ValueError("CATALOG_SCHEMA is required (the parsing tables: parse_manifest, chunks)")

logger.info(f"Target: {LAKEBASE_PROJECT_ID}/{LAKEBASE_BRANCH}/{LAKEBASE_DATABASES}, source {CATALOG_SCHEMA}.*{TABLE_SUFFIX}")

# COMMAND ----------

# MAGIC %md
# MAGIC # Inputs
# MAGIC ## Parsing tables
# MAGIC `parse_manifest` lists every document in scope and `chunks` tells which ones have passages in the chat index. This task is the last of the chain and runs only after the index sync, so the date it records means "queryable", not just "written".

# COMMAND ----------

INTRAQUAL_REF_URL_BASE = "https://intraqual.lat.corp/intraqual_prod/identification.aspx?ref="
# Never let a broken upstream run empty the app's catalog.
MIN_KEEP_RATIO = 0.5

MANIFEST_TABLE = f"{CATALOG_SCHEMA}.parse_manifest{TABLE_SUFFIX}"
CHUNKS_TABLE = f"{CATALOG_SCHEMA}.chunks{TABLE_SUFFIX}"
for table in (MANIFEST_TABLE, CHUNKS_TABLE):
    if not spark.catalog.tableExists(table):
        raise RuntimeError(f"{table} does not exist - run the parsing tasks first")

# COMMAND ----------

# MAGIC %md
# MAGIC # Data Preparation
# MAGIC #N/A

# COMMAND ----------

# MAGIC %md
# MAGIC # Data Transformations
# MAGIC ## Tr. 1 - Document catalog
# MAGIC One row per REF in scope: title, division, `in_chat`, the Intraqual link built from the REF exactly like the chunks' `url` column, and the revision (`indice`) and publication date of its current IDDOC (the highest one: each revision gets a new IDDOC) — the chat gives them to the answer model with each document. `base_ref` stays NULL: the app derives it from the REF.

# COMMAND ----------

catalog_rows = [
    (r["ref"], r["title"], INTRAQUAL_REF_URL_BASE + r["ref"].replace(" ", "%20"), r["division"], bool(r["in_chat"]),
     (r["revision"] or "").strip() or None, r["doc_date"])
    for r in spark.sql(f"""
        SELECT trim(m.ref) AS ref, first(m.titre, true) AS title, first(m.division, true) AS division,
               max(c.REF IS NOT NULL) AS in_chat,
               max_by(m.indice, m.IDDOC) AS revision, max_by(m.doc_date, m.IDDOC) AS doc_date
        FROM {MANIFEST_TABLE} m
        LEFT JOIN (SELECT DISTINCT REF FROM {CHUNKS_TABLE}) c ON c.REF = m.ref
        WHERE m.ref IS NOT NULL AND trim(m.ref) <> ''
        GROUP BY trim(m.ref)
    """).collect()
]
n_in_chat = sum(1 for r in catalog_rows if r[4])
logger.info(f"{len(catalog_rows)} documents in scope, {n_in_chat} with passages in the chat index")

# COMMAND ----------

# MAGIC %md
# MAGIC # Quality Checks
# MAGIC ## Catalog not empty
# MAGIC A manifest that gave no document means an upstream failure; writing it would empty the app's catalog.

# COMMAND ----------

if not catalog_rows:
    raise RuntimeError(f"{MANIFEST_TABLE} gave no document - the catalog would be emptied")

# COMMAND ----------

# MAGIC %md
# MAGIC # Outputs
# MAGIC ## Connect to Lakebase
# MAGIC Lakebase is a Postgres endpoint that classic job compute cannot reach over its private endpoint, hence serverless. The connection uses the notebook's own identity: a short-lived database credential, no profile.

# COMMAND ----------

import psycopg2
from psycopg2.extras import execute_values
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


def connect(database):
    conn = psycopg2.connect(
        host=lakebase_host,
        port=5432,
        database=database,
        user=username,
        password=credential.token,
        sslmode="require",
    )
    logger.info(f"Connected to {lakebase_host}/{database} as {username}")
    return conn

# COMMAND ----------

# MAGIC %md
# MAGIC ## Rewrite doc_catalog, then bump documents_as_of and updated_at
# MAGIC Both tables are created by the app's startup migration (`server/services/lakebase.py`); `CREATE TABLE IF NOT EXISTS` here only covers a database the app has not opened yet. The `revision` / `doc_date` columns are added by whichever of the app and this notebook owns the table; when neither has done it yet, the catalog is written without them and the warning names the owner.
# MAGIC
# MAGIC The catalog is replaced in one transaction, so the app never reads a half-written table, and the write is refused if it would shrink the catalog by more than half. `knowledge_base_metadata` is a single row (`id = 1`); `ON CONFLICT DO UPDATE` makes the bump idempotent.

# COMMAND ----------

for database in LAKEBASE_DATABASES:
    conn = connect(database)
    try:
        with conn, conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS doc_catalog (
                    ref        TEXT PRIMARY KEY,
                    base_ref   TEXT,
                    title      TEXT,
                    url        TEXT,
                    division   TEXT,
                    in_chat    BOOLEAN NOT NULL DEFAULT TRUE,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """)
            cur.execute("SELECT count(*) FROM information_schema.columns "
                        "WHERE table_name = 'doc_catalog' AND column_name IN ('revision', 'doc_date')")
            with_revision = cur.fetchone()[0] == 2
            if not with_revision:
                # Only the table's owner may add columns: this notebook's identity when it created the table.
                cur.execute("SAVEPOINT add_revision")
                try:
                    cur.execute("ALTER TABLE doc_catalog ADD COLUMN IF NOT EXISTS revision TEXT")
                    cur.execute("ALTER TABLE doc_catalog ADD COLUMN IF NOT EXISTS doc_date DATE")
                    cur.execute("RELEASE SAVEPOINT add_revision")
                    with_revision = True
                    logger.info(f"{database}.doc_catalog: revision/doc_date columns added")
                except psycopg2.Error as exc:
                    cur.execute("ROLLBACK TO SAVEPOINT add_revision")
                    cur.execute("SELECT tableowner FROM pg_tables WHERE tablename = 'doc_catalog'")
                    owner = cur.fetchone()[0]
                    logger.warning(f"{database}.doc_catalog: cannot add revision/doc_date as {username} "
                                   f"(table owner: {owner}): {str(exc).strip()}")
            cur.execute("""
                CREATE TABLE IF NOT EXISTS knowledge_base_metadata (
                    id              INTEGER PRIMARY KEY DEFAULT 1,
                    documents_as_of DATE,
                    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    CONSTRAINT single_row CHECK (id = 1)
                )
            """)
            cur.execute("SELECT count(*) FROM doc_catalog")
            before = cur.fetchone()[0]
            if before and len(catalog_rows) < MIN_KEEP_RATIO * before:
                raise RuntimeError(f"{database}.doc_catalog: {len(catalog_rows)} documents now vs {before} "
                                   f"before — refusing to shrink it by more than half (check {MANIFEST_TABLE})")
            cur.execute("DELETE FROM doc_catalog")
            if with_revision:
                execute_values(cur, "INSERT INTO doc_catalog (ref, title, url, division, in_chat, revision, doc_date) "
                                    "VALUES %s", catalog_rows, page_size=1000)
            else:
                logger.warning(f"{database}.doc_catalog has no revision/doc_date columns: catalog written without "
                               "them (start the app once, or add them as the table owner)")
                execute_values(cur, "INSERT INTO doc_catalog (ref, title, url, division, in_chat) VALUES %s",
                               [r[:5] for r in catalog_rows], page_size=1000)
            cur.execute("""
                INSERT INTO knowledge_base_metadata (id, documents_as_of, updated_at)
                VALUES (1, CURRENT_DATE, NOW())
                ON CONFLICT (id) DO UPDATE SET
                    documents_as_of = EXCLUDED.documents_as_of,
                    updated_at      = EXCLUDED.updated_at
            """)
        logger.info(f"{database}: doc_catalog {before} -> {len(catalog_rows)} documents, documents_as_of=today")
    finally:
        conn.close()
