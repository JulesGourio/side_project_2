"""The chatbot's complete specification: every setting that can change an answer, one key per setting.

``spec()`` is what an MLflow agent version records (one parameter per key) and what its signature is computed from:
any change of index, search size, model, prompt or engine code gives another signature, hence another agent version.
Every ``CHAT_*`` variable the server reads is either a key of the spec or listed in ``OPERATIONAL_VARIABLES``
(``tests/test_chat_spec.py`` fails otherwise).
"""

import hashlib
import json
import os
from pathlib import Path
from typing import Dict

from server.services.chat import config as chat_config
from server.services.chat import llm as chat_llm
from server.services.chat import prompts
from server.services.chat.engine import config as engine_config

ROOT = Path(__file__).resolve().parents[3]

# Variable -> spec key it sets.
SPEC_VARIABLES: Dict[str, str] = {
    'CHAT_INDEX': 'retrieval.index',
    'CHAT_PAIR_RERANK_TOP_K': 'retrieval.top_k_first_message',
    'CHAT_RERANK_TOP_K': 'retrieval.top_k_follow_up',
    'CHAT_PAIR_RAW_TOP_K': 'retrieval.raw_top_k_first_message',
    'CHAT_RAW_TOP_K': 'retrieval.raw_top_k_follow_up',
    'CHAT_RAW_ON': 'retrieval.raw_on',
    'CHAT_MAX_SEARCH_PASSAGES': 'retrieval.max_passages',
    'CHAT_PASSAGE_ORDER': 'retrieval.passage_order',
    'CHAT_TRANSLATE_ONLY': 'retrieval.translate_only',
    'CHAT_STOPWORDS': 'retrieval.stopwords',
    'CHAT_TRANSLATE_BRIDGE_ENABLED': 'retrieval.translate_bridge',
    'CHAT_LLM_ENDPOINT': 'answer.endpoint',
    'CHAT_LLM_FALLBACK_ENDPOINTS': 'answer.fallbacks',
    'CHAT_ANSWER_MAX_TOKENS': 'answer.max_tokens',
    'CHAT_REASONING_MIN_TOKENS': 'answer.reasoning_min_tokens',
    'CHAT_MAX_HISTORY_TOKENS': 'answer.history_max_tokens',
    'CHAT_REWRITE_ENDPOINT': 'rewrite.endpoint',
    'CHAT_REWRITE_FALLBACK_ENDPOINTS': 'rewrite.fallbacks',
    'CHAT_REWRITE_MAX_TOKENS': 'rewrite.max_tokens',
    'CHAT_REWRITE_REASONING_EFFORT': 'rewrite.reasoning_effort',
    'CHAT_TRANSLATE_ENDPOINT': 'translate.endpoint',
    'CHAT_TRANSLATE_FALLBACK_ENDPOINTS': 'translate.fallbacks',
    'CHAT_TRANSLATE_MAX_TOKENS': 'translate.max_tokens',
}

# Variables that change how a turn runs (timeouts, retries, access, logging), never what it answers.
OPERATIONAL_VARIABLES = frozenset({
    'CHAT_ALLOWED_ORIGINS', 'CHAT_ENABLED', 'CHAT_COOLDOWN_S', 'CHAT_FIRST_TOKEN_TIMEOUT_S', 'CHAT_LLM_DEADLINE_S',
    'CHAT_LLM_ROUNDS', 'CHAT_MAX_CONCURRENT_ANSWERS', 'CHAT_REWRITE_TIMEOUT_S', 'CHAT_STALL_TIMEOUT_S',
    'CHAT_MLFLOW_EXPERIMENT', 'CHAT_MLFLOW_EXPERIMENT_ID', 'CHAT_MLFLOW_WAREHOUSE_ID',
})

# Code whose change can change an answer: the engine, the model calls, the prompt loading, the code normalisation.
CODE_FILES = ('server/services/chat/engine/*.py', 'server/services/chat/llm.py', 'server/services/chat/prompts.py',
              'server/services/chat/config.py', 'server/services/chat/translation_bridge.py',
              'server/routers/chat/helpers.py', 'utils/parsing_pipeline/code_normalization.py')


def _list(values) -> str:
    """A list of endpoints or search inputs as one comparable value.

    :param values: the items, in order
    :return: the items joined by ``+``, ``none`` when empty
    """
    return '+'.join(values) or 'none'


