"""Builds the Usage Tracking dashboard of one environment from original/Qualibot_Usage_Tracking.lvdash.json.

Usage: python3 build_dashboard.py [dev|uat|prod]   (default uat; writes
resources/dashboards/qualibot_usage_tracking_<env>.lvdash.json, deployed by the bundle)
"""
import sys
import copy
import json
import re
from pathlib import Path

HERE = Path(__file__).parent
SRC = HERE / "original" / "Qualibot_Usage_Tracking.lvdash.json"

TARGET = sys.argv[1] if len(sys.argv) > 1 else "uat"
ENV = "uat"  # the dashboard is built on the UAT names, then retargeted at the end
CONFIGS = {
    "dev": {"workspace_id": "2865348338307293", "host": "https://dbc-c623749d-731b.cloud.databricks.com",
            "experiment_id": None, "lakebase_project": "e9185a28-13c6-42da-9ccc-0dbcf6b97b60", "label": "DEV"},
    "uat": {"workspace_id": "3155144190025826", "host": "https://dbc-3a17bfce-9e88.cloud.databricks.com",
            "experiment_id": "258804869957724", "lakebase_project": "d9fd81bc-5865-4ef0-8718-520a9e8d6c68",
            "label": "UAT"},
    "prod": {"workspace_id": "1210945332048776", "host": "https://dbc-77b87d5a-6bcb.cloud.databricks.com",
             "experiment_id": None, "lakebase_project": "deb195f2-6189-419e-a640-b29fa71e540c", "label": "PROD (LEAP-Core)"},
}
CFG = CONFIGS[TARGET]
UAT = CONFIGS["uat"]
OUT = HERE.parent.parent / "resources" / "dashboards" / f"qualibot_usage_tracking_{TARGET}.lvdash.json"
DASH_ID = "01f13d51cb401021adbe602186380270"
EXCLUDED_EMAILS = (
    "'sam.crawshayjones@latecoere.aero', 'yann.depledt.external@latecoere.aero', "
    "'brice.appenzeller.external@latecoere.aero', 'jules.gourio.external@latecoere.aero'"
)

d = json.loads(SRC.read_text(encoding="utf-8"))
DS = {x["name"]: x for x in d["datasets"]}
PAGES = {p["displayName"]: p for p in d["pages"]}
LOG = []


def get_sql(name: str) -> str:
    """Query text of a dataset.

    :param name: Dataset name.
    :return: SQL text.
    """
    return "".join(DS[name]["queryLines"])


def set_sql(name: str, sql: str) -> None:
    """Replaces the query text of a dataset.

    :param name: Dataset name.
    :param sql: New SQL text.
    """
    DS[name]["queryLines"] = sql.strip("\n").splitlines(keepends=True)
    if not DS[name]["queryLines"][-1].endswith("\n"):
        DS[name]["queryLines"][-1] += "\n"


def sub(name: str, pattern: str, repl: str, count: int = 1, flags: int = 0, optional: bool = False) -> None:
    """Regex replacement in one dataset; fails when the pattern does not match exactly `count` times (0 = at least once).

    :param name: Dataset name.
    :param pattern: Regex.
    :param repl: Replacement (literal, no group references).
    :param count: Expected number of matches.
    :param flags: Regex flags.
    :param optional: True when no match is acceptable.
    """
    sql = get_sql(name)
    new, n = re.subn(pattern, lambda _: repl, sql, flags=flags)
    if optional and n == 0:
        return
    if (count and n != count) or n == 0:
        raise SystemExit(f"{name}: {n} match(es) for {pattern!r}, expected {count}")
    set_sql(name, new)


def widget(page: str, name: str) -> dict:
    """Widget of a page by its name.

    :param page: Page display name.
    :param name: Widget name.
    :return: The widget dict.
    """
    for it in PAGES[page]["layout"]:
        if it["widget"]["name"] == name:
            return it["widget"]
    raise SystemExit(f"widget {page}/{name} not found")


def widget_title(w: dict) -> str:
    """Title of a widget.

    :param w: Widget dict.
    :return: Title text ('' when none).
    """
    t = w.get("spec", {}).get("frame", {}).get("title")
    return t.get("value") if isinstance(t, dict) else (t or "")


def set_title(w: dict, title: str) -> None:
    """Sets a widget title, keeping its shape (plain text or object).

    :param w: Widget dict.
    :param title: New title.
    """
    frame = w["spec"]["frame"]
    if isinstance(frame.get("title"), dict):
        frame["title"]["value"] = title
    else:
        frame["title"] = title
    frame["showTitle"] = True


# ---------------------------------------------------------------- 1. mojibake
def fix_mojibake(o):
    """Repairs UTF-8 text that was decoded as cp1252 (possibly twice), recursively.

    :param o: JSON value.
    :return: The repaired value.
    """
    if isinstance(o, str):
        s = o
        for _ in range(3):
            if re.search("[ÃÂ]", s):
                try:
                    s = s.encode("cp1252").decode("utf-8")
                except (UnicodeEncodeError, UnicodeDecodeError):
                    break
            else:
                break
        return s
    if isinstance(o, list):
        return [fix_mojibake(x) for x in o]
    if isinstance(o, dict):
        return {k: fix_mojibake(v) for k, v in o.items()}
    return o


d = fix_mojibake(d)
DS = {x["name"]: x for x in d["datasets"]}
PAGES = {p["displayName"]: p for p in d["pages"]}
LOG.append("mojibake titles repaired")

# ---------------------------------------------------------------- 2. table names (pipeline refactor, Lakebase renames)
RENAMES = [
    (r"uat_landingzone\.qualibot\.processed_files_v1", f"{ENV}_proj.qualibot.parsed_documents"),
    (r"uat_landingzone\.qualibot\.chunks_v1", f"{ENV}_proj.qualibot.chunks"),
    (r"uat_landingzone\.qualibot\.image_metadata_v1", f"{ENV}_proj.qualibot.image_metadata"),
    (r"uat_landingzone\.qualibot\.parsing_run_health_v1", f"{ENV}_proj.qualibot.parsing_run_health"),
    (r"uat_landingzone\.qualibot\.document_change_log_v1", f"{ENV}_proj.qualibot.document_change_log"),
    (r"uat_landingzone\.qualibot\._pipeline_checkpoint_v1", f"{ENV}_proj.qualibot.parsing_checkpoint"),
    (r"uat_proj\.qualibot\.messages\b", f"{ENV}_proj.qualibot.compare_messages"),
    (r"uat_proj\.qualibot\.feedbacks\b", f"{ENV}_proj.qualibot.compare_feedbacks"),
    (r"dev_landingzone\.qualibot\.chat_messages", f"{ENV}_proj.qualibot.chat_messages"),
    (r"dev_landingzone\.qualibot\.messages\b", f"{ENV}_proj.qualibot.compare_messages"),
]
for name in DS:
    sql = get_sql(name)
    for pat, rep in RENAMES:
        sql = re.sub(pat, rep, sql)
    set_sql(name, sql)
LOG.append("legacy table names -> <env>_proj.qualibot.* (parsing tables, compare_messages, compare_feedbacks)")

LOG.append("DocCompare file links point to the uat_proj volume")

# ---------------------------------------------------------------- 3. group exclusion: deterministic, NULL-safe
for name in DS:
    sql = get_sql(name)
    sql = sql.replace("ANY_VALUE(groups) AS groups", "FLATTEN(COLLECT_SET(groups)) AS groups")
    sql = re.sub(
        r"NOT IN \(\s*SELECT\s+email\s+FROM\s+excluded_by_group\s*\)",
        "NOT IN (SELECT email FROM excluded_by_group WHERE email IS NOT NULL)",
        sql,
    )
    set_sql(name, sql)
LOG.append("ANY_VALUE(groups) -> union of groups; NOT IN made NULL-safe")

# ---------------------------------------------------------------- 4. billing: restatements netted
for name in ("qb_cost_all", "ka_endpoint_usage", "qb_cost_hourly", "qb_cost_jobs"):
    sub(name, r"\n[ \t]*AND u\.usage_quantity > 0", "", count=0, optional=True)
    sub(name, r"\n[ \t]*AND u\.record_type != 'RETRACTION'", "", count=0, optional=True)
LOG.append("billing: no quantity>0 / RETRACTION filter (restatements no longer double counted)")

# ---------------------------------------------------------------- 5. KA usage: messages per env, counted once
set_sql("qb_ka_usage", f"""
WITH ka_billing AS (
  SELECT
    u.usage_date,
    CASE
      WHEN u.workspace_id = '2865348338307293' THEN 'DEV'
      WHEN u.workspace_id = '3155144190025826' THEN 'UAT'
      WHEN u.workspace_id = '1210945332048776' THEN 'PROD (LEAP-Core)'
      ELSE 'OTHER'
    END AS env,
    u.usage_metadata.endpoint_name AS endpoint_name,
    SUM(u.usage_quantity) AS dbus,
    SUM(u.usage_quantity * lp.pricing.effective_list.default) AS cost_usd
  FROM system.billing.usage u
  LEFT JOIN system.billing.list_prices lp
      ON u.sku_name = lp.sku_name
      AND u.cloud = lp.cloud
      AND u.usage_start_time >= lp.price_start_time
      AND (lp.price_end_time IS NULL OR u.usage_start_time < lp.price_end_time)
  WHERE
    u.usage_date >= current_date() - INTERVAL 30 DAYS
    AND u.billing_origin_product = 'AGENT_BRICKS'
    AND u.usage_metadata.endpoint_name ILIKE 'ka-%'
    AND u.usage_metadata.endpoint_name NOT IN ('ka-df2b7829-endpoint')
  GROUP BY u.usage_date, u.workspace_id, u.usage_metadata.endpoint_name
),
chat_daily AS (
  SELECT '{CFG["label"].split(" ")[0]}' AS env, DATE(TRY_CAST(created_at AS TIMESTAMP)) AS usage_date, COUNT(*) AS n_user_messages
  FROM {ENV}_proj.qualibot.chat_messages
  WHERE role = 'user' AND deleted = false
    AND DATE(TRY_CAST(created_at AS TIMESTAMP)) >= current_date() - INTERVAL 30 DAYS
  GROUP BY 2
),
env_day_cost AS (
  SELECT usage_date, env, SUM(cost_usd) AS env_cost_usd FROM ka_billing GROUP BY usage_date, env
),
ranked AS (
  SELECT b.*, ROW_NUMBER() OVER (PARTITION BY b.usage_date, b.env ORDER BY b.cost_usd DESC) AS rn
  FROM ka_billing b
)
SELECT
  r.usage_date,
  r.env,
  r.endpoint_name,
  r.dbus,
  r.cost_usd,
  CASE WHEN r.rn = 1 THEN c.n_user_messages END AS n_user_messages,
  CASE WHEN r.rn = 1 AND r.env = 'DEV' AND COALESCE(c.n_user_messages, 0) > 0
    THEN round(d.env_cost_usd / c.n_user_messages, 4) END AS cost_per_request_dev,
  CASE WHEN r.rn = 1 AND r.env = 'DEV' AND COALESCE(c.n_user_messages, 0) > 0
    THEN round(d.env_cost_usd * 100 / c.n_user_messages, 2) END AS cents_per_request_dev
FROM ranked r
  LEFT JOIN chat_daily c ON r.usage_date = c.usage_date AND r.env = c.env
  LEFT JOIN env_day_cost d ON r.usage_date = d.usage_date AND r.env = d.env
ORDER BY r.usage_date DESC, r.env, r.endpoint_name
""")
for it in PAGES["Qualibot Costing"]["layout"]:
    ww = it["widget"]
    if widget_title(ww) == "KA - Daily Cost ($)":
        q = ww["queries"][0]["query"]["fields"][0]
        q["expression"] = q["expression"].replace("AVG(", "SUM(")
        q["name"] = q["name"].replace("avg(", "sum(")
        ww["spec"]["encodings"]["value"]["fieldName"] = q["name"]
        set_title(ww, "KA - Cost (30d, $)")
