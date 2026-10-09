"""Lakebase (autoscaling PostgreSQL) service.

Manages an asyncpg connection pool backed by a Databricks Lakebase project.
A background task refreshes the OAuth token every 55 minutes (token lifetime ~1h).

Required env vars:
  LAKEBASE_PROJECT_ID   — Lakebase project ID (e.g. "latec-compare")
  LAKEBASE_DATABASE     — PostgreSQL database name (default: "doccompare")

Optional:
  LAKEBASE_BRANCH       — branch (default: "production")
  LAKEBASE_ENDPOINT     — endpoint (default: "primary")
"""

import asyncio
import json
import logging
import os
import time
import traceback
from typing import Any, Dict, List, Optional

import asyncpg
from databricks.sdk import WorkspaceClient

from . import error_origin

logger = logging.getLogger(__name__)

_pool: Optional[asyncpg.Pool] = None
_refresh_task: Optional[asyncio.Task] = None

# Used only when the real expiry is unknown (fallback workspace token, see _get_host_and_token); the database-
# credential path reads the actual expire_time.
_DEFAULT_REFRESH_INTERVAL_S = 55 * 60  # 55 min — token lifetime is ~1h
_REFRESH_SAFETY_BUFFER_S = 5 * 60  # refresh this long before actual expiry
_MIN_REFRESH_INTERVAL_S = 60  # never refresh tighter than this

# Bounds the initial TCP connect (asyncpg's default is 60s). A deployed app connects instantly; local dev may have
# port 5432 blocked,
# and a short timeout lets it fail fast into the existing no-history fallback.
_CONNECT_TIMEOUT_S = float(os.getenv('LAKEBASE_CONNECT_TIMEOUT_S', '5'))


def _app_version() -> str:
    return os.getenv('APP_VERSION', '2')


# ── chat_turns: one row per chat turn (filled from services/turn_log.TurnLog) ──
# The columns written from TurnLog.data, in order — used by both CREATE TABLE and the
# INSERT, so a column added here reaches the table on the next app start.
CHAT_TURN_COLUMNS: List[tuple] = [
    # who / where
    ('session_id',            'TEXT'),
    ('user_id',               'TEXT'),
    ('workspace_id',          'TEXT'),
    ('app_version',           'TEXT'),
    ('division',              'TEXT'),
    # what the turn should have run with: chat_vsi.settings() (index, search sizes, models
    # and their fallbacks, token limits) — compare with the columns below
    ('index_name',            'TEXT'),
    ('config',                'JSONB'),
    # the question
    ('question',              'TEXT'),
    ('question_lang',         'TEXT'),
    ('question_en',           'TEXT'),               # English question sent to the search when bridged
    ('history_messages',      'INTEGER'),            # messages of the conversation sent to the engine
    # search-query rewrite
    ('fr_query',              'TEXT'),
    ('en_query',              'TEXT'),
    ('rewrite_ok',            'BOOLEAN'),
    ('rewrite_endpoint',      'TEXT'),               # model that rewrote (NULL: every model failed)
    ('rewrite_input_tokens',  'INTEGER'),
    ('rewrite_output_tokens', 'INTEGER'),
    ('rewrite_cost_eur',      'DOUBLE PRECISION'),
    # retrieval
    ('named_refs',            'TEXT[]'),             # REFs named in the conversation (REF lookup)
    ('titled_refs',           'TEXT[]'),             # documents whose title matched (title lookup)
    ('vs_calls_expected',     'INTEGER'),            # Vector Search queries of the main search
    ('vs_calls_ok',           'INTEGER'),
    ('rerank_ok',             'BOOLEAN'),
    ('passages_retrieved',    'INTEGER'),            # rows of chat_retrieved_chunks
    ('passages_sent',         'INTEGER'),            # ... of which handed to the LLM (kept)
    ('documents_sent',        'INTEGER'),
    ('prompt_chars',          'INTEGER'),
    ('instructions_sha',      'TEXT'),               # sha256[:16] of the system instructions used
    # answer
    ('answer_endpoint',       'TEXT'),               # model that answered (NULL: none)
    ('llm_fallback',          'BOOLEAN'),
    ('llm_attempts',          'JSONB'),              # every call: endpoint, outcome, status, seconds
    ('truncated',             'BOOLEAN'),            # output limit reached
    ('continuations',         'INTEGER'),            # answer resumed after a cut
    ('input_tokens',          'INTEGER'),
    ('output_tokens',         'INTEGER'),
    ('thinking_tokens',       'INTEGER'),
    ('cost_eur',              'DOUBLE PRECISION'),   # answer only (rewrite: rewrite_cost_eur)
    ('answer_chars',          'INTEGER'),
    ('citations_count',       'INTEGER'),
    ('cited_refs',            'TEXT[]'),
    ('sources_count',         'INTEGER'),            # chips shown (cited + named in prose)
    ('answer_translated',     'BOOLEAN'),            # translated back by the bridge
    ('user_message',          'TEXT'),               # failure message shown (NULL: none)
]
# Step durations (TurnLog.timings_ms), stored as <step>_ms INTEGER columns. first_token is
# counted from the start of the turn (what the user waits); the others are the step itself.
CHAT_TURN_TIMINGS = ('total', 'translate_in', 'retrieval', 'rewrite', 'search', 'ref_lookup', 'title_lookup',
                     'queue_wait', 'first_token', 'generation', 'translate_back', 'persist')

