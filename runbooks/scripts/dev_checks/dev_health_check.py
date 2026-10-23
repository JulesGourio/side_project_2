# Databricks notebook source
# Read-only health check of the DEV environment: copy the cell into a notebook of the DEV workspace and run it.

# COMMAND ----------

import logging

from databricks.sdk import WorkspaceClient

logging.basicConfig(level=logging.INFO, format="%(message)s", force=True)
logger = logging.getLogger("dev_health_check")

SCHEMA = "dev_proj.qualibot"
INDEX = f"{SCHEMA}.chunks_index"
STAGING = "/Volumes/dev_proj/qualibot/staging/lakebase_export"
WEEK = "created_at > current_timestamp() - INTERVAL 7 DAYS"

CHECKS = [
    ("Groups synced for the visitors (users)",
     f"SELECT count(*) users, sum(int(can_chat)) can_chat, sum(int(can_compare)) can_compare, max(groups_synced_at) last_sync FROM {SCHEMA}.users"),
    ("Last exported chat turn",
     f"SELECT count(*) turns, max(created_at) last_turn FROM {SCHEMA}.chat_turns"),
    ("Chat turns of the week by status",
     f"SELECT status, count(*) n FROM {SCHEMA}.chat_turns WHERE {WEEK} GROUP BY status ORDER BY n DESC"),
    ("Errors of the week by severity and stage",
     f"SELECT severity, stage, count(*) n FROM {SCHEMA}.app_errors WHERE {WEEK} GROUP BY severity, stage ORDER BY n DESC LIMIT 20"),
    ("Comparisons attributed to a user (llm_requests)",
     f"SELECT count(*) total, count(user_id) with_user FROM {SCHEMA}.llm_requests"),
    ("Grants kept on an exported table (chat_turns)",
     f"SHOW GRANTS ON TABLE {SCHEMA}.chat_turns"),
    ("Owner property of an exported table",
     f"SHOW TBLPROPERTIES {SCHEMA}.chat_turns"),
    ("Grants on the index",
     f"SHOW GRANTS ON TABLE {INDEX}"),
    ("parsed_documents by status",
     f"SELECT parse_status, count(*) n FROM {SCHEMA}.parsed_documents GROUP BY parse_status ORDER BY n DESC"),
    ("Documents logged NEW more than once (must be empty)",
     f"SELECT IDDOC, count(*) n FROM {SCHEMA}.document_change_log WHERE event_type = 'NEW' GROUP BY IDDOC HAVING count(*) > 1 LIMIT 20"),
    ("Passages by division",
     f"SELECT division, count(*) chunks, count(DISTINCT IDDOC) docs FROM {SCHEMA}.chunks GROUP BY division ORDER BY chunks DESC"),
    ("Documents in the RAG without passage (must be 0)",
     f"SELECT count(*) docs_without_chunks FROM {SCHEMA}.parsed_documents p WHERE parse_status = 'SUCCESS' AND is_in_rag AND NOT EXISTS (SELECT 1 FROM {SCHEMA}.chunks c WHERE c.IDDOC = p.IDDOC)"),
]


def run_check(title: str, sql: str) -> None:
    """Runs one query and logs its result, or the reason it failed.

    :param title: what the query checks
    :param sql: the read-only statement
    :return: None
    """
    logger.info("\n=== %s", title)
    try:
        logger.info(spark.sql(sql).limit(30).toPandas().to_string(index=False))
    except Exception as e:
        logger.info("FAILED: %s", str(e).splitlines()[0])


for check_title, check_sql in CHECKS:
    run_check(check_title, check_sql)

logger.info("\n=== Export files of the staging volume")
try:
    logger.info("\n".join(sorted(entry.name for entry in dbutils.fs.ls(STAGING))))
except Exception as e:
    logger.info("FAILED: %s", str(e).splitlines()[0])

logger.info("\n=== Vector Search index")
try:
    status = WorkspaceClient().vector_search_indexes.get_index(INDEX).status
    logger.info("ready=%s indexed_rows=%s message=%s", status.ready, status.indexed_row_count, status.message)
except Exception as e:
    logger.info("FAILED: %s", str(e).splitlines()[0])
