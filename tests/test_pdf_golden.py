"""Layout of the PDF report: words, links and drawings per page on a fixed document.

tests/golden/pdf_report.json holds the expected layout; the
creation date is the only non-deterministic part of the file and is not compared.
"""

import datetime as dt
import json
from pathlib import Path

import pymupdf

from server.services.infra.pdf_report import Section, Source, build_report_pdf

GOLDEN = Path(__file__).parent / 'golden' / 'pdf_report.json'

_MARKDOWN = """# Résumé des changements

Texte avec **gras**, *italique*, ***les deux***, `QP-0098`, ~~barré~~, un lien [Intraqual](https://example.org/a)
et des citations [1][2, 3]. Une URL nue https://example.org/b, des entités &amp; &lt;tag&gt; et un emoji 🙂 ✅.

## Liste

- Point « un » → suite
  - sous-point avec une longue phrase qui doit passer à la ligne pour vérifier le retrait des lignes suivantes
    et sa continuation
    - niveau trois
1. premier
2. deuxième

> citation sur
> deux lignes
> - et une puce

```
code   indenté
  deux
```

---

### Tableau

| Section | Changement | Criticité |
|---|---|---|
| §3.1 | Étendu à LMX ✅ | High |
| §4.2 | Valeur passée de 35 Nm à 40 Nm avec un texte assez long pour forcer un retour à la ligne dans la cellule | Low |

#### Sous-titre
##### Niveau cinq
Dernier paragraphe.
""" + '\n\n'.join(f'Paragraphe de remplissage numéro {i} pour forcer plusieurs pages. ' * 6 for i in range(14))


def _snapshot() -> dict:
    now = dt.datetime(2026, 10, 13, 9, 0, tzinfo=dt.UTC)
    sources = [Source('QP-1518', 'https://example.org/qp', 1, 'Qualification CND, rév. D, 2024-10-11'),
               Source('MR-1465', None, 2), Source('XX-1', 'https://example.org/x', None, 'cited in prose')]
    pdfs = {
        'single': build_report_pdf(kind='change_summary', title='Résumé', now=now,
                                   meta=[('Old file', 'a.pdf'), ('New file', 'b.pdf'), ('Skipped', '')],
                                   sections=[Section(_MARKDOWN)]),
        'chat': build_report_pdf(kind='chat_session', title='Une très longue question ' * 12, now=now,
                                 sections=[Section(_MARKDOWN[:600], question='Quelle est la règle FAI ?',
                                                   sources=sources),
                                           Section('Réponse **deux** [1].', question='Et ensuite ?',
                                                   sources=sources[:1])]),
        'empty': build_report_pdf(kind='unknown', title='', now=now, sections=[Section('')]),
    }
    out: dict = {}
    for name, data in pdfs.items():
        doc = pymupdf.open(stream=data, filetype='pdf')
        pages = []
        for page in doc:
            pages.append({
                'words': [[round(x, 1) for x in w[:4]] + [w[4]] for w in page.get_text('words')],
                'links': sorted([round(link['from'].x0, 1), round(link['from'].y0, 1), link.get('uri', '')]
                                for link in page.get_links()),
                'drawings': [[d['type'], [round(v, 1) for v in d['rect']]] for d in page.get_drawings()],
            })
        out[name] = {'pages': pages, 'title': doc.metadata.get('title'), 'author': doc.metadata.get('author')}
    return json.loads(json.dumps(out))


def test_pdf_layout_matches_golden():
    expected = json.loads(GOLDEN.read_text(encoding='utf-8'))
    actual = _snapshot()
    assert actual.keys() == expected.keys()
    for name in expected:
        assert actual[name] == expected[name], name
