"""Document catalog — resolves document references (``REF``) cited in a chat
answer back to their intraqual URL, so they can be surfaced as clickable
"Sources" chips even when the endpoint emitted no ``url_citation`` annotation
for them.

Why this exists
---------------
The "Sources" chips under a chat answer are built solely from what the
Knowledge Assistant endpoint annotates (``url_citation`` events + RETRIEVER
trace spans). But the model is prompted to *name* many documents in prose
(metadata-derived lists, contextual mentions), and those are never annotated —
so they appear in the text but not as clickable chips. This module closes that
gap by matching catalog ``REF`` strings against the answer text.

The catalog is the slim ``server/data/doc_catalog.json`` produced by
``utils/deploy/build_doc_catalog.py`` from ``intraqual_docs.jsonl``. The whole module
degrades gracefully: if the file is missing (e.g. local dev without the export),
every function is a no-op and sources are left unchanged.
"""

import json
import logging
import os
import re
from functools import lru_cache
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

_DEFAULT_CATALOG = os.path.join(os.path.dirname(__file__), '..', 'data', 'doc_catalog.json')
_CATALOG_PATH = os.getenv('DOC_CATALOG_PATH', _DEFAULT_CATALOG)

# Our own intraqual_ref_url() scheme (parsing/utils.py, introduced 2026-07-07)
# embeds the REF directly in the URL: .../identification.aspx?ref=<REF>. The
# catalog's url_to_ref map is keyed on the OLD liredocumentdepuisrecherche?id=
# scheme and never matches these — extracting REF straight from the URL is
# scheme-accurate and needs no catalog/rebuild at all.
_REF_IN_URL_RE = re.compile(r'[?&]ref=([^&]+)', re.IGNORECASE)


def _ref_from_url(url: str) -> Optional[str]:
    """Pull the REF back out of one of our own ref=<REF> URLs, or None."""
    m = _REF_IN_URL_RE.search(url or '')
    if not m:
        return None
    from urllib.parse import unquote
    return unquote(m.group(1)).strip() or None


def _norm(ref: str) -> str:
    """Normalise a REF for matching: collapse whitespace, uppercase."""
    return ' '.join(ref.split()).upper()


# Mirrors utils/deploy/build_doc_catalog.py:_base_ref — needed here too because a
# citation in the chat text may use a spelling that never existed literally
# in the catalog (e.g. the model writes "PRLAT529" but the only real entries
# are "PRLAT-529" / "PRLAT-529_GB"). Canonicalizing both sides the same way
# lets a bare/re-punctuated mention still resolve to its document's group.
_LANG_SUFFIX_RE = re.compile(r'[-_. ]+(FR|EN|GB|MX|BG|CZ|BR|ES)$', re.IGNORECASE)
_NON_ALNUM_RE = re.compile(r'[^A-Z0-9]')


def _canon(ref: str) -> str:
    stripped = _LANG_SUFFIX_RE.sub('', ref.upper())
    return _NON_ALNUM_RE.sub('', stripped)


def canon_ref(ref: str) -> str:
    """Public: canonical grouping key for a REF (site/language suffix + punctuation
    stripped) — used by callers outside this module (e.g. vector_search.py) that
    need to cluster same-document language variants without a full catalog lookup."""
    return _canon(ref)


# Flag emoji for each recognised site/language suffix (see _LANG_SUFFIX_RE). "EN" has
# no dedicated country — it's a generic/international English translation rather than
# a specific site — so it shares the GB flag as a language (not country) marker.
_SITE_FLAGS: Dict[str, str] = {
    'FR': '🇫🇷', 'EN': '🇬🇧', 'GB': '🇬🇧', 'MX': '🇲🇽',
    'BG': '🇧🇬', 'CZ': '🇨🇿', 'BR': '🇧🇷', 'ES': '🇪🇸',
}


def site_code(ref: str) -> Optional[str]:
    """The trailing site/language suffix of a REF (e.g. "GB" from "MI-1331-GB"),
    upper-cased, or None if the REF carries no recognised suffix — most of the
    corpus has none; it marks the default/untranslated document, not a known
    language."""
    m = _LANG_SUFFIX_RE.search((ref or '').upper())
    return m.group(1).upper() if m else None