LOG.append("KA usage: messages counted once per env/day; Daily Cost widget now a 30d sum")

# ---------------------------------------------------------------- 6. users / chat consistency
sub("chat_data", r"\)ORDER BY m\.created_at DESC",
    f")\n  AND COALESCE(u.email, '') NOT IN ({EXCLUDED_EMAILS})\n  AND m.deleted = false\nORDER BY m.created_at DESC")
sub("chat_sessions_agg", r"\n(\s*)GROUP BY\n\s*m\.session_id",
    f"\n  AND COALESCE(u.email, '') NOT IN ({EXCLUDED_EMAILS})\n  AND m.deleted = false\nGROUP BY\n  m.session_id")
sub("chat_conversations_paired", r"m\.deleted = false\n",
    f"m.deleted = false\n    AND COALESCE(u.email, '') NOT IN ({EXCLUDED_EMAILS})\n")
for col in ("reasoning_steps", "tool_name", "tool_query", "tool_result"):
    sub("chat_data", rf"\n\s*m\.{col},", "")
for it_page in PAGES.values():
    for it in it_page["layout"]:
        for q in it["widget"].get("queries", []):
            fl = q["query"].get("filters")
            if fl:
                q["query"]["filters"] = [f for f in fl if "@latecoere.aero" not in f["expression"]]
LOG.append("chat: bot/admin exclusion and deleted=false in the datasets (removed from 6 widgets); KA-only columns dropped from chat_data")

# DocCompare: one feedback per message
sub("messages_users", r"LEFT JOIN uat_proj\.qualibot\.compare_feedbacks fb\s+ON fb\.message_id = m\.id",
    """LEFT JOIN (
      SELECT message_id, vote, comment,
        ROW_NUMBER() OVER (PARTITION BY message_id ORDER BY created_at DESC) AS rn
      FROM uat_proj.qualibot.compare_feedbacks
    ) fb
      ON fb.message_id = m.id
      AND fb.rn = 1""")
LOG.append("messages_users: latest feedback only (no duplicated analyses)")

# Temporal analyses: same total whatever the granularity
sub("analyses_temporelles", r"WHERE\s+DAYOFWEEK\(dt\) NOT IN \(1, 7\)",
    "WHERE\n    DAYOFWEEK(dt) NOT IN (1, 7)\n    OR dt IN (SELECT DATE(created_at) FROM messages_filtered)")
sub("analyses_temporelles", r"weekly AS \(.*?\n\),\nmonthly AS \(.*?\n\)\n",
    """weekly AS (
  SELECT
    s.periode,
    COUNT(mf.id) AS analyses
  FROM (
    SELECT EXPLODE(SEQUENCE(CAST(DATE_TRUNC('week', min_d) AS DATE), CAST(DATE_TRUNC('week', max_d) AS DATE), INTERVAL 7 DAYS)) AS periode
    FROM date_bounds
  ) s
    LEFT JOIN messages_filtered mf
      ON CAST(DATE_TRUNC('week', DATE(mf.created_at)) AS DATE) = s.periode
  GROUP BY s.periode
),
monthly AS (
  SELECT
    s.periode,
    COUNT(mf.id) AS analyses
  FROM (
    SELECT EXPLODE(SEQUENCE(CAST(DATE_TRUNC('month', min_d) AS DATE), CAST(DATE_TRUNC('month', max_d) AS DATE), INTERVAL 1 MONTH)) AS periode
    FROM date_bounds
  ) s
    LEFT JOIN messages_filtered mf
      ON CAST(DATE_TRUNC('month', DATE(mf.created_at)) AS DATE) = s.periode
  GROUP BY s.periode
)
""", flags=re.S)
LOG.append("analyses_temporelles: weekend activity kept, weeks/months zero-filled")

# Quality dataset: live tables instead of a frozen export
sub("chat_quality_data",
    r"LEFT JOIN read_files\('/Volumes/uat_landingzone/qualibot/staging/lakebase_export/chat_messages\.json', format => 'json'\) m",
    f"LEFT JOIN {ENV}_proj.qualibot.chat_messages m")
sub("chat_quality_data",
    r"FROM read_files\('/Volumes/uat_landingzone/qualibot/staging/lakebase_export/users\.json', format => 'json'\)",
    f"FROM {ENV}_proj.qualibot.users")
sub("mlflow_scorer_data", r"UNION ALL", "UNION")
LOG.append("quality: live chat_messages/users; mlflow trace_to_message UNION")

# ---------------------------------------------------------------- 7. user lookups: user_growth unchanged otherwise
# ---------------------------------------------------------------- 8. Health check and Intraqual changes
set_sql("hc_chunks", f"""
SELECT
  division,
  count(*) AS n_chunks
FROM {ENV}_proj.qualibot.chunks
GROUP BY division
""")

set_sql("hc_trend_30d", f"""
WITH h AS (
  SELECT job_run_id, run_date, SUM(docs_success) AS docs_success, SUM(docs_error) AS docs_error
  FROM {ENV}_proj.qualibot.parsing_run_health
  WHERE run_date >= current_date() - INTERVAL 30 DAYS
  GROUP BY job_run_id, run_date
),
c AS (
  SELECT job_run_id, count_if(parse_status = 'SKIPPED_SOURCE_REMOVED') AS n_removed
  FROM {ENV}_proj.qualibot.parsed_documents
  WHERE job_run_id IS NOT NULL
  GROUP BY job_run_id
)
SELECT h.run_date AS day, s.parse_status, s.n
FROM h
  LEFT JOIN c ON c.job_run_id = h.job_run_id
  LATERAL VIEW stack(
    3,
    'SUCCESS', h.docs_success,
    'ERROR', h.docs_error,
    'SKIPPED_SOURCE_REMOVED', COALESCE(c.n_removed, 0)
  ) s AS parse_status, n
WHERE s.n > 0
ORDER BY day DESC, parse_status
""")

set_sql("hc_error_details", f"""
WITH true_errors AS (
  SELECT
    IDDOC, ref, source_file_name, error_trace,
    date(ingestion_timestamp) AS since,
    CAST(datediff(current_date(), ingestion_timestamp) AS INT) AS days,
    'ERROR' AS category,
    CAST(NULL AS BIGINT) AS n_chunks,
    source_file_extension,
    ROUND(source_file_size_bytes / 1024.0, 1) AS file_size_kb
  FROM {ENV}_proj.qualibot.parsed_documents
  WHERE parse_status = 'ERROR'
),
current_backlog AS (
  SELECT IDDOC, ref, source_file_name, source_file_extension, source_file_size_bytes, date(ingestion_timestamp) AS ingested_on
  FROM {ENV}_proj.qualibot.parsed_documents
  WHERE parse_status = 'EMPTY_TEXT'
),
backlog_history AS (
  SELECT e.IDDOC, min(run_date) AS first_seen
  FROM {ENV}_proj.qualibot.parsing_run_health t
  LATERAL VIEW explode(t.errors) exploded AS e
  WHERE e.parse_status = 'EMPTY_TEXT'
  GROUP BY e.IDDOC
),
chunk_counts AS (
  SELECT IDDOC, count(*) AS n_chunks FROM {ENV}_proj.qualibot.chunks GROUP BY IDDOC
),
needs_ocr AS (
  SELECT
    cb.IDDOC, cb.ref,
    cb.source_file_name,
    CAST(NULL AS STRING) AS error_trace,
    COALESCE(h.first_seen, cb.ingested_on) AS since,
    CAST(datediff(current_date(), COALESCE(h.first_seen, cb.ingested_on)) AS INT) AS days,
    'NEEDS_OCR' AS category,
    COALESCE(cc.n_chunks, 0) AS n_chunks,
    cb.source_file_extension,
    ROUND(cb.source_file_size_bytes / 1024.0, 1) AS file_size_kb
  FROM current_backlog cb
  LEFT JOIN backlog_history h ON h.IDDOC = cb.IDDOC
  LEFT JOIN chunk_counts cc ON cc.IDDOC = cb.IDDOC
)
SELECT * FROM true_errors
UNION ALL
SELECT * FROM needs_ocr
ORDER BY category, since
""")

set_sql("hc_ocr_backlog_detail", f"""
WITH current_backlog AS (
  SELECT IDDOC, ref, source_file_name, source_file_extension, source_file_size_bytes, date(ingestion_timestamp) AS ingested_on
  FROM {ENV}_proj.qualibot.parsed_documents
  WHERE parse_status = 'EMPTY_TEXT'
),
history AS (
  SELECT e.IDDOC, min(run_date) AS first_seen
  FROM {ENV}_proj.qualibot.parsing_run_health t
  LATERAL VIEW explode(t.errors) exploded AS e
  WHERE e.parse_status = 'EMPTY_TEXT'
  GROUP BY e.IDDOC
),
chunk_counts AS (
  SELECT IDDOC, count(*) AS n_chunks FROM {ENV}_proj.qualibot.chunks GROUP BY IDDOC
)
SELECT
  cb.IDDOC,
  cb.ref,
  cb.source_file_name,
  cb.source_file_extension,
  ROUND(cb.source_file_size_bytes / 1024.0, 1) AS file_size_kb,
  COALESCE(h.first_seen, cb.ingested_on) AS first_seen,
  datediff(current_date(), COALESCE(h.first_seen, cb.ingested_on)) AS days_in_backlog,
  COALESCE(cc.n_chunks, 0) AS n_chunks,
  CASE WHEN COALESCE(cc.n_chunks, 0) > 0 THEN 'indexed' ELSE 'empty' END AS kb_status
FROM current_backlog cb
  LEFT JOIN history h ON h.IDDOC = cb.IDDOC
  LEFT JOIN chunk_counts cc ON cc.IDDOC = cb.IDDOC
ORDER BY first_seen
""")

sub("hc_last_run_v2", r"count\(\*\) AS n_total_corpus",
    "count_if(NOT (parse_status LIKE 'SKIPPED%' OR parse_status = 'FILTERED_BY_DATE')) AS n_total_corpus")
