"""Build the slim document catalog used to turn document references cited in a
chat answer into clickable "Sources" chips.

The Knowledge Assistant endpoint only emits ``url_citation`` annotations for a
subset of the documents it actually names in prose (metadata-derived lists and
contextual mentions get no annotation), so those documents never become
clickable chips. This catalog lets the app resolve any ``REF`` the model writes
in the answer text back to its intraqual URL and re-surface it as a source.

Source of truth: ``intraqual_docs.jsonl`` (one JSON object per line, the export
of the intraqual metadata table — also available in Databricks). Only three
fields are kept: ``Reference`` (the REF), ``url`` (clickable link) and
``Titre`` (human title).

Each entry also carries a ``base_ref``: intraqual encodes the document's
site/language as a trailing suffix on the REF (e.g. ``GO-1316_FR`` /
``GO-1316_GB``, ``PRLAT524.FR``, ``PRLAT-529_GB``), inconsistently — the
separator varies (``_``, ``.``, ``-``, space), the base itself sometimes
carries a dash that a bare citation drops (``PRLAT-529`` vs ``PRLAT529``),
and some documents have every variant, some only one, some none. ``base_ref``
strips the suffix *and* every non-alphanumeric character, so every spelling
of the same document — literal REF, punctuation-normalized, with or without
a language suffix — collapses to the same key. That lets the app both (a)
recognise a REF cited in a form that doesn't literally exist in the catalog
(e.g. the model writes "PRLAT529" but the only real entry is
"PRLAT-529_GB") and (b) surface its other-language siblings (see
``server/services/doc_catalog.py``).

Usage (regenerate when the intraqual export is refreshed):
    python utils/deploy/build_doc_catalog.py
    python utils/deploy/build_doc_catalog.py --src path/to/intraqual_docs.jsonl
"""

import argparse
import json
import os
import re
import sys

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_DEFAULT_SRC = os.path.join(_REPO_ROOT, 'intraqual_docs.jsonl')
_DEFAULT_OUT = os.path.join(_REPO_ROOT, 'server', 'data', 'doc_catalog.json')

# Site/language suffixes observed with meaningful volume in
# intraqual_docs.Reference, requiring an explicit separator (a bare trailing
# "EN"/"BG"/... with no separator is almost always part of the document code
# itself, not a language marker — e.g. "P0004VA", "E0239MI"). A long tail of
# one-off 2-3 letter suffixes also exists (PMP, OLD, IS...) but those aren't
# languages, so they're left alone.
_LANG_SUFFIX_RE = re.compile(r'[-_. ]+(FR|EN|GB|MX|BG|CZ|BR|ES)$', re.IGNORECASE)
_NON_ALNUM_RE = re.compile(r'[^A-Z0-9]')


def _base_ref(ref: str) -> str:
    """Canonical grouping/matching key: strip a trailing site/language suffix,
    then every remaining non-alphanumeric character.
    e.g. 'PRLAT-529_GB' -> 'PRLAT529', 'PRLAT524.FR' -> 'PRLAT524'."""
    stripped = _LANG_SUFFIX_RE.sub('', ref.upper())
    return _NON_ALNUM_RE.sub('', stripped)


def build(src: str, out: str) -> None:
    if not os.path.exists(src):
        sys.exit(f'Source not found: {src}')

    entries: list[dict] = []
    seen: set[str] = set()
    skipped = 0
    with open(src, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                skipped += 1
                continue
            ref = (row.get('Reference') or '').strip()
            if not ref:
                skipped += 1
                continue
            # Normalised key dedupes case / whitespace variants; keep the first
            # occurrence that carries a URL, otherwise the first seen.
            key = ' '.join(ref.split()).upper()
            url = (row.get('url') or '').strip() or None
            title = (row.get('Titre') or '').strip() or ref
            if key in seen:
                continue
            seen.add(key)
            entries.append({'ref': ref, 'url': url, 'title': title, 'base_ref': _base_ref(ref)})

    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, 'w', encoding='utf-8') as f:
        json.dump(entries, f, ensure_ascii=False)

    size_kb = os.path.getsize(out) / 1024
    print(f'Wrote {len(entries)} entries ({skipped} skipped) -> {out} ({size_kb:.0f} KB)')


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--src', default=_DEFAULT_SRC, help='intraqual_docs.jsonl path')
    p.add_argument('--out', default=_DEFAULT_OUT, help='output slim catalog path')
    args = p.parse_args()
    build(args.src, args.out)
