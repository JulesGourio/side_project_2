"""Idempotent creation and migration of the Lakebase tables, run at every app start."""

import logging
import os

import asyncpg

from .columns import CHAT_CHUNK_COLUMNS, CHAT_TURN_COLUMNS, CHAT_TURN_TIMINGS

logger = logging.getLogger(__name__)


async def _add_columns(conn, table: str, columns) -> None:
    for col, typedef in columns:
        await conn.execute(f'ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {col} {typedef}')


# Session folders of the former documents volume: <env>_landingzone/qualibot/doc_compare.
LEGACY_VOLUME_PATTERN = r'^/Volumes/[^/]+_landingzone/qualibot/doc_compare'

# Renamed tables, copied in dependency order (a feedback points to its message). The legacy tables are kept.
LEGACY_TABLE_COPIES = (
    ('messages', 'compare_messages'),
    ('feedbacks', 'compare_feedbacks'),
    ('errors', 'app_errors'),
)


# Column of a renamed table that points to a row of another one: (column, legacy parent, new parent).
LEGACY_PARENT = {'compare_feedbacks': ('message_id', 'messages', 'compare_messages')}


async def _carry_over_legacy_rows(conn) -> None:
    """Copy the rows of the renamed tables into the new ones, ids kept so that a feedback still points to its message.
    Only the legacy rows whose id is missing from the new table are copied, so a rerun adds nothing and the new table
    may already hold rows of its own. Nothing is copied when a legacy id is already used there: the row would take the
    place of another one's history. Once every message is copied, a feedback whose message was deleted before the
    rename is copied without it. A copy that cannot be made is logged and the schema goes on; the legacy tables are
    kept either way."""
    for legacy, current in LEGACY_TABLE_COPIES:
        if not await conn.fetchval('SELECT to_regclass($1) IS NOT NULL', legacy):
            continue
        try:
            if not await conn.fetchval(
                    f'SELECT EXISTS (SELECT 1 FROM {legacy} l '
                    f'WHERE NOT EXISTS (SELECT 1 FROM {current} c WHERE c.id = l.id))'):
                continue
            if await conn.fetchval(f'SELECT EXISTS (SELECT 1 FROM {legacy} l JOIN {current} c ON c.id = l.id)'):
                logger.warning('Legacy rows %s -> %s not copied: some ids are already used in %s '
                               '(the copy would replace the history of other rows)', legacy, current, current)
                continue
            legacy_cols = {r['column_name'] for r in await conn.fetch(
                'SELECT column_name FROM information_schema.columns WHERE table_schema = current_schema() '
                'AND table_name = $1', legacy)}
            current_cols = [r['column_name'] for r in await conn.fetch(
                'SELECT column_name FROM information_schema.columns WHERE table_schema = current_schema() '
                'AND table_name = $1 ORDER BY ordinal_position', current)]
            cols = [c for c in current_cols if c in legacy_cols]
            values = [f'l.{c}' for c in cols]
            parent = LEGACY_PARENT.get(current)
            if parent:
                column, legacy_parent, current_parent = parent
                parent_complete = not await conn.fetchval(
                    f'SELECT EXISTS (SELECT 1 FROM {legacy_parent} l '
                    f'WHERE NOT EXISTS (SELECT 1 FROM {current_parent} c WHERE c.id = l.id))')
                if parent_complete:
                    values = [f'CASE WHEN EXISTS (SELECT 1 FROM {current_parent} p WHERE p.id = l.{column}) '
                              f'THEN l.{column} END' if c == column else v for c, v in zip(cols, values)]
                elif await conn.fetchval(
                        f'SELECT EXISTS (SELECT 1 FROM {legacy} f WHERE f.{column} IS NOT NULL '
                        f'AND NOT EXISTS (SELECT 1 FROM {current_parent} m WHERE m.id = f.{column}))'):
                    logger.warning('Legacy rows %s -> %s not copied: some point to a %s row that is not there '
                                   '(the legacy history was not copied into it, ids would match other rows)',
                                   legacy, current, current_parent)
                    continue
            copied = await conn.execute(
                f'INSERT INTO {current} ({", ".join(cols)}) SELECT {", ".join(values)} FROM {legacy} l '
                f'WHERE NOT EXISTS (SELECT 1 FROM {current} c WHERE c.id = l.id)')
            if 'id' in current_cols:
                await conn.execute(
                    f"SELECT setval(pg_get_serial_sequence('{current}', 'id'), COALESCE(MAX(id), 1), MAX(id) IS NOT NULL) "
                    f'FROM {current}')
            logger.info('Copied legacy rows %s -> %s (%s)', legacy, current, copied)
        except asyncpg.PostgresError as e:
            logger.warning('Legacy rows %s -> %s not copied: %s', legacy, current, e)