for it in PAGES["Health Check - Parsing"]["layout"]:
    ww = it["widget"]
    if widget_title(ww) == "Last Run Duration":
        set_title(ww, "Avg Parse Time (s/doc)")
    if widget_title(ww) == "Docs in Error":
        ww["queries"][0]["query"]["filters"] = []
LOG.append("health check: tables of the current pipeline, debug filter removed, backlog/trend/scope counts consistent")

STATUS_EXCLUDED = ("'SKIPPED_REF_OUT_OF_SCOPE', 'FILTERED_BY_DATE', 'SKIPPED_IDDOC_NOT_FOUND', 'SKIPPED_SOURCE_REMOVED', "
                   "'SKIPPED_EMPTY_SMALL_FILE', 'SKIPPED_REF_MANUAL', 'SKIPPED_CATEGORY_EXCLUDED', "
                   "'SKIPPED_SUPERSEDED_REVISION', 'SKIPPED_EMPTY_FOLDER', 'SKIPPED_EMPTY_IMAGES'")
STATUS_ERROR = "'EMPTY_TEXT', 'ERROR', 'TIMEOUT', 'SKIP_TOO_LARGE', 'MISSING_CHUNKS'"
for name in ("dc_log", "dc_counters"):
    sub(name, r"il\.parse_status IN \(\s*'SKIPPED_REF_OUT_OF_SCOPE'.*?\)\s*THEN\s*'Excluded'",
        f"il.parse_status IN ({STATUS_EXCLUDED})\n      THEN\n        'Excluded'", flags=re.S)
    sub(name, r"il\.parse_status IN \('EMPTY_TEXT', 'ERROR', 'TIMEOUT'\)", f"il.parse_status IN ({STATUS_ERROR})")
sub("dc_log", r"FROM\s+prod_bronze\.intraqual\.gd_doc_latest",
    "FROM\n    prod_gold.quality_system.document_revisions_intraqual")
sub("dc_log", r"scope_check AS \(\s*SELECT\s+IDDOC,\s+COURANT,\s+ETAT,\s+NONVISIBLE,\s+CASE.*?END AS in_qualibot_scope",
    """scope_check AS (
  SELECT
    document_revision_ID AS IDDOC,
    revision_status_code AS COURANT,
    lifecycle_state_code AS ETAT,
    is_hidden AS NONVISIBLE,
    (revision_status_code = 1 AND lifecycle_state_code = 7 AND is_hidden = false) AS in_qualibot_scope""", flags=re.S)
sub("dc_log", r"CONCAT\('Out of scope: courant=', sc\.COURANT, ' etat=', sc\.ETAT, ' nonvisible=', sc\.NONVISIBLE\)",
    "CONCAT('Out of scope: revision_status=', CAST(sc.COURANT AS STRING), ' lifecycle_state=', CAST(sc.ETAT AS STRING), ' hidden=', CAST(sc.NONVISIBLE AS STRING))")
sub("dc_log", r"' \(pipeline bug: no processed_files entry written for empty folders\)'", "' (no parsed_documents row)'")
sub("doc_search_meta", r"'SKIPPED_SOURCE_REMOVED'\)", "'SKIPPED_SOURCE_REMOVED', 'SKIPPED_SUPERSEDED_REVISION')")
sub("doc_search_meta", r"IDDOC, ref, titre, parse_status, parser_strategy,",
    "IDDOC, ref, titre, parse_status, parser_strategy, source_file_name, source_file_extension,\n  ROUND(source_file_size_bytes / 1024.0, 1) AS file_size_kb,")


def add_columns(page: str, name: str, src: str, cols: list, after: str) -> None:
    """Adds columns to a table widget, after an existing column.

    :param page: Page display name.
    :param name: Widget name.
    :param src: Relationship source name of the dataset (None for a widget reading the dataset directly).
    :param cols: [(field, display name, width)].
    :param after: Existing field whose column the new ones follow.
    """
    w = widget(page, name)
    q = w["queries"][0]["query"]
    enc = w["spec"]["encodings"]["columns"]
    pos = next(i for i, c in enumerate(enc) if c["fieldName"].endswith(after)) + 1
    for k, (field, display, width) in enumerate(cols):
        fname = f"{src}__{field}" if src else field
        expr = f"`{src}`.`{field}`" if src else f"`{field}`"
        q["fields"].append({"name": fname, "expression": expr})
        col = {"fieldName": fname, "displayName": display, "columnWidth": width}
        if field == "file_size_kb":
            col["format"] = {"type": "number-plain", "abbreviation": "none", "decimalPlaces": {"type": "exact", "places": 1}}
        enc.insert(pos + k, col)


SIZE_COLS = [("file_size_kb", "Size (KB)", 90), ("source_file_extension", "Type", 70)]
add_columns("Health Check - Parsing", "table_errors", "HC__Error_Details", SIZE_COLS, "ref")
add_columns("Health Check - Parsing", "table_ocr_backlog", "HC__OCR_Backlog_Detail",
            [("source_file_name", "File", 260)] + SIZE_COLS, "ref")
add_columns("Document Search", "ds_table_meta", "DocSearch__Meta",
            [("source_file_name", "File", 260)] + SIZE_COLS, "ref")
set_sql("doc_search_text", f"""
WITH best AS (
  SELECT
    IDDOC, ingestion_timestamp, parse_status,
    ROW_NUMBER() OVER (
      PARTITION BY IDDOC
      ORDER BY CASE WHEN parse_status = 'SUCCESS' THEN 0 ELSE 1 END, ingestion_timestamp DESC
    ) AS rn
  FROM {ENV}_proj.qualibot.parsing_checkpoint
)
SELECT c.IDDOC, c.ref, c.parse_status, c.parser_strategy, c.document_text, c.parser_error
FROM best b
  JOIN {ENV}_proj.qualibot.parsing_checkpoint c
    ON c.IDDOC = b.IDDOC AND c.ingestion_timestamp = b.ingestion_timestamp AND c.parse_status = b.parse_status
WHERE b.rn = 1
""")
LOG.append("intraqual changes / doc search: current tables, status lists, scope table of the data asset")

# ---------------------------------------------------------------- 8b. costs: every Lakebase project and the old KA vector endpoints
LAKEBASE_PROJECTS = ", ".join(f"'{c['lakebase_project']}'" for c in CONFIGS.values())
for name in ("qb_cost_all", "qb_cost_hourly"):
    sub(name, r"u\.usage_metadata\.project_id = 'd9fd81bc-5865-4ef0-8718-520a9e8d6c68'",
        f"u.usage_metadata.project_id IN ({LAKEBASE_PROJECTS})")
    sub(name, r"AND u\.usage_metadata\.endpoint_name = 'qualibot'",
        "AND (u.usage_metadata.endpoint_name = 'qualibot' OR u.usage_metadata.endpoint_name ILIKE 'ka-%-vs-endpoint')")
    sub(name, r"WHEN billing_origin_product = 'VECTOR_SEARCH' THEN 'Vector Search'",
        "WHEN billing_origin_product = 'VECTOR_SEARCH' AND endpoint_name ILIKE 'ka-%' THEN 'Vector Search (old KA endpoints)'\n    WHEN billing_origin_product = 'VECTOR_SEARCH' THEN 'Vector Search'")
LOG.append("costs: 3 Lakebase projects, old KA vector endpoints shown as their own category")

# ---------------------------------------------------------------- 8c. Quality page: MLflow KA-era section removed (source table absent)
qp = PAGES["ChatBot - Quality"]["layout"]
gone = [i for i in qp if i["widget"]["name"].startswith("mlflow_")]
cut = max(i["position"]["y"] + i["position"]["height"] for i in gone)
top = min(i["position"]["y"] for i in gone)
qp[:] = [i for i in qp if i not in gone]
for i in qp:
    if i["position"]["y"] >= cut:
        i["position"]["y"] -= cut - top
DS.pop("mlflow_scorer_data")
d["datasets"] = [x for x in d["datasets"] if x["name"] in DS]
d["relationshipGraphs"][0]["sources"] = [s for s in d["relationshipGraphs"][0]["sources"] if s["datasetName"] in DS]
LOG.append("quality: MLflow section and dataset removed (ka_mlflow_scorer_assessments does not exist)")

# ---------------------------------------------------------------- 8d. Quality page (LLM judge) and trace links removed
d["pages"] = [p for p in d["pages"] if p["displayName"] != "ChatBot - Quality"]
PAGES.pop("ChatBot - Quality")
DS.pop("chat_quality_data")
HOST = UAT["host"] + "/ml/experiments/"
TAIL = f"/traces?o={UAT['workspace_id']}&selectedEvaluationId="


def trace_link_sql(trace_col: str, division_col: str, mlflow_col: str) -> str:
    """SQL expression of the link to the MLflow trace of a message.

    :param trace_col: Column with the chat trace id (`tr-<hex>` for the former KA traces).
    :param division_col: Column with the division.
    :param mlflow_col: Column with the MLflow trace id of the turn (`trace:/<catalog>.<schema>.<experiment>/<hex>`).
    :return: SQL expression, NULL when the message has no trace.
    """
    # The experiment id is part of the turn's trace id (trace:/<catalog>.<schema>.<experiment id>/<hex>): the link needs no per-env
    # configuration (a PROD/DEV dashboard used to get NULL links because CONFIGS had no id).
    exp = f"REGEXP_EXTRACT({mlflow_col}, '^trace:/[^/]*[.]([0-9]+)/', 1)"
    return (
        f"CASE "
        f"WHEN {mlflow_col} RLIKE '^trace:/[^/]*[.][0-9]+/' THEN CONCAT('{HOST}', {exp}, '{TAIL}', "
        f"REPLACE(REPLACE({mlflow_col}, ':', '%3A'), '/', '%2F')) "
        f"WHEN {mlflow_col} IS NOT NULL AND {mlflow_col} != '' AND '{CFG["experiment_id"] or ""}' != '' THEN CONCAT('{HOST}{CFG["experiment_id"]}{TAIL}', "
        f"REPLACE(REPLACE({mlflow_col}, ':', '%3A'), '/', '%2F')) "
        f"WHEN {trace_col} RLIKE '^tr-[0-9a-f]{{32}}$' THEN CONCAT('{HOST}', "
        f"CASE WHEN {division_col} = 'AS' THEN '2748374992560664' WHEN {division_col} = 'IS' THEN '2748374992560665' "
        f"ELSE '4171178917767011' END, '{TAIL}', {trace_col}) "
        f"ELSE NULL END"
    )


TURNS_JOIN = (f"(SELECT assistant_message_id, MAX(mlflow_trace_id) AS mlflow_trace_id "
              f"FROM {ENV}_proj.qualibot.chat_turns WHERE mlflow_trace_id IS NOT NULL AND mlflow_trace_id != '' "
              f"GROUP BY assistant_message_id)")
sub("chat_conversations_paired", r"CASE WHEN a\.trace_id IS NOT NULL.*?AS trace_link",
    trace_link_sql("a.trace_id", "u.division", "tt.mlflow_trace_id") + " AS trace_link", flags=re.S)
