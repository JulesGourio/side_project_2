"""Canonical keys of the document codes in a text: variants of one code give one key, other tokens are left alone."""

from server.services.chat.engine import code_keys
from server.services.chat.engine.code_keys import CodeKeys
from server.services.infra import doc_catalog

REFS = ['ST-1027', 'SF-1027', 'SF-1154-1_FR', 'MI-1413', 'MI-1413_FR', 'Q0375MI', 'Q0126QP_GB', 'IF20316_FR',
        'UDE2-00-113', 'TDA8-00/027', 'QTP-01-174', 'LdB IT-243', 'LdB P-100', 'NF-10149', 'MM-1059_BG', 'GO-1070',
        'MIT-1174_FR', 'DGL-1056']
KEYS = CodeKeys(REFS)


def test_prefixes_and_attached_letters_come_from_the_refs():
    assert {'ST', 'SF', 'MI', 'MIT', 'Q', 'IF', 'UDE2', 'TDA8', 'QTP', 'LDB IT', 'LDB P', 'NF', 'MM', 'GO',
            'DGL'} <= KEYS.prefixes
    assert {'MI', 'QP'} <= KEYS.tails
    assert CodeKeys([]).normalize('ST-1027') == 'ST-1027'


def test_spellings_of_one_code_share_a_key():
    for written in ['ST-1027', 'st-1027', 'ST 1027', 'ST_1027', 'ST.1027', 'ST - 1027', 'ST -1027', 'St1027']:
        assert KEYS.normalize(f'C\'est quoi {written} ?') == "C'est quoi ST1027 ?", written


def test_unicode_dashes():
    for written in ['ST\u20101027', 'ST\u20111027', 'ST\u22121027', 'ST\u20131027', 'ST\u20141027']:
        assert KEYS.normalize(written) == 'ST1027', written
    assert KEYS.normalize('REF : MI-1413 \u2013 C Guide') == 'REF : MI1413 \u2013 C Guide'    # a spaced dash is a title break
    assert KEYS.normalize('SF 1027 \u2013 2024') == 'SF1027 \u2013 2024'


def test_language_suffix_is_dropped_from_the_key():
    assert KEYS.normalize('NF-10149_Fr') == 'NF10149'
    assert KEYS.normalize('MM-1059_BG et MM-1059') == 'MM1059 et MM1059'
    assert KEYS.normalize('SF-1154-1_FR') == 'SF11541'
    assert KEYS.normalize('ST 1027 en 2024') == 'ST1027 en 2024'      # "en" is a word here, not a suffix


def test_letters_attached_to_the_digits_are_part_of_the_key():
    assert KEYS.normalize('Q0375MI') == 'Q0375MI'
    assert KEYS.normalize('q0375mi_fr') == 'Q0375MI'
    assert KEYS.normalize('Q0126QP_GB') == 'Q0126QP'


def test_spacing_variants_seen_in_documents():
    assert KEYS.normalize('REF : MI -1413 – C Guide') == 'REF : MI1413 – C Guide'
    assert KEYS.normalize('| MI - 1413 | Instruction') == '| MI1413 | Instruction'
    assert KEYS.normalize('## UDE2 -00 -113 ATP') == '## UDE200113 ATP'
    assert KEYS.normalize('REF : TDA8 - 00/027 – J') == 'REF : TDA800027 – J'
    assert KEYS.normalize('END --- QTP 01-174 Complementary') == 'END --- QTP01174 Complementary'
    assert KEYS.normalize('Ver LDB IT 243 (Instrução)') == 'Ver LDBIT243 (Instrução)'


def test_key_equals_the_canonical_ref_of_the_catalog():
    for ref in REFS:
        assert KEYS.normalize(ref) == doc_catalog.canon_ref(ref), ref


def test_other_tokens_are_left_alone():
    for text in ['page 12', 'le 27 octobre 2026', 'du 2026-10-27', 'cost 1027', 'ST seul', 'MI-14136x', 'Q 1 2024',
                 'ABSTRACT 1027', 'post-ST1027']:
        assert KEYS.normalize(text) == text, text
    assert KEYS.normalize('MI-1413_FRANCE') == 'MI1413_FRANCE'          # only the code part changes


def test_several_codes_in_one_sentence():
    assert KEYS.normalize('Lien entre SF 1027 et st-1027, puis MI-1413_FR ?') == 'Lien entre SF1027 et ST1027, puis MI1413 ?'


def test_annotate_adds_the_key_once():
    out = KEYS.annotate('Voir ST-1027 et SF1027.')
    assert out == 'Voir ST-1027 [ST1027] et SF1027.'
    assert KEYS.annotate(out) == out
    assert KEYS.annotate('[Source: MI-1413 | Title: x]') == '[Source: MI-1413 [MI1413] | Title: x]'


def test_language_suffixes_follow_the_catalog():
    assert set(code_keys.LANG_SUFFIXES) == set(doc_catalog._LANG_SUFFIX_RE.pattern.split('(')[1].split(')')[0].split('|'))
