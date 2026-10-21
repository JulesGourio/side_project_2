"""Column lists of the chat logging tables, shared by the schema and the INSERTs."""

import json
from typing import Any, List

# ── chat_turns: one row per chat turn (filled from services/chat/turn_log.TurnLog) ──
# The columns written from TurnLog.data, in order — used by both CREATE TABLE and the
# INSERT, so a column added here reaches the table on the next app start.
CHAT_TURN_COLUMNS: List[tuple] = [
    # who / where
    ('session_id',            'TEXT'),
    ('user_id',               'TEXT'),
    ('workspace_id',          'TEXT'),
    ('app_version',           'TEXT'),
    ('division',              'TEXT'),
    # what the turn should have run with: engine.settings() (index, search sizes, models
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
    ('rewrite_mode',          'TEXT'),               # translate: opening message in French / English, translated into the other; rewrite: bilingual rewrite
    ('rewrite_endpoint',      'TEXT'),               # model that rewrote (NULL: every model failed)
    ('rewrite_attempts',      'JSONB'),              # every try of the rewrite or translation, retries included
    ('rewrite_input_tokens',  'INTEGER'),
    ('rewrite_output_tokens', 'INTEGER'),
    ('rewrite_cost_eur',      'DOUBLE PRECISION'),
    # retrieval
    ('named_refs',            'TEXT[]'),             # REFs named in the conversation (REF lookup)
    ('vs_calls_expected',     'INTEGER'),            # Vector Search queries of the main search
    ('vs_calls_ok',           'INTEGER'),
    ('rerank_ok',             'BOOLEAN'),
    ('passages_retrieved',    'INTEGER'),            # rows of chat_retrieved_chunks
    ('passages_sent',         'INTEGER'),            # ... of which handed to the LLM (kept)
    ('documents_sent',        'INTEGER'),
    ('prompt_chars',          'INTEGER'),
    ('mlflow_trace_id',       'TEXT'),               # the turn's trace in the MLflow experiment (CHAT_MLFLOW_EXPERIMENT)
    ('instructions_sha',      'TEXT'),               # sha256[:16] of the system instructions used
    # answer
    ('answer_endpoint',       'TEXT'),               # model that answered (NULL: none)
    ('llm_fallback',          'BOOLEAN'),
    ('llm_attempts',          'JSONB'),              # every call: endpoint, outcome, status, seconds, first_text_s
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
CHAT_TURN_TIMINGS = ('total', 'translate_in', 'retrieval', 'rewrite', 'search', 'ref_lookup',
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
    ('source',            'TEXT'),               # search | ref_lookup
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
    """The base type of a column type: its first word, without ``NOT NULL`` or ``DEFAULT``.

    :param typedef: column type, such as ``BOOLEAN NOT NULL DEFAULT TRUE``
    :return: the base type, such as ``BOOLEAN``
    """
    return typedef.split()[0]


def _db_value(value: Any, typedef: str) -> Any:
    """Python value -> asyncpg parameter for a column of type ``typedef``.

    :param value: Python value; None is kept
    :param typedef: SQL type of the column
    :return: the value converted for the database parameter
    """
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
    """The SQL placeholders of a list of column types; ``JSONB`` ones are cast.

    :param typedefs: SQL types of the columns, in order
    :param offset: number of the placeholders already used; 0 starts at ``$1``
    :return: the placeholders, comma-separated
    """
    return ', '.join(f'${i}::jsonb' if _base_type(t) == 'JSONB' else f'${i}'
                     for i, t in enumerate(typedefs, offset + 1))