async def _move_legacy_volume_paths(conn) -> None:
    """Rows copied from another environment point at its documents volume: move them under COMPARE_VOLUME_PATH.
    Runs at every start, so rows copied by an earlier start are fixed too; it only touches the old paths."""
    volume_root = os.getenv('COMPARE_VOLUME_PATH', '').rstrip('/')
    if not volume_root:
        return
    await conn.execute(
        "UPDATE compare_messages SET volume_session_path = regexp_replace(volume_session_path, $1, $2) "
        'WHERE volume_session_path ~ $1',
        LEGACY_VOLUME_PATTERN, volume_root)


class _OwnerTolerantConn:
    """Connection whose statements may fail on a table owned by another role (42501, 'must be owner'): the failure is
    logged and the next statement runs, so one such table does not stop the creation of the tables after it."""

    def __init__(self, conn):
        self._conn = conn
        self.skipped: list = []

    def __getattr__(self, name):
        return getattr(self._conn, name)

    async def execute(self, query, *args):
        try:
            return await self._conn.execute(query, *args)
        except asyncpg.InsufficientPrivilegeError as e:
            self.skipped.append((str(e), ' '.join(query.split())[:100]))


async def ensure_schema(pool: asyncpg.Pool) -> None:
    # Additive only: no DROP, DELETE or TRUNCATE at start-up.
    # Lakebase rewrites the table, so it looks empty for a moment before the data is back.
    async with pool.acquire() as raw_conn:
        conn = _OwnerTolerantConn(raw_conn)
        # ── messages (primary history table) ─────────────────────────────────
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS compare_messages (
                id                  SERIAL PRIMARY KEY,
                created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                user_id             TEXT,
                workspace_url       TEXT,
                old_filename        TEXT NOT NULL,
                new_filename        TEXT NOT NULL,
                old_file_hash       TEXT,
                new_file_hash       TEXT,
                analysis_text       TEXT,
                impact_text         TEXT,
                volume_session_path TEXT,
                file_type           TEXT,
                processing_method   TEXT,
                processor_version   TEXT,
                ttft_s              DOUBLE PRECISION,
                generation_s        DOUBLE PRECISION,
                input_tokens        INTEGER,
                output_tokens       INTEGER,
                thinking_tokens     INTEGER,
                total_tokens        INTEGER,
                cost_eur            DOUBLE PRECISION,
                app_version         TEXT
            )
        ''')
        # Columns added to existing tables (idempotent).
        await _add_columns(conn, 'compare_messages', [
            ('file_type',         'TEXT'),
            ('processing_method', 'TEXT'),
            ('processor_version', 'TEXT'),
            ('ttft_s',            'DOUBLE PRECISION'),
            ('generation_s',      'DOUBLE PRECISION'),
            ('input_tokens',      'INTEGER'),
            ('output_tokens',     'INTEGER'),
            ('thinking_tokens',   'INTEGER'),
            ('total_tokens',      'INTEGER'),
            ('cost_eur',          'DOUBLE PRECISION'),
            ('app_version',       'TEXT'),
            ('user_id',           'TEXT'),
            ('workspace_id',      'TEXT'),
            ('llm_request_id',    'INTEGER'),
            # Denormalized from llm_requests.endpoint_name so the model is visible on this row directly.
            ('endpoint_name',     'TEXT'),
        ])


        # ── feedbacks ─────────────────────────────────────────────────────────
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS compare_feedbacks (
                id              SERIAL PRIMARY KEY,
                created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                message_id      INTEGER REFERENCES compare_messages(id) ON DELETE SET NULL,
                user_id         TEXT,
                workspace_id    TEXT,
                workspace_url   TEXT,
                vote            TEXT NOT NULL CHECK (vote IN (\'up\', \'down\')),
                comment         TEXT
            )
        ''')
        await _add_columns(conn, 'compare_feedbacks', [
            ('workspace_id',       'TEXT'),
            # Triage (Databricks dashboard): mark a feedback as handled + why.
            ('resolved',           'BOOLEAN NOT NULL DEFAULT FALSE'),
            ('resolution_reason',  'TEXT'),
        ])

        # ── llm_requests (audit: what was sent to the LLM) ───────────────────
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS llm_requests (
                id              SERIAL PRIMARY KEY,
                created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                old_file_hash   TEXT,
                new_file_hash   TEXT,
                file_type       TEXT,
                endpoint_name   TEXT,
                messages_json   TEXT,
                input_tokens    INTEGER,
                output_tokens   INTEGER,
                thinking_tokens INTEGER,
                total_tokens    INTEGER,
                cost_eur        DOUBLE PRECISION,
                http_status     INTEGER,
                error_type      TEXT,
                error_msg       TEXT
            )
        ''')
        await _add_columns(conn, 'llm_requests', [
            ('endpoint_name',   'TEXT'),
            ('input_tokens',    'INTEGER'),
            ('output_tokens',   'INTEGER'),
            ('thinking_tokens', 'INTEGER'),
            ('total_tokens',    'INTEGER'),
            ('cost_eur',        'DOUBLE PRECISION'),
            ('http_status',     'INTEGER'),
            ('error_type',      'TEXT'),
            ('error_msg',       'TEXT'),
            # Outcome and step durations of the /compare/analyze stream (ms):
            # 'ok' | 'error' | 'aborted' (browser left before the end).
            ('status',          'TEXT'),
            ('queue_wait_ms',   'INTEGER'),     # waiting for an analysis slot
            ('build_ms',        'INTEGER'),     # file extraction + prompt
            ('first_token_ms',  'INTEGER'),     # LLM call start -> first text
            ('generation_ms',   'INTEGER'),     # LLM call start -> end
            ('total_ms',        'INTEGER'),
            ('chunked_parts',   'INTEGER'),     # map-reduce parts (0: one call)
        ])

        # ── errors (application errors persisted for monitoring) ──────────────
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS app_errors (
                id              SERIAL PRIMARY KEY,
                created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                user_id         TEXT,
                workspace_id    TEXT,
                endpoint        TEXT,
                error_type      TEXT,
                error_msg       TEXT,
                old_filename    TEXT,
                new_filename    TEXT,
                file_type       TEXT,
                stack_trace     TEXT,
                llm_request_id  INTEGER
            )
        ''')
        await _add_columns(conn, 'app_errors', [
            ('llm_request_id', 'INTEGER'),
            # 'error' (the request failed) or 'warning' (it went on, degraded: a
            # step that did not run as configured — e.g. the chat's search-query
            # rewrite failed, so the search ran with the question only).
            ('severity',       "TEXT NOT NULL DEFAULT 'error'"),
            # Step that failed: chat translate_in / rewrite / search / ref_lookup /
            # llm / translate_back / persist / ws; compare build / llm.
            ('stage',          'TEXT'),
            ('http_status',    'INTEGER'),
            ('upstream',       'TEXT'),     # Vector Search index or model endpoint called
            # Chat: same trace_id as chat_turns / chat_messages (join key).
            ('trace_id',       'TEXT'),
            ('session_id',     'TEXT'),
            # What was expected vs what happened (configured endpoints, attempts,
            # queries failed / sent…) and the stack trace when there is one.
            ('context',        'JSONB'),
            ('app_version',    'TEXT'),
            # Where in the code: the deepest frame of the stack trace in this repository, else
            # the code that reported it (path:line in function).
            ('origin',         'TEXT'),
            # Same route + step + type + origin (line ignored): groups the same error across requests.
            ('fingerprint',    'TEXT'),
            # What the user was shown (NULL: nothing, the request went on).
            ('user_message',   'TEXT'),
        ])
        await conn.execute('CREATE INDEX IF NOT EXISTS idx_app_errors_created_at ON app_errors (created_at)')
        await conn.execute('CREATE INDEX IF NOT EXISTS idx_app_errors_trace_id ON app_errors (trace_id)')
        await conn.execute('CREATE INDEX IF NOT EXISTS idx_app_errors_fingerprint ON app_errors (fingerprint)')

        # ── impact_requests (audit: every /compare/impact call, success or failure) ──
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS impact_requests (
                id                  SERIAL PRIMARY KEY,
                created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                user_id             TEXT,
                workspace_id        TEXT,
                method              TEXT NOT NULL,
                old_file_hash       TEXT,
                new_file_hash       TEXT,
                changes_chars       INTEGER,
                truncated           BOOLEAN,
                chunks_returned     INTEGER,
                num_documents       INTEGER,
                duration_s          DOUBLE PRECISION,
                endpoint_name       TEXT,
                input_tokens        INTEGER,
                output_tokens       INTEGER,
                total_tokens        INTEGER,
                cost_eur            DOUBLE PRECISION,
                http_status         INTEGER,
                error_type          TEXT,
                error_msg           TEXT
            )
        ''')
        await _add_columns(conn, 'impact_requests', [
            # 'ok' | 'error' | 'aborted' (browser left before the end)
            ('status',          'TEXT'),
            ('app_version',     'TEXT'),
            ('index_name',      'TEXT'),
            ('queries_used',    'INTEGER'),     # one Vector Search query per change
            ('queries_failed',  'INTEGER'),
            ('candidates',      'INTEGER'),     # documents sent to the judge
            ('not_judged',      'INTEGER'),     # found but past impact_max_candidates
            ('judge_failed',    'INTEGER'),     # judge calls that failed after retries
            # Step durations (ms); duration_s stays the total.
            ('extract_ms',      'INTEGER'),     # change list -> queries
            ('search_ms',       'INTEGER'),
            ('judge_ms',        'INTEGER'),     # all judge calls (run in parallel)
        ])

        # ── impact_document_results (one row per document judged by the LLM; business-readable: documents and
        # verdict, no retrieval internals) ──
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS impact_document_results (
                id                      SERIAL PRIMARY KEY,
                created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                request_id              INTEGER NOT NULL REFERENCES impact_requests(id) ON DELETE CASCADE,
                ref                     TEXT,
                division                TEXT,
                url                     TEXT,
                impacted                BOOLEAN,
                confidence              TEXT,
                section                 TEXT,
                reason                  TEXT,
                other_documents_json    TEXT
            )
        ''')
        await _add_columns(conn, 'impact_document_results', [
            # Technical trace of the judge call, at the end of the row on purpose.
            ('status',          'TEXT'),        # impacted / check / not_impacted / error
            ('judge_ms',        'INTEGER'),
            ('judge_attempts',  'INTEGER'),
            ('input_tokens',    'INTEGER'),
            ('output_tokens',   'INTEGER'),
        ])
        await conn.execute('''
            CREATE INDEX IF NOT EXISTS idx_impact_document_results_request_id
                ON impact_document_results (request_id)
        ''')

        # ── impact_feedbacks (user votes on an impact search: on the whole result (ref NULL, optional comment) or on
        # one document's verdict) ──
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS impact_feedbacks (
                id                  SERIAL PRIMARY KEY,
                created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                impact_request_id   INTEGER REFERENCES impact_requests(id) ON DELETE SET NULL,
                message_id          INTEGER REFERENCES compare_messages(id) ON DELETE SET NULL,
                user_id             TEXT,
                workspace_id        TEXT,
                workspace_url       TEXT,
                old_file_hash       TEXT,
                new_file_hash       TEXT,
                ref                 TEXT,
                verdict_shown       TEXT,
                vote                TEXT NOT NULL CHECK (vote IN (\'up\', \'down\')),
                comment             TEXT,
                resolved            BOOLEAN NOT NULL DEFAULT FALSE,
                resolution_reason   TEXT
            )
        ''')
        await conn.execute('''
            CREATE INDEX IF NOT EXISTS idx_impact_feedbacks_request_id
                ON impact_feedbacks (impact_request_id)
        ''')

        # ── impact_cache (result cache for /compare/impact, keyed like messages; "Re-run" bypasses it) ──
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS impact_cache (
                id              SERIAL PRIMARY KEY,
                created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                old_file_hash   TEXT NOT NULL,
                new_file_hash   TEXT NOT NULL,
                app_version     TEXT NOT NULL,
                result_json     TEXT NOT NULL
            )
        ''')
        await conn.execute('''
            CREATE INDEX IF NOT EXISTS idx_impact_cache_hashes
                ON impact_cache (old_file_hash, new_file_hash, app_version)
        ''')

        # ── summary_cache (cache for /compare/summarize, keyed on the single document's own hash + app_version) ──
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS summary_cache (
                id              SERIAL PRIMARY KEY,
                created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                file_hash       TEXT NOT NULL,
                app_version     TEXT NOT NULL,
                result_json     TEXT NOT NULL
            )
        ''')
        await conn.execute(
            "ALTER TABLE summary_cache ADD COLUMN IF NOT EXISTS endpoint_name TEXT"
        )
        await conn.execute('''
            CREATE INDEX IF NOT EXISTS idx_summary_cache_hash
                ON summary_cache (file_hash, app_version)
        ''')

        # ── users ─────────────────────────────────────────────────────────────
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS users (
                id            SERIAL PRIMARY KEY,
                created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                user_id       TEXT UNIQUE NOT NULL,
                workspace_id  TEXT,
                email         TEXT,
                display_name  TEXT,
                workspace_url TEXT,
                role          TEXT NOT NULL DEFAULT \'user\',
                is_active     BOOLEAN NOT NULL DEFAULT TRUE
            )
        ''')
        # Columns added to existing tables (idempotent).
        await _add_columns(conn, 'users', [
            ('workspace_id', 'TEXT'),
        ])

        # (deduplication and unique index applied after data migrations below)

        # ── chat_sessions ─────────────────────────────────────────────────────
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS chat_sessions (
                id            TEXT PRIMARY KEY,
                created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                user_id       TEXT,
                workspace_id  TEXT,
                workspace_url TEXT,
                name          TEXT NOT NULL DEFAULT 'New conversation'
            )
        ''')
        await _add_columns(conn, 'chat_sessions', [
            ('workspace_id', 'TEXT'),
            # Soft-delete: rows are never physically removed, only flagged, so a
            # trace of every conversation is kept in the database for audit.
            ('deleted',      'BOOLEAN NOT NULL DEFAULT FALSE'),
            ('deleted_at',   'TIMESTAMPTZ'),
            # Set once the owner clicks "Share" — grants read-only access to
            # anyone with the token, independent of the row's own `id`.
            ('share_token',  'TEXT'),
        ])
        await conn.execute('''
            CREATE UNIQUE INDEX IF NOT EXISTS chat_sessions_share_token_idx
            ON chat_sessions(share_token)
            WHERE share_token IS NOT NULL
        ''')

        # ── chat_messages ─────────────────────────────────────────────────────
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS chat_messages (
                id              SERIAL PRIMARY KEY,
                created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                session_id      TEXT REFERENCES chat_sessions(id) ON DELETE CASCADE,
                user_id         TEXT,
                workspace_id    TEXT,
                workspace_url   TEXT,
                role            TEXT NOT NULL CHECK (role IN (\'user\', \'assistant\')),
                content         TEXT NOT NULL,
                trace_id        TEXT
            )
        ''')
        await _add_columns(conn, 'chat_messages', [
            # The turn's chat_turns.trace_id (chat-…).
            ('trace_id',        'TEXT'),
            ('workspace_id',    'TEXT'),
            # Engine label: chat-all / chat-as / chat-is.
            # NULL on copies of a shared conversation. The model that actually
            # answered is chat_turns.answer_endpoint.
            ('endpoint_name',   'TEXT'),
            ('sources_json',    'TEXT'),
            # Soft-delete flag — see chat_sessions above.
            ('deleted',         'BOOLEAN NOT NULL DEFAULT FALSE'),
            ('deleted_at',      'TIMESTAMPTZ'),
            # Turn outcome: 'ok' or 'error'. Failed turns are saved too, so the
            # user's question and the failure reason stay traceable.
            ('status',          "TEXT NOT NULL DEFAULT 'ok'"),
            ('error_msg',       'TEXT'),
            # Division scope the question was asked under: 'ALL' / 'AS' / 'IS'.
            ('division',        "TEXT NOT NULL DEFAULT 'ALL'"),
            # ISO 639-1 code of the question's language, as detected by the
            # chat translation bridge (server/services/chat/translation_bridge.py).
            # Empty when the bridge is disabled (CHAT_TRANSLATE_BRIDGE_ENABLED).
            ('question_lang',   'TEXT'),
        ])

        await conn.execute('''
            CREATE INDEX IF NOT EXISTS chat_messages_session_idx
            ON chat_messages(session_id)
        ''')

        # ── chat_turns (one row per chat turn, see services/chat/turn_log.py) ── no FK on session_id: a turn is logged even when its messages are not saved ──
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS chat_turns (
                id                    SERIAL PRIMARY KEY,
                created_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                trace_id              TEXT NOT NULL UNIQUE,
                user_message_id       INTEGER REFERENCES chat_messages(id) ON DELETE SET NULL,
                assistant_message_id  INTEGER REFERENCES chat_messages(id) ON DELETE SET NULL,
                -- ok | degraded (answered, a step did not run as configured) | error | aborted
                status                TEXT NOT NULL,
                error_stage           TEXT,
                error_type            TEXT,
                error_msg             TEXT,
                warnings              TEXT[] NOT NULL DEFAULT '{}'
            )
        ''')
        await _add_columns(conn, 'chat_turns',
                           CHAT_TURN_COLUMNS + [(f'{t}_ms', 'INTEGER') for t in CHAT_TURN_TIMINGS])
        await conn.execute('CREATE INDEX IF NOT EXISTS idx_chat_turns_created_at ON chat_turns (created_at)')
        await conn.execute('CREATE INDEX IF NOT EXISTS idx_chat_turns_session_id ON chat_turns (session_id)')
        await conn.execute('CREATE INDEX IF NOT EXISTS idx_chat_turns_assistant_message_id '
                           'ON chat_turns (assistant_message_id)')

        # ── chat_retrieved_chunks (every passage retrieved for a turn, with its text: what the answer was really built on) ──
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS chat_retrieved_chunks (
                id          BIGSERIAL PRIMARY KEY,
                created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                turn_id     INTEGER NOT NULL REFERENCES chat_turns(id) ON DELETE CASCADE
            )
        ''')
        await _add_columns(conn, 'chat_retrieved_chunks', CHAT_CHUNK_COLUMNS)
        await conn.execute('CREATE INDEX IF NOT EXISTS idx_chat_retrieved_chunks_turn_id '
                           'ON chat_retrieved_chunks (turn_id)')
        await conn.execute('CREATE INDEX IF NOT EXISTS idx_chat_retrieved_chunks_ref ON chat_retrieved_chunks (ref)')

        # ── catalog_metadata ───────────────────────────────────────────
        # Single-row table (id=1) holding the 'documents as of' date shown in the chat UI.
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS catalog_metadata (
                id              INTEGER PRIMARY KEY DEFAULT 1,
                documents_as_of DATE,
                updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                CONSTRAINT single_row CHECK (id = 1)
            )
        ''')
        await conn.execute('''
            INSERT INTO catalog_metadata (id)
            VALUES (1)
            ON CONFLICT (id) DO NOTHING
        ''')

        # ── doc_catalog ───────────────────────────────────────────────────────
        # Every document of the parsing scope (REF, title, link, whether it is in the chat index). Rewritten by parsing task
        # 6_update_kb_metadata after each daily run; read by services/infra/doc_catalog.py.
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS doc_catalog (
                ref        TEXT PRIMARY KEY,
                base_ref   TEXT,               -- NULL: computed by the app (doc_catalog._canon)
                title      TEXT,
                url        TEXT,
                division   TEXT,
                in_chat    BOOLEAN NOT NULL DEFAULT TRUE,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        ''')
        # Current revision (Intraqual indice) and publication date, given to the answer model with each document.
        # A failure here must not stop the rest of the schema; the columns are looked up first, since ALTER needs table ownership.
        have = {r['column_name'] for r in await conn.fetch(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'doc_catalog'")}
        missing = [(c, t) for c, t in [('revision', 'TEXT'), ('doc_date', 'DATE')] if c not in have]
        try:
            await _add_columns(conn, 'doc_catalog', missing)
        except Exception as e:
            logger.warning('doc_catalog: %s not added by the app (%s) — parsing task '
                           '6_update_kb_metadata adds them when it owns the table', '/'.join(c for c, _ in missing), e)

        # ── chat_feedbacks ────────────────────────────────────────────────────
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS chat_feedbacks (
                id            SERIAL PRIMARY KEY,
                created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                message_id    INTEGER REFERENCES chat_messages(id) ON DELETE SET NULL,
                session_id    TEXT REFERENCES chat_sessions(id) ON DELETE SET NULL,
                user_id       TEXT,
                workspace_id  TEXT,
                workspace_url TEXT,
                vote          TEXT NOT NULL CHECK (vote IN (\'up\', \'down\')),
                comment       TEXT
            )
        ''')
        await _add_columns(conn, 'chat_feedbacks', [
            ('workspace_id',       'TEXT'),
            # Triage (Databricks dashboard): mark a feedback as handled + why.
            ('resolved',           'BOOLEAN NOT NULL DEFAULT FALSE'),
            ('resolution_reason',  'TEXT'),
        ])


        # ── Data migrations (idempotent — WHERE only matches rows in the composite format) ──

        await _carry_over_legacy_rows(conn)
        await _move_legacy_volume_paths(conn)

        # 1. Trim trailing whitespace and line breaks from emails.
        await conn.execute("UPDATE users SET email = BTRIM(email) WHERE email ~ '\\s'")
        await conn.execute("UPDATE users SET email = NULL WHERE TRIM(COALESCE(email,'')) = ''")

        # 3. Split composite user_ids (numeric@workspace) into separate columns for every table
        for table in ('users', 'compare_messages', 'compare_feedbacks',
                      'chat_sessions', 'chat_messages', 'chat_feedbacks'):
            try:
                await conn.execute(f'''
                    UPDATE {table}
                    SET workspace_id = SPLIT_PART(user_id, '@', 2),
                        user_id      = SPLIT_PART(user_id, '@', 1)
                    WHERE user_id ~ '^\\d+@\\d+$'
                ''')
            except asyncpg.PostgresError as e:
                logger.warning('Composite user ids of %s not split: %s', table, e)

        # Partial unique index on email so the same real-world user is never duplicated across workspaces.
        # No row is deleted to get there: with duplicates left, the index is skipped and a warning says so.
        try:
            await conn.execute('''
                CREATE UNIQUE INDEX IF NOT EXISTS users_email_idx
                ON users(email)
                WHERE email IS NOT NULL
            ''')
        except asyncpg.PostgresError as e:
            logger.warning('users_email_idx not created: %s', e)

        if conn.skipped:
            logger.warning('Lakebase schema: %d statement(s) skipped, the app does not own their table (first: %s: %s) — '
                           'run runbooks/scripts/lakebase/ensure_schema_as_owner.py as the owner', len(conn.skipped),
                           conn.skipped[0][0], conn.skipped[0][1])

    logger.info('Lakebase schema ready')