# ── chat_retrieved_chunks: one row per passage retrieved for a turn ──
CHAT_CHUNK_COLUMNS: List[tuple] = [
    ('trace_id',          'TEXT'),
    ('position',          'INTEGER'),            # order of retrieval (merged by best rank)
    ('kept',              'BOOLEAN NOT NULL DEFAULT TRUE'),  # handed to the LLM
    ('drop_reason',       'TEXT'),               # other_language | over_cap (kept = false)
    ('prompt_rank',       'INTEGER'),            # order in the prompt (kept only)
    ('doc_number',        'INTEGER'),            # [n] of its document in the prompt
    ('cited',             'BOOLEAN'),            # its document is cited by the answer
    ('source',            'TEXT'),               # search | ref_lookup | title_lookup
    ('hits',              'JSONB'),              # [{q, via, rank, score}]: which query found it, where
    ('best_score',        'DOUBLE PRECISION'),
    ('chunk_id',          'TEXT'),
    ('iddoc',             'TEXT'),
    ('ref',               'TEXT'),
    ('division',          'TEXT'),
    ('url',               'TEXT'),
    ('semantic_headers',  'TEXT'),
    ('chunk_text',        'TEXT'),
]


def _base_type(typedef: str) -> str:
    return typedef.split()[0]


def _db_value(value: Any, typedef: str) -> Any:
    """Python value -> asyncpg parameter for a column of type ``typedef``."""
    if value is None:
        return None
    base = _base_type(typedef)
    if base == 'JSONB':
        return json.dumps(value, ensure_ascii=False, default=str)
    if base == 'TEXT[]':
        return [str(v) for v in value]
    if base == 'TEXT':
        return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    if base == 'INTEGER':
        return int(value)
    if base == 'DOUBLE':
        return float(value)
    if base == 'BOOLEAN':
        return bool(value)
    return value


def _placeholders(typedefs: List[str], offset: int = 0) -> str:
    return ', '.join(f'${i}::jsonb' if _base_type(t) == 'JSONB' else f'${i}'
                     for i, t in enumerate(typedefs, offset + 1))


# --- Internal helpers ---


def _cfg() -> dict:
    return {
        'project_id': os.getenv('LAKEBASE_PROJECT_ID', ''),
        'branch': os.getenv('LAKEBASE_BRANCH', 'production'),
        'endpoint': os.getenv('LAKEBASE_ENDPOINT', 'primary'),
        'database': os.getenv('LAKEBASE_DATABASE', 'doccompare'),
    }


def _get_host_and_token(
    project_id: str, branch: str, endpoint: str
) -> tuple[str, str, str, Optional[float]]:
    """Fetch host and OAuth token from Databricks SDK (sync, run in thread).

    Lakebase OAuth auth uses the literal username "token"; the identity is
    carried inside the OAuth token itself, not in the PostgreSQL user field.

    Strategy:
      1. Try generate_database_credential (purpose-built Lakebase credential).
      2. Fall back to the workspace OAuth token already available in the SDK
         config (always present in a Databricks App environment).

    Returns the token's actual remaining lifetime in seconds (from the
    credential's expire_time) when known, so the refresh loop can schedule
    itself off ground truth instead of an assumed lifetime — the fallback
    workspace token carries no expiry info, so that path returns None.
    """
    w = WorkspaceClient()

    branch_path = f'projects/{project_id}/branches/{branch}'
    endpoint_path = f'{branch_path}/endpoints/{endpoint}'

    # Get endpoint host via native SDK postgres service
    eps = list(w.postgres.list_endpoints(parent=branch_path))
    if not eps:
        raise RuntimeError(f'No endpoints found for {branch_path}')
    host = eps[0].status.hosts.host
    logger.debug(f'Lakebase host resolved: {host}')

    # PostgreSQL username = SP application ID (or user email for human users).
    # Lakebase OAuth maps the connection to the Databricks identity via this field.
    me = w.current_user.me()
    username = me.user_name or me.display_name or ''
    if not username:
        raise RuntimeError('Could not resolve current user identity for Lakebase auth')
    logger.debug(f'Lakebase connecting as: {username}')

    # Try purpose-built database credential first
    token: str | None = None
    expires_in_s: Optional[float] = None
    try:
        cred = w.postgres.generate_database_credential(endpoint=endpoint_path)
        token = cred.token or None
        if token:
            if cred.expire_time is not None:
                expires_in_s = cred.expire_time.ToSeconds() - time.time()
            logger.debug(
                f'generate_database_credential succeeded (token len={len(token)}, '
                f'expires_in={expires_in_s}s)'
            )
        else:
            logger.warning('generate_database_credential returned empty token — falling back to workspace token')
    except Exception as e:
        logger.warning(f'generate_database_credential failed ({e}) — falling back to workspace token')

    # Fallback: workspace OAuth token (always present in Databricks App env).
    # No expiry is exposed here, so the caller falls back to a fixed interval.
    if not token:
        token = w.config.token
        if token:
            logger.info('Using workspace OAuth token for Lakebase auth (fallback, unknown expiry)')
        else:
            raise RuntimeError('No authentication token available for Lakebase')

    return host, username, token, expires_in_s


def _next_refresh_delay(expires_in_s: Optional[float]) -> float:
    """Delay before the next credential refresh, based on real expiry when known."""
    if expires_in_s is None:
        return _DEFAULT_REFRESH_INTERVAL_S
    return max(expires_in_s - _REFRESH_SAFETY_BUFFER_S, _MIN_REFRESH_INTERVAL_S)


async def _build_pool(host: str, username: str, token: str, database: str) -> asyncpg.Pool:
    return await asyncpg.create_pool(
        host=host,
        port=5432,
        database=database,
        user=username,
        password=token,
        ssl='require',
        min_size=1,
        max_size=10,
        server_settings={'timezone': 'UTC'},
        timeout=_CONNECT_TIMEOUT_S,
    )


