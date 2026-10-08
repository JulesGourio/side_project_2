"""Document titles as a search channel for Chat VSI (step 3 of ``chat_vsi.py``).

Many questions ask for a document rather than a fact: "trouve-moi le template du CMP",
"dans le processus Stocker…", "procedures for work centers". The passage search can miss
them because the document's passages talk about the content, not about its title. The
app's document catalog (``server/data/doc_catalog.json``, ~7,600 REFs) holds every title, in
every language variant: matching the question's words against them finds such documents
directly (IQ22-223 "Stocker", IN_MRPC009 "Work center management", Q0102QP "First Article
Inspection (FAI)").

Scoring is lexical and cheap (no model call): accent-folded words, stop words dropped,
6-character prefixes as a crude stemmer, each word weighted by its rarity across titles
(IDF). A title must be mostly covered by the question (``_MIN_COVERAGE``) and the shared
words must be rare enough (``_MIN_IDF``): "FAI Report" or "Stocker" pass, a title
sharing only "qualité" does not.
"""

import math
import re
import unicodedata
from collections import Counter
from functools import lru_cache
from typing import Dict, List, Set, Tuple

from .doc_catalog import _catalog

_MIN_COVERAGE = 0.5      # share of the title's weight the question must cover
_MIN_IDF = 4.0           # summed rarity of the shared words (one rare word ≈ 5–8)
_MIN_SCORE = 1.2         # coverage * sqrt(shared rarity)

_STOP = set("""
le la les un une des du de et ou en au aux pour par sur dans avec sans est sont quel quelle quels quelles qui que quoi
comment cet cette ces son ses leur leurs pas plus moins tout tous toute toutes nous vous moi mon mes
the of to for in on at by with and or is are what which who how does can from this that these those all any you your its
document documents doc docs procedure procedures processus process liste list donne give trouve find fais make explain
explique selon according regle regles rule rules peux peut pouvez could would should about entre between
""".split())


def words(text: str) -> List[str]:
    folded = unicodedata.normalize('NFKD', text or '').encode('ascii', 'ignore').decode().lower()
    return [w[:6] for w in re.findall(r'[a-z0-9]+', folded) if len(w) >= 3 and w not in _STOP]


@lru_cache(maxsize=1)
def _title_index() -> Tuple[Dict[str, List[Set[str]]], Dict[str, List[str]], Dict[str, float]]:
    """(document -> word sets of its titles, document -> its REFs, word -> IDF), built once."""
    cat = _catalog()
    if not cat:
        return {}, {}, {}
    titles: Dict[str, List[Set[str]]] = {}
    refs: Dict[str, List[str]] = {}
    for canon, entries in cat.by_canon.items():
        refs[canon] = [e['ref'] for e in entries if e.get('ref')]
        sets = {frozenset(words(e.get('title') or '')) for e in entries}
        titles[canon] = [set(s) for s in sets if s]
    df = Counter(w for sets in titles.values() for w in set().union(*sets) if sets)
    n = max(1, len(titles))
    return titles, refs, {w: math.log(n / (1 + c)) for w, c in df.items()}


def documents_titled(texts: List[str], limit: int) -> List[Tuple[str, List[str], float]]:
    """Best title matches for the question texts: [(document, its REFs, score)], best first."""
    titles, refs, idf = _title_index()
    asked = set(w for t in texts for w in words(t))
    if not asked or not titles:
        return []
    found = []
    for canon, sets in titles.items():
        best = 0.0
        for title in sets:
            shared = asked & title
            if not shared:
                continue
            shared_idf = sum(idf.get(w, 0.0) for w in shared)
            coverage = shared_idf / (sum(idf.get(w, 0.0) for w in title) or 1.0)
            if coverage >= _MIN_COVERAGE and shared_idf >= _MIN_IDF:
                best = max(best, coverage * math.sqrt(shared_idf))
        if best >= _MIN_SCORE:
            found.append((canon, refs[canon], round(best, 2)))
    found.sort(key=lambda x: -x[2])
    return found[:limit]