sub("chat_conversations_paired", r"(AND a\.msg_rn = an\.next_asst_rn)",
    "AND a.msg_rn = an.next_asst_rn\n    LEFT JOIN " + TURNS_JOIN + " tt ON tt.assistant_message_id = a.id")
sub("chat_data", r"m\.trace_id,", "m.trace_id,\n  tt.mlflow_trace_id,")
sub("chat_data", r"AND fb\.rn = 1\s*\nWHERE", "AND fb.rn = 1\n    LEFT JOIN " + TURNS_JOIN + " tt\n      ON m.id = tt.assistant_message_id\nWHERE")
for c in DS["chat_data"]["columns"]:
    if c["displayName"] == "trace_link":
        c["expression"] = trace_link_sql("trace_id", "division", "mlflow_trace_id")
        c["description"] = "Link to the MLflow trace of the message"
LOG.append("page ChatBot - Quality (LLM judge) removed; trace links rebuilt (turn trace in qualibot_chat_traces, KA traces by division)")

# ---------------------------------------------------------------- 9. unused datasets
for name in ("debug_recent_users", "hc_index_freshness", "scoring_runs_data", "scoring_runs_summary"):
    DS.pop(name)
d["datasets"] = [x for x in d["datasets"] if x["name"] in DS]
d["relationshipGraphs"][0]["sources"] = [s for s in d["relationshipGraphs"][0]["sources"] if s["datasetName"] in DS]
LOG.append("2 unused datasets removed")

# ---------------------------------------------------------------- 10. global filters
gf = PAGES["Global Filters"]["layout"]
for it in gf:
    w = it["widget"]
    if w["name"] == "3fcbea78":
        w["spec"]["frame"] = {"showTitle": True, "title": "Utilisateur"}
    if w["name"] == "date_range":
        for ds_name, field in (("chat_data", "Date"), ("chat_conversations_paired", "session_date")):
            qn = f"dashboards/{DASH_ID}/datasets/{ds_name}_{field}"
            w["queries"].append({"name": qn, "query": {"datasetName": ds_name, "fields": [
                {"name": field, "expression": f"`{field}`"},
                {"name": f"{field}_associativity", "expression": "COUNT_IF(`associative_filter_predicate_group`)"},
            ], "disaggregated": False}})
            w["spec"]["encodings"]["fields"].append({"fieldName": field, "queryName": qn})
LOG.append("global filters: period also applies to chat, quality and conversations; user filter titled")

# ---------------------------------------------------------------- 11. new page: ChatBot - Reliability
SKIP_ADMINS = """
  NOT array_contains(COALESCE(u.groups, CAST(ARRAY() AS ARRAY<STRING>)), 'Role-Project-LEAP-CoreDev')
  AND NOT array_contains(COALESCE(u.groups, CAST(ARRAY() AS ARRAY<STRING>)), 'Role-Project-LEAP-CoreAdmin')
  AND COALESCE(u.email, '') NOT IN (%s)""" % EXCLUDED_EMAILS
USERS = f"""(
  SELECT user_id, MAX(REPLACE(REPLACE(email, '\\n', ''), '\\r', '')) AS email, FLATTEN(COLLECT_SET(groups)) AS groups
  FROM {ENV}_proj.qualibot.users GROUP BY user_id
) u"""
d["datasets"].append({
    "name": "chat_turns_data", "displayName": "ChatBot Turns (reliability)",
    "queryLines": (f"""
SELECT
  DATE(TRY_CAST(t.created_at AS TIMESTAMP)) AS turn_date,
  TRY_CAST(t.created_at AS TIMESTAMP) AS created_at,
  t.trace_id, t.status, t.error_stage, t.error_type, t.division, t.question_lang,
  CAST(t.warnings AS STRING) AS warnings,
  t.total_ms / 1000.0 AS total_s,
  t.first_token_ms / 1000.0 AS first_token_s,
  t.retrieval_ms / 1000.0 AS retrieval_s,
  t.generation_ms / 1000.0 AS generation_s,
  t.passages_retrieved, t.passages_sent, t.cost_eur,
  COALESCE(u.email, 'Unknown user') AS email
FROM {ENV}_proj.qualibot.chat_turns t
  LEFT JOIN {USERS}
    ON t.user_id = u.user_id
WHERE t.status IN ('ok', 'degraded', 'error')
  AND {SKIP_ADMINS}
""").strip("\n").splitlines(keepends=True),
    "columns": [
        {"displayName": "Is error", "description": "1 if the turn ended without answer", "expression": "CASE WHEN status = 'error' THEN 1 ELSE 0 END"},
        {"displayName": "Is degraded", "description": "1 if the answer did not run as configured", "expression": "CASE WHEN status = 'degraded' THEN 1 ELSE 0 END"},
        {"displayName": "Semaine", "description": "Week start", "expression": "DATE_TRUNC('week', created_at)"},
    ],
})
d["datasets"].append({
    "name": "chat_turns_daily", "displayName": "ChatBot Turn Latency by Day",
    "queryLines": (f"""
WITH t AS (
  SELECT DATE(TRY_CAST(t.created_at AS TIMESTAMP)) AS turn_date, t.first_token_ms / 1000.0 AS first_token_s,
         t.total_ms / 1000.0 AS total_s
  FROM {ENV}_proj.qualibot.chat_turns t
    LEFT JOIN {USERS}
      ON t.user_id = u.user_id
  WHERE t.status IN ('ok', 'degraded')
    AND {SKIP_ADMINS}
),
agg AS (
  SELECT turn_date,
    percentile_approx(first_token_s, 0.5) AS first_token_p50,
    percentile_approx(first_token_s, 0.95) AS first_token_p95,
    percentile_approx(total_s, 0.5) AS total_p50,
    percentile_approx(total_s, 0.95) AS total_p95
  FROM t GROUP BY turn_date
)
SELECT turn_date, s.metric, s.seconds
FROM agg
  LATERAL VIEW stack(
    4,
    'First token p50', first_token_p50,
    'First token p95', first_token_p95,
    'Total p50', total_p50,
    'Total p95', total_p95
  ) s AS metric, seconds
ORDER BY turn_date
""").strip("\n").splitlines(keepends=True),
})
d["datasets"].append({
    "name": "app_errors_top", "displayName": "App Errors by Fingerprint (30d)",
    "queryLines": (f"""
SELECT
  fingerprint,
  MAX(severity) AS severity,
  MAX(stage) AS stage,
  MAX(error_type) AS error_type,
  MAX(origin) AS origin,
  MAX(upstream) AS upstream,
  MAX(error_msg) AS sample_message,
  COUNT(*) AS occurrences,
  COUNT(DISTINCT user_id) AS users,
  MIN(TRY_CAST(created_at AS TIMESTAMP)) AS first_seen,
  MAX(TRY_CAST(created_at AS TIMESTAMP)) AS last_seen
FROM {ENV}_proj.qualibot.app_errors
WHERE TRY_CAST(created_at AS TIMESTAMP) >= current_timestamp() - INTERVAL 30 DAYS
GROUP BY fingerprint
ORDER BY occurrences DESC
""").strip("\n").splitlines(keepends=True),
})

PG = "ChatBot - Reliability"


def counter(name, title, expr, fname, x, y, fmt=None, display="Value"):
    """Counter widget on chat_turns_data.

    :param name: Widget name.
    :param title: Title.
    :param expr: Aggregate expression.
    :param fname: Field name.
    :param x: Grid x.
    :param y: Grid y.
    :param fmt: Optional value format.
    :param display: Value display name.
    :return: Layout item.
    """
    enc = {"fieldName": fname, "displayName": display}
    if fmt:
        enc["format"] = fmt
    return {"widget": {"name": name,
                       "queries": [{"name": "main_query", "query": {"datasetName": "chat_turns_data", "fields": [
                           {"name": fname, "expression": expr}], "disaggregated": False}}],
                       "spec": {"frame": {"title": title, "showTitle": True}, "version": 2, "widgetType": "counter",
                                "encodings": {"value": enc}, "data": {"queryName": "main_query"}}},
            "position": {"x": x, "y": y, "width": 3, "height": 3}}


