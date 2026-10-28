"""One spelling for the codes (utils/parsing_pipeline/code_normalization.py), on the passages and the queries."""

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

EXAMPLES = [(p.pattern_id, text, code) for p in cn.CODE_PATTERNS for text, code in p.examples]
COUNTER_EXAMPLES = [(p.pattern_id, text) for p in cn.CODE_PATTERNS for text in p.counter_examples]


# --- The examples written with each pattern ---

@pytest.mark.parametrize('pattern_id, text, code', EXAMPLES)
def test_every_pattern_finds_its_examples(pattern_id, text, code):
    assert [(m.canonical, m.pattern_id) for m in cn.match_codes(text)] == [(code, pattern_id)]


@pytest.mark.parametrize('pattern_id, text', COUNTER_EXAMPLES)
def test_no_pattern_finds_its_counter_examples(pattern_id, text):
    assert pattern_id not in {m.pattern_id for m in cn.match_codes(text)}


def test_pattern_ids_are_unique():
    ids = [p.pattern_id for p in cn.CODE_PATTERNS]
    assert len(ids) == len(set(ids))


# --- Spellings of one code ---

@pytest.mark.parametrize('written', ['SF-1027', 'SF1027', 'SF 1027', 'SF_1027', 'SF.1027', 'SF-1027_BG', 'SF1027_GB',
                                     'SF‑1027', 'SF – 1027', 'SF -1027', 'SF-1027_Fr'])
def test_every_spelling_of_a_code_has_the_same_canonical_form(written):
    assert cn.canonical_codes(f'voir {written} pour la formation') == ['SF1027']


@pytest.mark.parametrize('text', [
    'LEVEL 2', 'page 12', '4.2.1', '15/03/2024', 'cost 1027', 'GB 128 et IN 300', '145.A.30', 'PTD-TEST-6-A-01',
    'Manual_Rev2025.11_v2.0', 'A/B', 'N/A', 'R 0,55', 'DA 9.10', 'STRUCTURE 1000', 'Partie 3', 'a 12.5', 'EN 2024',
    'lines of 10 holes', 'Le 23/01/2018', 'F7XC535556349NUE', 'il y a un tableau', 'QP-1518.5',
])
def test_no_code_in_ordinary_text(text):
    assert cn.find_codes(text) == []


def test_a_space_separates_number_groups_only_where_the_catalog_does():
    assert cn.canonical_codes('DLV-2018 12 pages') == ['DLV2018']
    assert cn.canonical_codes('IAQ 10 01 01') == ['IAQ100101']


def test_a_code_with_more_groups_than_another_is_read_whole():
    assert cn.canonical_codes('IP-029-005 et IP-029') == ['IP029005', 'IP029']


def test_two_codes_glued_by_an_underscore_are_two_codes():
    assert cn.canonical_codes('IF20037_DLV-31-5207') == ['IF20037', 'DLV315207']


# --- Questions ---

def test_a_lower_case_ref_is_a_code_in_a_question_only():
    assert cn.normalize_codes('cité dans sf-1027 ?') == 'cité dans sf-1027 ?'
    assert cn.normalize_query('cité dans sf-1027 ?') == 'cité dans sf-1027 [SF1027] ?'
    assert cn.normalize_query('cité dans sf 1027 ?') == 'cité dans sf 1027 [SF1027] ?'


@pytest.mark.parametrize('text', ['if 2 parts', 'go 2024', 'mo 2024 et va 3', 'in 300 cases', 'le 23 mars'])
def test_words_in_lower_case_are_not_codes_in_a_question(text):
    assert cn.find_codes(text, accept_lowercase=True) == []


def test_standards_and_aircraft_keep_their_case_in_a_question():
    assert cn.find_codes('en 9100 et a 320', accept_lowercase=True) == []


# --- Annotation ---

