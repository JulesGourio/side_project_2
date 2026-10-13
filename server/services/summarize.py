"""Single-document summarization — /compare/summarize.

Independent of the diff analysis: summarizes one uploaded document (old or
new) with a single cheap LLM call, so a user can get the gist of either file
without running a comparison.
"""

import logging
from typing import Any, Dict, List, Union

import httpx

from .streaming import _cost_eur, supports_temperature

logger = logging.getLogger(__name__)

_LLM_TIMEOUT_S = 60.0

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
) -> Dict[str, Any]:
    """One non-streaming chat-completion call; returns the parsed JSON response."""
    url = f'{host}/serving-endpoints/{llm_endpoint}/invocations'
    payload: Dict[str, Any] = {
        'messages': [
            {'role': 'system', 'content': system_prompt},
            {'role': 'user', 'content': user_content},
        ],
        'max_tokens': max_tokens,
    }
    if supports_temperature(llm_endpoint):
        payload['temperature'] = 0.0
    headers = {'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'}

    async with httpx.AsyncClient(timeout=_LLM_TIMEOUT_S) as client:
        resp = await client.post(url, json=payload, headers=headers)
        resp.raise_for_status()
        return resp.json()


def _usage_from_completion(completion: Dict[str, Any], llm_endpoint: str) -> Dict[str, Any]:
    usage = completion.get('usage') or {}
    input_tokens = usage.get('prompt_tokens', 0) or 0
    output_tokens = usage.get('completion_tokens', 0) or 0
    total_tokens = usage.get('total_tokens', input_tokens + output_tokens) or (input_tokens + output_tokens)
    return {
        'input_tokens': input_tokens,
        'output_tokens': output_tokens,
        'total_tokens': total_tokens,
        'cost_eur': _cost_eur(llm_endpoint, input_tokens, output_tokens),
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

    Raises on network/HTTP failure - the caller is expected to catch and
    report it as an error response.
    """
    truncated = len(text) > max_chars
    text = text[:max_chars]

    completion = await _call_llm(host, token, llm_endpoint, _SUMMARY_SYSTEM_PROMPT, text, max_tokens)
    summary = (completion.get('choices', [{}])[0].get('message', {}).get('content', '') or '').strip()

    return {
        'summary': summary,
        'truncated': truncated,
        'usage': _usage_from_completion(completion, llm_endpoint),
    }


async def summarize_image(
    host: str,
    token: str,
    llm_endpoint: str,
    content_blocks: List[Dict[str, Any]],
    max_tokens: int,
) -> Dict[str, Any]:
    """Describe a single image with one vision LLM call.

    Raises on network/HTTP failure - the caller is expected to catch and
    report it as an error response.
    """
    completion = await _call_llm(host, token, llm_endpoint, _SUMMARY_IMAGE_SYSTEM_PROMPT, content_blocks, max_tokens)
    summary = (completion.get('choices', [{}])[0].get('message', {}).get('content', '') or '').strip()

    return {
        'summary': summary,
        'truncated': False,
        'usage': _usage_from_completion(completion, llm_endpoint),
    }
