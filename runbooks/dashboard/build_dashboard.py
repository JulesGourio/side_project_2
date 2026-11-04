"""Builds the corrected UAT dashboard from original/Qualibot_Usage_Tracking.lvdash.json.

Usage: python3 build_dashboard.py   (writes Qualibot_Usage_Tracking.lvdash.json next to this file)
"""
import copy
import json
import re
from pathlib import Path

HERE = Path(__file__).parent
SRC = HERE / "original" / "Qualibot_Usage_Tracking.lvdash.json"
OUT = HERE / "Qualibot_Usage_Tracking.lvdash.json"

ENV = "uat"
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
    (r"dev_landingzone\.qualibot\.chat_messages", "dev_proj.qualibot.chat_messages"),
    (r"dev_landingzone\.qualibot\.messages\b", "dev_proj.qualibot.compare_messages"),
]
for name in DS:
    sql = get_sql(name)
    for pat, rep in RENAMES:
        sql = re.sub(pat, rep, sql)
    set_sql(name, sql)
LOG.append("legacy table names -> <env>_proj.qualibot.* (parsing tables, compare_messages, compare_feedbacks)")

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
  SELECT 'DEV' AS env, DATE(TRY_CAST(created_at AS TIMESTAMP)) AS usage_date, COUNT(*) AS n_user_messages
  FROM dev_proj.qualibot.chat_messages
  WHERE role = 'user' AND deleted = false
    AND DATE(TRY_CAST(created_at AS TIMESTAMP)) >= current_date() - INTERVAL 30 DAYS
  GROUP BY 2
  UNION ALL
  SELECT 'UAT', DATE(TRY_CAST(created_at AS TIMESTAMP)), COUNT(*)
  FROM uat_proj.qualibot.chat_messages
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
    CAST(NULL AS BIGINT) AS n_chunks
  FROM {ENV}_proj.qualibot.parsed_documents
  WHERE parse_status = 'ERROR'
),
current_backlog AS (
  SELECT IDDOC, ref, date(ingestion_timestamp) AS ingested_on
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
    CAST(NULL AS STRING) AS source_file_name,
    CAST(NULL AS STRING) AS error_trace,
    COALESCE(h.first_seen, cb.ingested_on) AS since,
    CAST(datediff(current_date(), COALESCE(h.first_seen, cb.ingested_on)) AS INT) AS days,
    'NEEDS_OCR' AS category,
    COALESCE(cc.n_chunks, 0) AS n_chunks
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
  SELECT IDDOC, ref, date(ingestion_timestamp) AS ingested_on
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
LAKEBASE_PROJECTS = "'d9fd81bc-5865-4ef0-8718-520a9e8d6c68', 'e9185a28-13c6-42da-9ccc-0dbcf6b97b60', 'deb195f2-6189-419e-a640-b29fa71e540c'"
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

# ---------------------------------------------------------------- 9. unused datasets
for name in ("debug_recent_users", "hc_index_freshness"):
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
        for ds_name, field in (("chat_data", "Date"), ("chat_quality_data", "Date"),
                               ("chat_conversations_paired", "session_date")):
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
idx = [p["displayName"] for p in d["pages"]].index("ChatBot - Quality")
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

OUT.write_text(json.dumps(d, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
print("\n".join("- " + x for x in LOG))
