"""Documents citing each reference code: the exact answer to "which documents cite SF-1027?".

A similarity search returns the passages closest to a question, never the complete list of the documents that cite a
code. The Lakebase table ``doc_references`` holds that list: one row per (code, citing document), the code in its
canonical form (``SF1027``, see ``utils/parsing_pipeline/code_normalization.py``), rewritten by parsing task
``6_update_kb_metadata`` after each daily run. The app loads it at startup and every ``DOC_CATALOG_REFRESH_S`` seconds
(``references_refresher``), like the document catalog. Until the table has rows every lookup returns nothing.
"""

import asyncio
import logging
from typing import Any, Dict, List

from server.services.infra.doc_catalog import _REFRESH_S, document_info

logger = logging.getLogger(__name__)

# code -> documents citing it (the table loaded from Lakebase; empty until the first successful load).
_live: Dict[str, List[Dict[str, Any]]] = {}


def set_references(rows: List[Dict[str, Any]]) -> None:
    """Replace the live references.

    :param rows: ``doc_references`` rows as dicts: ``cited_code``, ``citing_ref``, ``division``, ``passages``
    """
    global _live
    by_code: Dict[str, List[Dict[str, Any]]] = {}
    for r in rows:
        by_code.setdefault(r['cited_code'], []).append(
            {'ref': r['citing_ref'], 'division': r.get('division'), 'passages': r.get('passages') or 0})
    _live = by_code


def loaded() -> bool:
    """Whether the references are loaded: until then "cited by no document" cannot be told from "not loaded yet".

    :return: True once a non-empty ``doc_references`` was loaded
    """
    return bool(_live)


def citing_documents(code: str, division: str = 'ALL') -> List[Dict[str, Any]]:
    """The documents citing a code, most recently published first, with their catalog title, revision and date.

    :param code: canonical code, e.g. ``SF1027``
    :param division: ``ALL``, or ``AS`` / ``IS``: that division's documents and the shared ones (``GROUP``)
    :return: dicts with ``ref``, ``division``, ``passages``, ``title``, ``revision``, ``doc_date`` (the last three
        when the catalog has them); empty when no document cites the code or the table is not loaded
    """
    keep = None if division == 'ALL' else {division, 'GROUP'}
    docs = [{**d, **document_info(d['ref'])} for d in _live.get(code, [])
            if keep is None or d['division'] in keep]
    return sorted(docs, key=lambda d: (str(d.get('doc_date') or ''), d['ref']), reverse=True)


async def refresh_from_lakebase(pool) -> int:
    """Load the Lakebase table ``doc_references``. Returns the number of rows loaded; 0 (live references left as they
    are) when the table is empty, missing or unreadable.

    :param pool: database pool with an ``acquire()`` context manager
    :return: the number of rows loaded
    """
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch('SELECT cited_code, citing_ref, division, passages FROM doc_references')
    except Exception as exc:  # noqa: BLE001 — keep the references we have
        logger.warning('doc_references: Lakebase read failed, keeping the current references: %s', exc)
        return 0
    if not rows:
        logger.warning('doc_references: Lakebase table doc_references is empty — run the parsing pipeline '
                       '(rechunk, then task 6_update_kb_metadata) to fill it')
        return 0
    set_references([dict(r) for r in rows])
    logger.info('doc_references: loaded %d rows (%d codes) from Lakebase', len(rows), len(_live))
    return len(rows)


async def references_refresher(get_pool) -> None:
    """Background task: reload the references from Lakebase now, then every DOC_CATALOG_REFRESH_S.

    :param get_pool: callable returning the current pool, or None before it exists
    """
    while True:
        pool = get_pool()
        if pool is not None:
            await refresh_from_lakebase(pool)
        await asyncio.sleep(_REFRESH_S)
