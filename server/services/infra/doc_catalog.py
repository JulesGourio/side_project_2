"""Document catalog: resolves the document references (``REF``) of a chat answer to their Intraqual link, title and
revision, so documents named in prose become clickable "Sources" chips even without an annotation.

The catalog is the Lakebase table ``doc_catalog`` (every document of the parsing scope, with title, link, ``in_chat``),
rewritten by parsing task ``6_update_kb_metadata`` after each daily run; the app reloads it at startup and every
``DOC_CATALOG_REFRESH_S`` seconds (``catalog_refresher``). Until that table has rows (app started before the first
pipeline run, local dev, tests) every function degrades to a no-op: no REF is resolved and no chip is shown.
"""

import asyncio
import logging
import os
import re
from typing import Any, Dict, List

logger = logging.getLogger(__name__)

_REFRESH_S = float(os.getenv('DOC_CATALOG_REFRESH_S', '1800'))

# The catalog loaded from Lakebase (None until the first successful load).
_live: '_Catalog | None' = None

# The pipeline's intraqual_ref_url() embeds the REF in the URL (identification.aspx?ref=<REF>): the REF is read from there.
_REF_IN_URL_RE = re.compile(r'[?&]ref=([^&]+)', re.IGNORECASE)


def _ref_from_url(url: str) -> str | None:
    """Pull the REF back out of one of our own ref=<REF> URLs, or None.

    :param url: URL of a source, which may carry ``ref=<REF>``
    :return: the REF, or None when the URL carries none
    """
    m = _REF_IN_URL_RE.search(url or '')
    if not m:
        return None
    from urllib.parse import unquote
    return unquote(m.group(1)).strip() or None


def _norm(ref: str) -> str:
    """Normalise a REF for matching: collapse whitespace, uppercase.

    :param ref: REF as written
    :return: the REF upper-cased, with single spaces
    """
    return ' '.join(ref.split()).upper()


# A citation may spell a REF differently from the catalog (punctuation or suffix missing); canonicalizing both sides
# the same way resolves it to the document's group.
_LANG_SUFFIX_RE = re.compile(r'[-_. ]+(FR|EN|GB|MX|BG|CZ|BR|ES)$', re.IGNORECASE)
_NON_ALNUM_RE = re.compile(r'[^A-Z0-9]')


def _canon(ref: str) -> str:
    """Canonical form of a REF: upper-cased, without its language suffix and punctuation.

    :param ref: REF in any spelling
    :return: the canonical key
    """
    stripped = _LANG_SUFFIX_RE.sub('', ref.upper())
    return _NON_ALNUM_RE.sub('', stripped)


def canon_ref(ref: str) -> str:
    """Public: canonical grouping key for a REF (site/language suffix + punctuation
    stripped) — used by callers outside this module (e.g. vector_search.py) that
    need to cluster same-document language variants without a full catalog lookup.

    :param ref: REF in any spelling
    :return: the canonical key
    """
    return _canon(ref)


# Flag emoji per recognised suffix. 'EN' is an international translation rather than a site, so it shares the GB flag.
_SITE_FLAGS: Dict[str, str] = {
    'FR': '🇫🇷', 'EN': '🇬🇧', 'GB': '🇬🇧', 'MX': '🇲🇽',
    'BG': '🇧🇬', 'CZ': '🇨🇿', 'BR': '🇧🇷', 'ES': '🇪🇸',
}


def site_code(ref: str) -> str | None:
    """The trailing site/language suffix of a REF (e.g. "GB" from "XX-1234-GB"),
    upper-cased, or None if the REF carries no recognised suffix — most of the
    corpus has none; it marks the default/untranslated document, not a known
    language.

    :param ref: REF, any case; None allowed
    :return: the upper-cased suffix, or None when the REF has none
    """
    m = _LANG_SUFFIX_RE.search((ref or '').upper())
    return m.group(1).upper() if m else None


def site_flag(ref: str) -> str:
    """Best-effort flag emoji for a REF's site/language suffix; '' if the REF has
    no suffix or the suffix isn't in _SITE_FLAGS.

    :param ref: REF, any case; None allowed
    :return: the flag emoji, or an empty string
    """
    code = site_code(ref)
    return _SITE_FLAGS.get(code, '') if code else ''


def title_for_ref(ref: str) -> str:
    """Best-effort human-readable title (intraqual ``Titre``) for a REF; '' if the
    catalog isn't loaded or the REF is unknown.

    :param ref: REF of the document
    :return: the title, or an empty string when unknown
    """
    cat = _catalog()
    if not cat:
        return ''
    entry = cat.by_ref.get(_norm(ref))
    return (entry.get('title') or '') if entry else ''


def document_info(ref: str) -> Dict[str, Any]:
    """{title, revision, doc_date (ISO)} of a REF from the catalog, only the fields it has; {} when unknown.

    :param ref: REF of the document
    :return: dict with the ``title``, ``revision`` and ``doc_date`` (ISO) the catalog has; empty when unknown
    """
    cat = _catalog()
    entry = cat.by_ref.get(_norm(ref)) if cat else None
    if not entry:
        return {}
    date = entry.get('doc_date')
    info = {'title': entry.get('title'), 'revision': entry.get('revision'),
            'doc_date': date.isoformat() if hasattr(date, 'isoformat') else date}
    return {k: v for k, v in info.items() if v}


def with_document_info(sources: List[dict]) -> List[dict]:
    """``sources`` with the catalog title, revision and date of each REF (``doc_title``,
    ``revision``, ``doc_date``), for the source chips and the exports.

    :param sources: source dicts whose ``title`` holds the REF; updated in place
    :return: the same list, with ``doc_title``, ``revision`` and ``doc_date`` added where known
    """
    for s in sources or []:
        info = document_info(s.get('title') or '')
        if info.get('title'):
            s['doc_title'] = info['title']
        for key in ('revision', 'doc_date'):
            if info.get(key):
                s[key] = info[key]
    return sources