PCT = {"type": "number-percent", "decimalPlaces": {"type": "exact", "places": 1}}
SEC = {"type": "number-plain", "abbreviation": "none", "decimalPlaces": {"type": "exact", "places": 1}}
layout = [
    {"widget": {"name": "rel_header", "multilineTextboxSpec": {"lines": [
        "## ChatBot Reliability\n",
        "One row per chat turn (`chat_turns`): outcome, latency and cost. `degraded` = an answer was given but a step did not run as configured; `error` = no answer. Source: Delta copy of Lakebase, as fresh as the last export."]}},
     "position": {"x": 0, "y": 0, "width": 12, "height": 2}},
    counter("rel_turns", "Turns", "COUNT(`*`)", "count(*)", 0, 2, display="Turns"),
    counter("rel_err_rate", "Error rate", "AVG(`Is error`)", "avg(Is error)", 3, 2, PCT, "Error rate"),
    counter("rel_deg_rate", "Degraded rate", "AVG(`Is degraded`)", "avg(Is degraded)", 6, 2, PCT, "Degraded rate"),
    counter("rel_ttft", "Avg first token (s)", "AVG(`first_token_s`)", "avg(first_token_s)", 9, 2, SEC, "Seconds"),
    {"widget": {"name": "rel_status_day",
                "queries": [{"name": "main_query", "query": {"datasetName": "chat_turns_data", "fields": [
                    {"name": "turn_date", "expression": "`turn_date`"},
                    {"name": "status", "expression": "`status`"},
                    {"name": "count(*)", "expression": "COUNT(`*`)"}], "disaggregated": False}}],
                "spec": {"version": 3, "frame": {"title": "Turns by Day and Status", "showTitle": True},
                         "mark": {"layout": "stack"}, "widgetType": "bar",
                         "encodings": {
                             "x": {"fieldName": "turn_date", "displayName": "Day", "scale": {"type": "temporal"}},
                             "y": {"fieldName": "count(*)", "displayName": "Turns", "scale": {"type": "quantitative", "domain": {"min": 0}}},
                             "color": {"fieldName": "status", "displayName": "Status", "scale": {"type": "categorical", "mappings": [
                                 {"value": "ok", "color": "#2E7D32"}, {"value": "degraded", "color": "#E65100"},
                                 {"value": "error", "color": "#C62828"}]}}},
                         "data": {"queryName": "main_query"}}},
     "position": {"x": 0, "y": 5, "width": 6, "height": 7}},
    {"widget": {"name": "rel_latency",
                "queries": [{"name": "main_query", "query": {"datasetName": "chat_turns_daily", "fields": [
                    {"name": "turn_date", "expression": "`turn_date`"},
                    {"name": "metric", "expression": "`metric`"},
                    {"name": "avg(seconds)", "expression": "AVG(`seconds`)"}], "disaggregated": False}}],
                "spec": {"version": 3, "frame": {"title": "Latency by Day (seconds)", "showTitle": True},
                         "widgetType": "line",
                         "encodings": {
                             "x": {"fieldName": "turn_date", "displayName": "Day", "scale": {"type": "temporal"}},
                             "y": {"fieldName": "avg(seconds)", "displayName": "Seconds", "scale": {"type": "quantitative"}},
                             "color": {"fieldName": "metric", "displayName": "Metric", "scale": {"type": "categorical"}}},
                         "data": {"queryName": "main_query"}}},
     "position": {"x": 6, "y": 5, "width": 6, "height": 7}},
    {"widget": {"name": "rel_errors_table",
                "queries": [{"name": "main_query", "query": {"datasetName": "app_errors_top", "fields": [
                    {"name": c, "expression": f"`{c}`"} for c in (
                        "severity", "stage", "error_type", "origin", "upstream", "sample_message", "occurrences",
                        "users", "first_seen", "last_seen")], "disaggregated": True}}],
                "spec": {"version": 2, "frame": {"title": "Errors and Warnings by Fingerprint (30d)", "showTitle": True},
                         "widgetType": "table", "rowsPerPage": 25,
                         "encodings": {"columns": [
                             {"fieldName": "severity", "displayName": "Severity", "columnWidth": 90},
                             {"fieldName": "stage", "displayName": "Stage", "columnWidth": 110},
                             {"fieldName": "error_type", "displayName": "Type", "columnWidth": 160},
                             {"fieldName": "origin", "displayName": "Origin", "columnWidth": 280},
                             {"fieldName": "upstream", "displayName": "Upstream", "columnWidth": 200},
                             {"fieldName": "sample_message", "displayName": "Message", "columnWidth": 360},
                             {"fieldName": "occurrences", "displayName": "Count", "columnWidth": 80},
                             {"fieldName": "users", "displayName": "Users", "columnWidth": 70},
                             {"fieldName": "first_seen", "displayName": "First seen"},
                             {"fieldName": "last_seen", "displayName": "Last seen"}]},
                         "data": {"queryName": "main_query"}}},
     "position": {"x": 0, "y": 12, "width": 12, "height": 8}},
    {"widget": {"name": "rel_turns_table",
                "queries": [{"name": "main_query", "query": {"datasetName": "chat_turns_data", "fields": [
                    {"name": c, "expression": f"`{c}`"} for c in (
                        "created_at", "email", "status", "error_stage", "warnings", "division", "first_token_s",
                        "total_s", "passages_sent", "cost_eur", "trace_id")],
                    "filters": [{"expression": "`status` IN ('degraded', 'error')"}], "disaggregated": True}}],
                "spec": {"version": 2, "frame": {"title": "Degraded and Failed Turns", "showTitle": True},
                         "widgetType": "table", "rowsPerPage": 25,
                         "encodings": {"columns": [
                             {"fieldName": "created_at", "displayName": "Date"},
                             {"fieldName": "email", "displayName": "User", "columnWidth": 200},
                             {"fieldName": "status", "displayName": "Status", "columnWidth": 90},
                             {"fieldName": "error_stage", "displayName": "Error stage", "columnWidth": 110},
                             {"fieldName": "warnings", "displayName": "Warnings", "columnWidth": 260},
                             {"fieldName": "division", "displayName": "Division", "columnWidth": 80},
                             {"fieldName": "first_token_s", "displayName": "First token (s)", "columnWidth": 100},
                             {"fieldName": "total_s", "displayName": "Total (s)", "columnWidth": 90},
                             {"fieldName": "passages_sent", "displayName": "Passages", "columnWidth": 80},
                             {"fieldName": "cost_eur", "displayName": "Cost (EUR)", "columnWidth": 90},
                             {"fieldName": "trace_id", "displayName": "Trace", "columnWidth": 280}]},
                         "data": {"queryName": "main_query"}}},
     "position": {"x": 0, "y": 20, "width": 12, "height": 8}},
]
page = {"name": "chatbot_reliability", "displayName": PG, "layout": layout, "pageType": "PAGE_TYPE_CANVAS"}
idx = [p["displayName"] for p in d["pages"]].index("ChatBot - Satisfaction")
d["pages"].insert(idx + 1, page)
LOG.append("new page ChatBot - Reliability (chat_turns, app_errors)")

# period filter also on turns
for it in gf:
    w = it["widget"]
    if w["name"] == "date_range":
        qn = f"dashboards/{DASH_ID}/datasets/chat_turns_data_turn_date"
        w["queries"].append({"name": qn, "query": {"datasetName": "chat_turns_data", "fields": [
            {"name": "turn_date", "expression": "`turn_date`"},
            {"name": "turn_date_associativity", "expression": "COUNT_IF(`associative_filter_predicate_group`)"}],
            "disaggregated": False}})
        w["spec"]["encodings"]["fields"].append({"fieldName": "turn_date", "queryName": qn})

# ---------------------------------------------------------------- 11a. ChatBot eligible users include the Internal sub-group
INTERNAL_GROUP = "Role-LATECOERE-User-Internal"
sub(
    "adoption_droits",
    r"request_params\['targetGroupName'\] = 'Role-Project-LEAP-End-users-Qualibot-ChatBot'",
    "request_params['targetGroupName'] IN ('Role-Project-LEAP-End-users-Qualibot-ChatBot', "
    f"'{INTERNAL_GROUP}')",
    count=2,
)
# a member is a person: group names added as principals (CoreDev, the sub-group itself) are not counted
sub(
    "adoption_droits",
    r"(action_name = 'addPrincipalToGroup'\s+AND request_params\['targetGroupName'\] IN \('Role-Project-LEAP-End-users-Qualibot-ChatBot')",
    "action_name = 'addPrincipalToGroup'\n    AND request_params['targetUserName'] LIKE '%@%'\n    AND request_params['targetGroupName'] IN ('Role-Project-LEAP-End-users-Qualibot-ChatBot'",
)

# ---------------------------------------------------------------- 11b. Document diagnostic: why a document is, or is not, in the chatbot
ROOTS_IN_SCOPE = "3870, 948, 3209, 12, 962, 3208, 4001, 3867"
d["datasets"].append({
    "name": "doc_diagnostic", "displayName": "Document Diagnostic",
    "queryLines": (f"""
WITH rev0 AS (
  SELECT document_revision_ID AS IDDOC, document_reference AS ref, document_title AS titre, published_date,
         revision_status_code, lifecycle_state_code, is_hidden, root_category_ID, category_ID_path,
         last(CASE WHEN root_category_ID IS NOT NULL THEN struct(root_category_ID AS rc, category_ID_path AS cp) END, true)
           OVER (PARTITION BY document_reference ORDER BY document_revision_ID
                 ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING) AS earlier
  FROM prod_gold.quality_system.document_revisions_intraqual
),
rev AS (
  SELECT IDDOC, ref, titre, published_date, revision_status_code, lifecycle_state_code, is_hidden,
         COALESCE(root_category_ID, earlier.rc) AS root_category_ID,
         CASE WHEN root_category_ID IS NULL THEN earlier.cp ELSE category_ID_path END AS category_ID_path
  FROM rev0
),
cat AS (
  SELECT ancestor_category_ID AS id, first(ancestor_category_name) AS name
  FROM prod_gold.quality_system.category_ancestors_intraqual GROUP BY ancestor_category_ID
),
cur AS (
  SELECT ref, MAX(IDDOC) AS current_iddoc FROM rev WHERE revision_status_code = 1 GROUP BY ref
),
pd AS (
  SELECT IDDOC, ref AS pd_ref, titre AS pd_titre, indice, parse_status, error_trace, source_file_name,
         source_file_extension, source_file_size_bytes
  FROM {ENV}_proj.qualibot.parsed_documents
),
ck AS (
  SELECT IDDOC, MAX(ref) AS ck_ref FROM {ENV}_proj.qualibot.parsing_checkpoint GROUP BY IDDOC
),
ch AS (
  SELECT IDDOC, COUNT(*) AS n_chunks FROM {ENV}_proj.qualibot.chunks GROUP BY IDDOC
),
excl AS (
  SELECT IDDOC, MAX(reason) AS reason FROM {ENV}_proj.qualibot.rag_exclusions GROUP BY IDDOC
),
ids AS (
  SELECT IDDOC FROM rev UNION SELECT IDDOC FROM pd UNION SELECT IDDOC FROM ck
),
j AS (
  SELECT
    i.IDDOC,
    COALESCE(rev.ref, pd.pd_ref, ck.ck_ref) AS ref,
    COALESCE(rev.titre, pd.pd_titre) AS titre,
    pd.indice, rev.published_date, cat.name AS root_category,
    rev.revision_status_code, rev.lifecycle_state_code, rev.is_hidden,
    pd.parse_status, pd.error_trace, pd.source_file_name, pd.source_file_extension,
    ROUND(pd.source_file_size_bytes / 1024.0, 1) AS file_size_kb,
    COALESCE(ch.n_chunks, 0) AS n_chunks,
    cur.current_iddoc,
    excl.reason AS exclusion_reason,
    rev.IDDOC IS NOT NULL AS in_intraqual,
    ck.IDDOC IS NOT NULL AS has_checkpoint,
    pd.IDDOC IS NOT NULL AS has_parsed_row,
    (rev.root_category_ID IN ({ROOTS_IN_SCOPE}) OR exists(rev.category_ID_path, x -> x IN (3967, 4006))) AS in_categories
  FROM ids i
    LEFT JOIN rev ON rev.IDDOC = i.IDDOC
    LEFT JOIN cat ON cat.id = rev.root_category_ID
    LEFT JOIN cur ON cur.ref = rev.ref
    LEFT JOIN pd ON pd.IDDOC = i.IDDOC
    LEFT JOIN ck ON ck.IDDOC = i.IDDOC
    LEFT JOIN ch ON ch.IDDOC = i.IDDOC
    LEFT JOIN excl ON excl.IDDOC = i.IDDOC
),
v AS (
  SELECT *,
    CASE
      WHEN n_chunks > 0 THEN '01 IN CHATBOT'
      WHEN NOT in_intraqual THEN '02 NOT IN INTRAQUAL'
      WHEN revision_status_code <> 1 THEN '03 NOT CURRENT REVISION'
      WHEN lifecycle_state_code <> 7 THEN '04 NOT PUBLISHED'
      WHEN is_hidden THEN '05 HIDDEN'
      WHEN NOT in_categories THEN '06 CATEGORY OUT OF SCOPE'
      WHEN exclusion_reason IS NOT NULL OR parse_status IN ('SKIPPED_REF_MANUAL', 'SKIPPED_CATEGORY_EXCLUDED')
        THEN '08 MANUAL EXCLUSION'
      WHEN parse_status = 'SKIPPED_SUPERSEDED_REVISION' THEN '09 REPLACED BY NEWER REVISION'
      WHEN NOT has_parsed_row AND has_checkpoint THEN '10 PARSED, RESULT NOT BUILT'
      WHEN NOT has_parsed_row THEN '11 NOT PROCESSED YET'
      WHEN parse_status = 'ERROR' THEN '12 PARSE ERROR'
      WHEN parse_status = 'EMPTY_TEXT' THEN '13 NO TEXT, NEEDS OCR'
      WHEN parse_status = 'IMAGES_PENDING' THEN '14 IMAGES TO DESCRIBE'
      WHEN parse_status = 'SKIP_TOO_LARGE' THEN '15 FILE TOO LARGE'
      WHEN parse_status IN ('SKIPPED_EMPTY_SMALL_FILE', 'SKIPPED_EMPTY_IMAGES') THEN '16 EMPTY DOCUMENT'
      WHEN parse_status = 'SKIPPED_EMPTY_FOLDER' THEN '17 NO FILE ON VOLUME'
      WHEN parse_status = 'SUCCESS' THEN '18 PARSED BUT NO PASSAGE'
      ELSE '19 OTHER STATUS'
    END AS reason
  FROM j
)
SELECT
  ref, IDDOC, titre, indice, published_date, root_category, reason,
  CASE LEFT(reason, 2)
    WHEN '01' THEN CONCAT(CAST(n_chunks AS STRING), ' passages in the chunks table (the index follows its next sync)')
    WHEN '02' THEN 'Absent from the Intraqual tables: deleted at the source, or an identifier that never existed'
    WHEN '03' THEN CONCAT('An older revision (revision_status_code=', CAST(revision_status_code AS STRING),
                          '); the current one is IDDOC ', COALESCE(CAST(current_iddoc AS STRING), 'none (REF withdrawn)'))
    WHEN '04' THEN CONCAT('Not published: lifecycle_state_code=', CAST(lifecycle_state_code AS STRING), ' (only 7 is published)')
    WHEN '05' THEN 'Hidden in Intraqual (is_hidden = true)'
    WHEN '06' THEN CONCAT('Root category "', COALESCE(root_category, 'none'), '" is not in the indexed scope')
    WHEN '08' THEN CONCAT('Manual exclusion', COALESCE(CONCAT(': ', exclusion_reason), ''))
    WHEN '09' THEN CONCAT('Older revision of the same REF; only the newest revision is indexed (current IDDOC ',
                          COALESCE(CAST(current_iddoc AS STRING), '?'), ')')
    WHEN '10' THEN 'A parsed text exists in the checkpoint but the passage tables were not rebuilt for it: the next run builds it'
    WHEN '11' THEN 'In scope, never processed: the next pipeline run picks it up'
    WHEN '12' THEN CONCAT('Parsing failed (retried by the next run): ', COALESCE(LEFT(error_trace, 200), ''))
    WHEN '13' THEN 'No text could be extracted (scan) and the file is above the empty-file threshold: waiting for OCR'
    WHEN '14' THEN 'No text, images still to be described by the vision model'
    WHEN '15' THEN 'File above the size ceiling of its format: not parsed'
    WHEN '16' THEN 'No text or no usable image, and a file of at most 50 KiB: treated as empty'
    WHEN '17' THEN 'No parseable file in the Intraqual volume folder of this document'
    WHEN '18' THEN 'Parsing succeeded but produced no passage; the pipeline marks it MISSING_CHUNKS on its next run'
    ELSE CONCAT('Status ', COALESCE(parse_status, 'unknown'))
  END AS explanation,
  parse_status, source_file_name, source_file_extension, file_size_kb, n_chunks, current_iddoc
FROM v
ORDER BY ref, IDDOC DESC
""").strip("\n").splitlines(keepends=True),
})
# the REF filter also drives the diagnostic
for pg in d["pages"]:
    for it in pg["layout"]:
        w = it["widget"]
        if w["name"] == "ds_filter_ref":
            w["queries"].append({"name": "diag_ref_q", "query": {"datasetName": "doc_diagnostic", "fields": [
                {"name": "ref", "expression": "`ref`"},
                {"name": "ref_associativity", "expression": "COUNT_IF(`associative_filter_predicate_group`)"}],
                "disaggregated": False}})
            w["spec"]["encodings"]["fields"].append({"fieldName": "ref", "queryName": "diag_ref_q"})
