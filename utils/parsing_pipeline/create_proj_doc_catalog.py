# Databricks notebook source
# MAGIC %md
# MAGIC # PROJ DOC_CATALOG
# MAGIC
# MAGIC **Description:**
# MAGIC Last link of the daily chain. Two writes to Lakebase:
# MAGIC 1. `doc_catalog` rewritten from `parse_manifest` (every document in scope: REF, title, division, link, current revision and its date) and
# MAGIC    `chunks` (`in_chat` = the document has passages in the chat index). The app reads it every 30 min (`server/services/infra/doc_catalog.py`):
# MAGIC    links of the REFs cited in an answer, titles in impact search, revision and date given to the answer model with each document.
# MAGIC 2. `catalog_metadata.documents_as_of` (today's date) and `updated_at` (now): the "documents as of" date shown in the chat UI toolbar
# MAGIC    (`GET /config/knowledge-base-date`).
# MAGIC 3. `chunk_codes` rewritten from `chunks`: each code cited in the passages (form, part, standard or document number such as
# MAGIC    `SF-1027`) with the passages that cite it. The chat looks up the codes of a question there, since the hybrid search splits
# MAGIC    `SF-1027` into `SF` and `1027` (`server/services/chat/engine/codes.py`).
# MAGIC
# MAGIC **Highlighted complexities:**
# MAGIC Lakebase is a Postgres endpoint, not Unity Catalog: unreachable from classic job compute over its private endpoint, so this task has no
# MAGIC cluster spec on purpose (serverless `environment_key`). It depends on `5_sync_index`, not on `4_describe_images`:
# MAGIC the date should only advance once the chunks are actually queryable, not just written to Delta.
# MAGIC `lakebase_database` may list several databases, comma-separated; empty = skip.
# MAGIC
# MAGIC **Intended Pipeline**
# MAGIC - Qualibot Parsing Pipeline — Daily (task `6_update_kb_metadata`)
# MAGIC
# MAGIC **Inputs Data**
# MAGIC - `{PIPELINE_WRITE_ENV}_proj.qualibot.parse_manifest`, `{PIPELINE_WRITE_ENV}_proj.qualibot.chunks`
# MAGIC
# MAGIC **Output Tables (Pipeline)**
# MAGIC - `{lakebase_database}.doc_catalog` (Lakebase/Postgres, full rewrite in one transaction)
# MAGIC - `{lakebase_database}.catalog_metadata` (Lakebase/Postgres, id=1 single row)
# MAGIC - `{lakebase_database}.chunk_codes` (Lakebase/Postgres, full rewrite in the same transaction)

# COMMAND ----------

# MAGIC %md
# MAGIC # Technical debt
# MAGIC - The Intraqual host of the REF link is written here and in `worker.intraqual_ref_url`: change both together.
# MAGIC - With several target databases the rewrite is one transaction per database, not one across all of them.

# COMMAND ----------

# MAGIC %md
# MAGIC # Configuration

# COMMAND ----------

# MAGIC %md
# MAGIC ## Config Widgets
# MAGIC The environments, the quality bypass and the debug mode are widgets: the job passes them as base parameters,
# MAGIC and the notebook copies them to the environment that `config` reads when it is imported.


# COMMAND ----------

import os

dbutils.widgets.removeAll()

dbutils.widgets.text("pipeline_write_env", "", "Environment the tables are written to: dev, uat or prod")
dbutils.widgets.text("pipeline_read_env", "", "Environment of the tables another task of this job wrote (empty: pipeline_write_env)")
dbutils.widgets.text("reference_read_env", "prod", "Environment of the reference tables (Intraqual data asset)")
dbutils.widgets.dropdown("bypass_quality_checks", "false", ["false", "true"], "true: RED quality checks are logged as AMBER")
dbutils.widgets.text("debug", "false")
dbutils.widgets.dropdown("log_level", "info", ["debug", "info", "warning", "error", "critical"])
dbutils.widgets.text("run_mode", "incremental", "Parsing run mode: incremental, full or rechunk")
dbutils.widgets.text("parse_filter", "", "Comma-separated IDDOC list for a targeted run (empty: no filter)")
dbutils.widgets.text("max_parse_files", "500", "Most files one run may parse; 0 = no ceiling")
dbutils.widgets.text("max_source_staleness_hours", "36", "Freshness window of the Intraqual tables; 0 disables the guard")
dbutils.widgets.text("redescribe_all_images", "false", "true: describe every image again")

