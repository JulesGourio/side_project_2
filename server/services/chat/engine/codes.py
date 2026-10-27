"""Codes named in a question (form, part, standard or document numbers) and the passages that cite them verbatim.

The hybrid search splits ``SF-1027`` into ``SF`` and ``1027``, so a passage that cites the code exactly can rank below
passages that only share one of the parts. Parsing task ``6_update_kb_metadata`` writes, for each code found in the
chunks, the passages that cite it (Lakebase ``chunk_codes``); the turn adds those passages to the search results.
"""

import logging
import re
from typing import Dict, List

import asyncpg

from server.services.infra.lakebase import get_pool

logger = logging.getLogger(__name__)

# Same pattern and bounds as the parsing pipeline's codes.py, which extracts the codes of the chunks.
CODE_PATTERN = r'(?<![A-Za-z0-9])(?:[A-Za-z0-9]+(?:[-_][A-Za-z0-9]+)+|[A-Za-z]{2,}[0-9]{3,})(?![A-Za-z0-9])'
CODE_MIN_CHARS = 5
CODE_MAX_CHARS = 40
_CODE_RE = re.compile(CODE_PATTERN)
_LETTER_RE = re.compile(r'[A-Za-z]')
_DIGIT_RE = re.compile(r'[0-9]')

MAX_CODES = 4                   # codes of one question looked up
CODE_LOOKUP_K = 8               # passages fetched for the codes of one question
_LOOKUP_TIMEOUT_S = 3.0


def is_code(token: str) -> bool:
    """Whether a token matched by ``CODE_PATTERN`` is kept as a code.

    :param token: one match of the pattern
    :return: true when it has a letter, a digit and a length within the bounds
    """
    return (CODE_MIN_CHARS <= len(token) <= CODE_MAX_CHARS and bool(_LETTER_RE.search(token))
            and bool(_DIGIT_RE.search(token)))


def codes_in(text: str, exclude: List[str] = ()) -> List[str]:
    """Codes of the text, upper-cased, in order of appearance, without duplicates (at most ``MAX_CODES``).

    :param text: the question
    :param exclude: codes left out (the REFs the REF lookup already covers), any case
    :return: the codes
    """
    skip = {c.upper() for c in exclude}
    found = [m.group(0).upper() for m in _CODE_RE.finditer(text or '') if is_code(m.group(0))]
    return [c for c in dict.fromkeys(found) if c not in skip][:MAX_CODES]


async def chunk_ids_citing(codes: List[str]) -> List[str]:
    """Passages that cite the codes, the rarest code's first (at most ``CODE_LOOKUP_K``).

    :param codes: upper-cased codes
    :return: chunk ids; empty when Lakebase or the table is unavailable
    """
    pool = get_pool()
    if pool is None or not codes:
        return []
    try:
        async with pool.acquire(timeout=_LOOKUP_TIMEOUT_S) as conn:
            rows = await conn.fetch('SELECT code, chunk_ids FROM chunk_codes WHERE code = ANY($1::text[])', codes,
                                    timeout=_LOOKUP_TIMEOUT_S)
    except asyncpg.UndefinedTableError:
        # The parsing task creates the table on its first run.
        logger.info('chat: no chunk_codes table yet, code lookup skipped')
        return []
    by_code: Dict[str, List[str]] = {r['code']: list(r['chunk_ids'] or []) for r in rows}
    ids: List[str] = []
    for code in sorted(by_code, key=lambda c: len(by_code[c])):
        ids.extend(i for i in by_code[code] if i not in ids)
    return ids[:CODE_LOOKUP_K]
