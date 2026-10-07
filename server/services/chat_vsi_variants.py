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


def stream_chat_vsi(host: str, token: str, division: str,
                    messages: List[Dict[str, str]]) -> AsyncGenerator[str, None]:
    """The selected variant's stream — same event contract as ``streaming.stream_chat``."""
    return VARIANTS[vsi_variant()](host, token, division, messages)