PIPELINE_WRITE_ENV = dbutils.widgets.get("pipeline_write_env")
if PIPELINE_WRITE_ENV not in ("dev", "uat", "prod"):
    raise ValueError(f"pipeline_write_env must be dev, uat or prod, got {PIPELINE_WRITE_ENV!r}")
PIPELINE_READ_ENV = dbutils.widgets.get("pipeline_read_env") or PIPELINE_WRITE_ENV
DEBUG = dbutils.widgets.get("debug").lower() == "true"
LOG_LEVEL = dbutils.widgets.get("log_level")

# config reads the environment when it is imported, so the widget values are copied there first.
os.environ["PIPELINE_WRITE_ENV"] = PIPELINE_WRITE_ENV
os.environ["PIPELINE_READ_ENV"] = dbutils.widgets.get("pipeline_read_env")
os.environ["REFERENCE_READ_ENV"] = dbutils.widgets.get("reference_read_env")
os.environ["BYPASS_QUALITY_CHECKS"] = dbutils.widgets.get("bypass_quality_checks")
os.environ["PARSING_RUN_MODE"] = dbutils.widgets.get("run_mode")
os.environ["PARSING_PARSE_FILTER"] = dbutils.widgets.get("parse_filter")
os.environ["PARSING_MAX_PARSE_FILES"] = dbutils.widgets.get("max_parse_files")
os.environ["PARSING_MAX_SOURCE_STALENESS_HOURS"] = dbutils.widgets.get("max_source_staleness_hours")
os.environ["PARSING_REDESCRIBE_ALL_IMAGES"] = dbutils.widgets.get("redescribe_all_images")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Config Standard Package Imports

# COMMAND ----------

import os
import sys

_ctx = dbutils.notebook.entry_point.getDbutils().notebook().getContext()
# The repository folder and its root (leap_utils/ lives there) go on sys.path: two levels above this folder.
REPO_DIR = "/Workspace" + os.path.dirname(_ctx.notebookPath().get())
sys.path.insert(0, os.path.dirname(os.path.dirname(REPO_DIR)))
sys.path.insert(0, REPO_DIR)

# COMMAND ----------

# MAGIC %md
# MAGIC ## Config Function Imports
# MAGIC `tables` lives next to this notebook; Databricks puts the notebook folder on `sys.path`.

# COMMAND ----------

import codes
import tables
from tables import table_exists

# COMMAND ----------

# MAGIC %md
# MAGIC ## Config logger

# COMMAND ----------

from leap_utils.common import logger as log_utils


# COMMAND ----------

# MAGIC %md
# MAGIC ### Job parameters
# MAGIC This task is serverless: no `PIPELINE_WRITE_ENV` environment variable, so the Lakebase target and the write environment come from the job parameters. `lakebase_database` may list several databases, comma-separated; empty means nothing to do.

# COMMAND ----------

logger = log_utils.setup_applevel_logger(log_level=log_utils.LOGGER_MAPPING[LOG_LEVEL])


dbutils.widgets.text("lakebase_project_id", "qualibot")
LAKEBASE_PROJECT_ID = dbutils.widgets.get("lakebase_project_id")
dbutils.widgets.text("lakebase_branch", "production")
LAKEBASE_BRANCH = dbutils.widgets.get("lakebase_branch")
dbutils.widgets.text("lakebase_endpoint", "primary")
LAKEBASE_ENDPOINT = dbutils.widgets.get("lakebase_endpoint")
dbutils.widgets.text("lakebase_database", "")
LAKEBASE_DATABASES = [
    d.strip() for d in dbutils.widgets.get("lakebase_database").split(",") if d.strip()
]

if not LAKEBASE_DATABASES:
    logger.info("No lakebase_database parameter: nothing to do.")
    dbutils.notebook.exit("no_lakebase_database")