LOG.append("Document explorer: diagnostic of each document (reason it is, or is not, in the chatbot)")

# ---------------------------------------------------------------- 12. restructure: 7 pages, one 12-column grid, one palette
PALETTE = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#6250d6", "#e34948"]
d["uiSettings"]["theme"]["visualizationColors"] = PALETTE
COLOR_MAP = {"#2E7D32": "#0ca30c", "#C62828": "#d03b3b", "#D32F2F": "#d03b3b", "#E65100": "#ec835a",
             "#EF6C00": "#ec835a", "#1976D2": "#2a78d6", "#7B1FA2": "#6250d6", "#1B5E93": "#2a78d6"}

W = {}
for pg in d["pages"]:
    for it in pg["layout"]:
        W[(pg["displayName"], it["widget"]["name"])] = it
OLD_PAGES = {pg["displayName"]: pg for pg in d["pages"]}


def pie_to_bar(w: dict) -> None:
    """Turns a pie widget into a vertical bar of the same query.

    :param w: Widget dict (modified in place).
    """
    sp = w.get("spec", {})
    if sp.get("widgetType") != "pie":
        return
    ang, col = sp["encodings"]["angle"], sp["encodings"]["color"]
    sp["widgetType"] = "bar"
    sp["encodings"] = {
        "x": {"fieldName": col["fieldName"], "displayName": col.get("displayName", ""), "scale": {"type": "categorical"}},
        "y": {"fieldName": ang["fieldName"], "displayName": ang.get("displayName", "Count"),
              "format": {"type": "number-plain", "abbreviation": "compact"},
              "scale": {"type": "quantitative", "domain": {"min": 0}}},
        "color": {"fieldName": col["fieldName"], "displayName": col.get("displayName", ""), "legend": {"hide": True},
                  "scale": col.get("scale", {"type": "categorical"})},
        "label": {"show": True},
    }


def take(page: str, name: str, x: int, y: int, w: int, h: int, title: str | None = None) -> dict:
    """Copy of an existing widget, placed on the 12-column grid.

    :param page: Source page display name.
    :param name: Widget name.
    :param x: Grid column (0-11).
    :param y: Grid row.
    :param w: Width in columns.
    :param h: Height in rows.
    :param title: New title, or None to keep it.
    :return: Layout item.
    """
    it = copy.deepcopy(W[(page, name)])
    it["position"] = {"x": x, "y": y, "width": w, "height": h}
    if title:
        set_title(it["widget"], title)
    pie_to_bar(it["widget"])
    return it


def header(name: str, title: str, text: str, y: int = 0) -> dict:
    """Page header text box.

    :param name: Widget name.
    :param title: Page title.
    :param text: One-line description.
    :param y: Grid row.
    :return: Layout item.
    """
    return {"widget": {"name": name, "multilineTextboxSpec": {"lines": [f"## {title}\n", text]}},
            "position": {"x": 0, "y": y, "width": 12, "height": 2}}


def section(name: str, title: str, text: str, y: int) -> dict:
    """Section title text box.

    :param name: Widget name.
    :param title: Section title.
    :param text: One-line description.
    :param y: Grid row.
    :return: Layout item.
    """
    return {"widget": {"name": name, "multilineTextboxSpec": {"lines": [f"### {title}\n", text]}},
            "position": {"x": 0, "y": y, "width": 12, "height": 2}}


U, C, R, DC, PA, CO = ("Adoption & Growth", "ChatBot - Overview", "ChatBot - Reliability", "DocCompare - Overview",
                       "Health Check - Parsing", "Qualibot Costing")
CS, DP, DS_, IC = "ChatBot - Satisfaction", "DocCompare - Performance", "DocCompare - Satisfaction", "Intraqual Changes"

usage = [
    header("u_header", "Usage", "Adoption and activity of ChatBot and DocCompare. Test and admin accounts are excluded."),
    take(U, "kpi_inscrits", 0, 2, 4, 3, "Registered this month"),
    take(U, "kpi_questions_cb", 4, 2, 4, 3, "ChatBot questions, last week"),
    take(U, "kpi_analyses_dc", 8, 2, 4, 3, "DocCompare analyses, last week"),
    take(U, "counter_ad_cb", 0, 5, 3, 3, "ChatBot eligible users"),
    take(U, "counter_taux_cb", 3, 5, 3, 3, "ChatBot activation rate"),
    take(U, "counter_ad_dc", 6, 5, 3, 3, "DocCompare eligible users"),
    take(U, "counter_taux_dc", 9, 5, 3, 3, "DocCompare activation rate"),
    take(U, "growth_chart", 0, 8, 12, 7, "Cumulative users and rights granted"),
    take(U, "bar_activity_cb", 0, 15, 6, 7, "ChatBot, weekly active users"),
    take(U, "bar_activity_dc", 6, 15, 6, 7, "DocCompare, weekly active users"),
    take(U, "bar_activation", 0, 22, 6, 7, "Activation by tool"),
    take(U, "table_non_utilisateurs", 6, 22, 6, 7, "Eligible users who never used the tool"),
]

chatbot = [
    header("c_header", "ChatBot", "Questions, satisfaction and conversations of the document chatbot. Reliability and errors are on the next page."),
    take(C, "total_questions", 0, 2, 2, 3, "Questions"),
    take(C, "total_sessions", 2, 2, 2, 3, "Sessions"),
    take(C, "total_users_chat", 4, 2, 2, 3, "Active users"),
    take(C, "counter_questions_session", 6, 2, 2, 3, "Questions per session"),
    take(CS, "counter_satisfaction", 8, 2, 2, 3, "Satisfaction rate"),
    take(CS, "counter_feedback_rate", 10, 2, 2, 3, "Feedback rate"),
    take(C, "bar_activity", 0, 5, 8, 7, "Questions per week"),
    take(C, "pie_division", 8, 5, 4, 7, "Questions by division"),
    take(C, "pie_langue", 0, 12, 4, 6, "Questions by language"),
    take(C, "pie_workplace", 4, 12, 4, 6, "Questions by site"),
    take(CS, "pie_votes", 8, 12, 4, 6, "Votes"),
    take(CS, "bar_fb_week", 0, 18, 6, 7, "Feedback per week"),
    take(C, "table_top_users_site", 6, 18, 6, 7, "Top user by site"),
    take(C, "conversations_table", 0, 25, 12, 10, "Conversation history"),
    take(CS, "table_fb_detail", 0, 35, 12, 9, "Feedback details"),
]

