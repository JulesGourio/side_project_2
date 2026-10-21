"""Single-document summarization — /compare/summarize.

Independent of the diff analysis: summarizes one uploaded document (old or
new) with a single cheap LLM call, so a user can get the gist of either file
without running a comparison.
"""

import logging
from typing import Any, Dict, List, Tuple, Union

from server.services.infra.streaming import _cost_eur
from server.services.compare.config import SUMMARY_LLM_TIMEOUT_S
from server.services.compare.llm import complete_chain, compare_chain, message_text

logger = logging.getLogger(__name__)

_SUMMARY_SYSTEM_PROMPT = """\
You are a technical documentation assistant. Summarize the document text given to you: its purpose \
and scope, the key requirements or content it specifies, and its overall structure. Be concise and \
factual - no filler, no meta-commentary about the summary itself or how it was produced. Plain \
Markdown, roughly 150-300 words, at most a couple of "##" subheadings and only if genuinely useful \
for a long or multi-part document.

LANGUAGE: write the summary in the SAME language as the document text given to you. Do not switch \
to English when the document is not in English.\
"""

_SUMMARY_IMAGE_SYSTEM_PROMPT = """\
You are a technical documentation assistant. Describe this image concisely: what it depicts (photo, \
diagram, schematic, table, chart...), the key labeled elements, values, or callouts visible, and its \
likely purpose in a technical document. Be factual - describe only what is visible, no speculation, \
no meta-commentary about the description itself. Plain Markdown, roughly 100-200 words.

LANGUAGE: if the image contains readable text (labels, headers, table content), write the \
description in that SAME language. Do not switch to English when the visible text is not in English.\
"""


async def _call_llm(
    host: str,
    token: str,
    llm_endpoint: str,
    system_prompt: str,
    user_content: Union[str, List[Dict[str, Any]]],
    max_tokens: int,
) -> Tuple[Dict[str, Any], str]:
    """One non-streaming call through the endpoint chain; returns (completion, endpoint that answered).

    :param host: Databricks workspace URL
    :param token: Databricks access token
    :param llm_endpoint: primary model endpoint
    :param system_prompt: system prompt
    :param user_content: text, or content blocks, of the user message
    :param max_tokens: output budget
    :return: ``(completion, endpoint)``
    """
    messages = [
        {'role': 'system', 'content': system_prompt},
        {'role': 'user', 'content': user_content},
    ]
    return await complete_chain(host, token, compare_chain(llm_endpoint), messages, max_tokens, SUMMARY_LLM_TIMEOUT_S)


def _usage_from_completion(completion: Dict[str, Any], endpoint: str) -> Dict[str, Any]:
    """Token counts and cost of one completion, from its usage block.

    :param completion: completion returned by the endpoint
    :param endpoint: endpoint that produced it, for the cost
    :return: the token counts, total and cost in euros
    """
    usage = completion.get('usage') or {}
    input_tokens = usage.get('prompt_tokens', 0) or 0
    output_tokens = usage.get('completion_tokens', 0) or 0
    total_tokens = usage.get('total_tokens', input_tokens + output_tokens) or (input_tokens + output_tokens)
    return {
        'input_tokens': input_tokens,
        'output_tokens': output_tokens,
        'total_tokens': total_tokens,
        'cost_eur': _cost_eur(endpoint, input_tokens, output_tokens),
    }


async def summarize_text(
    host: str,
    token: str,
    llm_endpoint: str,
    text: str,
    max_chars: int,
    max_tokens: int,
) -> Dict[str, Any]:
    """Summarize a single document's extracted text with one LLM call.

    Raises CompareLlmUnavailable when every endpoint of the chain failed - the caller reports it as an error response.

    :param host: Databricks workspace URL
    :param token: Databricks access token
    :param llm_endpoint: primary model endpoint
    :param text: text extracted from the document
    :param max_chars: longest text sent to the model; the rest is cut
    :param max_tokens: output budget
    :return: dict with ``summary``, ``truncated``, ``endpoint`` and ``usage``
    """
    truncated = len(text) > max_chars
    text = text[:max_chars]

    completion, endpoint = await _call_llm(host, token, llm_endpoint, _SUMMARY_SYSTEM_PROMPT, text, max_tokens)
    summary = message_text(completion['choices'][0]['message']).strip()

    return {
        'summary': summary,
        'truncated': truncated,
        'endpoint': endpoint,
        'usage': _usage_from_completion(completion, endpoint),
    }


async def summarize_image(
    host: str,
    token: str,
    llm_endpoint: str,
    content_blocks: List[Dict[str, Any]],
    max_tokens: int,
) -> Dict[str, Any]:
    """Describe a single image with one vision LLM call.

    Raises CompareLlmUnavailable when every endpoint of the chain failed - the caller reports it as an error response.

    :param host: Databricks workspace URL
    :param token: Databricks access token
    :param llm_endpoint: primary model endpoint
    :param content_blocks: user content blocks, the image among them
    :param max_tokens: output budget
    :return: dict with ``summary``, ``truncated``, ``endpoint`` and ``usage``
    """
    completion, endpoint = await _call_llm(host, token, llm_endpoint, _SUMMARY_IMAGE_SYSTEM_PROMPT, content_blocks, max_tokens)
    summary = message_text(completion['choices'][0]['message']).strip()

    return {
        'summary': summary,
        'truncated': False,
        'endpoint': endpoint,
        'usage': _usage_from_completion(completion, endpoint),
    }