logger.info(
    f"Target: {LAKEBASE_PROJECT_ID}/{LAKEBASE_BRANCH}/{LAKEBASE_DATABASES}, source {PIPELINE_WRITE_ENV}_proj.qualibot"
)

# COMMAND ----------

# MAGIC %md
# MAGIC # Inputs
# MAGIC ## Parsing tables
# MAGIC `parse_manifest` lists every document in scope and `chunks` tells which ones have passages in the chat index. This task is the last of the chain and runs only after the index sync, so the date it records means "queryable", not just "written".

# COMMAND ----------

INTRAQUAL_REF_URL_BASE = "https://intraqual.lat.corp/intraqual_prod/identification.aspx?ref="
# Never let a broken upstream run empty the app's catalog.
MIN_KEEP_RATIO = 0.5

MANIFEST_TABLE = f"{PIPELINE_READ_ENV}_proj.qualibot.parse_manifest"
CHUNKS_TABLE = f"{PIPELINE_READ_ENV}_proj.qualibot.chunks"

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
    (
        r["ref"],
        r["title"],
        INTRAQUAL_REF_URL_BASE + r["ref"].replace(" ", "%20"),
        r["division"],
        bool(r["in_chat"]),
        (r["revision"] or "").strip() or None,
        r["doc_date"],
    )
    for r in spark.sql(f"""
        SELECT trim(m.ref) AS ref, first(m.titre, true) AS title, first(m.division, true) AS division,
               max(c.REF IS NOT NULL) AS in_chat,
               max_by(m.indice, m.IDDOC) AS revision, max_by(m.doc_date, m.IDDOC) AS doc_date
        FROM {MANIFEST_TABLE} m
        LEFT JOIN (SELECT DISTINCT trim(REF) AS REF FROM {CHUNKS_TABLE}) c ON c.REF = trim(m.ref)
        WHERE m.ref IS NOT NULL AND trim(m.ref) <> ''
        GROUP BY trim(m.ref)
    """).collect()
]
n_in_chat = sum(1 for r in catalog_rows if r[4])
logger.info(f"{len(catalog_rows)} documents in scope, {n_in_chat} with passages in the chat index")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Tr. 2 - Codes cited in the passages
# MAGIC One row per code (upper case) with the passages that cite it, for the codes cited by at most `codes.CODE_MAX_CHUNKS` passages: a code found everywhere points at no passage in particular. The header line of each chunk is left out, otherwise every chunk would cite its own REF.

# COMMAND ----------

code_rows = [(r["code"], list(r["chunk_ids"])) for r in codes.chunk_codes(spark.table(CHUNKS_TABLE)).collect()]
logger.info(f"{len(code_rows)} codes cited in the passages (at most {codes.CODE_MAX_CHUNKS} passages each)")

# COMMAND ----------

# MAGIC %md
# MAGIC # Quality Checks
# MAGIC ## Pre-condition: parsing tables exist
# MAGIC The two tables are checked first: a missing table fails here by name.

# COMMAND ----------

for table in (MANIFEST_TABLE, CHUNKS_TABLE):
    if not table_exists(table):
        raise RuntimeError(f"{table} does not exist - run the parsing tasks first")

# COMMAND ----------

# MAGIC %md
# MAGIC ## doc_catalog rows
# MAGIC A manifest that gave no document means an upstream failure; writing it would empty the app's catalog (RED). `ref` is the primary key of `doc_catalog` (RED). The title and the division are expected on 99 % of the documents (AMBER).

# COMMAND ----------

tables.check_rows(
    [{"ref": r[0], "title": r[1], "division": r[3]} for r in catalog_rows],
    f"doc_catalog (from {MANIFEST_TABLE})",
    [
        tables.non_empty(),
        tables.not_null("ref"),
        tables.unique("ref"),
        tables.not_null("title", mostly=0.99, severity=tables.AMBER),
        tables.not_null("division", mostly=0.99, severity=tables.AMBER),
    ],
    logger,
)

# COMMAND ----------

# MAGIC %md
# MAGIC ## chunk_codes rows
# MAGIC `code` is the primary key of `chunk_codes` (RED). No code at all in a non-empty `chunks` means the extraction broke (AMBER: the chat then only loses the code lookup).

