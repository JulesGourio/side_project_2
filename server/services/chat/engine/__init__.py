"""Chat engine: answers a question from the Vector Search index.

Flow: search queries (queries.py), hybrid search with the reranker (search.py), named documents (retrieval.py),
one language variant per document, prompt (answer.py), streamed answer with [n] citations (citations.py, chat/llm.py).
stream_chat yields the events routers/chat/ws.py expects and fills the turn's TurnLog (turn_log.py).
Settings are read from the environment at call time (config.py).
"""

from server.services.chat.engine.answer import LANGUAGE_REMINDER, build_prompt, stream_chat
from server.services.chat.engine.citations import CitationStreamParser, parse_citations
from server.services.chat.engine.config import (
    division_filter,
    load_instructions,
    normalize_division,
    prompt_parts,
    settings,
)
from server.services.chat.engine.queries import REWRITE_PROMPT, rewrite_queries, split_bilingual, strip_added_noise
from server.services.chat.engine.retrieval import _clean_history, group_documents, retrieve_for_turn

__all__ = [
    'CitationStreamParser',
    'LANGUAGE_REMINDER',
    'REWRITE_PROMPT',
    '_clean_history',
    'build_prompt',
    'division_filter',
    'group_documents',
    'load_instructions',
    'normalize_division',
    'parse_citations',
    'prompt_parts',
    'retrieve_for_turn',
    'rewrite_queries',
    'settings',
    'split_bilingual',
    'stream_chat',
    'strip_added_noise',
]
