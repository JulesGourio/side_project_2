"""Stop word removal from the French and English search queries (Snowball lists, ``stopword_lists/``)."""

import re
from functools import lru_cache
from pathlib import Path
from typing import FrozenSet, List, Tuple

_LISTS_DIR = Path(__file__).with_name('stopword_lists')
_LIST_FILES = {'fr': 'french.txt', 'en': 'english.txt'}
# In the Snowball lists, yet they reverse what a query asks ("pièces sans certificat", "not compliant").
_KEPT = {'fr': frozenset({'ne', 'n', 'pas', 'sans'}), 'en': frozenset({'no', 'nor', 'not', 'cannot', 'against'})}
_TOKEN_RE = re.compile(r'\S+')
# Punctuation around a word: ``(procédure,`` -> ``procédure``.
_EDGES_RE = re.compile(r"^[^\w']*(.*?)[^\w']*$")
# French elision: ``l'audit`` -> ``l`` + ``audit``.
_ELISION_RE = re.compile(r"^(\w+)'(\w.*)$")


@lru_cache(maxsize=None)
def stopword_list(language: str) -> FrozenSet[str]:
    """The Snowball stop words of ``language``, lower case, apostrophes straight.

    :param language: ``fr`` or ``en``
    :return: the words of the list; ``|`` starts a comment, a word is the first item of its line
    """
    words = set()
    for line in (_LISTS_DIR / _LIST_FILES[language]).read_text(encoding='utf-8').splitlines():
        word = line.split('|', 1)[0].strip()
        if word:
            words.add(word.lower().replace('’', "'"))
    return frozenset(words)


def _is_stopword(word: str, language: str) -> bool:
    """Whether ``word`` is dropped from a query of ``language``.

    :param word: a word without its surrounding punctuation, apostrophes straight
    :param language: ``fr`` or ``en``
    :return: True for a lower or capitalised stop word; a word with a digit or in upper case is a code, an acronym or a
        division (``IS``, ``AS``, revision ``A``) and is kept, so are the negations
    """
    if not word or word.isupper() or any(c.isdigit() for c in word):
        return False
    low = word.lower()
    if low in _KEPT[language] or (language == 'en' and low.endswith("n't")):
        return False
    return low in stopword_list(language)


def remove_stopwords(query: str, language: str) -> Tuple[str, List[str]]:
    """``query`` without the stop words of ``language``.

    :param query: a search query in ``language``
    :param language: ``fr`` or ``en``
    :return: the query and the words removed, in order; the query unchanged and no word when every word is a stop word
    """
    kept: List[str] = []
    removed: List[str] = []
    for token in _TOKEN_RE.findall(query):
        word = _EDGES_RE.match(token.replace('’', "'")).group(1)
        elided = _ELISION_RE.match(word) if language == 'fr' else None
        # Lower-cased: a capital ``L'`` opens a sentence, it is no acronym.
        if elided and _is_stopword(elided.group(1).lower(), language):
            removed.append(elided.group(1) + "'")
            word = token = elided.group(2)
        if _is_stopword(word, language):
            removed.append(word)
        else:
            kept.append(token)
    if not kept:
        return query, []
    return ' '.join(kept), removed
