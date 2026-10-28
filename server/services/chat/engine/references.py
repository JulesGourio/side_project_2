"""Questions of the kind "which documents cite SF-1027?": the complete list, from ``doc_references``, not from the search.

``cited_codes_asked`` tells such a question apart (a reference code and a citing word), ``exact_citations`` looks the
codes up, and ``exact_citations_block`` writes what the answer model gets: the count, the documents (all of them up to
``LIST_ALL_MAX``, the most recent ones beyond, with the per-division breakdown and the link of the full list).
"""

import re
from typing import Dict, List, Tuple

from server.services.chat import prompts
from server.services.infra import doc_references
from server.services.infra.doc_catalog import canon_ref
from utils.parsing_pipeline.code_normalization import find_codes

# Up to this many citing documents the answer model gets the whole list; beyond, the most recent ones and the link.
LIST_ALL_MAX = 30
CITATIONS_CSV_PATH = '/api/chat/citations/{code}.csv?division={division}'

# Words of a question that asks where a code is cited, in French and English ("cité", "référencé dans", "which
# documents", "mentions"...). Without one, a code in the question is a topic, not a citation to look up.
_CITING_WORDS_RE = re.compile(
    r"\b(?:cit(?:é|e|ée|és|ées|ent|ed|es|ing)|r[ée]f[ée]renc(?:é|e|ée|és|ées|ent|ed|es|ing)|fai(?:t|re|sant) r[ée]f[ée]rence"
    r"|refers? to|referred to|mention(?:n[ée]e?s?|nent|ne|ed|s|ing)?|dans quels? documents?|in which documents?"
    r"|parlent de|talk about)\b",
    re.IGNORECASE)

ExactCitations = Dict[str, Tuple[str, List[dict]]]


def cited_codes_asked(question: str) -> List[Tuple[str, str]]:
    """The reference codes the question asks the citing documents of.

    :param question: the user question, without its date line
    :return: ``(code as written, canonical code)`` pairs, in order; empty when the question names no code or does not
        ask where it is cited
    """
    if not _CITING_WORDS_RE.search(question or ''):
        return []
    written: Dict[str, str] = {}
    for start, end, canon in find_codes(question, accept_lowercase=True):
        written.setdefault(canon, question[start:end])
    return [(w, canon) for canon, w in written.items()]


def exact_citations(asked: List[Tuple[str, str]], division: str) -> ExactCitations:
    """The documents citing each code asked, from ``doc_references``.

    :param asked: result of ``cited_codes_asked``
    :param division: normalised division code: ``ALL``, ``AS`` or ``IS``
    :return: canonical code -> (code as written, documents citing it); empty while the references are not loaded, so a
        missing table is never read as "cited by no document"
    """
    if not doc_references.loaded():
        return {}
    return {canon: (written, doc_references.citing_documents(canon, division)) for written, canon in asked}


def _documents(docs: List[dict]) -> List[List[dict]]:
    """The citing REFs grouped by document: the language versions of one document (Q0102QP_GB, Q0102QP_FR) count once.

    :param docs: entries of ``doc_references.citing_documents``, most recent first
    :return: one list of versions per document, in the order of their first version
    """
    groups: Dict[str, List[dict]] = {}
    for d in docs:
        groups.setdefault(canon_ref(d['ref']), []).append(d)
    return list(groups.values())


def _document_line(versions: List[dict]) -> str:
    """One line of the list: REF, title, division, publication date, passages citing the code, other versions.

    :param versions: the citing versions of one document, the most recent first
    :return: the Markdown list item
    """
    doc = versions[0]
    title = f' "{doc["title"]}"' if doc.get('title') else ''
    parts = [f'division {doc["division"]}'] if doc.get('division') else []
    parts += [f'published {doc["doc_date"]}'] if doc.get('doc_date') else []
    parts += [f'{doc["passages"]} passage{"s" if doc["passages"] > 1 else ""} citing it']
    others = f'; also cited in {", ".join(v["ref"] for v in versions[1:])}' if len(versions) > 1 else ''
    return f'- {doc["ref"]}{title} ({", ".join(parts)}{others})'


def exact_citations_block(found: ExactCitations, division: str) -> str:
    """What the answer model gets about the citing documents: the instructions, then one section per code.

    :param found: result of ``exact_citations``
    :param division: normalised division code, for the link of the full list
    :return: the block, or an empty string when there is nothing to add
    """
    if not found:
        return ''
    sections = []
    for canon, (written, docs) in found.items():
        name = written
        documents = _documents(docs)
        if not documents:
            sections.append(f'Code {name}: cited by no document of the base.')
            continue
        head = f'Code {name}: cited by {len(documents)} document{"s" if len(documents) > 1 else ""}'
        if len(documents) <= LIST_ALL_MAX:
            sections.append(f'{head}:\n' + '\n'.join(_document_line(v) for v in documents))
            continue
        by_division: Dict[str, int] = {}
        for versions in documents:
            key = versions[0].get('division') or '—'
            by_division[key] = by_division.get(key, 0) + 1
        breakdown = ', '.join(f'{k}: {v}' for k, v in sorted(by_division.items()))
        link = CITATIONS_CSV_PATH.format(code=canon, division=division)
        sections.append(f'{head} ({breakdown}). The {LIST_ALL_MAX} most recently published:\n'
                        + '\n'.join(_document_line(v) for v in documents[:LIST_ALL_MAX])
                        + f'\nFull list (download link): {link}')
    return prompts.read('exact_citations') + '\n\n' + '\n\n'.join(sections)
