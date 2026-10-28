"""One spelling for the reference codes (utils/parsing_pipeline/code_normalization.py), on the passages and the queries."""

import asyncio
import os
import sys

import httpx
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'utils', 'parsing_pipeline'))
for key, value in {'PIPELINE_WRITE_ENV': 'dev', 'REFERENCE_READ_ENV': 'ref', 'PARSING_IMAGES_VOLUME': 'img'}.items():
    os.environ.setdefault(key, value)

import code_normalization as cn  # noqa: E402
import config  # noqa: E402


@pytest.mark.parametrize('written', ['SF-1027', 'SF1027', 'SF 1027', 'SF_1027', 'SF.1027', 'SF-1027_BG', 'SF1027_GB'])
def test_every_spelling_of_a_code_has_the_same_canonical_form(written):
    assert cn.canonical_codes(f'voir {written} pour la formation') == ['SF1027']


def test_the_canonical_form_follows_the_code_as_written():
    assert cn.normalize_codes('voir SF-1027 et SF 1027.') == 'voir SF-1027 [SF1027] et SF 1027 [SF1027].'


def test_a_code_already_in_its_canonical_spelling_is_left_alone():
    assert cn.normalize_codes('voir SF1027 et QP2163') == 'voir SF1027 et QP2163'


def test_normalizing_twice_changes_nothing():
    once = cn.normalize_codes('SF-1027, DLV-33-3010 et IAQ 10 01 01')
    assert cn.normalize_codes(once) == once


def test_the_language_suffix_stays_with_the_code_and_out_of_the_canonical_form():
    assert cn.normalize_codes('[Source: SF-1027_BG | Title: FAI]') == '[Source: SF-1027_BG [SF1027] | Title: FAI]'


def test_codes_of_several_number_groups():
    assert cn.canonical_codes('DLV-37-9046, QIS-46B-147 D, TDA7-00-062, IAQ 10 01 01') == \
        ['DLV379046', 'QIS46B147', 'TDA700062', 'IAQ100101']


def test_a_space_separates_number_groups_only_where_the_catalog_does():
    # DLV writes its groups with dashes: the page count after it is not part of the code.
    assert cn.canonical_codes('DLV-2018 12 pages') == ['DLV2018']
    assert cn.canonical_codes('IAQ 10 01 01') == ['IAQ100101']


def test_a_number_longer_than_the_family_allows_is_not_a_code():
    assert cn.find_codes('MI-140470') == []


def test_a_code_inside_a_longer_word_is_not_a_code():
    assert cn.find_codes('XSF-1027 et SF-1027A1') == []


def test_words_that_a_number_follows_are_not_codes():
    assert cn.find_codes('GB 128 et IN 300') == []


def test_a_lower_case_prefix_is_a_code_in_a_query_only():
    assert cn.normalize_codes('cité dans sf-1027 ?') == 'cité dans sf-1027 ?'
    assert cn.normalize_query('cité dans sf-1027 ?') == 'cité dans sf-1027 [SF1027] ?'


def test_a_text_without_code_is_returned_as_it_is():
    text = 'Quelle est la procédure de qualification ?'
    assert cn.normalize_codes(text) is text
    assert cn.normalize_codes('') == ''


def test_hand_written_patterns_add_codes_the_catalog_does_not_have(monkeypatch):
    monkeypatch.setattr(cn, 'EXTRA_PATTERNS', [r'(?i)\bEN[-_ ]?9100\b'])
    assert cn.normalize_codes('selon EN 9100 et EN9100') == 'selon EN 9100 [EN9100] et EN9100'


def test_families_are_generated_from_the_catalog_refs():
    refs = ['SF-1027_BG', 'SF-1154', 'SF-20316', 'DLV-33-3010', 'DLV-2018', 'IAQ 10 01 01', 'IAQ 06 00 01',
            'Q0102QP_GB', 'Q0170MI', 'LdB P-139', 'GB-003', 'GB-004', 'ONE-1234']
    assert cn.families_from_refs(refs) == {
        'DLV': cn.CodeFamily(2, 4, 2, False),
        'IAQ': cn.CodeFamily(2, 2, 3, True),
        'SF': cn.CodeFamily(4, 5, 1, False),
    }


def test_the_rendered_families_are_the_python_of_the_module():
    families = cn.families_from_refs(['SF-1027', 'SF-1154', 'IAQ 10 01 01', 'IAQ 06 00 01'])
    namespace = {'Dict': dict, 'CodeFamily': cn.CodeFamily}
    exec(cn.render_families(families), namespace)
    assert namespace['CODE_FAMILIES'] == families


def test_the_module_is_shipped_to_the_executors():
    assert 'code_normalization.py' in config.WORKER_MODULES


def test_the_app_and_the_parsing_pipeline_run_the_same_rules():
    from utils.parsing_pipeline import code_normalization as app_side
    assert app_side.CODE_FAMILIES == cn.CODE_FAMILIES
    assert app_side.normalize_query('SF-1027') == 'SF-1027 [SF1027]'


def _capture_payloads(monkeypatch):
    sent = []

    async def _post(self, url, json=None, headers=None, timeout=None):
        sent.append(json)
        body = {'manifest': {'columns': [{'name': 'chunk_id'}, {'name': 'chunk_text'}]}, 'result': {'data_array': []}}
        return httpx.Response(200, json=body, request=httpx.Request('POST', url))

    monkeypatch.setattr(httpx.AsyncClient, 'post', _post)
    return sent


def test_the_chat_searches_the_canonical_form_of_the_codes(monkeypatch):
    from server.services.chat.engine import search
    sent = _capture_payloads(monkeypatch)
    asyncio.run(search._query('https://h', 't', 'idx', 'dans quel document SF-1027 est cité', 5, {}, rerank=False))
    assert sent[0]['query_text'] == 'dans quel document SF-1027 [SF1027] est cité'


def test_the_impact_search_searches_the_canonical_form_of_the_codes(monkeypatch):
    from server.services.infra import vector_search
    sent = _capture_payloads(monkeypatch)
    asyncio.run(vector_search._fetch_chunks('https://h', 't', 'idx', 'NS-10441 ajoutée', 5))
    assert sent[0]['query_text'] == 'NS-10441 [NS10441] ajoutée'
