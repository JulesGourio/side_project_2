"""Regression guard for structured-comparison prompt noise.

The structured prompt is meant to SKIP trivial rows (number-rendering swaps,
date metadata, synonym swaps). This test runs the noise detectors over the
sample output(s) under test_prompt/ and fails if the noise level exceeds a
budget — so a prompt change that re-introduces minor-wording rows is caught.

Run only the detectors (no LLM, no token needed):
    pytest tests/test_prompt_noise.py -v

The thresholds below are the CURRENT (pre-fix) baseline observed on the
NAS 410 sample. Tighten them after improving the prompt to lock in the gain.
"""

import glob
import os

import pytest

from tests.prompt_noise import scan_rows, scan_xlsx

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SAMPLES = glob.glob(os.path.join(HERE, 'test_prompt', 'analysis_*.xlsx'))

# Budgets — lower these once the prompt is fixed to prevent regressions.
MAX_NUMBER_RENDERING = 8     # currently 8 on the NAS 410 sample (target: 0)
MAX_HIGH_NOISE = 4           # noise rows wrongly marked High (currently 4; target: 0)
MAX_IMPERATIVE_RATIONALE = 0  # rationale must read as a summary, never a command (2026-08-19)


@pytest.mark.skipif(not SAMPLES, reason='no analysis_*.xlsx sample under test_prompt/')
@pytest.mark.parametrize('xlsx', SAMPLES, ids=[os.path.basename(p) for p in SAMPLES])
def test_noise_within_budget(xlsx):
    findings = scan_xlsx(xlsx)

    by_cat = {}
    for f in findings:
        by_cat.setdefault(f.category, []).append(f)

    # Human-readable report on failure / with -s.
    print(f'\n=== Noise report: {os.path.basename(xlsx)} ===')
    for cat, items in sorted(by_cat.items()):
        print(f'  {cat}: {len(items)}')
        for f in items:
            print(f'    [{f.row}] {f.criticality} | {f.type}')
            print(f'        - {f.before}')
            print(f'        + {f.after}')

    num_render = len(by_cat.get('NUMBER_RENDERING', []))
    high_noise = sum(1 for f in findings if f.criticality.lower() == 'high')
    imperative = len(by_cat.get('IMPERATIVE_RATIONALE', []))

    assert num_render <= MAX_NUMBER_RENDERING, (
        f'{num_render} number-rendering rows (budget {MAX_NUMBER_RENDERING}) — '
        f'prompt is reporting digits-vs-spelled-out as real changes'
    )
    assert high_noise <= MAX_HIGH_NOISE, (
        f'{high_noise} noise rows marked High (budget {MAX_HIGH_NOISE}) — '
        f'trivial changes are surfacing at the top of the report'
    )
    assert imperative <= MAX_IMPERATIVE_RATIONALE, (
        f'{imperative} rationale row(s) read as a command (budget {MAX_IMPERATIVE_RATIONALE}) — '
        f'rationale must summarize the change, never instruct the reader'
    )


# ---------------------------------------------------------------------------
# Rationale style — pure in-memory rows, no xlsx sample or LLM call needed.
# These lock in what the 2026-08-19 "summary, not instruction" rewrite expects.
# ---------------------------------------------------------------------------

def _row(rationale, criticality='Medium', type_='Value changed', before='10 bar', after='15 bar'):
    return {'type': type_, 'criticality': criticality, 'before': before, 'after': after,
            'rationale': rationale}


@pytest.mark.parametrize('rationale', [
    'Mettre à jour la procédure de test avec le nouveau seuil.',
    'Vérifier que le plan de formation reste dimensionné pour le nouvel effectif.',
    'Update the test procedure to reflect the new threshold.',
    'Refaire les tests de pression avec le nouveau seuil de 15 bar.',
    'Re-torque J3 connectors.',
])
def test_imperative_rationale_is_flagged(rationale):
    findings = scan_rows([_row(rationale)])
    assert len(findings) == 1
    assert findings[0].category == 'IMPERATIVE_RATIONALE'


@pytest.mark.parametrize('rationale', [
    '',
    'Le seuil de pression du circuit hydraulique passe de 10 à 15 bar.',
    'The hydraulic circuit pressure threshold is raised from 10 to 15 bar.',
    "L'organigramme ajoute un responsable de la conformité Partie IS.",
])
def test_descriptive_rationale_is_not_flagged(rationale):
    findings = scan_rows([_row(rationale)])
    assert findings == []