def code_fingerprint(root: Path = ROOT) -> str:
    """Short SHA-256 of the code listed in ``CODE_FILES``.

    :param root: folder holding ``server/`` and ``utils/``
    :return: 12 hexadecimal characters
    """
    digest = hashlib.sha256()
    for pattern in CODE_FILES:
        for path in sorted(root.glob(pattern)):
            digest.update(str(path.relative_to(root)).encode('utf-8'))
            digest.update(path.read_bytes())
    return digest.hexdigest()[:12]


def spec(code_normalization: bool = True) -> Dict[str, str]:
    """Every setting that can change an answer, as read now from the environment, one string per key.

    :param code_normalization: whether reference codes are normalised (an evaluation can turn it off)
    :return: ``{"retrieval.index": ..., "answer.endpoint": ..., "prompt.system_base": <hash>, "code.engine": <hash>}``
        in a fixed key order
    """
    answer = engine_config.llm_endpoint()
    values = {
        'retrieval.index': engine_config.index_name(),
        'retrieval.top_k_first_message': engine_config.pair_rerank_top_k(),
        'retrieval.top_k_follow_up': engine_config.rerank_top_k(),
        'retrieval.raw_top_k_first_message': engine_config.pair_raw_top_k(),
        'retrieval.raw_top_k_follow_up': engine_config.raw_top_k(),
        'retrieval.raw_on': _list(engine_config.raw_on()),
        'retrieval.max_passages': engine_config.max_search_passages(),
        'retrieval.passage_order': engine_config.passage_order(),
        'retrieval.translate_only': engine_config.translate_only_enabled(),
        'retrieval.stopwords': engine_config.stopwords_enabled(),
        'retrieval.translate_bridge': chat_config.TRANSLATE_BRIDGE_ENABLED,
        'retrieval.code_normalization': code_normalization,
        'answer.endpoint': answer,
        'answer.fallbacks': _list(chat_llm.answer_chain(answer)[1:]),
        'answer.max_tokens': engine_config.answer_max_tokens(),
        'answer.reasoning_min_tokens': int(chat_llm._env_float('CHAT_REASONING_MIN_TOKENS', 6000)),
        'answer.history_max_tokens': int(os.getenv('CHAT_MAX_HISTORY_TOKENS', '8000')),
        'rewrite.endpoint': engine_config.rewrite_endpoint(),
        'rewrite.fallbacks': _list(chat_llm.rewrite_chain(engine_config.rewrite_endpoint(), answer)[1:]),
        'rewrite.max_tokens': engine_config.rewrite_max_tokens(),
        'rewrite.reasoning_effort': chat_llm.rewrite_reasoning_effort(),
        'translate.endpoint': chat_config.TRANSLATE_ENDPOINT,
        'translate.fallbacks': _list(chat_config.TRANSLATE_FALLBACK_ENDPOINTS),
        'translate.max_tokens': engine_config.translate_max_tokens(),
        **{f'prompt.{key}': prompts.digest(key)[:12] for key in prompts.FILES},
        'code.engine': code_fingerprint(),
    }
    return {key: str(value).lower() if isinstance(value, bool) else str(value) for key, value in values.items()}


def signature(values: Dict[str, str]) -> str:
    """SHA-256 of a spec, keys sorted: equal specs, equal signatures.

    :param values: output of ``spec``
    :return: 64 hexadecimal characters
    """
    return hashlib.sha256(json.dumps(values, sort_keys=True).encode('utf-8')).hexdigest()


def short_label(values: Dict[str, str]) -> str:
    """The search settings that differ most between versions, readable in a name.

    :param values: output of ``spec``
    :return: e.g. ``k32-21`` (top-k first message / follow-ups), ``k32-21-brute`` when the raw search is on
    """
    raw = max(int(values['retrieval.raw_top_k_first_message']), int(values['retrieval.raw_top_k_follow_up']))
    return f"k{values['retrieval.top_k_first_message']}-{values['retrieval.top_k_follow_up']}" + ('-brute' if raw else '')


def version_name(values: Dict[str, str], prefix: str = 'qualibot') -> str:
    """Name of the agent version of a spec: the prefix, the readable settings, then 6 characters of the signature.

    :param values: output of ``spec``
    :param prefix: ``qualibot``, or ``qualibot-<env>`` for the version an environment runs
    :return: e.g. ``qualibot-prod-k32-21-a1b2c3``
    """
    return f'{prefix}-{short_label(values)}-{signature(values)[:6]}'


