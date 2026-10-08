"""Shared Lakebase schema migrations — imported by migrate_lakebase.py (CLI) and
migrate_lakebase_job.py (notebook) so the list only has to be edited once.
"""

# ── Migrations (idempotent, in order) ─────────────────────────────────────────
MIGRATIONS: list[tuple[str, str]] = [
    (
        "users.groups",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS groups TEXT[] NOT NULL DEFAULT '{}'",
    ),
    # Feedback triage (Databricks dashboard): mark a feedback as handled + why.
    # Nullable/additive columns, no impact on the app.
    (
        "feedbacks.resolved",
        "ALTER TABLE feedbacks ADD COLUMN IF NOT EXISTS resolved BOOLEAN NOT NULL DEFAULT FALSE",
    ),
    (
        "feedbacks.resolution_reason",
        "ALTER TABLE feedbacks ADD COLUMN IF NOT EXISTS resolution_reason TEXT",
    ),
    (
        "chat_feedbacks.resolved",
        "ALTER TABLE chat_feedbacks ADD COLUMN IF NOT EXISTS resolved BOOLEAN NOT NULL DEFAULT FALSE",
    ),
    (
        "chat_feedbacks.resolution_reason",
        "ALTER TABLE chat_feedbacks ADD COLUMN IF NOT EXISTS resolution_reason TEXT",
    ),
    # Translate glossary: term definition and its provenance ('REF#chunk' extracted from the corpus, 'generated' by an
    # LLM, 'cross_lang_pair' corroborated by a translation pair).
    (
        "glossary_terms.definition",
        "ALTER TABLE glossary_terms ADD COLUMN IF NOT EXISTS definition TEXT",
    ),
    (
        "glossary_terms.definition_source",
        "ALTER TABLE glossary_terms ADD COLUMN IF NOT EXISTS definition_source TEXT",
    ),
    # ── Cleanup: dead tables left behind by feature removals ──────────────────
    # Translate lives in its own app (qualibot-translate) and chat_sources is superseded by
    # chat_messages.sources_json.
    ("drop translation_llm_calls", "DROP TABLE IF EXISTS translation_llm_calls CASCADE"),
    ("drop translation_questions", "DROP TABLE IF EXISTS translation_questions CASCADE"),
    ("drop translation_segments", "DROP TABLE IF EXISTS translation_segments CASCADE"),
    ("drop translation_jobs", "DROP TABLE IF EXISTS translation_jobs CASCADE"),
    ("drop glossary_candidates", "DROP TABLE IF EXISTS glossary_candidates CASCADE"),
    ("drop glossary_terms", "DROP TABLE IF EXISTS glossary_terms CASCADE"),
    ("drop dnt_rules", "DROP TABLE IF EXISTS dnt_rules CASCADE"),
    # chat_sources: superseded by chat_messages.sources_json (see lakebase.py),
    # never dropped from the live database when the code stopped using it.
    ("drop chat_sources", "DROP TABLE IF EXISTS chat_sources CASCADE"),
    # users.can_translate is not dropped here: only the app's own service principal owns table users; the app's
    # startup migrations (server/services/lakebase.py) drop it.

    # Add future migrations here:
    # ("description", "ALTER TABLE ..."),
]


import logging

logger = logging.getLogger(__name__)


def apply_migrations(conn, migrations=MIGRATIONS):
    """Run each migration in its own transaction so one failure (e.g. an
    ownership error on a table this identity doesn't own) can't roll back
    migrations that already succeeded — a single shared transaction silently
    discarded every prior DROP/ALTER once one later statement raised.
    Returns (applied, failed) name lists; failed entries are (name, error).
    """
    applied, failed = [], []
    for name, sql in migrations:
        logger.info(f"  [{name}] {sql[:72]}...")
        try:
            with conn.cursor() as cur:
                cur.execute(sql)
            conn.commit()
            applied.append(name)
        except Exception as e:
            conn.rollback()
            failed.append((name, str(e)))
    return applied, failed