async def _ensure_database(host: str, username: str, token: str, database: str) -> None:
    """Create the PostgreSQL database if it does not exist.

    Connects to the default 'postgres' system database first (always present),
    then issues CREATE DATABASE if needed.  Requires CREATE DB privilege on the
    service principal.
    """
    conn = await asyncpg.connect(
        host=host, port=5432, database='postgres',
        user=username, password=token, ssl='require',
        timeout=_CONNECT_TIMEOUT_S,
    )
    try:
        exists = await conn.fetchval(
            "SELECT 1 FROM pg_database WHERE datname = $1", database
        )
        if not exists:
            # CREATE DATABASE cannot run inside a transaction — asyncpg uses
            # autocommit implicitly for statements outside explicit transactions.
            # Identifier can't be parameterized ($1): escape embedded quotes so a
            # hostile LAKEBASE_DATABASE value can't break out of the identifier.
            safe_db = database.replace('"', '""')
            await conn.execute(f'CREATE DATABASE "{safe_db}"')
            logger.info(f'Database "{database}" created')
        else:
            logger.debug(f'Database "{database}" already exists')
    finally:
        await conn.close()


async def _ensure_schema(pool: asyncpg.Pool) -> None:
    # DROP COLUMN is allowed for genuinely obsolete columns (no longer written or read).
    # Lakebase rewrites the table internally — the table appears empty for a short time,
    # then all data is back. Never DROP TABLE or TRUNCATE.
    async with pool.acquire() as conn:
        # ── messages (primary history table) ─────────────────────────────────
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS messages (
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
        # Add columns introduced after initial deployment (idempotent)
        for col, typedef in [
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
        ]:
            await conn.execute(
                f"ALTER TABLE messages ADD COLUMN IF NOT EXISTS {col} {typedef}"
            )


        # ── feedbacks ─────────────────────────────────────────────────────────
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS feedbacks (
                id              SERIAL PRIMARY KEY,
                created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                message_id      INTEGER REFERENCES messages(id) ON DELETE SET NULL,
                user_id         TEXT,
                workspace_id    TEXT,
                workspace_url   TEXT,
                vote            TEXT NOT NULL CHECK (vote IN (\'up\', \'down\')),
                comment         TEXT
            )
        ''')
        for col, typedef in [
            ('workspace_id',       'TEXT'),
            # Triage (Databricks dashboard): mark a feedback as handled + why.
            ('resolved',           'BOOLEAN NOT NULL DEFAULT FALSE'),
            ('resolution_reason',  'TEXT'),
        ]:
            await conn.execute(
                f"ALTER TABLE feedbacks ADD COLUMN IF NOT EXISTS {col} {typedef}"
            )

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
        for col, typedef in [
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
        ]:
            await conn.execute(
                f"ALTER TABLE llm_requests ADD COLUMN IF NOT EXISTS {col} {typedef}"
            )

        # ── errors (application errors persisted for monitoring) ──────────────
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS errors (
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
        for col, typedef in [
            ('llm_request_id', 'INTEGER'),
            # 'error' (the request failed) or 'warning' (it went on, degraded: a
            # step that did not run as configured — e.g. the chat's search-query
            # rewrite failed, so the search ran with the question only).
            ('severity',       "TEXT NOT NULL DEFAULT 'error'"),
            # Step that failed: chat translate_in / rewrite / search / ref_lookup /
            # title_lookup / llm / translate_back / persist / ws; compare build / llm.
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
        ]:
            await conn.execute(f"ALTER TABLE errors ADD COLUMN IF NOT EXISTS {col} {typedef}")
        await conn.execute('CREATE INDEX IF NOT EXISTS idx_errors_created_at ON errors (created_at)')
        await conn.execute('CREATE INDEX IF NOT EXISTS idx_errors_trace_id ON errors (trace_id)')
        await conn.execute('CREATE INDEX IF NOT EXISTS idx_errors_fingerprint ON errors (fingerprint)')

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
        for col, typedef in [
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
        ]:
            await conn.execute(f"ALTER TABLE impact_requests ADD COLUMN IF NOT EXISTS {col} {typedef}")

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
        for col, typedef in [
            # Technical trace of the judge call, at the end of the row on purpose.
            ('status',          'TEXT'),        # impacted / check / not_impacted / error
            ('judge_ms',        'INTEGER'),
            ('judge_attempts',  'INTEGER'),
            ('input_tokens',    'INTEGER'),
            ('output_tokens',   'INTEGER'),
        ]:
            await conn.execute(f"ALTER TABLE impact_document_results ADD COLUMN IF NOT EXISTS {col} {typedef}")
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
                message_id          INTEGER REFERENCES messages(id) ON DELETE SET NULL,
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
                is_active     BOOLEAN NOT NULL DEFAULT TRUE,
                can_compare   BOOLEAN NOT NULL DEFAULT TRUE,
                can_chat      BOOLEAN NOT NULL DEFAULT TRUE
            )
        ''')
        # Migrations: add columns for databases created before these existed
        for col, typedef in [
            ('can_chat',     'BOOLEAN NOT NULL DEFAULT TRUE'),
            ('workspace_id', 'TEXT'),
        ]:
            await conn.execute(
                f"ALTER TABLE users ADD COLUMN IF NOT EXISTS {col} {typedef}"
            )

        # Migrations: add groups column (persistent group membership for traceability)
        await conn.execute(
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS groups TEXT[] NOT NULL DEFAULT '{}'"
        )

        # Translate lives in its own app (qualibot-translate): drop the unused column.
        await conn.execute(
            "ALTER TABLE users DROP COLUMN IF EXISTS can_translate"
        )

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
        for col, typedef in [
            ('workspace_id', 'TEXT'),
            # Soft-delete: rows are never physically removed, only flagged, so a
            # trace of every conversation is kept in the database for audit.
            ('deleted',      'BOOLEAN NOT NULL DEFAULT FALSE'),
            ('deleted_at',   'TIMESTAMPTZ'),
            # Set once the owner clicks "Share" — grants read-only access to
            # anyone with the token, independent of the row's own `id`.
            ('share_token',  'TEXT'),
        ]:
            await conn.execute(
                f"ALTER TABLE chat_sessions ADD COLUMN IF NOT EXISTS {col} {typedef}"
            )
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
                trace_id        TEXT,
                tool_name       TEXT,
                tool_query      TEXT,
                reasoning_steps TEXT
            )
        ''')
        for col, typedef in [
            # The turn's chat_turns.trace_id (vsi-…); on Knowledge Assistant turns, its MLflow trace id.
            ('trace_id',        'TEXT'),
            # Knowledge Assistant only, no longer written (the retrieval is in chat_turns /
            # chat_retrieved_chunks); kept for the history of the KA turns.
            ('tool_name',       'TEXT'),
            ('tool_query',      'TEXT'),
            ('tool_result',     'TEXT'),
            ('reasoning_steps', 'TEXT'),
            ('workspace_id',    'TEXT'),
            # Engine label: vsi-all / vsi-as / vsi-is (KA turns: ka-… endpoint).
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
            # chat translation bridge (server/services/translation_bridge.py).
            # Empty when the bridge is disabled (CHAT_TRANSLATE_BRIDGE_ENABLED).
            ('question_lang',   'TEXT'),
        ]:
            await conn.execute(
                f"ALTER TABLE chat_messages ADD COLUMN IF NOT EXISTS {col} {typedef}"
            )

        await conn.execute('''
            CREATE INDEX IF NOT EXISTS chat_messages_session_idx
            ON chat_messages(session_id)
        ''')

        # ── chat_turns (one row per chat turn: every step, its duration, what
        #    it should have run with and what went wrong — services/turn_log.py)
        #    No FK on session_id: a turn whose messages could not be saved is
        #    still logged. ──────────────────────────────────────────────────────
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
        for col, typedef in CHAT_TURN_COLUMNS + [(f'{t}_ms', 'INTEGER') for t in CHAT_TURN_TIMINGS]:
            await conn.execute(f'ALTER TABLE chat_turns ADD COLUMN IF NOT EXISTS {col} {typedef}')
        await conn.execute('CREATE INDEX IF NOT EXISTS idx_chat_turns_created_at ON chat_turns (created_at)')
        await conn.execute('CREATE INDEX IF NOT EXISTS idx_chat_turns_session_id ON chat_turns (session_id)')
        await conn.execute('CREATE INDEX IF NOT EXISTS idx_chat_turns_assistant_message_id '
                           'ON chat_turns (assistant_message_id)')

        # ── chat_retrieved_chunks (every passage retrieved for a turn, with its
        #    text: the chunks of a superseded revision are deleted from the index
        #    tables, this keeps what the answer was really built on) ────────────
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS chat_retrieved_chunks (
                id          BIGSERIAL PRIMARY KEY,
                created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                turn_id     INTEGER NOT NULL REFERENCES chat_turns(id) ON DELETE CASCADE
            )
        ''')
        for col, typedef in CHAT_CHUNK_COLUMNS:
            await conn.execute(f'ALTER TABLE chat_retrieved_chunks ADD COLUMN IF NOT EXISTS {col} {typedef}')
        await conn.execute('CREATE INDEX IF NOT EXISTS idx_chat_retrieved_chunks_turn_id '
                           'ON chat_retrieved_chunks (turn_id)')
        await conn.execute('CREATE INDEX IF NOT EXISTS idx_chat_retrieved_chunks_ref ON chat_retrieved_chunks (ref)')

        # ── knowledge_base_metadata ───────────────────────────────────────────
        # Single-row table (id=1) holding the "documents as of" date shown in
        # the chat UI. Update documents_as_of manually after each KB refresh.
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS knowledge_base_metadata (
                id              INTEGER PRIMARY KEY DEFAULT 1,
                documents_as_of DATE,
                updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                CONSTRAINT single_row CHECK (id = 1)
            )
        ''')
        await conn.execute('''
            INSERT INTO knowledge_base_metadata (id)
            VALUES (1)
            ON CONFLICT (id) DO NOTHING
        ''')

        # ── doc_catalog ───────────────────────────────────────────────────────
        # Every document of the parsing scope (REF, title, link, in the chat index or
        # not). Rewritten by the parsing pipeline after each daily run (task
        # 6_update_kb_metadata); read by server/services/doc_catalog.py.
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
        # Current revision (Intraqual indice) and its publication date, given to the chat's answer
        # model with each document. Added here: the pipeline's identity does not own this table.
        for col, typedef in [('revision', 'TEXT'), ('doc_date', 'DATE')]:
            await conn.execute(f'ALTER TABLE doc_catalog ADD COLUMN IF NOT EXISTS {col} {typedef}')

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
        for col, typedef in [
            ('workspace_id',       'TEXT'),
            # Triage (Databricks dashboard): mark a feedback as handled + why.
            ('resolved',           'BOOLEAN NOT NULL DEFAULT FALSE'),
            ('resolution_reason',  'TEXT'),
        ]:
            await conn.execute(
                f"ALTER TABLE chat_feedbacks ADD COLUMN IF NOT EXISTS {col} {typedef}"
            )


        # ── Data migrations (idempotent — WHERE only matches old composite format) ──

        # 0. Drop obsolete columns (Lakebase rewrites the table — safe but transient empty state)
        for _tbl, _col in [
            ('messages',     'duration_s'),
            ('users',        'can_export'),
            ('llm_requests', 'user_id'),
            ('llm_requests', 'workspace_id'),
        ]:
            try:
                await conn.execute(f'ALTER TABLE {_tbl} DROP COLUMN IF EXISTS {_col}')
            except Exception as e:
                logger.debug('Could not drop %s.%s: %s', _tbl, _col, e)

        # 1. Trim trailing whitespace / \r\n from emails introduced by SCIM responses
        await conn.execute("UPDATE users SET email = BTRIM(email) WHERE email ~ '\\s'")
        await conn.execute("UPDATE users SET email = NULL WHERE TRIM(COALESCE(email,'')) = ''")

        # 2. Delete NULL-email users whose numeric ID already has an email-carrying entry (same person from two
        # workspaces),
        #    otherwise the split UPDATE below hits a UNIQUE violation on user_id.
        await conn.execute(r'''
            DELETE FROM users
            WHERE email IS NULL
              AND user_id ~ '^\d+@\d+$'
              AND SPLIT_PART(user_id, '@', 1) IN (
                  SELECT SPLIT_PART(user_id, '@', 1) FROM users
                  WHERE email IS NOT NULL AND user_id ~ '^\d+@\d+$'
              )
        ''')

        # 3. Split composite user_ids (numeric@workspace) into separate columns for every table
        for table in ('users', 'messages', 'feedbacks',
                      'chat_sessions', 'chat_messages', 'chat_feedbacks'):
            await conn.execute(f'''
                UPDATE {table}
                SET workspace_id = SPLIT_PART(user_id, '@', 2),
                    user_id      = SPLIT_PART(user_id, '@', 1)
                WHERE user_id ~ '^\\d+@\\d+$'
            ''')

        # ── Deduplication + unique index (run after data cleanup) ─────────────
        # Keep the most recently updated row per email, delete older duplicates.
        await conn.execute('''
            DELETE FROM users a
            USING users b
            WHERE a.email IS NOT NULL
              AND a.email = b.email
              AND a.updated_at < b.updated_at
        ''')

        # Partial unique index on email so the same real-world user is never
        # duplicated across workspaces.
        await conn.execute('''
            CREATE UNIQUE INDEX IF NOT EXISTS users_email_idx
            ON users(email)
            WHERE email IS NOT NULL
        ''')

    logger.info('Lakebase schema ready')



async def _token_refresh_loop(
    project_id: str, branch: str, endpoint: str, database: str, delay: float
) -> None:
    global _pool
    while True:
        await asyncio.sleep(delay)
        try:
            host, username, token, expires_in_s = await asyncio.to_thread(
                _get_host_and_token, project_id, branch, endpoint
            )
            new_pool = await _build_pool(host, username, token, database)
            old_pool = _pool
            _pool = new_pool
            if old_pool:
                # Grace period for in-flight requests that already grabbed the old pool via get_pool().
                await asyncio.sleep(30)
                await old_pool.close()
            delay = _next_refresh_delay(expires_in_s)
            logger.info(f'Lakebase token refreshed (next refresh in {delay:.0f}s)')
        except Exception as e:
            # The current token keeps expiring regardless: retry soon instead of waiting for the next cycle.
            delay = 60
            logger.error(f'Lakebase token refresh failed (retrying in {delay}s): {e}')


# --- Public API ---


async def init_lakebase() -> None:
    global _pool, _refresh_task
    cfg = _cfg()
    project_id = cfg['project_id']
    if not project_id:
        logger.warning('LAKEBASE_PROJECT_ID not set — history feature disabled')
        return
    try:
        host, username, token, expires_in_s = await asyncio.to_thread(
            _get_host_and_token, project_id, cfg['branch'], cfg['endpoint']
        )
        await _ensure_database(host, username, token, cfg['database'])
        _pool = await _build_pool(host, username, token, cfg['database'])
        try:
            await _ensure_schema(_pool)
        except Exception as e:
            # A failing schema migration must never skip the refresh loop below: the live pool's token expires in ~1h.
            logger.error(f'Lakebase schema ensure failed — continuing with existing schema: {e}')
        _refresh_task = asyncio.create_task(
            _token_refresh_loop(
                project_id, cfg['branch'], cfg['endpoint'], cfg['database'],
                _next_refresh_delay(expires_in_s),
            )
        )
        logger.info(f'Lakebase ready ({host})')
    except Exception as e:
        logger.error(f'Lakebase init failed — history disabled: {e}')


async def shutdown_lakebase() -> None:
    global _pool, _refresh_task
    if _refresh_task:
        _refresh_task.cancel()
        try:
            await _refresh_task
        except asyncio.CancelledError:
            pass
    if _pool:
        await _pool.close()
    logger.info('Lakebase shut down')


def get_pool() -> Optional[asyncpg.Pool]:
    return _pool


async def upsert_user(
    conn,
    *,
    user_id: str,
    workspace_id: Optional[str] = None,
    email: Optional[str] = None,
    workspace_url: Optional[str] = None,
) -> None:
    """Register (or update) an identity in the shared `users` table.

    Used by BOTH the document-comparison flow and the chat flow, so that every
    identity that interacts with the app is registered exactly once — whether
    the user compared a document or only chatted.  Without this, chat-only users
    end up with a user_id in chat_sessions/chat_messages that has no matching
    row in `users`.

    Deduplication key is the email (stable across workspaces); falls back to
    user_id when no email could be resolved.  Must run inside an existing
    connection/transaction (takes an acquired `conn`).
    """
    if not user_id:
        return
    if email:
        # If this user_id previously had no email, fill it now: the email-keyed upsert below would otherwise conflict
        # on user_id.
        await conn.execute(
            'UPDATE users SET email = $2, updated_at = NOW()'
            ' WHERE user_id = $1 AND email IS NULL',
            user_id, email,
        )
        # Upsert on email — handles cross-workspace dedup (same person, new workspace)
        await conn.execute(
            '''
            INSERT INTO users (user_id, workspace_id, email, workspace_url)
            VALUES ($1, $2, $3, $4)
            ON CONFLICT (email) WHERE email IS NOT NULL DO UPDATE SET
                user_id       = EXCLUDED.user_id,
                workspace_id  = EXCLUDED.workspace_id,
                workspace_url = COALESCE(EXCLUDED.workspace_url, users.workspace_url),
                updated_at    = NOW()
            ''',
            user_id, workspace_id, email, workspace_url,
        )
    else:
        # No email resolved — fall back to user_id deduplication
        await conn.execute(
            '''
            INSERT INTO users (user_id, workspace_id, email, workspace_url)
            VALUES ($1, $2, $3, $4)
            ON CONFLICT (user_id) DO UPDATE SET
                workspace_id  = COALESCE(EXCLUDED.workspace_id, users.workspace_id),
                workspace_url = COALESCE(EXCLUDED.workspace_url, users.workspace_url),
                updated_at    = NOW()
            ''',
            user_id, workspace_id, None, workspace_url,
        )


_ERROR_INSERT = '''
    INSERT INTO errors
        (endpoint, error_type, error_msg, user_id, workspace_id,
         old_filename, new_filename, file_type, stack_trace, llm_request_id,
         severity, stage, http_status, upstream, trace_id, session_id, context, app_version,
         origin, fingerprint, user_message)
    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16, $17::jsonb, $18,
            $19, $20, $21)
