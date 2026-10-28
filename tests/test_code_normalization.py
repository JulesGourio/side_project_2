"""One spelling for the codes (utils/parsing_pipeline/code_normalization.py), on the passages and the queries.

Most tests run on a small catalog built from ``REFS`` by the generator, so they do not depend on the catalog block
generated into the module; the last ones check that block and the integration with the app.
"""

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

# Catalog REFs of every format, as the catalog writes them. SF-1027 is deliberately absent: it is found by the shape
# of SF-1029 and SF-1154.
REFS = [
    'SF-1029', 'SF-1154_BG', 'MI-1365', 'MI-13991', 'MI-1000', 'QTP-01-174', 'QTP-01-245', 'IF20308_CZ', 'IF20316',
    'IF20322_BR', 'IF20037', 'IF20037_DLV-31-5207', 'LdB IT-243', 'LdB P-099', 'LdB P-155', 'LdB - PM-001',
    'PRLAT506_FR', 'PRLAT566_FR', 'FDAQL2193', 'QIS-46A-352 A', 'QIS-45D-008 A', 'QIS-45D-008 B', 'QIS-46B-147 D',
    'IAQ 04 04 05', 'IAQ 10 01 01', 'Q0697MI_FR', 'Q0375MI', 'Q0009QP', 'P0323F', 'DLV-31-5207', 'DLV-33-3010',
    'DLV-2018', '19 --058', 'TDA8-00/027', 'TDA7-00-062', '777F-MDCD-QP', '777F-ABCD-QR', 'ŘLCZ-07/18', 'ŘLCZ-08/18',
    'NS-10441', 'NS-1717', 'IP-029', 'IP-025', 'GO-1585', 'QM-1031', 'QM-1064-IS', 'INE045_FR', 'INE028_FR_EN',
    'PRO-R80', 'PRO-R40', 'PROLAT_R40_FR', 'PROLAT_R30_FR', 'OBS_A8P', 'OBS_A9P', 'QP-1518', 'QP-2163', 'MM-1059-IS',
    'MM-1060', 'M.O.P', 'IN/AQ/559', 'INAQ561', 'NF-10845', 'GB-003', 'GB-004', 'R05', '19-001',
]


def _module_catalog() -> dict:
    """The generated blocks of the module, in the shape of ``catalog_from_refs``.

    :return: the catalog
    """
    return {'families': cn.CODE_FAMILIES, 'site_letters': cn.SITE_LETTERS, 'site_types': cn.SITE_TYPES,
            'ldb_types': cn.LDB_TYPES, 'phase_prefixes': cn.PHASE_PREFIXES, 'canonical': cn.CATALOG_CANONICAL,
            'signatures': cn.REF_SIGNATURES}


@pytest.fixture
def catalog():
    """The module running on the catalog of ``REFS``, restored afterwards."""
    saved = _module_catalog()
    cn.use_catalog(cn.catalog_from_refs(REFS))
    yield
    cn.use_catalog(saved)


def codes(text: str) -> list:
    """Canonical codes of a passage.

    :param text: passage
    :return: the codes
    """
    return cn.canonical_codes(text)


def query_codes(text: str) -> list:
    """Canonical codes of a question.

    :param text: question
    :return: the codes
    """
    return cn.canonical_codes(text, accept_lowercase=True)


# --- Spellings of one code ---

@pytest.mark.parametrize('written', ['SF-1027', 'SF1027', 'SF 1027', 'SF_1027', 'SF.1027', 'SF-1027_BG', 'SF1027_GB',
                                     'SF‑1027', 'SF – 1027', 'SF -1027'])
def test_every_spelling_of_a_code_has_the_same_canonical_form(catalog, written):
    assert codes(f'voir {written} pour la formation') == ['SF1027']