class _Catalog:
    """Holds the REF indexes and the compiled match pattern (built once)."""

    def __init__(self, entries: List[dict]):
        # norm REF -> {ref, url, title, base_ref}
        """Builds the REF indexes and the pattern that finds REFs in a text.

        :param entries: catalog rows: ``ref``, ``url``, ``title``, ``base_ref``, ``revision``, ``doc_date``
        """
        self.by_ref: Dict[str, dict] = {}
        # catalog URL -> canonical REF (reverse lookup, to relabel chips)
        self.url_to_ref: Dict[str, str] = {}
        # base_ref (canonical, punctuation- and suffix-stripped) -> [entries]
        # sharing it, i.e. the same document in other site/languages (see _canon)
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

        # One alternation of every REF and its canonical form, so a bare citation matches even when the catalog holds a suffixed spelling.
        # Longest first (the most specific REF wins), bounded by lookarounds on non-word characters or hyphens.
        alts = set()
        for e in entries:
            ref = e.get('ref')
            if not ref:
                continue
            alts.add(ref)
            alts.add(e.get('base_ref') or _canon(ref))
        refs = sorted(alts, key=len, reverse=True)
        self.pattern: re.Pattern | None = None
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
        de-duplicated.

        :param text: answer or question text
        :return: the catalog entries in order of first mention, without duplicates
        """
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
        ``ref`` (empty if unknown or if it has no siblings).

        :param ref: REF of the document
        :return: the other site or language entries of the same document
        """
        entry = self.by_ref.get(_norm(ref))
        canon = (entry.get('base_ref') or _canon(ref)) if entry else _canon(ref)
        group = self.by_canon.get(canon, [])
        return [e for e in group if e is not entry]


def _catalog() -> _Catalog | None:
    return _live


def set_catalog(entries: List[Dict[str, Any]]) -> None:
    """Replace the live catalog (entries: ref, url, title, base_ref, in_chat, revision, doc_date).

    :param entries: catalog rows as dicts: ``ref``, ``url``, ``title``, ``base_ref``, ``in_chat``, ``revision``,
        ``doc_date``
    """
    global _live
    _live = _Catalog(entries)


async def refresh_from_lakebase(pool) -> int:
    """Load the Lakebase table doc_catalog into the live catalog. Returns the number of
    documents loaded; 0 (live catalog left as is) when the table is empty or unreadable.

    :param pool: database pool with an ``acquire()`` context manager
    :return: the number of documents loaded; 0 when the table is empty or unreadable
    """
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch('SELECT ref, url, title, base_ref, in_chat, revision, doc_date FROM doc_catalog')
    except Exception as exc:  # noqa: BLE001 — keep the catalog we have
        logger.warning('doc_catalog: Lakebase read failed, keeping the current catalog: %s', exc)
        return 0
    if not rows:
        logger.warning('doc_catalog: Lakebase table doc_catalog is empty — run the parsing '
                       'pipeline task 6_update_kb_metadata to fill it')
        return 0
    set_catalog([dict(r) for r in rows])
    logger.info('doc_catalog: loaded %d documents from Lakebase', len(rows))
    return len(rows)


async def catalog_refresher(get_pool) -> None:
    """Background task: reload the catalog from Lakebase now, then every DOC_CATALOG_REFRESH_S.

    :param get_pool: callable returning the current pool, or None before it exists
    """
    while True:
        pool = get_pool()
        if pool is not None:
            await refresh_from_lakebase(pool)
        await asyncio.sleep(_REFRESH_S)


def other_language_refs(ref: str) -> List[dict]:
    """Public: other site/language REFs for the same document as ``ref`` (each a
    dict with ``ref``/``url``/``title``/``base_ref``) — empty if the catalog isn't
    loaded (e.g. local dev without the export) or the document has no known
    siblings. Used by vector_search.py to show impacted-doc results alongside
    the REFs of that same document in other languages, even when the vector
    search didn't itself independently retrieve those other-language chunks.

    :param ref: REF of the document
    :return: the other site or language entries of the same document; empty when the catalog is not loaded
    """
    cat = _catalog()
    if not cat:
        return []
    return cat.group_siblings(ref)


def augment_sources(content: str, sources: List[dict]) -> List[dict]:
    """Re-surface documents the answer cites in prose but that the endpoint did
    not annotate, and relabel existing chips to their REF where possible.

    1. Relabel: any existing source whose URL carries our own ref=<REF> query
       param (or, failing that, is in the catalog) gets its title set to the
       canonical REF (so chips read "XX-1234" instead of a raw URL).
    2. Append: any catalog REF named in ``content`` that is not already present
       (by REF title or by URL) is added as a new ``{title, url}`` source.
    3. Link languages: every REF now present (relabeled or newly appended)
       gets its other site/language REFs for the same document (e.g. the
       ``_FR``/``_GB`` counterpart of a cited ``_EN`` doc) appended too, when
       not already present.

    Existing source order is preserved, so inline ``⟦n⟧`` citation markers keep
    pointing at the same chips; new ones are appended at the end. Returns the
    (possibly extended) sources list; on any failure returns it unchanged.

    :param content: answer text, including the documents it cites in prose
    :param sources: source chips of the answer
    :return: the sources, with the cited documents and their language variants added
    """
    cat = _catalog()
    try:
        sources = list(sources or [])

        # 1. Relabel existing chips to their REF: our own ref=<REF> URLs are parsed directly (always accurate, no catalog needed).
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