reliability = [
    header("r_header", "ChatBot reliability", "One row per chat turn. Degraded: answered, but a step did not run as configured. Error: no answer. Copy of Lakebase, as fresh as the last export."),
    take(R, "rel_turns", 0, 2, 2, 3, "Turns"),
    take(R, "rel_err_rate", 2, 2, 2, 3, "Error rate"),
    take(R, "rel_deg_rate", 4, 2, 2, 3, "Degraded rate"),
    take(R, "rel_ttft", 6, 2, 2, 3, "Avg first token (s)"),
    take(C, "counter_sourcing", 8, 2, 2, 3, "Answers with sources"),
    take(C, "counter_longueur", 10, 2, 2, 3, "Avg answer length (chars)"),
    take(R, "rel_status_day", 0, 5, 6, 7),
    take(R, "rel_latency", 6, 5, 6, 7),
    take(C, "pie_sourcing", 0, 12, 4, 6, "Answers with and without sources"),
    take(C, "bar_erreurs_semaine", 4, 12, 8, 6, "Failed messages per week"),
    take(R, "rel_errors_table", 0, 18, 12, 8),
    take(R, "rel_turns_table", 0, 26, 12, 8),
]

doccompare = [
    header("dc_header", "DocCompare", "Document comparison analyses: volume, satisfaction and performance."),
    take(DC, "total_analyses", 0, 2, 2, 3, "Analyses"),
    take(DC, "users_inscrits", 2, 2, 2, 3, "Registered users"),
    take(DS_, "taux_sat", 4, 2, 2, 3, "Satisfaction rate"),
    take(DP, "avg_gen", 6, 2, 2, 3, "Avg generation (s)"),
    take(DP, "avg_ttft", 8, 2, 2, 3, "Avg first token (s)"),
    take(DP, "avg_tokens", 10, 2, 2, 3, "Avg tokens per analysis"),
    take(DC, "temporalite_filter", 0, 5, 8, 1),
    take(DC, "analyses_bar", 0, 6, 8, 7, "Analyses per period"),
    take(DC, "bar_method", 8, 6, 4, 7, "Analyses by version"),
    take(DC, "pie_filetype", 0, 13, 4, 6, "File types"),
    take(DS_, "pie_votes", 4, 13, 4, 6, "Votes"),
    take(DP, "tokens_method", 8, 13, 4, 6, "Tokens by version"),
    take(DP, "gen_day", 0, 19, 6, 7, "Avg generation time per week (s)"),
    take(DP, "scatter_gen_tokens", 6, 19, 6, 7, "Generation time vs tokens"),
    take(U, "table_classement", 0, 26, 12, 7, "Top users"),
    take(DC, "table_messages", 0, 33, 12, 9, "Analyses"),
    take(DS_, "fb_detail", 0, 42, 12, 8, "Feedback details"),
]

parsing = [
    header("p_header", "Parsing", "Health of the parsing pipeline and document changes detected in Intraqual."),
    take(PA, "counter_docs_in_scope", 0, 2, 2, 3, "Documents in scope"),
    take(PA, "counter_true_errors", 2, 2, 2, 3, "Documents in error"),
    take(PA, "counter_ocr_backlog", 4, 2, 2, 3, "OCR backlog"),
    take(PA, "counter_chunks_total", 6, 2, 2, 3, "Chunks"),
    take(PA, "counter_images_total", 8, 2, 2, 3, "Images described"),
    take(PA, "counter_avg_parse_time", 10, 2, 2, 3, "Avg parse time (s/doc)"),
    take(IC, "ic_counter_total", 0, 5, 3, 3, "Changes, 30 days"),
    take(IC, "ic_counter_new", 3, 5, 3, 3, "New documents, 30 days"),
    take(IC, "ic_counter_revised", 6, 5, 3, 3, "Revised documents, 30 days"),
    take(IC, "ic_counter_not_indexed", 9, 5, 3, 3, "Not indexed, 30 days"),
    take(PA, "bar_trend", 0, 8, 6, 7, "Documents per run outcome, 30 days"),
    take(IC, "ic_bar_trend", 6, 8, 6, 7, "Changes per day, 30 days"),
    take(PA, "bar_chunks", 0, 15, 6, 6, "Chunks by division"),
    take(PA, "bar_image_backlog", 6, 15, 6, 6, "Image description backlog"),
    take(PA, "table_runs", 0, 21, 12, 6, "Run history"),
    take(IC, "ic_table_log", 0, 27, 12, 8, "Document changes and indexing status"),
    take(PA, "table_errors", 0, 35, 12, 6, "Documents in error"),
    take(PA, "table_ocr_backlog", 0, 41, 12, 6, "OCR backlog detail"),
]

def diag_table() -> dict:
    """Table of the document diagnostic.

    :return: Layout item.
    """
    cols = [("ref", "REF", 110), ("IDDOC", "IDDOC", 80), ("titre", "Title", 260), ("indice", "Rev.", 50),
            ("published_date", "Published", 100), ("root_category", "Root category", 200), ("reason", "Reason", 230),
            ("explanation", "Explanation", 520), ("parse_status", "Parse status", 190), ("file_size_kb", "Size (KB)", 90),
            ("source_file_extension", "Type", 70), ("n_chunks", "Passages", 80), ("current_iddoc", "Current IDDOC", 110)]
    return {"widget": {"name": "ds_diag_table",
                       "queries": [{"name": "main_query", "query": {"datasetName": "doc_diagnostic", "fields": [
                           {"name": c, "expression": f"`{c}`"} for c, _, _ in cols], "disaggregated": True}}],
                       "spec": {"version": 2, "frame": {"title": "Why a document is, or is not, in the chatbot", "showTitle": True},
                                "widgetType": "table", "rowsPerPage": 25,
                                "encodings": {"columns": [{"fieldName": c, "displayName": n, "columnWidth": w_}
                                                          for c, n, w_ in cols]},
                                "data": {"queryName": "main_query"}}},
            "position": {"x": 0, "y": 3, "width": 12, "height": 8}}


def diag_bar() -> dict:
    """Bar chart of documents by reason.

    :return: Layout item.
    """
    return {"widget": {"name": "ds_diag_bar",
                       "queries": [{"name": "main_query", "query": {"datasetName": "doc_diagnostic", "fields": [
                           {"name": "reason", "expression": "`reason`"},
                           {"name": "count(*)", "expression": "COUNT(`*`)"}], "disaggregated": False}}],
                       "spec": {"version": 3, "frame": {"title": "Documents by reason", "showTitle": True},
                                "widgetType": "bar",
                                "encodings": {"x": {"fieldName": "reason", "displayName": "Reason", "scale": {"type": "categorical"}},
                                              "y": {"fieldName": "count(*)", "displayName": "Documents",
                                                    "format": {"type": "number-plain", "abbreviation": "compact"},
                                                    "scale": {"type": "quantitative", "domain": {"min": 0}}},
                                              "label": {"show": True}},
                                "data": {"queryName": "main_query"}}},
            "position": {"x": 0, "y": 11, "width": 12, "height": 7}}


explorer = [
    header("ds_header", "Document explorer",
           "Pick one or more REF. First table: the exact reason each document is, or is not, in the chatbot. Below: the parsing record, the parsed text and the indexed passages."),
    take("Document Search", "ds_filter_ref", 0, 2, 12, 1),
    diag_table(),
    diag_bar(),
    {"widget": {"name": "ds_legend", "multilineTextboxSpec": {"lines": [
        "### Reading the reasons (first matching case wins)\n",
        "1. **IN CHATBOT**: passages exist in the chunks table.\n",
        "2. **NOT IN INTRAQUAL**: deleted at the source.\n",
        "3. **NOT CURRENT REVISION**: an older revision, the current one is named.\n",
        "4. **NOT PUBLISHED** / 5. **HIDDEN**: status gate (published = lifecycle state 7, visible).\n",
        "6. **CATEGORY OUT OF SCOPE**: root category is not one of the indexed roots.\n",
        "8. **MANUAL EXCLUSION**, 9. **REPLACED BY NEWER REVISION**: only the newest revision of a REF is indexed.\n",
        "10. **PARSED, RESULT NOT BUILT**, 11. **NOT PROCESSED YET**: in scope, the next run handles it.\n",
        "12. **PARSE ERROR**, 13. **NO TEXT, NEEDS OCR**, 14. **IMAGES TO DESCRIBE**, 15. **FILE TOO LARGE**, 16. **EMPTY DOCUMENT** (file of at most 50 KiB without text), 17. **NO FILE ON VOLUME**, 18. **PARSED BUT NO PASSAGE**: pipeline outcomes."]}},
     "position": {"x": 0, "y": 18, "width": 12, "height": 7}},
    take("Document Search", "ds_table_meta", 0, 25, 12, 5, "Parsing record"),
    take("Document Search", "ds_table_text", 0, 30, 12, 6, "Parsed text, before chunking (checkpoint, includes revisions no longer indexed)"),
    take("Document Search", "ds_table_chunks", 0, 36, 12, 8, "Indexed chunks, as the chatbot sees them"),
]

costs = [
    take(CO, "header", 0, 0, 12, 2),
    take(CO, "filter_category", 0, 2, 4, 2),
    take(CO, "filter_env", 4, 2, 4, 2),
    take(CO, "counter_total", 0, 4, 3, 5, "Total cost, 30 days (USD)"),
    take(CO, "pie_category", 3, 4, 5, 5, "Cost by category"),
    take(CO, "pie_env", 8, 4, 4, 5, "Cost by environment"),
    take(CO, "bar_daily_category", 0, 9, 12, 8, "Daily cost by category"),
    take(CO, "line_trend", 0, 17, 6, 7, "Daily cost trend"),
    take(CO, "bar_endpoint", 6, 17, 6, 7, "Cost by endpoint"),
    take(CO, "table_jobs", 0, 24, 12, 8, "Pipeline jobs"),
    take(CO, "table_detail", 0, 32, 12, 8, "Detail by day and category"),
    take(CO, "filter_day", 0, 40, 4, 2),
    take(CO, "bar_hourly", 0, 42, 12, 8, "Hourly cost for the selected day"),
    take(CO, "header_compare_llm", 0, 50, 12, 2),
    take(CO, "counter_compare_total", 0, 52, 3, 3, "Estimated cost, 30 days (EUR)"),
    take(CO, "counter_compare_requests", 3, 52, 3, 3, "Requests, 30 days"),
    take(CO, "bar_compare_daily", 6, 52, 6, 6, "Estimated cost per day and endpoint"),
    take(CO, "table_compare_llm", 0, 58, 12, 6, "Estimate detail"),
    section("co_legacy", "Legacy: Knowledge Assistant", "Billed Agent Bricks endpoints of the former chatbot. Remove once the ka-* endpoints are deleted.", 64),
    take(CO, "counter_ka_requests", 0, 66, 3, 3, "Requests, 30 days"),
    take(CO, "counter_ka_cpr", 3, 66, 3, 3, "Avg cost per request (USD)"),
    take(CO, "counter_ka_tokens", 6, 66, 3, 3, "Cents per request"),
    take(CO, "counter_ka_mtoken", 9, 66, 3, 3, "Cost, 30 days (USD)"),
    take(CO, "bar_ka_requests", 0, 69, 6, 7, "Daily cost by environment"),
    take(CO, "line_ka_cpr", 6, 69, 6, 7, "Cost per request, DEV (cents)"),
    take(CO, "line_tokens_daily", 0, 76, 6, 6, "Daily cost by endpoint"),
    take(CO, "line_requests_daily", 6, 76, 6, 6, "Daily cost by environment"),
    take(CO, "table_ka_usage", 0, 82, 12, 8, "Usage detail"),
    take(CO, "table_endpoint_usage", 0, 90, 12, 6, "Billing detail by endpoint"),
    take(CO, "header_methodology", 0, 96, 12, 5),
]