def site_flag(ref: str) -> str:
    """Best-effort flag emoji for a REF's site/language suffix; '' if the REF has
    no suffix or the suffix isn't in _SITE_FLAGS."""
    code = site_code(ref)
    return _SITE_FLAGS.get(code, '') if code else ''


def title_for_ref(ref: str) -> str:
    """Best-effort human-readable title (intraqual ``Titre``) for a REF; '' if the
    catalog isn't loaded or the REF is unknown."""
    cat = _catalog()
    if not cat:
        return ''
    entry = cat.by_ref.get(_norm(ref))
    return (entry.get('title') or '') if entry else ''


class _Catalog:
    """Holds the REF indexes and the compiled match pattern (built once)."""

    def __init__(self, entries: List[dict]):
        # norm REF -> {ref, url, title, base_ref}
        self.by_ref: Dict[str, dict] = {}
        # catalog URL -> canonical REF (reverse lookup, to relabel chips)
        self.url_to_ref: Dict[str, str] = {}
        # base_ref (canonical, punctuation- and suffix-stripped) -> [entries]
        # sharing it, i.e. the same document in other site/languages (see
        # utils/deploy/build_doc_catalog.py:_base_ref)
        self.by_canon: Dict[str, List[dict]] = {}
        for e in entries:
            ref = e.get('ref') or ''
            if not ref:
                continue
            key = _norm(ref)
            self.by_ref.setdefault(key, e)
            url = e.get('url')
            if url:
                self.url_to_ref.setdefault(url, ref)
            canon = e.get('base_ref') or _canon(ref)
            self.by_canon.setdefault(canon, []).append(e)

        # One alternation of every REF *and* its canonical (punctuation- and
        # suffix-stripped) form, so a bare citation like "PRLAT529" matches
        # even when the catalog only holds "PRLAT-529_GB". Longest first so
        # "MI-1000_FR" wins over "MI-1000" / "MI1000". Bounded by non-[\w-]
        # lookarounds so a REF is never matched inside a larger token (e.g.
        # "GO-1125" not matched in "GO-11250").
        alts = set()
        for e in entries:
            ref = e.get('ref')
            if not ref:
                continue
            alts.add(ref)
            alts.add(e.get('base_ref') or _canon(ref))
        refs = sorted(alts, key=len, reverse=True)
        self.pattern: Optional[re.Pattern] = None
        if refs:
            alt = '|'.join(re.escape(r) for r in refs)
            try:
                self.pattern = re.compile(rf'(?<![\w-])(?:{alt})(?![\w-])', re.IGNORECASE)
            except re.error as exc:  # pragma: no cover - defensive
                logger.warning('doc_catalog: could not compile pattern: %s', exc)
                self.pattern = None

    def find_in_text(self, text: str) -> List[dict]:
        """Return catalog entries for every document mentioned in ``text``
        (matched literally or via its canonical form), including that
        document's other site/language entries, in order of first mention,
        de-duplicated."""
        if not text or not self.pattern:
            return []
        out: List[dict] = []
        seen_canon: set = set()
        seen_ref: set = set()
        for m in self.pattern.finditer(text):
            raw = m.group(0)
            entry = self.by_ref.get(_norm(raw))
            canon = entry.get('base_ref') or _canon(raw) if entry else _canon(raw)
            if canon in seen_canon:
                continue
            group = self.by_canon.get(canon)
            if not group:
                continue
            seen_canon.add(canon)
            for e in group:
                ref_key = _norm(e['ref'])
                if ref_key in seen_ref:
                    continue
                seen_ref.add(ref_key)
                out.append(e)
        return out

    def group_siblings(self, ref: str) -> List[dict]:
        """Return the other site/language REFs for the same document as
        ``ref`` (empty if unknown or if it has no siblings)."""
        entry = self.by_ref.get(_norm(ref))
        canon = (entry.get('base_ref') or _canon(ref)) if entry else _canon(ref)
        group = self.by_canon.get(canon, [])
        return [e for e in group if e is not entry]


