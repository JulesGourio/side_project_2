"""Passage splitting of the parsing pipeline (utils/parsing_pipeline/chunking.py)."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'utils', 'parsing_pipeline'))

import chunking  # noqa: E402

PROC = """# QP-1518 Qualification et certification du personnel CND

## 1. Objet

Cette procédure définit les règles de qualification et de certification du personnel CND conformément à l'EN 4179.

## 2. Domaine d'application

{p2}

## 3. Responsabilités

{p3}

### 3.1 Level 3

{p31}

### 3.2 Responsable de site

{p32}

## 5. Acuité visuelle

Le personnel doit passer un examen d'acuité visuelle annuel : Jaeger J1 à 30 cm. {p5}
"""


def para(word, n):
    return ' '.join([word] * n) + '.'


def doc():
    return PROC.format(p2=para('application', 120), p3=para('responsabilite', 120),
                       p31=para('niveautrois', 120), p32=para('responsable', 120), p5=para('acuite', 120))


def test_headers_are_a_real_lineage_and_sections_are_not_mixed():
    chunks = chunking.chunk_markdown(doc(), min_tokens=100, target_tokens=200, max_tokens=400)
    for c in chunks:
        h = c['metadata']
        # Header 3 only ever under its own Header 2
        if 'Header 3' in h:
            assert h['Header 2'] == '3. Responsabilités'
        # a passage never holds two level-2 sections (the tiny "1. Objet" may join "2.")
        sections = {s for s in ('2. Domaine', '3. Responsab', '5. Acuit') if s in c['chunk_text']}
        assert len(sections) <= 1, c['chunk_text']
    assert any('acuité visuelle' in c['chunk_text'].lower() and c['metadata'].get('Header 2') == '5. Acuité visuelle'
               for c in chunks)


def test_section_path_written_once_per_passage():
    for c in chunking.chunk_markdown(doc(), min_tokens=100, target_tokens=200, max_tokens=400):
        assert c['chunk_text'].count('QP-1518 Qualification') <= 1


def test_consecutive_passages_of_a_section_overlap():
    text = '# Doc\n\n## 1. Long\n\n' + '\n\n'.join(f'Phrase numéro {i} ' + 'mot ' * 60 for i in range(12))
    chunks = chunking.chunk_markdown(text, min_tokens=100, target_tokens=200, max_tokens=400)
    assert len(chunks) >= 2
    for a, b in zip(chunks, chunks[1:]):
        assert a['chunk_text'].strip()[-40:] in b['chunk_text']


def test_char_ceiling_holds_even_when_tokens_are_few():
    dots = '\n\n'.join('Chapitre ' + str(i) + ' ' + '.' * 300 + ' ' + str(i) for i in range(40))
    for c in chunking.chunk_markdown('# T\n\n' + dots, max_chars=2000, count_tokens=lambda t: len(t) // 50):
        assert c['chunk_char_count'] <= 2400  # body + section line + overlap


def test_toc_and_front_matter_are_marked():
    toc = '# Doc\n\n## Sommaire\n\n' + '\n'.join(f'{i}. Partie {i} ........ {i + 2}' for i in range(1, 8))
    assert chunking.chunk_markdown(toc)[0]['chunk_content_type'] == 'toc'
    fm = '# Doc\n\n## Historique des modifications\n\n| Indice | Date |\n|---|---|\n| A | 2020 |'
    assert chunking.chunk_markdown(fm)[0]['chunk_content_type'] == 'front_matter'
    body = '# Doc\n\n## 4. Approbation des dérogations\n\n' + 'La dérogation est approuvée par le Level 3. ' * 3
    assert chunking.chunk_markdown(body + '\n\n' + para('texte', 300))[-1]['chunk_content_type'] == 'text'


def test_long_tables_repeat_their_header():
    rows = '\n'.join(f'| ligne {i} | valeur {i} |' for i in range(200))
    chunks = chunking.chunk_markdown('# T\n\n| Col A | Col B |\n|---|---|\n' + rows, target_tokens=200, max_tokens=400)
    assert len(chunks) > 1 and all('| Col A | Col B |' in c['chunk_text'] for c in chunks)


def test_language_and_spreadsheet_header():
    assert chunking.detect_language('', 'Q0102QP_GB') == 'en'
    assert chunking.detect_language('Le contrôle des pièces est fait par le service qualité et la production.') == 'fr'
    rows = [['LATECOERE', '', ''], ['', '', ''], ['Procédé', 'Site', 'Statut'], ['Peinture', 'TLS', 'OK']]
    assert chunking.header_row_index(rows) == 2


def test_image_anchor_and_body():
    chunks = [(0, '[1. Objet]\n\nIntro du document.', {'Header 2': '1. Objet'}),
              (1, '[4. Logigramme]\n\nLe traitement des non-conformités suit les étapes ci-dessous.',
               {'Header 2': '4. Logigramme'})]
    ctx = 'Le traitement des non-conformités suit les étapes ci-dessous.\n\n[... IMAGE INSERTED HERE ...]\n\nSuite'
    idx, hdr = chunking.image_anchor(ctx, chunks)
    assert idx == 1 and hdr['Header 2'] == '4. Logigramme'
    body = chunking.image_passage_body('# [FLOWCHART] Logigramme', ['Figure 3 – Traitement des NC'], hdr)
    assert body.startswith('Section : 4. Logigramme\nLégende : Figure 3')


def test_long_transcription_is_split_with_its_title():
    text = '# [TEXT_DOC] Formulaire\n**Content:**\n\n' + '\n\n'.join(para('mot', 150) for _ in range(10))
    parts = chunking.split_long_description(text, max_chars=1500)
    assert len(parts) > 1 and all(p.startswith('# [TEXT_DOC] Formulaire') for p in parts)
    assert all(len(p) <= 1600 for p in parts)


def test_tiny_section_joins_the_next_one_with_its_own_marker():
    text = '# Doc\n\n## 1. Objet\n\nCourt.\n\n## 2. Suite\n\n' + para('contenu', 200)
    first = chunking.chunk_markdown(text, min_tokens=100, target_tokens=200, max_tokens=400)[0]
    assert '[1. Objet]' in first['chunk_text'] and first['metadata'] == {'Header 1': 'Doc'}


# Real first pages seen in chunks_v2a (2026-10-08).
COVER = """Type de document : 13 - Notice de Formulaire et formulaire - NF
Répartition des tâches
CONFIDENTIALITE
Niveau 1 Niveau 2 Niveau 3 - Information sensible Niveau 4 - Information Classifiée
Voir DGL-1056 pour règles de confidentialité
CIRCUIT DE VALIDATION
VALIDATION CIRCUIT
REDACTION / WRITTEN BY
BESSAC Audrey _ BESSAC Audrey
VALIDATION / VALIDATED BY
DUPONT Jean (2026-02-11)
RESUME / SUMMARY
Cette notice de formulaire explique comment remplir la fiche de répartition des tâches d'un service.
DOMAINE D'APPLICATION / SCOPE
Cette notice et son formulaire sont applicables pour tout Latécoère."""


def kinds(text, **kw):
    return [(c['chunk_content_type'], c['chunk_text']) for c in chunking.chunk_markdown(text, **kw)]


def test_cover_block_is_cut_from_the_summary_that_follows():
    out = kinds(COVER + '\n\n' + para('contenu', 200))
    front = [t for k, t in out if k == 'front_matter']
    content = [t for k, t in out if k != 'front_matter']
    assert front and 'BESSAC' in front[0] and 'RESUME' not in front[0]
    assert any('explique comment remplir' in t for t in content)


def test_toc_with_page_numbers_and_toc_in_a_table():
    toc = '# Plan\n\n## Table of contents\n\n' + '\n\n'.join(
        f'{i}. Partie numéro {i} {i + 2}' for i in range(1, 12)) + '\n\n## 1. Purpose\n\n' + para('texte', 200)
    out = kinds(toc)
    assert out[0][0] == 'toc' and all(k != 'toc' for k, _ in out[1:])
    table = ('| 1. | INTRODUCTION' + '.' * 40 + '4 | x |\n|---|---|---|\n'
             + '\n'.join(f'| {i}. | PARTIE {i}' + '.' * 40 + f'{i + 4} | x |' for i in range(2, 9)))
    assert kinds('# T\n\n' + table)[0][0] == 'toc'


def test_revision_history_and_approval_table_are_front_matter_but_not_a_process_section():
    hist = ('***VALIDATION***\n\n| **Written by** | **Checked by** | **Approved by** |\n|---|---|---|\n| A | B | C |\n\n'
            '***LATECOERE CHANGE HISTORY***\n\n| **Revision** | **Modifications** | **Date** |\n|---|---|---|\n| A | Logo | 2024 |')
    assert {k for k, _ in kinds(hist + '\n\n## 1. Scope\n\n' + para('texte', 200))[:1]} == {'front_matter'}
    body = '## 4. Approbation des dérogations\n\n' + 'La dérogation est approuvée par le Level 3 et validée par la qualité. ' * 5
    assert all(k != 'front_matter' for k, _ in kinds('# Doc\n\n' + para('intro', 2500) + '\n\n' + body))