@pytest.mark.parametrize('text, expected', [
    ('selon MI - 1365', ['MI1365']),
    ('QTP 01-174', ['QTP01174']),
    ('IF2.03.08_CZ', ['IF20308']),
    ('IF 2.03.08', ['IF20308']),
    ('IF 20316', ['IF20316']),
    ('IF20322_BR', ['IF20322']),
    ('LdB IT-243', ['LDBIT243']),
    ('LdB-IT-243', ['LDBIT243']),
    ('LDB IT 243', ['LDBIT243']),
    ('LdB - PM-001', ['LDBPM001']),
    ('PR/LAT/506', ['PRLAT506']),
    ('PRLAT566.FR', ['PRLAT566']),
    ('FD_AQL_2193', ['FDAQL2193']),
    ('IN/AQ/559', ['INAQ559']),
    ('IN/E/045', ['INE045']),
    ('INE028_FR_EN', ['INE028']),
    ('IAQ 04 04 05', ['IAQ040405']),
    ('Q0697MI_FR', ['Q0697MI']),
    ('MI-13991Table 3', ['MI13991']),
    ('IF20037_DLV-31-5207', ['IF20037DLV315207']),
    ('19 --058', ['19058']),
    ('19-001', ['19001']),
    ('TDA8 - 00/027', ['TDA800027']),
    ('DLV-37-9046, QIS-46B-147 D, TDA7-00-062, IAQ 10 01 01', ['DLV379046', 'QIS46B147D', 'TDA700062', 'IAQ100101']),
    ('ŘLCZ-07/18', ['RLCZ0718']),
    ('RLCZ-08/18', ['RLCZ0818']),
    ('PRO-R80, PROLAT_R40_FR, OBS_A8P', ['PROR80', 'PROLATR40', 'OBSA8P']),
    ('M.O.P', ['MOP']),
    ('QM-1031Quality', ['QM1031']),
    ('GO-1585-B', ['GO1585']),
    ('IF20308_FR-annexe', ['IF20308']),
])
def test_catalog_codes_of_every_format(catalog, text, expected):
    assert codes(text) == expected


def test_a_key_that_one_ref_completes_takes_that_ref(catalog):
    assert codes('QIS-46A-352') == ['QIS46A352A']
    assert codes('QIS-46A-352 A') == ['QIS46A352A']


def test_a_key_that_several_refs_share_is_its_own_canonical_form(catalog):
    assert codes('QIS-45D-008') == ['QIS45D008']
    assert codes('QIS-45D-008 B') == ['QIS45D008B']
    assert cn.normalize_codes('777F seul') == '777F seul'


def test_a_variant_suffix_belongs_to_the_code_a_language_does_not(catalog):
    assert codes('QM-1064-IS') == ['QM1064IS']
    assert codes('MM-1059') == ['MM1059IS']
    assert codes('MM-1060_FR') == ['MM1060']


@pytest.mark.parametrize('text, expected', [
    ('SF-1027', ['SF1027']), ('NS-1718', ['NS1718']), ('Q0256MI_FR', ['Q0256MI']), ('IF 2.03.17', ['IF20317']),
    ('LdB IT-999', ['LDBIT999']), ('PRO-R70', ['PROR70']), ('IAQ 05 05 05', ['IAQ050505']),
])
def test_a_ref_absent_from_the_catalog_is_found_by_its_shape(catalog, text, expected):
    assert codes(text) == expected


# --- What is not a code ---

@pytest.mark.parametrize('text', [
    'LEVEL 2', 'page 12', '4.2.1', '15/03/2024', 'XSF-1027', 'SF-1027A1', 'cost 1027', 'GB 128 et IN 300',
    'MI-140470', 'IT-243', 'IF 02/08/2001', 'IF 2.02', 'IF 2', '145.A.30', 'PTD-TEST-6-A-01', 'Manual_Rev2025.11_v2.0',
    'A/B', 'N/A', 'X-Y', 'D.E.S', 'E.E.E', 'R 0,55', 'DA 9.10', '000,0 FH', 'STRUCTURE 1000', 'EMBRAER - 550',
    'Partie 3', 'PART 1', 'a 12.5', 'EN 2024', 'lines of 10 holes', 'Le 23/01/2018', 'Level 2 Level 3',
    'F7XC535556349NUE', 'o8svjqyBWk', 'il y a un tableau', 'révision R05', 'lot 19001',
])
def test_no_code_in_ordinary_text(catalog, text):
    assert codes(text) == []


def test_a_space_separates_number_groups_only_where_the_catalog_does(catalog):
    assert codes('DLV-2018 12 pages') == ['DLV2018']
    assert codes('IAQ 10 01 01') == ['IAQ100101']


@pytest.mark.parametrize('text', ['if 2 parts', 'go 2024', 'mo 2024 et va 3', 'in 300 cases', 'le 23 mars'])
def test_words_in_lower_case_are_not_codes_in_a_question(catalog, text):
    assert query_codes(text) == []