def test_the_canonical_form_follows_the_code_as_written():
    assert cn.normalize_codes('voir SF-1027 et SF 1027.') == 'voir SF-1027 [SF1027] et SF 1027 [SF1027].'
    assert cn.normalize_codes('IP-029-005 ajouté') == 'IP-029-005 [IP029005] ajouté'


def test_the_canonical_form_follows_a_glued_word():
    assert cn.normalize_codes('MI-13991Table 3') == 'MI-13991Table [MI13991] 3'


def test_the_language_suffix_stays_with_the_code_and_out_of_the_canonical_form():
    assert cn.normalize_codes('[Source: SF-1027_BG | Title: FAI]') == '[Source: SF-1027_BG [SF1027] | Title: FAI]'
    assert cn.normalize_codes('SF-1027 EN 9100') == 'SF-1027 [SF1027] EN 9100 [EN9100]'


def test_a_code_already_in_its_canonical_spelling_is_left_alone():
    assert cn.normalize_codes('voir SF1027, Q0375MI et QP2163.') == 'voir SF1027, Q0375MI et QP2163.'


def test_normalizing_twice_changes_nothing():
    once = cn.normalize_codes('SF-1027, DLV-33-3010, IAQ 10 01 01, QTP 01-174, IF2.03.08_CZ, LdB IT-243 ; EN 9100')
    assert cn.normalize_codes(once) == once
    assert cn.normalize_query(cn.normalize_query('sf 1027')) == 'sf 1027 [SF1027]'


def test_a_text_without_code_is_returned_as_it_is():
    text = 'Quelle est la procédure de qualification ?'
    assert cn.normalize_codes(text) is text
    assert cn.normalize_codes('') == ''
    assert cn.find_codes(None) == []


def test_cited_codes_leave_out_the_source_line_and_the_passages_own_document():
    text = '[Source: Q0697MI_FR | Title: Contrôle]\nAppliquer Q0697MI, SF-1027 et MI - 1365.'
    assert cn.cited_codes(text, 'Q0697MI_FR') == ['SF1027', 'MI1365']


# --- Coverage of the catalog and generation of the families ---

def test_unmatched_refs_lists_the_refs_no_pattern_reads():
    assert cn.unmatched_refs(['SF-1029', 'QIS-46A-352 A', 'LdB IT-243', 'Q0697MI_FR', 'ZZ-0001', '']) == ['ZZ-0001']


def test_families_are_generated_from_the_catalog_refs():
    refs = ['SF-1027_BG', 'SF-1154', 'SF-20316', 'DLV-33-3010', 'DLV-2018', 'IAQ 10 01 01', 'IAQ 06 00 01',
            'Q0102QP_GB', 'Q0170MI', 'LdB P-139', 'GB-003', 'GB-004', 'ONE-1234', 'IF-20316', 'IF-20322']
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


# --- Integration ---

def test_the_module_is_shipped_to_the_executors():
    assert 'code_normalization.py' in config.WORKER_MODULES


def test_the_app_and_the_parsing_pipeline_run_the_same_rules():
    from utils.parsing_pipeline import code_normalization as app_side
    assert app_side.CODE_FAMILIES == cn.CODE_FAMILIES
    assert app_side.NORMALIZATION_VERSION == cn.NORMALIZATION_VERSION
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


def test_the_preview_notebook_holds_the_same_code():
    root = os.path.join(os.path.dirname(__file__), '..')
    with open(os.path.join(root, 'utils', 'parsing_pipeline', 'code_normalization.py'), encoding='utf-8') as fh:
        module = fh.read()
    with open(os.path.join(root, 'runbooks', 'retrieval', 'f3_dictionnaire_codes.py'), encoding='utf-8') as fh:
        notebook = fh.read()
    start, end = '# --- code normalization: start ---\n', '# --- code normalization: end ---\n'
    copied = notebook[notebook.index(start) + len(start):notebook.index(end)]
    assert copied == module[module.index('import re\n'):module.index('\n\nif __name__')].rstrip() + '\n'