PCT_FMT = {"type": "number-percent", "decimalPlaces": {"type": "exact", "places": 1}}
for items in (usage, chatbot, reliability, doccompare, parsing, explorer, costs):
    for it in items:
        w = it["widget"]
        if w.get("name") in ("counter_erreur", "counter_sourcing", "counter_satisfaction", "counter_feedback_rate",
                             "counter_taux_dc", "counter_taux_cb", "taux_sat") and "spec" in w:
            w["spec"]["encodings"]["value"]["format"] = PCT_FMT
        if w.get("name") == "avg_ttft":
            w["spec"]["encodings"]["value"]["displayName"] = "Seconds"

gf_page = next(pg for pg in d["pages"] if pg["displayName"] == "Global Filters")
LAYOUT = "GRID_V1"
d["pages"] = [
    {"name": n, "displayName": t, "layout": lay, "pageType": "PAGE_TYPE_CANVAS", "layoutVersion": LAYOUT}
    for n, t, lay in (("usage", "Usage", usage), ("chatbot", "ChatBot", chatbot),
                      ("chatbot_reliability", "ChatBot reliability", reliability), ("doccompare", "DocCompare", doccompare),
                      ("parsing", "Parsing", parsing), ("document_explorer", "Document explorer", explorer),
                      ("costs", "Costs", costs))
] + [gf_page]


def recolor(o):
    """Replaces the former chart colors by the validated palette, recursively.

    :param o: JSON value.
    :return: The value with new colors.
    """
    if isinstance(o, str):
        return COLOR_MAP.get(o, COLOR_MAP.get(o.upper(), o))
    if isinstance(o, list):
        return [recolor(x) for x in o]
    if isinstance(o, dict):
        return {k: (v if k == "visualizationColors" else recolor(v)) for k, v in o.items()}
    return o


# ---------------------------------------------------------------- 13. data origin, language, no-feedback bars
# PROD only: before this instant the Lakebase of PROD held the UAT content copied on 2026-10-30 (export of 16:09, load 16:42).
PROD_LIVE_FROM = "2026-10-30 17:00:00"
DS_ALL = {x["name"]: x for x in d["datasets"]}


def add_calc(ds_name: str, display: str, expr: str, desc: str) -> None:
    """Adds a calculated column to a dataset (replaces the one of the same name).

    :param ds_name: Dataset name.
    :param display: Column name.
    :param expr: SQL expression.
    :param desc: Description.
    """
    cols = [c for c in DS_ALL[ds_name].get("columns", []) if c["displayName"] != display]
    cols.append({"displayName": display, "description": desc, "expression": expr})
    DS_ALL[ds_name]["columns"] = cols


def origin_expr(ts: str) -> str:
    """SQL expression of the data origin of a row.

    :param ts: SQL timestamp expression of the row.
    :return: SQL expression.
    """
    if TARGET != "prod":
        return "'Live'"
    return f"CASE WHEN TRY_CAST({ts} AS TIMESTAMP) >= TIMESTAMP'{PROD_LIVE_FROM}' THEN 'PROD live' ELSE 'Imported from UAT' END"


def lang_expr(lang: str, text: str | None) -> str:
    """SQL expression of the language of a question: the stored code, else a keyword guess on the text (rows saved while the
    translation bridge was off have no code).

    :param lang: Column with the stored ISO code.
    :param text: SQL expression of the question text, or None.
    :return: SQL expression (es, fr, en, other codes as stored, unknown).
    """
    stored = f"CASE WHEN {lang} IS NULL OR {lang} IN ('', 'und', 'is', 'as', 'unknown') THEN NULL ELSE {lang} END"
    if text is None:
        return f"COALESCE({stored}, 'unknown')"
    t = f"LOWER({text})"
    return (f"COALESCE({stored}, CASE "
            f"WHEN {t} RLIKE '[ñ¿¡]' OR {t} RLIKE '(^|[^a-zà-ÿ])(el|los|las|una|qué|cómo|cuál|cuáles|puedo|necesito|hay|del|para|dónde|donde|cuando|cuándo)([^a-zà-ÿ]|$)' THEN 'es' "
            f"WHEN {t} RLIKE '(^|[^a-zà-ÿ])(le|les|des|est|quel|quelle|quels|comment|pour|dans|une|que|qui|sont|avec|où)([^a-zà-ÿ]|$)' THEN 'fr' "
            f"WHEN {t} RLIKE '(^|[^a-z])(the|what|how|is|are|of|and|for|which|where|when|can|does|do)([^a-z]|$)' THEN 'en' "
            f"ELSE 'unknown' END)")


for name, ts in (("chat_data", "created_at"), ("chat_conversations_paired", "debut_session"), ("chat_turns_data", "created_at"),
                 ("chat_sessions_agg", "debut_session"), ("messages_users", "created_at"), ("feedbacks_data", "created_at")):
    add_calc(name, "Origin", origin_expr(ts), "PROD live = after the UAT copy of 2026-10-30; Imported from UAT = rows copied from UAT (PROD only)")
add_calc("chat_data", "Language", lang_expr("question_lang", "COALESCE(user_question, content)"),
         "Stored language code, else keyword guess on the question")
add_calc("chat_conversations_paired", "Language", lang_expr("question_lang", "question"),
         "Stored language code, else keyword guess on the question")
add_calc("chat_turns_data", "Language", lang_expr("question_lang", None), "Stored language code")
add_calc("chat_data", "Feedback", "COALESCE(feedback_vote, 'none')", "up, down, or none when the message has no feedback")
# the language chart reads the new column; it no longer drops the questions saved without a code
for pg in d["pages"]:
    for it in pg["layout"]:
        w = it["widget"]
        if w["name"] in ("pie_langue", "pie_workplace"):
            w["queries"][0]["query"]["filters"] = []
            if w["name"] == "pie_langue":
                for f in w["queries"][0]["query"]["fields"]:
                    if "Language Group" in f["expression"]:
                        f["expression"] = "`ChatBot_Conversations_Paired`.`Language`"
                        f["name"] = "ChatBot_Conversations_Paired__Language"
                for k in ("x", "color"):
                    w["spec"]["encodings"][k]["fieldName"] = "ChatBot_Conversations_Paired__Language"
        if w["name"] == "bar_fb_week":
            q = w["queries"][0]["query"]
            q["filters"] = [{"expression": "`role` = 'assistant'"}]
            for f in q["fields"]:
                if f["name"] == "feedback_vote":
                    f["name"], f["expression"] = "Feedback", "`Feedback`"
            w["spec"]["encodings"]["color"].update({"fieldName": "Feedback", "displayName": "Feedback"})
            w["spec"]["encodings"]["color"]["scale"]["mappings"].append({"value": "none", "color": "#9aa0a6"})
            w["spec"]["encodings"]["y"]["displayName"] = "Messages"
            set_title(w, "Feedback per week (green up, red down, grey none)")


def global_filter(name: str, title: str, field: str, dsets: list, x: int, y: int) -> dict:
    """Builds a multi-select global filter on a calculated column present in several datasets.

    :param name: Widget name.
    :param title: Title.
    :param field: Column name.
    :param dsets: Datasets the filter applies to.
    :param x: Grid column.
    :param y: Grid row.
    :return: Layout item.
    """
    qs, names = [], []
    for ds_name in dsets:
        qn = f"dashboards/{DASH_ID}/datasets/{ds_name}_{field}"
        names.append(qn)
        qs.append({"name": qn, "query": {"datasetName": ds_name, "fields": [
            {"name": field, "expression": f"`{field}`"},
            {"name": f"{field}_associativity", "expression": "COUNT_IF(`associative_filter_predicate_group`)"}],
            "disaggregated": False}})
    return {"widget": {"name": name, "queries": qs, "spec": {
        "version": 2, "frame": {"showTitle": True, "title": title}, "widgetType": "filter-multi-select",
        "encodings": {"fields": [{"fieldName": field, "queryName": qn} for qn in names]}}},
        "position": {"x": x, "y": y, "width": 4, "height": 1}}


gf_page["layout"].append(global_filter(
    "origin_filter", "Data origin (PROD live / imported from UAT)" if TARGET == "prod" else "Data origin", "Origin",
    ["chat_data", "chat_conversations_paired", "chat_turns_data", "chat_sessions_agg", "messages_users", "feedbacks_data"], 1, 1))
gf_page["layout"].append(global_filter(
    "language_filter", "Language", "Language", ["chat_data", "chat_conversations_paired", "chat_turns_data"], 5, 1))
LOG.append("global filters Origin (PROD live vs imported from UAT) and Language; language chart/workplace no longer drop NULL question_lang; "
           "feedback histogram has grey 'none' bars")

d["pages"] = recolor(d["pages"])
LOG.append("restructured into 7 pages on one 12-column grid, pies as bars, validated palette")

# unused datasets (no widget query, no relationship expression)
SRC_NAME = {x["name"]: x["datasetName"] for x in d["relationshipGraphs"][0]["sources"]}
used = set()
for pg in d["pages"]:
    for it in pg["layout"]:
        for q in it["widget"].get("queries", []):
            qq = q["query"]
            if qq.get("datasetName"):
                used.add(qq["datasetName"])
            for f in qq.get("fields", []) + qq.get("filters", []):
                for src_name in re.findall(r"`([A-Za-z0-9_]+)`\.`", f["expression"]):
                    if src_name in SRC_NAME:
                        used.add(SRC_NAME[src_name])
for rel in d["relationshipGraphs"][0].get("relationships", []):
    for side in ("from", "to"):
        used.add(SRC_NAME[rel[side]])
dropped = [x["name"] for x in d["datasets"] if x["name"] not in used]
d["datasets"] = [x for x in d["datasets"] if x["name"] in used]
d["relationshipGraphs"][0]["sources"] = [s_ for s_ in d["relationshipGraphs"][0]["sources"] if s_["datasetName"] in used]
LOG.append("datasets no longer used removed: " + ", ".join(dropped))

text = json.dumps(d, indent=2, ensure_ascii=False).replace("volumes/uat_landingzone/qualibot/doc_compare", f"volumes/{ENV}_proj/qualibot/doc_compare")
if TARGET != "uat":
    # Links and catalogs of the UAT workspace become those of the target workspace.
    text = text.replace(UAT["host"], CFG["host"]).replace(f"o={UAT['workspace_id']}", f"o={CFG['workspace_id']}")
    text = text.replace(f"qualibot-{UAT['workspace_id']}.aws", f"qualibot-{CFG['workspace_id']}.aws")
    text = text.replace("uat_proj", f"{TARGET}_proj")
OUT.parent.mkdir(parents=True, exist_ok=True)
OUT.write_text(text + "\n", encoding="utf-8")
print("\n".join("- " + x for x in LOG))
