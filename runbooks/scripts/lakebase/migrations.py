"""Lakebase schema migrations, shared by migrate_lakebase.py (CLI) and migrate_lakebase_job.py (notebook)."""
import logging

logger = logging.getLogger(__name__)

# ── Migrations (idempotent, in order) ─────────────────────────────────────────
MIGRATIONS: list[tuple[str, str]] = [
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
    # ── Cleanup: tables the app does not use ──────────────────────────────────
    # Translate lives in its own app (qualibot-translate); the chat's sources are in chat_messages.sources_json.
    ("drop translation_llm_calls", "DROP TABLE IF EXISTS translation_llm_calls CASCADE"),
    ("drop translation_questions", "DROP TABLE IF EXISTS translation_questions CASCADE"),
    ("drop translation_segments", "DROP TABLE IF EXISTS translation_segments CASCADE"),
    ("drop translation_jobs", "DROP TABLE IF EXISTS translation_jobs CASCADE"),
    ("drop glossary_candidates", "DROP TABLE IF EXISTS glossary_candidates CASCADE"),
    ("drop glossary_terms", "DROP TABLE IF EXISTS glossary_terms CASCADE"),
    ("drop dnt_rules", "DROP TABLE IF EXISTS dnt_rules CASCADE"),
    ("drop chat_sources", "DROP TABLE IF EXISTS chat_sources CASCADE"),
    # users.can_translate is not dropped here: only the app's own service principal owns table users; the app's
    # startup migrations (server/services/infra/lakebase/schema.py) drop it.

    # Add future migrations here:
    # ("description", "ALTER TABLE ..."),
]


def apply_migrations(conn, migrations=MIGRATIONS):
    """Run each migration in its own transaction, so one failure (e.g. a table this identity does not own) cannot
    roll back the ones that succeeded. Returns (applied names, [(name, error)])."""
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
