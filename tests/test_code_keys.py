"""Glued keys of known codes, on spellings taken from the inventory of the corpus and of the chat questions."""

import re
from pathlib import Path

import pytest

from server.services.chat.engine.code_keys import CodeKeys

KEYS = ['SF1027', 'MI1365', 'QTP01174', 'IF20308', 'IF20308CZ', 'LDBIT243', 'IT243', 'PRLAT506', 'FDAQL2193',
        'QIS46A352A', 'QIS46A352', 'IAQ040405', 'Q0697MI', 'BAC5578', 'NE40012', 'EN9100', 'PART21', '2024T3',
        'MI13991', 'IF20037', 'DLV315207', 'IF20037DLV315207', '19058', 'B1', 'Q0375MI', 'TDA800027']
CK = CodeKeys(KEYS)


@pytest.mark.parametrize('text, key', [
    ('voir SF-1027', 'SF1027'),
    ('voir SF 1027', 'SF1027'),
    ('voir sf-1027.', 'SF1027'),
    ('voir SF‑1027', 'SF1027'),
    ('voir SF – 1027', 'SF1027'),
    ('selon MI - 1365', 'MI1365'),
    ('selon MI -1365', 'MI1365'),
    ('QTP 01-174', 'QTP01174'),
    ('IF2.03.08_CZ', 'IF20308CZ'),
    ('IF 2.03.08', 'IF20308'),
    ('LdB IT-243', 'IT243'),
    ('LDB IT 243', 'LDBIT243'),
    ('PR/LAT/506', 'PRLAT506'),
    ('FD_AQL_2193', 'FDAQL2193'),
    ('QIS-46A-352 A', 'QIS46A352A'),
    ('QIS-46A-352-a', 'QIS46A352A'),
    ('IAQ 04 04 05', 'IAQ040405'),
    ('Q0697MI_FR', 'Q0697MI'),
    ('BAC 5578', 'BAC5578'),
    ('NE 40-012', 'NE40012'),
    ('EN 9100', 'EN9100'),
    ('Part-21', 'PART21'),
    ('2024-T3', '2024T3'),
    ('MI-13991Table 3', 'MI13991'),
    ('IF20037_DLV-31-5207', 'IF20037DLV315207'),
    ('19 --058', '19058'),
    ('licence B 1', 'B1'),
    ('TDA8 - 00/027', 'TDA800027'),
])
def test_finds_the_key(text: str, key: str) -> None:
    """Every spelling seen in the corpus or the chat gives the key of its code.

    :param text: text that cites the code
    :param key: expected key
    """
    assert [m.key for m in CK.find(text)] == [key]


@pytest.mark.parametrize('text', [
    'LEVEL 2', 'page 12', '4.2.1', '15/03/2024', 'XSF-1027', 'SF-10273', 'cost 1027', 'SF-1028', 'the 2024 T3 results',
])
def test_leaves_other_text_alone(text: str) -> None:
    """Text without a known code, or a code glued inside a longer word, gives nothing.

    :param text: text without a known code
    """
    keys = [m.key for m in CK.find(text)]
    assert 'SF1027' not in keys and keys in ([], ['2024T3'])


