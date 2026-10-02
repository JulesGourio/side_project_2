"""Does databricks-qwen3-embedding-0-6b cluster by LANGUAGE or by MEANING?

Embeds N concepts, each phrased in fr/en/es, and compares mean cosine
similarity across three pair buckets:
  - same_meaning_diff_lang   (the cross-lingual case retrieval depends on)
  - diff_meaning_same_lang   (a same-language false friend)
  - diff_meaning_diff_lang   (baseline floor)

If diff_meaning_same_lang >= same_meaning_diff_lang, language is a stronger
similarity signal than meaning -> the embedding model itself is biased.
Run standalone: .venv/Scripts/python.exe probe_embedding_geometry.py [PROFILE]

Findings from the 2026-07-21 run (profile UAT) are recorded in ../README.md —
this script exists to re-run the check, not to re-derive the numbers there.
"""
import sys

import numpy as np
import httpx
from databricks.sdk.core import Config

PROFILE = sys.argv[1] if len(sys.argv) > 1 else "UAT"
MODEL = "databricks-qwen3-embedding-0-6b"

_cfg = Config(profile=PROFILE)
_auth = _cfg.authenticate()
HOST = _cfg.host.rstrip("/")
HEADERS = {"Authorization": _auth["Authorization"], "Content-Type": "application/json"}

# concept_id, lang, text — QMS-domain sentences, paraphrased not word-for-word
SENTENCES = [
    (1, "fr", "Un magasinier doit-il porter un tampon personnel pour ses tâches de gestion de stock ?"),
    (1, "en", "Does a warehouse operator need a personal stamp for stock management tasks?"),
    (1, "es", "¿Un almacenista debe tener un sello personal para sus tareas de gestión de almacén?"),
    (2, "fr", "Quel équipement de protection individuelle faut-il porter pour entrer dans une chambre froide ?"),
    (2, "en", "What personal protective equipment must be worn to enter a freezer room?"),
    (2, "es", "¿Qué equipo de protección personal se debe usar para entrar en un congelador?"),
    (3, "fr", "Comment déclarer une non-conformité produit détectée en contrôle qualité ?"),
    (3, "en", "How do you report a product non-conformity found during quality control?"),
    (3, "es", "¿Cómo se declara una no conformidad de producto detectada en control de calidad?"),
    (4, "fr", "Quelle est la procédure de qualification des opérateurs en contrôle non destructif ?"),
    (4, "en", "What is the operator qualification procedure for non-destructive testing?"),
    (4, "es", "¿Cuál es el procedimiento de calificación de operadores en ensayos no destructivos?"),
    (5, "fr", "Quels documents référencent les exigences de traçabilité des lots de production ?"),
    (5, "en", "Which documents reference production batch traceability requirements?"),
    (5, "es", "¿Qué documentos referencian los requisitos de trazabilidad de los lotes de producción?"),
]


def embed(texts):
    url = f"{HOST}/serving-endpoints/{MODEL}/invocations"
    out = []
    for i in range(0, len(texts), 16):
        batch = texts[i:i + 16]
        resp = httpx.post(url, headers=HEADERS, json={"input": batch}, timeout=60)
        resp.raise_for_status()
        out += [d["embedding"] for d in resp.json()["data"]]
    a = np.array(out, dtype=float)
    a /= (np.linalg.norm(a, axis=1, keepdims=True) + 1e-9)
    return a


def main():
    vecs = embed([s[2] for s in SENTENCES])
    n = len(SENTENCES)
    sim = vecs @ vecs.T

    buckets = {"same_meaning_diff_lang": [], "diff_meaning_same_lang": [],
               "diff_meaning_diff_lang": [], "same_meaning_same_lang": []}
    for i in range(n):
        for j in range(i + 1, n):
            cid_i, lang_i, _ = SENTENCES[i]
            cid_j, lang_j, _ = SENTENCES[j]
            key = ("same_meaning" if cid_i == cid_j else "diff_meaning") + "_" + \
                  ("same_lang" if lang_i == lang_j else "diff_lang")
            buckets[key].append(sim[i, j])

    print(f"=== Cosine similarity buckets ({MODEL}) ===")
    for k, v in buckets.items():
        if v:
            print(f"{k:28s} n={len(v):3d}  mean={np.mean(v):.4f}  min={np.min(v):.4f}  max={np.max(v):.4f}")

    print("\n=== Nearest-neighbor check: for each ES sentence, is #1 NN the correct concept? ===")
    by_concept = {}
    for idx, (cid, lang, _text) in enumerate(SENTENCES):
        by_concept.setdefault(cid, {})[lang] = idx
    for cid, langs in by_concept.items():
        es_idx = langs["es"]
        candidates = sorted(((j, sim[es_idx, j]) for j in range(n) if j != es_idx), key=lambda x: -x[1])
        top_j, top_sim = candidates[0]
        top_cid, top_lang, _ = SENTENCES[top_j]
        verdict = "OK" if top_cid == cid else "WRONG (concept drift)"
        print(f"concept {cid}: nearest neighbor = concept {top_cid} [{top_lang}] sim={top_sim:.4f}  {verdict}")


if __name__ == "__main__":
    main()
