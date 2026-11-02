"""Tests for the stop word removal of the search queries (server/services/chat/engine/stopwords.py).

Coverage:
  - French: articles, prepositions, elisions (l', d', qu'), capitalised sentence start
  - English: articles, auxiliaries, contractions
  - kept: negations, codes, acronyms and divisions in upper case, punctuation-only tokens
  - a query made only of stop words is returned unchanged
  - the lists load from the Snowball files, comments left out
"""

import pytest

from server.services.chat.engine.stopwords import remove_stopwords, stopword_list


@pytest.mark.parametrize('query,expected,removed', [
    ('Quelles sont les exigences de qualification CND ?', 'exigences qualification CND ?',
     ['Quelles', 'sont', 'les', 'de']),
    ("Le contrôle de l'audit d'une pièce", "contrôle audit pièce", ['Le', 'de', "l'", "d'", 'une']),
    ("L’audit des fournisseurs", 'audit fournisseurs', ["L'", 'des']),
    ("qu'est-ce que le SF-1027 ?", 'est-ce SF-1027 ?', ["qu'", 'que', 'le']),
])
def test_french_stop_words_are_removed(query, expected, removed):
    assert remove_stopwords(query, 'fr') == (expected, removed)


def test_english_stop_words_are_removed():
    assert remove_stopwords('What are the requirements for the NDT qualification?', 'en') == (
        'requirements NDT qualification?', ['What', 'are', 'the', 'for', 'the'])


@pytest.mark.parametrize('query,language,expected', [
    ('pièces sans certificat de conformité', 'fr', 'pièces sans certificat conformité'),
    ("ne pas utiliser la pièce", 'fr', 'ne pas utiliser pièce'),
    ("parts that are not compliant and don't pass", 'en', "parts not compliant don't pass"),
    ('documents of the IS division', 'en', 'documents IS division'),
    ('révision A du QP-1518', 'fr', 'révision A QP-1518'),
    ('procedure 2 of the manual', 'en', 'procedure 2 manual'),
])
def test_negations_codes_and_upper_case_words_are_kept(query, language, expected):
    assert remove_stopwords(query, language)[0] == expected


@pytest.mark.parametrize('query,language', [('de la', 'fr'), ('what is it', 'en'), ('', 'fr')])
def test_a_query_of_stop_words_only_is_unchanged(query, language):
    assert remove_stopwords(query, language) == (query, [])


def test_the_lists_are_the_snowball_ones():
    fr, en = stopword_list('fr'), stopword_list('en')
    assert {'le', 'la', 'les', 'de', 'des', 'du', 'qu', 'à'} <= fr
    assert {'été', 'est', 'son', '|'}.isdisjoint(fr)      # commented out in the Snowball list, comments left out
    assert {'the', 'a', 'of', 'what', "don't", 'is'} <= en
    assert all(w == w.lower() and w.strip() == w for w in fr | en)
