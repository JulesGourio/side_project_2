"""Pricing, costs and text-delta parsing shared by the chat and compare LLM calls.

The calls themselves: chat/llm.py and compare/llm.py.
"""

import json
import os
import re
from typing import Dict

# Read timeout per streamed attempt, and connect timeout (compare/llm.py).
DEFAULT_TIMEOUT_S = float(os.getenv('COMPARE_ANALYSIS_TIMEOUT_S', '300'))
DEFAULT_CONNECT_TIMEOUT_S = float(os.getenv('COMPARE_ANALYSIS_CONNECT_TIMEOUT_S', '30'))

_PRICING_USD: Dict[str, Dict[str, float]] = {
    'databricks-claude-haiku-4-5': {'input': 1.0, 'output': 5.0},
    # USD per 1M tokens = Databricks DBU rate x 0.078 EUR/DBU, via _EUR_PER_USD. DBU in/out: Sonnet 5.5 28.571/142.857.
    'databricks-claude-sonnet-5-5': {'input': 2.422, 'output': 12.112},
    'databricks-claude-opus-4-6':   {'input': 15.0, 'output': 75.0},
    # Judge / summary models, DBU in/out: gpt-5-4-mini 21.428/128.572, gpt-5-mini 6.427/28.571, gemini-3-1-flash-lite 6.428/38.572.
    # An unlisted endpoint falls back to _DEFAULT_PRICING.
    'databricks-gpt-5-4-mini':      {'input': 1.817, 'output': 10.901},
    'databricks-gpt-5-mini':        {'input': 0.545, 'output': 2.422},
    'databricks-gemini-3-1-flash-lite': {'input': 0.545, 'output': 3.270},
    # GPT-5.6 Luna: 2.857 / 17.143 DBU.
    'databricks-gpt-5-6-luna':      {'input': 0.242, 'output': 1.453},
    # GPT-6 Luna: 1.428571 / 7.142857 DBU.
    'databricks-gpt-6-luna':        {'input': 0.121, 'output': 0.606},
    # Gemini 3.8 Flash: 10.714285 / 53.571425 DBU.
    'databricks-gemini-3-8-flash':  {'input': 0.908, 'output': 4.542},
}
_DEFAULT_PRICING = {'input': 3.0, 'output': 15.0}
_EUR_PER_USD = float(os.getenv('EUR_PER_USD', '0.92'))

# These families reject any non-default temperature (a 0 sends a 400): GPT-5.6 and GPT-6 (names matching gpt-5-6 / gpt-6),
# bare gpt-5-mini (not gpt-5-4-mini, which accepts 0), and the 5 generation of the Sonnet / Opus / Fable family.
_NO_TEMPERATURE_RE = re.compile(r'gpt-5-6|gpt-6|gpt-5-mini\b|claude-(?:sonnet|opus)-5|claude-fable', re.I)


def supports_temperature(endpoint_name: str) -> bool:
    """Whether the endpoint accepts a temperature other than its default.

    :param endpoint_name: serving endpoint name
    :return: True when the endpoint takes a temperature
    """
    return not _NO_TEMPERATURE_RE.search(endpoint_name)


def _fix_mojibake(text: str) -> str:
    """Repairs UTF-8 text that was decoded as Latin-1, segment by segment.

    :param text: text that may be mis-decoded; None or empty is returned as it is
    :return: the repaired text
    """
    if not text:
        return text
    try:
        return text.encode('latin-1').decode('utf-8')
    except (UnicodeDecodeError, UnicodeEncodeError):
        pass

    def _fix_segment(match) -> str:
        """Decodes one run of Latin-1 characters as UTF-8 when that is possible.

        :param match: match of a run of characters between U+0080 and U+00FF
        :return: the decoded run, or the run unchanged
        """
        seg = match.group(0)
        try:
            return seg.encode('latin-1').decode('utf-8')
        except (UnicodeDecodeError, UnicodeEncodeError):
            return seg

    return re.sub(r'[\u0080-\u00ff]+', _fix_segment, text)


def _cost_eur(endpoint_name: str, input_tokens: int, output_tokens: int) -> float:
    """Cost in euros of one call, from the price per million tokens of its endpoint.

    :param endpoint_name: serving endpoint name; an unlisted endpoint uses the default price
    :param input_tokens: prompt tokens
    :param output_tokens: completion tokens
    :return: the cost, rounded to 6 decimals
    """
    p = _PRICING_USD.get(endpoint_name, _DEFAULT_PRICING)
    return round((input_tokens * p['input'] + output_tokens * p['output']) / 1_000_000 * _EUR_PER_USD, 6)


def _parse_text_delta(raw_data: str) -> str | None:
    """The text of one streamed chat completion chunk, None when the chunk has none.

    :param raw_data: JSON payload of one ``data:`` line
    :return: the text, or None
    """
    try:
        chunk = json.loads(raw_data)
    except json.JSONDecodeError:
        return None
    if chunk.get('object') != 'chat.completion.chunk':
        return None
    choices = chunk.get('choices', [])
    if not choices:
        return None
    content = choices[0].get('delta', {}).get('content')
    if not content:
        return None
    if isinstance(content, list):
        content = ''.join(
            item.get('text', '') if isinstance(item, dict) else item
            for item in content if isinstance(item, (dict, str))
        )
    return _fix_mojibake(content) if content else None