# COMMAND ----------

tables.check_rows(
    [{"code": r[0]} for r in code_rows],
    f"chunk_codes (from {CHUNKS_TABLE})",
    [
        tables.non_empty(severity=tables.AMBER),
        tables.not_null("code"),
        tables.unique("code"),
    ],
    logger,
)

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


def connect(database) -> psycopg2.extensions.connection:
    """Open a connection to a database of the Lakebase project.

    :param database: Name of the database to connect to.
    :return: open connection, authenticated with the credential of this run.
    """
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
# MAGIC ## Rewrite doc_catalog and chunk_codes, then bump documents_as_of and updated_at
# MAGIC Both tables are created by the app's startup migration (`server/services/infra/lakebase/schema.py`); `CREATE TABLE IF NOT EXISTS` here only covers a database the app has not opened yet. The `revision` / `doc_date` columns are added by whichever of the app and this notebook owns the table; when neither has done it yet, the catalog is written without them and the warning names the owner.
# MAGIC
# MAGIC `chunk_codes` belongs to this notebook's identity (the app only reads it) and is created here. The catalog and the codes are replaced in one transaction, so the app never reads a half-written table, and the write is refused if it would shrink the catalog by more than half. `catalog_metadata` is a single row (`id = 1`); `ON CONFLICT DO UPDATE` makes the bump idempotent.

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
            cur.execute(
                "SELECT count(*) FROM information_schema.columns "
                "WHERE table_name = 'doc_catalog' AND column_name IN ('revision', 'doc_date')"
            )
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
                    logger.warning(
                        f"{database}.doc_catalog: cannot add revision/doc_date as {username} "
                        f"(table owner: {owner}): {str(exc).strip()}"
                    )
            cur.execute("""
                CREATE TABLE IF NOT EXISTS catalog_metadata (
                    id              INTEGER PRIMARY KEY DEFAULT 1,
                    documents_as_of DATE,
                    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    CONSTRAINT single_row CHECK (id = 1)
                )
            """)
            cur.execute("SELECT count(*) FROM doc_catalog")
            before = cur.fetchone()[0]
            if before and len(catalog_rows) < MIN_KEEP_RATIO * before:
                raise RuntimeError(
                    f"{database}.doc_catalog: {len(catalog_rows)} documents now vs {before} "
                    f"before — refusing to shrink it by more than half (check {MANIFEST_TABLE})"
                )
            cur.execute("DELETE FROM doc_catalog")
            if with_revision:
                execute_values(
                    cur,
                    "INSERT INTO doc_catalog (ref, title, url, division, in_chat, revision, doc_date) "
                    "VALUES %s",
                    catalog_rows,
                    page_size=1000,
                )
            else:
                logger.warning(
                    f"{database}.doc_catalog has no revision/doc_date columns: catalog written without "
                    "them (start the app once, or add them as the table owner)"
                )
                execute_values(
                    cur,
                    "INSERT INTO doc_catalog (ref, title, url, division, in_chat) VALUES %s",
                    [r[:5] for r in catalog_rows],
                    page_size=1000,
                )
            cur.execute("""
                CREATE TABLE IF NOT EXISTS chunk_codes (
                    code      TEXT PRIMARY KEY,
                    chunk_ids TEXT[] NOT NULL
                )
            """)
            cur.execute("DELETE FROM chunk_codes")
            execute_values(cur, "INSERT INTO chunk_codes (code, chunk_ids) VALUES %s", code_rows, page_size=5000)
            cur.execute("""
                INSERT INTO catalog_metadata (id, documents_as_of, updated_at)
                VALUES (1, CURRENT_DATE, NOW())
                ON CONFLICT (id) DO UPDATE SET
                    documents_as_of = EXCLUDED.documents_as_of,
                    updated_at      = EXCLUDED.updated_at
            """)
        logger.info(
            f"{database}: doc_catalog {before} -> {len(catalog_rows)} documents, chunk_codes {len(code_rows)} codes, "
            "documents_as_of=today"
        )
    finally:
        conn.close()