@pytest.mark.parametrize('text, key', [
    ('F7XC535556349NUE', 'F7'), ('A2PFACT', 'A2'), ('o8svjqyBWk', 'O8'), ('F38050A', 'F38050'),
    ('il y a un tableau', 'YA'), ('les SG&A de LMXI', 'ADE'), ('e.g. BAPS', 'EG'), ('lines of 10 holes', 'OF10'),
    ('Le 23/01/2018', 'LE23'), ('R30 Develop Process', 'R30DEVELOP'), ('Plan – 787', 'PLAN787'),
    ('Level 2 Level 3', 'LEVEL2LEVEL3'), ('Rc 23', 'RC23'),
    ('PTD-TESTDSD241204-6-A-01', 'A01'), ('145.A.30', 'A30'), ('ECL-G-46', 'G46'), ('Manual_Rev2025.11_v2.0', 'V20'),
    ('2024 T3 1,2', 'T31'), ('R 0,55', 'R0'), ('DA 9.10', 'DA9'), ('000,0 FH', '0FH'),
    ('STRUCTURE 1000', 'STRUCTURE1000'),
    ('EMBRAER - 550', 'EMBRAER550'), ('NASM21059 - NUT', 'NASM21059NUT'), ('Regles_SAP_controle', 'SAP'),
    ('A/B', 'AB'), ('X-Y', 'XY'), ('N/A', 'NA'), ('M . O . E', 'MOE'),
])
def test_no_token_on_words(text: str, key: str) -> None:
    """Common words next to a number or a letter, and short codes glued inside a longer word, give nothing.

    :param text: text without a code
    :param key: key in the dictionary that the text must not give
    """
    assert CodeKeys([key]).find(text) == []


@pytest.mark.parametrize('text, key', [
    ('E.A.S.A', 'EASA'), ('N.D.T', 'NDT'), ('M.O.E', 'MOE'), ('OF 10', 'OF10'),
    ('MI-13991Table', 'MI13991'), ('QM-1031Quality', 'QM1031'), ('IP-025/Installation', 'IP025'),
    ('GO-1585-B', 'GO1585'), ('CR=11055/D/7X', '11055D7X'), ('IF20308_FR-annexe', 'IF20308'),
    ('LTC-IN-E-045-F7X', 'INE045'), ('PAQ 04.05', 'PAQ0405'), ('IF 2.03.08.', 'IF20308'),
])
def test_separated_codes_and_upper_case(text: str, key: str) -> None:
    """Separated acronyms, upper-case codes and a delimited code glued to a word give their key.

    :param text: text that cites the code
    :param key: expected key
    """
    assert [m.key for m in CodeKeys([key]).find(text)] == [key]


def test_annotates_without_changing_the_text() -> None:
    """The key is added after the word, the text is kept, a code written as its key gets nothing."""
    assert CK.annotate('voir SF-1027 et Q0375MI.') == 'voir SF-1027 [SF1027] et Q0375MI.'
    assert CK.annotate('MI-13991Table 3') == 'MI-13991Table [MI13991] 3'
    assert CK.annotate('Q0697MI_FR') == 'Q0697MI_FR [Q0697MI]'


def test_annotate_is_idempotent() -> None:
    """A second pass adds nothing."""
    once = CK.annotate('SF-1027, QTP 01-174 et IF2.03.08_CZ')
    assert CK.annotate(once) == once


def test_question_and_passage_share_the_key() -> None:
    """A question and a passage that spell the code differently carry the same key."""
    q = CK.annotate('dans quel document cite-t-on SF 1027 ?')
    p = CK.annotate('Formulaire SF-1027 rempli par le contrôleur')
    assert '[SF1027]' in q and '[SF1027]' in p


def test_one_key_per_document() -> None:
    """Spellings mapped to one key give that key."""
    ck = CodeKeys({'LDBIT243': 'LDBIT243', 'IT243': 'LDBIT243', 'Q0697MIFR': 'Q0697MI', 'Q0697MI': 'Q0697MI'})
    assert [m.key for m in ck.find('IT-243 et LdB IT 243')] == ['LDBIT243', 'LDBIT243']
    assert ck.annotate('Q0697MI_FR') == 'Q0697MI_FR [Q0697MI]'


def test_runbook_notebook_has_the_same_code() -> None:
    """The DEV notebook embeds the same matcher as the module."""
    root = Path(__file__).resolve().parent.parent
    block = re.compile(r'# --- matcher: start ---.*?# --- matcher: end ---', re.S)
    module = block.search((root / 'server/services/chat/engine/code_keys.py').read_text(encoding='utf-8')).group(0)
    notebook = block.search((root / 'runbooks/retrieval/f3_dictionnaire_codes.py').read_text(encoding='utf-8'))
    assert notebook is not None and notebook.group(0) == module
