"""Chat — the chat engine: answers from the Vector Search index.

What happens for each question:

1. **Search queries** (``queries.py``) — two cases (``opening_language()``). The first message of a conversation,
   in French or English: only translated into the other language (``translate_query``, no rewrite, nothing
   expanded), then two queries. Anything else (follow-up, other or unsure language): GPT-6 Luna turns the last
   question, with the conversation as context, into one standalone search query in French and one in English
   (acronyms expanded when certain).
2. **Search** (``search.py``) — the queries (the question as asked + the rewrites or the translation) in HYBRID
   mode on the single passage index: for each, the top ``rerank_top_k()`` passages (12; ``pair_rerank_top_k()``,
   16, with two queries) reranked by the Databricks reranker (reading REF, section headings and text); for the
   French rewrite only (``raw_on()``), also the raw top ``raw_top_k()`` (12). Everything is merged by best rank,
   then cut to ``max_search_passages()`` (0 = no cap, the default). The chat's division (AS / IS) is a filter on
   the ``division`` column (ALL: no filter).
3. **Named documents** (``retrieval.py``) — REFs named in the question or the earlier turns (MI-14242…) get
   their own filtered search, ranked first.
4. **One language per document** — only the best-ranked language variant of a document is kept.
5. **Prompt** (``answer.py``) — the division's own part (``prompts/chat/system/<div>.md``: role, scope, what
   only that division needs) + the base shared by every division (``system/base.md``: language, sources,
   versions, documents only, off-topic refusal, document types, citations) + the numbered documents + the
   question + one line naming the language the answer must be written in.
6. **Answer** — GPT-6 Luna, GPT-5.6 Luna as fallback (``chat/llm.py``: retries, fallback, continuation of a cut
   answer, concurrency limit); ``[n]`` markers are turned into citations (``citations.py``).

``stream_chat`` yields the event stream ``routers/chat/ws.py`` expects (text deltas, ``sources`` +
``citations``, ``metadata``, ``error``, ``[DONE]``) and fills the turn's ``TurnLog`` (``turn_log.py``: every
step, its duration, the passages retrieved, what went wrong — saved in Lakebase ``chat_turns`` /
``chat_retrieved_chunks`` / ``errors`` by the route). Settings (``config.py``, environment, read at call time):
``CHAT_INDEX``, ``CHAT_LLM_ENDPOINT`` / ``CHAT_LLM_FALLBACK_ENDPOINTS``,
``CHAT_REWRITE_ENDPOINT`` / ``CHAT_REWRITE_FALLBACK_ENDPOINTS``,
``CHAT_ANSWER_MAX_TOKENS``, ``CHAT_REWRITE_MAX_TOKENS``, ``VS_MAX_CONCURRENT_QUERIES`` /
``VS_QUERY_RETRIES`` (``vs_gate.py``),
``CHAT_RERANK_TOP_K`` / ``CHAT_RAW_TOP_K`` / ``CHAT_RAW_ON`` / ``CHAT_MAX_SEARCH_PASSAGES`` (search sizes,
defaults = the configuration in use; other values are for ``retrieval_eval`` comparisons),
plus the resilience settings documented in ``chat/llm.py``.
"""

from .answer import LANGUAGE_REMINDER, build_prompt, stream_chat
from .citations import CitationStreamParser, parse_citations
from .config import (
    division_filter,
    load_instructions,
    normalize_division,
    prompt_parts,
    settings,
)
from .queries import REWRITE_PROMPT, rewrite_queries, split_bilingual, strip_added_noise
from .retrieval import _clean_history, group_documents, retrieve_for_turn

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