def test_a_lower_case_prefix_followed_by_a_space_is_a_code_in_a_question_only(catalog):
    assert cn.normalize_codes('cité dans sf 1027 ?') == 'cité dans sf 1027 ?'
    assert cn.normalize_query('cité dans sf 1027 ?') == 'cité dans sf 1027 [SF1027] ?'
    assert cn.normalize_query('cité dans sf-1027 ?') == 'cité dans sf-1027 [SF1027] ?'


def test_a_catalog_code_in_lower_case_is_a_code_in_a_passage(catalog):
    assert cn.normalize_codes('voir mi-1365') == 'voir mi-1365 [MI1365]'


# --- Codes that are not REFs ---

@pytest.mark.parametrize('text, expected', [
    ('selon EN 9100', ['EN9100']), ('NAS 1789', ['NAS1789']), ('ISO 9001:2015', ['ISO9001']),
    ('prEN 6122', ['PREN6122']), ('BAC 5578', ['BAC5578']), ('NE 40-012', ['NE40012']),
    ('MIL-STD-810G', ['MILSTD810G']), ('ATA 52', ['ATA52']),
    ('A350-900', ['A350900']), ('A 320', ['A320']), ('ATR 72-600', ['ATR72600']), ('Boeing 787-9', ['BOEING7879']),
    ('PART 21G', ['PART21G']), ('Part-145', ['PART145']), ('CS-25', ['CS25']), ('AMC 20-29', ['AMC2029']),
    ('2024-T3', ['2024T3']), ('7075 T6', ['7075T6']), ('15-5PH', ['155PH']),
    ('E.A.S.A', ['EASA']), ('N.D.T', ['NDT']), ('MM-02', ['MM02']), ('L523 77000', ['L52377000']), ('8-D', ['8D']),
])
def test_codes_that_are_not_refs(catalog, text, expected):
    assert codes(text) == expected


def test_hand_written_patterns_may_be_plain_regexes(catalog, monkeypatch):
    monkeypatch.setattr(cn, 'EXTRA_PATTERNS', [r'(?i)\bXYZ[-_ ]?9100\b'])
    assert cn.normalize_codes('selon XYZ 9100 et XYZ9100') == 'selon XYZ 9100 [XYZ9100] et XYZ9100'


# --- Annotation ---

def test_the_canonical_form_follows_the_identifier_holding_the_code(catalog):
    assert cn.normalize_codes('voir SF-1027 et SF 1027.') == 'voir SF-1027 [SF1027] et SF 1027 [SF1027].'
    assert cn.normalize_codes('MI-13991Table 3') == 'MI-13991Table [MI13991] 3'
    assert cn.normalize_codes('IP-029-005 ajouté') == 'IP-029-005 [IP029] ajouté'
    assert cn.normalize_codes('Q0697MI_FR') == 'Q0697MI_FR [Q0697MI]'


def test_a_code_already_in_its_canonical_spelling_is_left_alone(catalog):
    assert cn.normalize_codes('voir SF1027, Q0375MI et QP2163.') == 'voir SF1027, Q0375MI et QP2163.'


def test_normalizing_twice_changes_nothing(catalog):
    once = cn.normalize_codes('SF-1027, DLV-33-3010, IAQ 10 01 01, QTP 01-174 et IF2.03.08_CZ, LdB IT-243 ; EN 9100')
    assert cn.normalize_codes(once) == once
    assert cn.normalize_query(cn.normalize_query('sf 1027')) == 'sf 1027 [SF1027]'


def test_the_language_suffix_stays_with_the_code_and_out_of_the_canonical_form(catalog):
    assert cn.normalize_codes('[Source: SF-1027_BG | Title: FAI]') == '[Source: SF-1027_BG [SF1027] | Title: FAI]'


def test_a_question_and_a_passage_that_spell_a_code_differently_carry_the_same_bracket(catalog):
    assert '[SF1027]' in cn.normalize_query('dans quel document cite-t-on SF 1027 ?')
    assert '[SF1027]' in cn.normalize_codes('Formulaire SF-1027 rempli par le contrôleur')


def test_a_text_without_code_is_returned_as_it_is(catalog):
    text = 'Quelle est la procédure de qualification ?'
    assert cn.normalize_codes(text) is text
    assert cn.normalize_codes('') == ''
    assert cn.find_codes(None) == []


def test_cited_codes_leave_out_the_source_line_and_the_passages_own_document(catalog):
    text = '[Source: Q0697MI_FR | Title: Contrôle]\nAppliquer Q0697MI, SF-1027 et MI - 1365.'
    assert cn.cited_codes(text, 'Q0697MI_FR') == ['SF1027', 'MI1365']
    assert cn.cited_codes('SF-1029 et SF-1029_BG', 'SF-1029_BG') == []


