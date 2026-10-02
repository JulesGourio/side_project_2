"""Live index probe: same concepts as probe_embedding_geometry.py, but
queried against the real Vector Search index in HYBRID (production mode)
and ANN (pure vector) query_type, to isolate whether the keyword/BM25
component of HYBRID is what drags down non-fr/en queries.

Run standalone: .venv/Scripts/python.exe probe_index_language_bias.py [PROFILE]

Findings from the 2026-07-21 run are recorded in ../README.md.
"""
import sys

import httpx
from databricks.sdk.core import Config

PROFILE = sys.argv[1] if len(sys.argv) > 1 else "UAT"
INDEX = "uat_landingzone.qualibot.chunks_index_v1"
NUM_RESULTS = 8

_cfg = Config(profile=PROFILE)
_auth = _cfg.authenticate()
HOST = _cfg.host.rstrip("/")
HEADERS = {"Authorization": _auth["Authorization"], "Content-Type": "application/json"}

QUERIES = [
    (1, "fr", "Un magasinier doit-il porter un tampon personnel pour ses tâches de gestion de stock ?"),
    (1, "en", "Does a warehouse operator need a personal stamp for stock management tasks?"),
    (1, "es", "¿Un almacenista debe tener un sello personal para sus tareas de gestión de almacén?"),
    (2, "fr", "Quel équipement de protection individuelle faut-il porter pour entrer dans une chambre froide ?"),
    (2, "en", "What personal protective equipment must be worn to enter a freezer room?"),
    (2, "es", "¿Qué equipo de protección personal se debe usar para entrar en un congelador?"),
]


def query_index(text, query_type, n=NUM_RESULTS):
    url = f"{HOST}/api/2.0/vector-search/indexes/{INDEX}/query"
    payload = {
        "query_text": text,
        "columns": ["chunk_id", "IDDOC", "REF", "division", "chunk_text"],
        "num_results": n,
        "query_type": query_type,
    }
    resp = httpx.post(url, json=payload, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    cols = [c["name"] for c in data.get("manifest", {}).get("columns", [])]
    rows = data.get("result", {}).get("data_array", [])
    return [dict(zip(cols, row)) for row in rows]


def jaccard(a, b):
    if not a and not b:
        return float("nan")
    return len(a & b) / len(a | b) if (a | b) else float("nan")


def main():
    results = {}
    for cid, lang, text in QUERIES:
        for qtype in ("HYBRID", "ANN"):
            rows = query_index(text, qtype)
            iddocs = [r.get("IDDOC") for r in rows]
            results[(cid, lang, qtype)] = iddocs
            print(f"concept={cid} lang={lang} qtype={qtype:6s} -> IDDOCs: {iddocs}")

    print("\n=== Overlap: top-8 IDDOC set overlap between language pairs, per query_type ===")
    by_concept = {}
    for (cid, lang, qtype), iddocs in results.items():
        by_concept.setdefault((cid, qtype), {})[lang] = set(x for x in iddocs if x is not None)
    for qtype in ("HYBRID", "ANN"):
        print(f"\n--- query_type={qtype} ---")
        for cid in sorted(set(c for c, q in by_concept if q == qtype)):
            sets = by_concept[(cid, qtype)]
            fr, en, es = sets.get("fr", set()), sets.get("en", set()), sets.get("es", set())
            print(f"concept {cid}: |fr|={len(fr)} |en|={len(en)} |es|={len(es)}  "
                  f"J(fr,en)={jaccard(fr, en):.2f} J(fr,es)={jaccard(fr, es):.2f} J(en,es)={jaccard(en, es):.2f}")


if __name__ == "__main__":
    main()
