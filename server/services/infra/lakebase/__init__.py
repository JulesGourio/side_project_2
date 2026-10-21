"""Lakebase (autoscaling PostgreSQL): pool, schema, request logging, caches and users."""

from server.services.infra.lakebase.audit import (
    store_chat_turn,
    store_error,
    store_impact_request,
    store_llm_request,
    update_llm_request_run,
    update_llm_request_usage,
)
from server.services.infra.lakebase.cache import (
    get_cached_analysis, get_cached_impact_result, get_cached_summary, store_analysis_cache, store_impact_cache,
    store_summary_cache,
)
from server.services.infra.lakebase.columns import CHAT_CHUNK_COLUMNS, CHAT_TURN_COLUMNS, CHAT_TURN_TIMINGS
from server.services.infra.lakebase.connection import get_pool, init_lakebase, shutdown_lakebase
from server.services.infra.lakebase.users import upsert_user

__all__ = [
    'CHAT_CHUNK_COLUMNS',
    'CHAT_TURN_COLUMNS',
    'CHAT_TURN_TIMINGS',
    'get_cached_analysis',
    'get_cached_impact_result',
    'get_cached_summary',
    'get_pool',
    'init_lakebase',
    'shutdown_lakebase',
    'store_analysis_cache',
    'store_chat_turn',
    'store_error',
    'store_impact_cache',
    'store_impact_request',
    'store_llm_request',
    'store_summary_cache',
    'update_llm_request_run',
    'update_llm_request_usage',
    'upsert_user',
]