# Spec key -> (section, what it is). The text of an agent version lists every key of the spec under these headings.
DESCRIPTIONS: Dict[str, tuple] = {
    'retrieval.index': ('Retrieval', 'Vector Search index the passages are searched in'),
    'retrieval.top_k_first_message': ('Retrieval', 'reranked passages kept per query for the first message of a conversation'),
    'retrieval.top_k_follow_up': ('Retrieval', 'reranked passages kept per query for follow-up questions and other languages'),
    'retrieval.raw_top_k_first_message': ('Retrieval', 'raw hybrid (not reranked) passages per query, first message; 0 = no raw search'),
    'retrieval.raw_top_k_follow_up': ('Retrieval', 'raw hybrid passages per query, follow-ups; 0 = no raw search'),
    'retrieval.raw_on': ('Retrieval', 'queries that also get a raw search (question, French rewrite, English rewrite)'),
    'retrieval.max_passages': ('Retrieval', 'cap on the merged search passages, before the reference lookups'),
    'retrieval.passage_order': ('Retrieval', 'order of the merged passages: best reranker score, or best rank'),
    'retrieval.translate_only': ('Retrieval', 'true = the first message is only translated into the other language (two queries); false = bilingual rewrite'),
    'retrieval.stopwords': ('Retrieval', 'true = French and English stop words removed from the keyword side of the queries'),
    'retrieval.translate_bridge': ('Retrieval', 'true = the question is translated to English before the search'),
    'retrieval.code_normalization': ('Retrieval', 'true = document codes (REF, SF1027 style) normalised in queries and lookups'),
    'answer.endpoint': ('Answer', 'serving endpoint that writes the answer'),
    'answer.fallbacks': ('Answer', 'endpoints tried in order when the answer endpoint fails'),
    'answer.max_tokens': ('Answer', 'token budget of the answer (reasoning included)'),
    'answer.reasoning_min_tokens': ('Answer', 'minimum budget given to a reasoning model'),
    'answer.history_max_tokens': ('Answer', 'tokens of conversation history sent to the model'),
    'rewrite.endpoint': ('Rewrite', 'serving endpoint that rewrites the question'),
    'rewrite.fallbacks': ('Rewrite', 'endpoints tried in order when the rewrite endpoint fails'),
    'rewrite.max_tokens': ('Rewrite', 'token budget of the rewrite'),
    'rewrite.reasoning_effort': ('Rewrite', 'reasoning effort asked of the rewrite model; empty = none'),
    'translate.endpoint': ('Translation', 'serving endpoint that translates the question'),
    'translate.fallbacks': ('Translation', 'endpoints tried in order when the translation endpoint fails'),
    'translate.max_tokens': ('Translation', 'token budget of the translation'),
    'code.engine': ('Code', 'fingerprint of the engine, model-call, prompt-loading and code-normalisation source files'),
    'eval.fusion': ('Evaluation only', 'merge of the result lists: rrf = reciprocal rank fusion (the app merges by best rank)'),
    'eval.ann_bm25_split': ('Evaluation only', 'BM25 weight when each hybrid query is split into an embeddings query and a keyword query'),
}
for _key in prompts.FILES:
    DESCRIPTIONS[f'prompt.{_key}'] = ('Prompts', f'SHA-256 prefix of the prompt file `{prompts.FILES[_key]}` (registered as chat_{_key})')


def describe(values: Dict[str, str], title: str = '') -> str:
    """The whole configuration as text, in English, one line per setting, for an agent version's description.

    :param values: output of ``spec`` (an evaluation may add ``eval.*`` keys)
    :param title: first line, e.g. the version name; empty for none
    :return: markdown: a heading per section (Retrieval, Answer, Rewrite, Translation, Prompts, Code), then ``- key = value: what it is``
        for every key of ``values``
    """
    sections: Dict[str, list] = {}
    for key, value in values.items():
        section, meaning = DESCRIPTIONS.get(key, ('Other', 'setting of the spec'))
        sections.setdefault(section, []).append(f'- `{key}` = `{value}`: {meaning}')
    lines = [f'# {title}', ''] if title else []
    for section in ('Retrieval', 'Answer', 'Rewrite', 'Translation', 'Prompts', 'Code', 'Evaluation only', 'Other'):
        if section in sections:
            lines += [f'## {section}', *sections[section], '']
    return '\n'.join(lines).rstrip()
