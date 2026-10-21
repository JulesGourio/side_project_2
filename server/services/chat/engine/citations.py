"""Turning the model's [n] markers into citations while the answer streams."""

import logging
import re
from typing import Any, Dict, List, Tuple

from server.services.chat.engine.types import DocumentPassages

logger = logging.getLogger(__name__)

# A complete citation marker in the model's output, and a possible start of one at the
# very end of a chunk (held back until the next chunk decides).
_MARKER_RE = re.compile(r'\[(\d+)\]')
_PARTIAL_MARKER_RE = re.compile(r'\[\d*$')


class CitationStreamParser:
    """Strips ``[n]`` markers from streamed text and records where each citation goes.

    ``feed()`` returns the text that is safe to show now; a trailing ``[`` / ``[12`` that
    could still become a marker is held back until the next chunk. ``pos`` is the length of
    the clean text emitted before the marker — the offset the chat route uses to place ``⟦n⟧``.
    Markers numbering a document that doesn't exist are dropped from the text without a
    citation. Sources are numbered by first appearance.
    """

    def __init__(self, documents: List[Tuple[str, DocumentPassages]]):
        """Keeps the documents that the answer's ``[n]`` markers number.

        :param documents: (REF, document) pairs in prompt order: marker ``n`` cites ``documents[n - 1]``
        """
        self._documents = documents
        self._pending = ''
        self._emitted = 0
        self._n_of: Dict[str, int] = {}
        self.sources: List[Dict[str, Any]] = []
        self.citations: List[Dict[str, int]] = []

    def feed(self, chunk: str) -> str:
        """Returns the part of the streamed text that is safe to show now.

        :param chunk: next piece of the streamed answer text
        :return: the clean text that is safe to show now
        """
        text = self._pending + chunk
        partial = _PARTIAL_MARKER_RE.search(text)
        cut = partial.start() if partial else len(text)
        self._pending = text[cut:]
        return self._consume(text[:cut])

    @property
    def emitted_chars(self) -> int:
        """Length of the clean answer text emitted so far."""
        return self._emitted

    def flush(self) -> str:
        """End of stream: a held-back incomplete marker is plain text."""
        text, self._pending = self._pending, ''
        return self._consume(text)

    def _consume(self, text: str) -> str:
        """Strips the complete markers of ``text`` and records their citations.

        :param text: streamed text with no incomplete marker at its end
        :return: the text without its markers
        """
        out, pos = [], 0
        for m in _MARKER_RE.finditer(text):
            out.append(text[pos:m.start()])
            pos = m.end()
            i = int(m.group(1))
            if not 1 <= i <= len(self._documents):
                continue
            ref, doc = self._documents[i - 1]
            if ref not in self._n_of:
                self.sources.append({'title': ref, 'url': doc['url'], 'doc_uri': doc['url']})
                self._n_of[ref] = len(self.sources)
            self.citations.append({'n': self._n_of[ref], 'pos': self._emitted + sum(len(s) for s in out)})
        out.append(text[pos:])
        clean = ''.join(out)
        self._emitted += len(clean)
        return clean


def parse_citations(raw: str, documents: List[Tuple[str, DocumentPassages]]) -> Tuple[str, List[dict], List[dict]]:
    """Whole-text parse (reference for the streaming parser, used by the tests).

    :param raw: the whole answer text
    :param documents: (REF, document) pairs in prompt order
    :return: the clean text, the sources in order of first citation, and the citations with their positions
    """
    sources, citations, n_of, clean, pos = [], [], {}, '', 0
    for m in _MARKER_RE.finditer(raw):
        clean += raw[pos:m.start()]
        pos = m.end()
        i = int(m.group(1))
        if not 1 <= i <= len(documents):
            continue
        ref, doc = documents[i - 1]
        if ref not in n_of:
            sources.append({'title': ref, 'url': doc['url'], 'doc_uri': doc['url']})
            n_of[ref] = len(sources)
        citations.append({'n': n_of[ref], 'pos': len(clean)})
    clean += raw[pos:]
    return clean, sources, citations
