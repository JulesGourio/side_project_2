"""Chat VSI variants — which version of the VSI engine answers.

``CHAT_VSI_VARIANT`` (read at call time, default ``baseline``):

- ``baseline`` — ``chat_vsi.stream_chat_vsi``, the engine as delivered, unchanged;
- ``rerank``   — ``chat_vsi_rerank.stream_chat_vsi_rerank``, baseline + Vector Search reranker.

Each variant lives in its own module, so the baseline code stays as a reference and every
variant can be evaluated against it (``utils/databricks_ops/evaluation/golden_eval_ka_vs_vsi.py``,
widget ``vsi_variant``). ``chat.py`` calls ``stream_chat_vsi`` from here.
"""

import os
from typing import Any, AsyncGenerator, Dict, List

from . import chat_vsi, chat_vsi_rerank

VARIANTS = {
    'baseline': chat_vsi.stream_chat_vsi,
    'rerank': chat_vsi_rerank.stream_chat_vsi_rerank,
}


def vsi_variant() -> str:
    v = os.getenv('CHAT_VSI_VARIANT', 'baseline').strip().lower()
    return v if v in VARIANTS else 'baseline'


def variant_settings(variant: str | None = None) -> Dict[str, Any]:
    """The settings the variant runs with — saved with evaluation results."""
    v = variant or vsi_variant()
    if v == 'rerank':
        return chat_vsi_rerank.settings()
    return {'variant': 'baseline', 'num_results': chat_vsi.num_results(), 'llm': chat_vsi.llm_endpoint()}


async def retrieve_documents(host: str, token: str, division: str,
                             messages: List[Dict[str, str]]) -> Dict[str, Any]:
    """Only the search step of the selected variant: the passages it would hand to the LLM,
    with no answer generated (retrieval evaluation). Same steps as the variant's own stream.
    Raises ``chat_vsi.ChatVsiError`` on a search failure."""
    div = chat_vsi.normalize_division(division)
    index_name = chat_vsi.index_for_division(div)
    endpoint = chat_vsi.llm_endpoint()
    conversation = chat_vsi._clean_history(messages)
    if vsi_variant() == 'rerank':
        return await chat_vsi_rerank.retrieve_for_turn(host, token, index_name, endpoint, conversation)
    question = chat_vsi._without_date(conversation[-1]['content'])
    fr_query = await chat_vsi.search_query_fr(host, token, endpoint,
                                              conversation[:-1] + [{'role': 'user', 'content': question}])
    rows = await chat_vsi.retrieve(host, token, index_name, [question] + ([fr_query] if fr_query else []),
                                   chat_vsi.num_results())
    return {'question': question, 'fr_query': fr_query, 'rows': rows, 'reranked': False, 'named': []}


def stream_chat_vsi(host: str, token: str, division: str,
                    messages: List[Dict[str, str]]) -> AsyncGenerator[str, None]:
    """The selected variant's stream — same event contract as ``streaming.stream_chat``."""
    return VARIANTS[vsi_variant()](host, token, division, messages)
