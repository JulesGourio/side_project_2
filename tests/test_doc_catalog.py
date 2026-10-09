"""Tests for the document catalog's source (server/services/doc_catalog.py).

Coverage:
  - the Lakebase table replaces the bundled snapshot once loaded
  - an empty or unreadable table keeps the catalog already loaded
  - the title lookup only proposes documents with passages in the chat index (in_chat)
  - the title index is rebuilt when a refresh brings a new catalog
  - the link written by the pipeline (task 6) is the same as the chunks' url column
"""

import asyncio
import os
import re
from unittest.mock import AsyncMock, MagicMock

import pytest

from server.services import chat_vsi_titles, doc_catalog

ROOT = os.path.join(os.path.dirname(__file__), '..')


def _pool(rows=None, exc=None):
    conn = MagicMock()
    conn.fetch = AsyncMock(side_effect=exc) if exc else AsyncMock(return_value=rows or [])
    pool = MagicMock()
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)
    return pool


def _row(ref, title, in_chat=True):
    return {'ref': ref, 'url': f'https://intraqual/{ref}', 'title': title, 'base_ref': None, 'in_chat': in_chat}


# Rarity (IDF) only means something in a real-sized catalog: 300 unrelated titles around the tested ones.
_FILLER = [_row(f'FX-{i:04d}', f'Procédure atelier numéro {i} contrôle qualité') for i in range(300)]


@pytest.fixture(autouse=True)
def _reset_live():
    saved = doc_catalog._live
    doc_catalog._live = None
    yield
    doc_catalog._live = saved


def test_lakebase_rows_replace_the_bundled_snapshot():
    loaded = asyncio.run(doc_catalog.refresh_from_lakebase(_pool([_row('ZZ-9001_FR', 'Gestion des gabarits')])))
    assert loaded == 1
    assert doc_catalog.title_for_ref('ZZ-9001_FR') == 'Gestion des gabarits'
    assert doc_catalog.title_for_ref('MI-1000') == ''          # bundled snapshot no longer used
    assert doc_catalog._catalog().by_canon['ZZ9001'][0]['ref'] == 'ZZ-9001_FR'   # base_ref derived by the app


@pytest.mark.parametrize('pool', [_pool([]), _pool(exc=OSError('connection refused'))])
def test_empty_or_unreadable_table_keeps_the_current_catalog(pool):
    asyncio.run(doc_catalog.refresh_from_lakebase(_pool([_row('ZZ-9001', 'Gestion des gabarits')])))
    assert asyncio.run(doc_catalog.refresh_from_lakebase(pool)) == 0
    assert doc_catalog.title_for_ref('ZZ-9001') == 'Gestion des gabarits'


def test_title_lookup_skips_documents_outside_the_chat_index():
    doc_catalog.set_catalog(_FILLER + [_row('ZZ-9001', 'Gestion des gabarits de perçage'),
                                       _row('ZZ-9002', 'Gabarits de perçage archivés', in_chat=False)])
    found = chat_vsi_titles.documents_titled(['gabarits de perçage'], 3)
    assert [canon for canon, _, _ in found] == ['ZZ9001']


def test_title_index_follows_a_refresh():
    doc_catalog.set_catalog(_FILLER + [_row('ZZ-9001', 'Gestion des gabarits de perçage')])
    assert chat_vsi_titles.documents_titled(['gabarits de perçage'], 3)
    doc_catalog.set_catalog(_FILLER + [_row('ZZ-9003', 'Plan de surveillance des fours')])
    assert chat_vsi_titles.documents_titled(['gabarits de perçage'], 3) == []
    assert [c for c, _, _ in chat_vsi_titles.documents_titled(['surveillance des fours'], 3)] == ['ZZ9003']


def test_pipeline_link_matches_the_chunks_url():
    utils_src = open(os.path.join(ROOT, 'utils', 'parsing_pipeline', 'utils.py'), encoding='utf-8').read()
    task_src = open(os.path.join(ROOT, 'utils', 'parsing_pipeline', '6_Update_Knowledge_Base_Metadata.py'),
                    encoding='utf-8').read()
    in_utils = re.search(r'_INTRAQUAL_REF_URL_BASE = "([^"]+)"', utils_src).group(1)
    in_task = re.search(r'INTRAQUAL_REF_URL_BASE = "([^"]+)"', task_src).group(1)
    assert in_task == in_utils


def test_document_info_and_sources_carry_the_revision():
    from datetime import date
    from server.services import doc_catalog
    doc_catalog.set_catalog([{'ref': 'QP-1518', 'url': 'u', 'title': 'NDT', 'revision': 'D',
                              'doc_date': date(2024, 10, 11)}])
    try:
        assert doc_catalog.document_info('QP-1518') == {'title': 'NDT', 'revision': 'D', 'doc_date': '2024-10-11'}
        assert doc_catalog.document_info('XX-1') == {}
        sources = doc_catalog.with_document_info([{'title': 'QP-1518', 'url': 'u', 'n': 1}, {'title': 'XX-1'}])
        assert sources == [{'title': 'QP-1518', 'url': 'u', 'n': 1, 'doc_title': 'NDT', 'revision': 'D',
                            'doc_date': '2024-10-11'}, {'title': 'XX-1'}]
    finally:
        doc_catalog._live = None
