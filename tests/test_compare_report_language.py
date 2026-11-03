"""The language the comparison report is written in follows the language of the new document."""

import pytest

from server.services.compare import report_language
from server.services.compare.report_language import detect_language, language_instruction, with_language_instruction
from server.services.compare.processors.xml import XMLProcessor

ENGLISH = ('The operator shall record every torque value measured on the primary fastener assembly before delivery. '
           'The supplier must deliver the part within ten working days after approval of the drawing by the customer. ') * 6
FRENCH = ("L'opérateur doit enregistrer chaque valeur de couple mesurée sur l'assemblage de la fixation principale avant la livraison. "
          "Le fournisseur doit livrer la pièce dans un délai de dix jours ouvrés après l'approbation du plan par le client. ") * 6
SPANISH = ('El operador debe registrar cada valor de par medido en el conjunto de la fijación principal antes de la entrega. '
           'El proveedor debe entregar la pieza en un plazo de diez días hábiles tras la aprobación del plano por el cliente. ') * 6


@pytest.mark.parametrize('text, code', [(ENGLISH, 'en'), (FRENCH, 'fr'), (SPANISH, 'es')])
def test_the_language_of_a_document_is_detected(text, code):
    assert detect_language(text) == code


@pytest.mark.parametrize('text', ['', 'A-1234 B-5678 C-9012', '12 34 56'])
def test_a_text_without_words_has_no_language(text):
    assert detect_language(text) is None
    assert language_instruction(text) == ''


def test_a_language_outside_the_known_ones_is_not_forced(monkeypatch):
    monkeypatch.setattr(report_language, 'fast_lang_guess', lambda _text: 'xx')
    assert detect_language(ENGLISH) is None


def test_the_instruction_names_the_language_of_the_new_document():
    assert 'in French' in language_instruction(FRENCH)
    assert 'in English' in language_instruction(ENGLISH)


def test_the_structured_report_keeps_its_keys_in_english():
    assert '"criticality"' in language_instruction(FRENCH, structured=True)
    assert '"criticality"' not in language_instruction(FRENCH)


def test_the_prompt_is_unchanged_when_the_language_is_unknown():
    assert with_language_instruction('PROMPT', '') == 'PROMPT'


def test_the_instruction_closes_the_system_prompt():
    prompt = with_language_instruction('PROMPT', ENGLISH)
    assert prompt.startswith('PROMPT\n\n') and prompt.endswith('these instructions.')


def _xml(sentence):
    return f'<doc><p>{sentence}</p></doc>'.encode()


@pytest.mark.parametrize('method', ['standard', 'structured'])
def test_the_processor_sends_the_language_of_the_new_document(method):
    old, new = _xml(ENGLISH), _xml(FRENCH + ' Une phrase nouvelle ajoutée au document pour la revision suivante.')
    result = XMLProcessor(method).build_messages(old, 'a.xml', new, 'b.xml')
    system = result.messages[0]['content']
    assert system.endswith('OUTPUT LANGUAGE: write the entire report in French, whatever the language of the old document or of these instructions.'
                           + (' The JSON keys and the values of "type" and "criticality" stay in English.' if method == 'structured' else ''))