@lru_cache(maxsize=1)
def _catalog() -> Optional[_Catalog]:
    path = os.path.normpath(_CATALOG_PATH)
    if not os.path.exists(path):
        logger.info('doc_catalog: no catalog at %s — reference resolution disabled', path)
        return None
    try:
        with open(path, 'r', encoding='utf-8') as f:
            entries = json.load(f)
    except Exception as exc:
        logger.warning('doc_catalog: failed to load %s: %s', path, exc)
        return None
    cat = _Catalog(entries if isinstance(entries, list) else [])
    logger.info('doc_catalog: loaded %d refs from %s', len(cat.by_ref), path)
    return cat


def other_language_refs(ref: str) -> List[dict]:
    """Public: other site/language REFs for the same document as ``ref`` (each a
    dict with ``ref``/``url``/``title``/``base_ref``) — empty if the catalog isn't
    loaded (e.g. local dev without the export) or the document has no known
    siblings. Used by vector_search.py to show impacted-doc results alongside
    the REFs of that same document in other languages, even when the vector
    search didn't itself independently retrieve those other-language chunks."""
    cat = _catalog()
    if not cat:
        return []
    return cat.group_siblings(ref)


def augment_sources(content: str, sources: List[dict]) -> List[dict]:
    """Re-surface documents the answer cites in prose but that the endpoint did
    not annotate, and relabel existing chips to their REF where possible.

    1. Relabel: any existing source whose URL carries our own ref=<REF> query
       param (or, failing that, is in the catalog) gets its title set to the
       canonical REF (so chips read "MI-1234" instead of a raw URL).
    2. Append: any catalog REF named in ``content`` that is not already present
       (by REF title or by URL) is added as a new ``{title, url}`` source.
    3. Link languages: every REF now present (relabeled or newly appended)
       gets its other site/language REFs for the same document (e.g. the
       ``_FR``/``_GB`` counterpart of a cited ``_EN`` doc) appended too, when
       not already present.

    Existing source order is preserved, so inline ``⟦n⟧`` citation markers keep
    pointing at the same chips; new ones are appended at the end. Returns the
    (possibly extended) sources list; on any failure returns it unchanged.
    """
    cat = _catalog()
    try:
        sources = list(sources or [])

        # 1. Relabel existing chips to their REF when we recognise the URL —
        # prefer parsing our own ref=<REF> scheme (always accurate, and does
        # not need the catalog at all) over the catalog's url_to_ref (keyed
        # on the older id=... scheme, so it never matches these URLs and
        # would otherwise leave `title` unset/raw-URL).
        for s in sources:
            url = s.get('url')
            if not url:
                continue
            ref = _ref_from_url(url) or (cat.url_to_ref.get(url) if cat else None)
            if ref:
                s['title'] = ref

        if not cat:
            return sources

        existing_urls = {s.get('url') for s in sources if s.get('url')}
        existing_refs = {_norm(s['title']) for s in sources if s.get('title')}

        # 2. Append documents cited in prose but missing from the chips.
        added = 0
        for entry in cat.find_in_text(content):
            ref, url = entry['ref'], entry.get('url')
            if _norm(ref) in existing_refs or (url and url in existing_urls):
                continue
            sources.append({'title': ref, 'url': url, 'doc_uri': url})
            existing_refs.add(_norm(ref))
            if url:
                existing_urls.add(url)
            added += 1
        if added:
            logger.info('doc_catalog: added %d cited documents to sources', added)

        # 3. Append language-variant siblings for every REF now present.
        added_variants = 0
        for ref in list(existing_refs):
            for sib in cat.group_siblings(ref):
                sib_ref, url = sib['ref'], sib.get('url')
                if _norm(sib_ref) in existing_refs or (url and url in existing_urls):
                    continue
                sources.append({'title': sib_ref, 'url': url, 'doc_uri': url})
                existing_refs.add(_norm(sib_ref))
                if url:
                    existing_urls.add(url)
                added_variants += 1
        if added_variants:
            logger.info('doc_catalog: added %d language-variant documents to sources', added_variants)
        return sources
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning('doc_catalog: augment_sources failed: %s', exc)
        return sources