# --- Generation ---

def test_families_are_generated_from_the_catalog_refs():
    refs = ['SF-1027_BG', 'SF-1154', 'SF-20316', 'DLV-33-3010', 'DLV-2018', 'IAQ 10 01 01', 'IAQ 06 00 01',
            'Q0102QP_GB', 'Q0170MI', 'LdB P-139', 'GB-003', 'GB-004', 'ONE-1234']
    assert cn.families_from_refs(refs) == {
        'DLV': cn.CodeFamily(2, 4, 2, False),
        'IAQ': cn.CodeFamily(2, 2, 3, True),
        'SF': cn.CodeFamily(4, 5, 1, False),
    }


def test_catalog_keys_keep_their_lowest_rank_and_a_shared_key_maps_to_itself():
    keys = cn.catalog_keys(['QIS-46A-352 A', 'QIS-45D-008 A', 'QIS-45D-008 B', 'SF-1027_BG', 'MM-1059-IS'])
    assert keys['QIS46A352'] == 'QIS46A352A'
    assert keys['QIS45D008'] == 'QIS45D008'
    assert keys['SF1027BG'] == 'SF1027' and keys['SF1027'] == 'SF1027'
    assert keys['MM1059'] == 'MM1059IS'


def test_the_catalog_has_the_site_codes_lettres_de_branchement_and_phases():
    catalog = cn.catalog_from_refs(REFS)
    assert catalog['site_letters'] == 'PQ' and set(catalog['site_types']) == {'F', 'MI', 'QP'}
    assert set(catalog['ldb_types']) >= {'IT', 'P'}
    assert set(catalog['phase_prefixes']) == {'OBS', 'PRO', 'PROLAT'}
    assert 'SF9999' in catalog['signatures'] and 'Q9999AA' in catalog['signatures']


def test_the_rendered_catalog_is_the_python_of_the_module():
    catalog = cn.catalog_from_refs(REFS)
    namespace = {'Dict': dict, 'Tuple': tuple, 'CodeFamily': cn.CodeFamily}
    exec(cn.render_catalog(catalog), namespace)
    assert namespace['CODE_FAMILIES'] == catalog['families']
    assert cn._parse_keys(namespace['_CATALOG_KEYS']) == catalog['canonical']
    assert set(namespace['_REF_SIGNATURES'].split()) == set(catalog['signatures'])
    assert namespace['SITE_TYPES'] == tuple(catalog['site_types'])


def test_rewriting_the_generated_section_keeps_the_rest_of_the_module():
    with open(cn.__file__, encoding='utf-8') as fh:
        source = fh.read()
    rewritten = cn.rewrite_generated_section(source, cn.catalog_from_refs(REFS))
    assert rewritten.split(cn._GENERATED_BEGIN)[0] == source.split(cn._GENERATED_BEGIN)[0]
    assert rewritten.split(cn._GENERATED_END)[1] == source.split(cn._GENERATED_END)[1]
    assert "'QIS46A352=QIS46A352A'" not in rewritten and 'QIS46A352=QIS46A352A' in rewritten


def test_catalog_drift_lists_the_refs_the_catalog_does_not_know(catalog):
    assert cn.catalog_drift(['SF-1029', 'SF-1154_BG', 'ZZ-0001', '']) == ['ZZ-0001']


# --- The generated block of the module and the app ---

def test_every_canonical_form_of_the_generated_catalog_is_a_key_of_its_own():
    assert all(cn.CATALOG_CANONICAL.get(canon) == canon for canon in cn.CATALOG_CANONICAL.values())


def test_the_generated_catalog_finds_every_kind_of_code():
    assert cn.canonical_codes('LdB IT-243, Q0697MI_FR et MI - 1365') == ['LDBIT243', 'Q0697MI', 'MI1365']


def test_the_module_is_shipped_to_the_executors():
    assert 'code_normalization.py' in config.WORKER_MODULES


def test_the_app_and_the_parsing_pipeline_run_the_same_rules():
    from utils.parsing_pipeline import code_normalization as app_side
    assert app_side.CATALOG_CANONICAL == cn.CATALOG_CANONICAL
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


def test_explain_codes_names_the_matcher(catalog):
    assert [m[3] for m in cn.explain_codes('MI-1365, SF-1027 et EN 9100')] == ['CATALOG', 'REF_SHAPE', 'STD-01']
