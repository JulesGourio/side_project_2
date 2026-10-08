"""Rank of given documents in Vector Search HYBRID results, per query wording.

Used on 2026-10-06 to tell retrieval gaps from generation gaps (docs/dev-conception.md):
- NDT question: expected documents ranked with English vs French queries, top 10 / top 30;
- Q2 / Q3 of the 5-question run: rank of the documents the KA cited and VSI missed.
Run locally (CLI profile `latecoere`): python3 scripts/vs_rank_check.py
"""
import json, subprocess
import httpx

HOST = 'https://dbc-c623749d-731b.cloud.databricks.com'
INDEX = 'dev_landingzone.qualibot.chunks_index_v1'
TOKEN = json.loads(subprocess.run(['databricks', 'auth', 'token', '-p', 'latecoere', '-o', 'json'],
                                  capture_output=True, text=True, check=True).stdout)['access_token']


def refs(query: str, k: int) -> list:
    r = httpx.post(f'{HOST}/api/2.0/vector-search/indexes/{INDEX}/query', timeout=30,
                   headers={'Authorization': f'Bearer {TOKEN}'},
                   json={'query_text': query, 'num_results': k, 'query_type': 'HYBRID', 'columns': ['REF']})
    r.raise_for_status()
    return [row[0] for row in (r.json().get('result') or {}).get('data_array', [])]


CASES = {
    'NDT': {
        'queries': {'EN': 'What documents reference the NDT/NDI qualification requirements?',
                    'FR (hand-written)': 'Quels documents font référence aux exigences de qualification du personnel CND (NDT/NDI) ?'},
        'docs': ['QP-1518', 'MR-1465', 'MI-14059', 'MR-1462', 'QM-1063', 'Q0451MQ', 'Q0451MQ_GB'],
    },
    'Q2': {
        'queries': {'EN': 'Which procedures must be updated when a supplier changes their process?',
                    'FR (generated)': "Quelles procédures doivent être mises à jour lorsqu'un fournisseur modifie son procédé ?"},
        'docs': ['QP-1153_GB', 'QP-1523_GB'],
    },
    'Q3': {
        'queries': {'EN': 'List the key quality standards applicable to composite part manufacturing.',
                    'FR (generated)': 'Normes qualité applicables à la fabrication de pièces composites'},
        'docs': ['IF20162_GB', 'IF20162_MX', 'IF20169', 'IF20169_MX'],
    },
}

for name, case in CASES.items():
    print(f'\n== {name}')
    results = {label: refs(q, 50) for label, q in case['queries'].items()}
    for doc in case['docs']:
        ranks = {label: (r.index(doc) + 1 if doc in r else None) for label, r in results.items()}
        print(f'  {doc}: ' + ' | '.join(f'{label} rank(top50)={rank}' for label, rank in ranks.items()))