'''


def _error_row(
    *,
    endpoint: str = '',
    error_type: str = '',
    error_msg: str = '',
    user_id: str = '',
    workspace_id: str = '',
    old_filename: str = '',
    new_filename: str = '',
    file_type: str = '',
    stack_trace: str = '',
    llm_request_id: Optional[int] = None,
    severity: str = 'error',
    stage: str = '',
    http_status: Optional[int] = None,
    upstream: str = '',
    trace_id: str = '',
    session_id: str = '',
    context: Optional[Dict[str, Any]] = None,
    origin: str = '',
    user_message: str = '',
) -> tuple:
    """Parameters of _ERROR_INSERT, in order. ``origin``: where in the code (``path:line in
    function``), taken from the stack trace when absent."""
    origin = origin or error_origin.from_stack_trace(stack_trace)
    return (
        endpoint or None,
        error_type or None,
        (error_msg or '')[:2000],
        user_id or None,
        workspace_id or None,
        old_filename or None,
        new_filename or None,
        file_type or None,
        (stack_trace or '')[:4000] or None,
        llm_request_id,
        severity or 'error',
        stage or None,
        http_status or None,
        upstream or None,
        trace_id or None,
        session_id or None,
        json.dumps(context, ensure_ascii=False, default=str) if context else None,
        _app_version(),
        origin or None,
        error_origin.fingerprint(endpoint, stage, error_type, origin),
        user_message or None,
    )


def store_error(*, exc: Optional[BaseException] = None, **kwargs: Any) -> Optional[asyncio.Task]:
    """Persist an application error to the errors table, in the background (never raises).
    Arguments: those of ``_error_row`` (all optional, keyword only); ``exc`` adds its stack trace.
    Called synchronously so the origin can be read from the caller's frame when there is no
    stack trace."""
    if exc is not None and not kwargs.get('stack_trace'):
        kwargs['stack_trace'] = ''.join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    kwargs.setdefault('origin', error_origin.from_exception(exc) or error_origin.from_stack_trace(
        kwargs.get('stack_trace', '')) or error_origin.caller(1))
    try:
        return asyncio.get_running_loop().create_task(_write_error(_error_row(**kwargs)))
    except RuntimeError:                   # no event loop (scripts, tests)
        return None


async def _write_error(row: tuple) -> None:
    # Bounded acquire: an exhausted pool must not pile up tasks on top of the original failure.
    pool = get_pool()
    if not pool:
        return
    try:
        async with pool.acquire(timeout=5.0) as conn:
            await conn.execute(_ERROR_INSERT, *row)
    except Exception as e:
        logger.debug('store_error failed (non-blocking): %s', e)


async def store_chat_turn(
    log: Any,
    *,
    endpoint: str = '',
    user_message_id: Optional[int] = None,
    assistant_message_id: Optional[int] = None,
) -> Optional[int]:
    """Write one chat turn (a finished ``turn_log.TurnLog``): its chat_turns row, its
    chat_retrieved_chunks rows and one errors row per warning/error issue — in one
    transaction. Best-effort, never raises; returns the chat_turns id."""
    pool = get_pool()
    if not pool:
        return None
    data = {**log.data, 'app_version': log.data.get('app_version') or _app_version()}
    columns = [c for c, _ in CHAT_TURN_COLUMNS] + [f'{t}_ms' for t in CHAT_TURN_TIMINGS]
    typedefs = [t for _, t in CHAT_TURN_COLUMNS] + ['INTEGER'] * len(CHAT_TURN_TIMINGS)
    values = ([_db_value(data.get(c), t) for c, t in CHAT_TURN_COLUMNS]
              + [log.timings_ms.get(t) for t in CHAT_TURN_TIMINGS])
    fixed = ['trace_id', 'user_message_id', 'assistant_message_id', 'status', 'error_stage', 'error_type',
             'error_msg', 'warnings']
    fixed_values = [log.trace_id, user_message_id, assistant_message_id, log.status or 'error',
                    log.error.get('stage'), log.error.get('type'), log.error.get('message'), log.codes]
    turn_sql = (f'INSERT INTO chat_turns ({", ".join(fixed + columns)}) '
                f'VALUES ({_placeholders(["TEXT"] * len(fixed) + typedefs)}) RETURNING id')

    chunk_cols = [c for c, _ in CHAT_CHUNK_COLUMNS]
    chunk_types = [t for _, t in CHAT_CHUNK_COLUMNS]
    chunk_sql = (f'INSERT INTO chat_retrieved_chunks (turn_id, {", ".join(chunk_cols)}) '
                 f'VALUES ($1, {_placeholders(chunk_types, offset=1)})')
    chunk_rows = [tuple(_db_value({**p, 'trace_id': log.trace_id}.get(c), t) for c, t in CHAT_CHUNK_COLUMNS)
                  for p in log.passages]

    error_rows = [
        _error_row(endpoint=endpoint, error_type=i['error_type'], error_msg=i['message'],
                   user_id=data.get('user_id') or '', workspace_id=data.get('workspace_id') or '',
                   severity=i['severity'], stage=i['stage'], http_status=i['http_status'],
                   upstream=i['upstream'] or '', trace_id=log.trace_id, session_id=data.get('session_id') or '',
                   stack_trace=(i.get('context') or {}).get('stack_trace', ''), origin=i.get('origin') or '',
                   user_message=(data.get('user_message') or '') if i['severity'] == 'error' else '',
                   context={'code': i['code'], 'at_ms': i['at_ms'],
                            **{k: v for k, v in (i.get('context') or {}).items() if k != 'stack_trace'}})
        for i in log.issues if i['severity'] in ('error', 'warning')
    ]
    try:
        async with pool.acquire(timeout=10.0) as conn, conn.transaction():
            turn_id = await conn.fetchval(turn_sql, *fixed_values, *values)
            if chunk_rows:
                await conn.executemany(chunk_sql, [(turn_id, *row) for row in chunk_rows])
            if error_rows:
                await conn.executemany(_ERROR_INSERT, error_rows)
        return turn_id
    except Exception as e:
        logger.warning('store_chat_turn failed (non-blocking): %s', e)
        return None


async def store_llm_request(
    *,
    old_file_hash: str = '',
    new_file_hash: str = '',
    file_type: str = '',
    endpoint_name: str = '',
    messages_json: str = '',
) -> Optional[int]:
    """Insert an LLM request audit record; returns the inserted id (or None on failure)."""
    pool = get_pool()
    if not pool:
        return None
    try:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                '''
                INSERT INTO llm_requests
                    (old_file_hash, new_file_hash, file_type, endpoint_name, messages_json)
                VALUES ($1, $2, $3, $4, $5)
                RETURNING id
                ''',
                old_file_hash or None,
                new_file_hash or None,
                file_type or None,
                endpoint_name or None,
                messages_json or None,
            )
        return row['id'] if row else None
    except Exception as e:
        logger.debug('store_llm_request failed: %s', e)
        return None


async def update_llm_request_usage(
    llm_request_id: Optional[int],
    *,
    input_tokens: int = 0,
    output_tokens: int = 0,
    thinking_tokens: int = 0,
    total_tokens: int = 0,
    cost_eur: float = 0.0,
    http_status: int = 0,
    error_type: str = '',
    error_msg: str = '',
) -> None:
    """Update token usage and status on an llm_requests row (best-effort, never raises)."""
    pool = get_pool()
    if not pool or not llm_request_id:
        return
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                '''
                UPDATE llm_requests
                SET input_tokens    = COALESCE($2, input_tokens),
                    output_tokens   = COALESCE($3, output_tokens),
                    thinking_tokens = COALESCE($4, thinking_tokens),
                    total_tokens    = COALESCE($5, total_tokens),
                    cost_eur        = COALESCE($6, cost_eur),
                    http_status     = COALESCE($7, http_status),
                    error_type      = COALESCE($8, error_type),
                    error_msg       = COALESCE($9, error_msg)
                WHERE id = $1
                ''',
                llm_request_id,
                input_tokens or None,
                output_tokens or None,
                thinking_tokens or None,
                total_tokens or None,
                cost_eur or None,
                http_status or None,
                error_type or None,
                (error_msg or '')[:2000] or None,
            )
    except Exception as e:
        logger.debug('update_llm_request_usage failed: %s', e)


async def update_llm_request_run(
    llm_request_id: Optional[int],
    *,
    status: str,
    queue_wait_ms: Optional[int] = None,
    build_ms: Optional[int] = None,
    first_token_ms: Optional[int] = None,
    generation_ms: Optional[int] = None,
    total_ms: Optional[int] = None,
    chunked_parts: Optional[int] = None,
) -> None:
    """Record the outcome and step durations of an /compare/analyze stream on its
    llm_requests row (best-effort, never raises)."""
    pool = get_pool()
    if not pool or not llm_request_id:
        return
    try:
        async with pool.acquire(timeout=5.0) as conn:
            await conn.execute(
                '''
                UPDATE llm_requests
                SET status = $2, queue_wait_ms = $3, build_ms = $4, first_token_ms = $5,
                    generation_ms = $6, total_ms = $7, chunked_parts = $8
                WHERE id = $1
                ''',
                llm_request_id, status, queue_wait_ms, build_ms, first_token_ms,
                generation_ms, total_ms, chunked_parts,
            )
    except Exception as e:
        logger.debug('update_llm_request_run failed: %s', e)


async def store_impact_request(
    *,
    method: str,
    user_id: str = '',
    workspace_id: str = '',
    old_file_hash: str = '',
    new_file_hash: str = '',
    changes_chars: int = 0,
    truncated: bool = False,
    chunks_returned: int = 0,
    num_documents: int = 0,
    duration_s: float = 0.0,
    endpoint_name: str = '',
    input_tokens: int = 0,
    output_tokens: int = 0,
    total_tokens: int = 0,
    cost_eur: float = 0.0,
    http_status: int = 200,
    error_type: str = '',
    error_msg: str = '',
    documents: List[Dict[str, Any]] | None = None,
    status: str = '',
    index_name: str = '',
    queries_used: Optional[int] = None,
    queries_failed: Optional[int] = None,
    candidates: Optional[int] = None,
    not_judged: Optional[int] = None,
    judge_failed: Optional[int] = None,
    extract_ms: Optional[int] = None,
    search_ms: Optional[int] = None,
    judge_ms: Optional[int] = None,
) -> Optional[int]:
    """Log one /compare/impact call — success or failure (best-effort, never raises).

    Returns the impact_requests id (None if the row could not be written) so
    feedback on the result can be linked back to it.

    When `documents` is given (the per-document judgments from
    synthesize_impact_with_llm), also persists one row per document to
    impact_document_results — the linked Intraqual document(s), the LLM's
    verdict/description, and nothing else, so a business reader auditing the
    trace isn't shown retrieval internals (chunk_count, query_hits, max_score)
    that don't mean anything to them.
    """
    pool = get_pool()
    if not pool:
        return None
    try:
        async with pool.acquire() as conn, conn.transaction():
            request_id = await conn.fetchval(
                '''
                INSERT INTO impact_requests
                    (user_id, workspace_id, method, old_file_hash, new_file_hash,
                     changes_chars, truncated, chunks_returned, num_documents, duration_s,
                     endpoint_name, input_tokens, output_tokens, total_tokens, cost_eur,
                     http_status, error_type, error_msg,
                     status, app_version, index_name, queries_used, queries_failed, candidates,
                     not_judged, judge_failed, extract_ms, search_ms, judge_ms)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16, $17, $18,
                        $19, $20, $21, $22, $23, $24, $25, $26, $27, $28, $29)
                RETURNING id
                ''',
                user_id or None,
                workspace_id or None,
                method,
                old_file_hash or None,
                new_file_hash or None,
                changes_chars or None,
                truncated,
                chunks_returned or None,
                num_documents or None,
                duration_s or None,
                endpoint_name or None,
                input_tokens or None,
                output_tokens or None,
                total_tokens or None,
                cost_eur or None,
                http_status or None,
                error_type or None,
                (error_msg or '')[:2000] or None,
                status or ('ok' if (http_status or 200) < 400 else 'error'),
                _app_version(),
                index_name or None,
                queries_used,
                queries_failed,
                candidates,
                not_judged,
                judge_failed,
                extract_ms,
                search_ms,
                judge_ms,
            )
            if documents:
                await conn.executemany(
                    '''
                    INSERT INTO impact_document_results
                        (request_id, ref, division, url, impacted, confidence, section, reason, other_documents_json,
                         status, judge_ms, judge_attempts, input_tokens, output_tokens)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14)
                    ''',
                    [
                        (
                            request_id,
                            d.get('ref') or None,
                            d.get('division') or None,
                            d.get('url') or None,
                            bool(d.get('impacted', False)),
                            d.get('confidence') or None,
                            '; '.join(d.get('sections') or []) or None,
                            (d.get('reason') or '')[:2000] or None,
                            json.dumps(d.get('other_languages') or []) if d.get('other_languages') else None,
                            d.get('status') or None,
                            d.get('judge_ms'),
                            d.get('judge_attempts'),
                            d.get('input_tokens'),
                            d.get('output_tokens'),
                        )
                        for d in documents
                    ],
                )
            return request_id
    except Exception as e:
        logger.debug('store_impact_request failed: %s', e)
        return None


async def get_cached_impact_result(old_file_hash: str, new_file_hash: str, app_version: str) -> Optional[dict]:
    """Return the most recent cached /compare/impact result for this file pair, or None."""
    if not old_file_hash or not new_file_hash:
        return None
    pool = get_pool()
    if not pool:
        return None
    try:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                '''
                SELECT result_json FROM impact_cache
                WHERE old_file_hash = $1 AND new_file_hash = $2 AND app_version = $3
                ORDER BY created_at DESC
                LIMIT 1
                ''',
                old_file_hash, new_file_hash, app_version,
            )
        return json.loads(row['result_json']) if row else None
    except Exception as e:
        logger.debug('Impact cache lookup failed: %s', e)
        return None


async def store_impact_cache(old_file_hash: str, new_file_hash: str, app_version: str, result: dict) -> None:
    """Persist a fresh /compare/impact result for later cache hits (best-effort, never raises)."""
    if not old_file_hash or not new_file_hash:
        return
    pool = get_pool()
    if not pool:
        return
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                '''
                INSERT INTO impact_cache (old_file_hash, new_file_hash, app_version, result_json)
                VALUES ($1, $2, $3, $4)
                ''',
                old_file_hash, new_file_hash, app_version, json.dumps(result),
            )
    except Exception as e:
        logger.debug('store_impact_cache failed: %s', e)


async def get_cached_summary(file_hash: str, app_version: str) -> Optional[dict]:
    """Return the most recent cached /compare/summarize result for this file, or None."""
    if not file_hash:
        return None
    pool = get_pool()
    if not pool:
        return None
    try:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                '''
                SELECT result_json FROM summary_cache
                WHERE file_hash = $1 AND app_version = $2
                ORDER BY created_at DESC
                LIMIT 1
                ''',
                file_hash, app_version,
            )
        return json.loads(row['result_json']) if row else None
    except Exception as e:
        logger.debug('Summary cache lookup failed: %s', e)
        return None


async def store_summary_cache(file_hash: str, app_version: str, result: dict, endpoint_name: str = '') -> None:
    """Persist a fresh /compare/summarize result for later cache hits (best-effort, never raises)."""
    if not file_hash:
        return
    pool = get_pool()
    if not pool:
        return
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                '''
                INSERT INTO summary_cache (file_hash, app_version, result_json, endpoint_name)
                VALUES ($1, $2, $3, $4)
                ''',
                file_hash, app_version, json.dumps(result), endpoint_name or None,
            )
    except Exception as e:
        logger.debug('store_summary_cache failed: %s', e)
