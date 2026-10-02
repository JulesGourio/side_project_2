"""Intraqual scraper — extrait les métadonnées et URLs de chaque document trouvé.

Fonctionnement :
  * Un seul callback serveur par page (GetSelectedFieldValues) — pas de requête
    par document, pas d'ouverture de fichier, pas de consultation dans l'audit trail.
  * La pagination attend le EndCallback DevExpress avant de passer à la page suivante.
  * FETCH_FILENAMES = True : un HEAD request par document pour récupérer le vrai nom
    de fichier serveur (ex. "MI-1000-A.doc") sans téléchargement ni consultation.

Lancement :
    python utils/deploy/scrap_ids_intraqual.py
    uv run --system-certs python utils/deploy/scrap_ids_intraqual.py
    → connecte-toi dans Edge, lance ta recherche, le scrape démarre automatiquement.

Mode diagnostic (variables d'env) :
    LIST_COLS=1   — liste les colonnes de la grille et quitte
    DIAG=1        — dump DOM de la grille et quitte
"""

from urllib.parse import urljoin, unquote
from playwright.sync_api import sync_playwright
import json
import os
import re
import sys
import time

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

# ── Configuration ──────────────────────────────────────────────────────────────
MAX_PAGES       = 0        # pages à scraper (0 = toutes)
FETCH_FILENAMES = False  # True = HEAD par doc → vrai nom fichier (ex. "MI-1000-A.doc")
                         #   1 requête/doc supplémentaire, pas de consultation loggée
OUT_JSON = "intraqual_docs.jsonl"  # JSONL : 1 objet JSON par ligne, lisible par Databricks

# Colonnes à extraire depuis la grille Intraqual.
# False = ignorée dans le CSV. IdDocument est toujours récupéré si FETCH_FILENAMES.
COLUMNS = {
    "Reference":                           True,   # REF du document (ex. MI-1000)
    "Titre":                               True,   # titre humain côté serveur
    "Indice":                              True,   # indice de révision (A, B, C…)
    "Type":                                True,   # type de document
    "NouvelIndiceENCours":                 True,   # indice en cours de révision
    "CategoriePrincipaleAvecArborescence": True,   # arborescence complète
    "Createur":                            True,   # auteur/créateur
    "MotsCles":                            True,   # mots-clés associés
    "CategoriePrincipale":                 True,   # catégorie principale
    "DateRevision":                        True,   # date de la révision
    "DateDiffusion":                       True,   # date de diffusion
    "TypeDiffusion":                       True,   # type de diffusion
    "MotifModifMajeure":                   True,   # motif de la dernière modification
    "IdDocument":                          True,   # ID entier interne (utile pour FETCH_FILENAMES)
    "Rang":                                True,   # rang dans les résultats
    "UrlLectureDocument":                  True,   # URL du viewer (token chiffré)
}
# ──────────────────────────────────────────────────────────────────────────────

URL           = "https://intraqual.lat.corp/intraqual_prod/V5/doc/cadre_doc.aspx?from=ident"
GRID_FALLBACK = "TableauRechercheDoc"
LOGIN_TIMEOUT_S    = 5 * 60       # temps pour se connecter + lancer la recherche
CALLBACK_TIMEOUT_MS = 30_000      # attente max d'un callback DevExpress
POLITENESS_DELAY_S  = 0.2         # pause entre pages (gentillesse envers le serveur)
_DIFF_CANDIDATES    = [3, 1, 2, 4, 0]   # valeurs diff= testées pour FETCH_FILENAMES


# ── JS snippets ───────────────────────────────────────────────────────────────

_DISCOVER_JS = r"""
() => {
  const grids = [];
  for (const k of Object.keys(window)) {
    try { if (window[k] instanceof ASPxClientGridView) grids.push(k); }
    catch (e) {}
  }
  return grids;
}
"""

_INFO_JS = r"""
(name) => {
  const g = ASPxClientGridView.Cast(name);
  if (!g) return { found: false };
  return {
    found: true,
    pageCount: g.GetPageCount(),
    pageIndex: g.GetPageIndex(),
    visibleRows: g.GetVisibleRowsOnPage(),
  };
}
"""

_ARM_JS = r"""
(name) => {
  const g = ASPxClientGridView.Cast(name);
  if (!g) return false;
  window.__cbDone = true;
  if (!window.__cbArmed) {
    g.EndCallback.AddHandler(() => { window.__cbDone = true; });
    window.__cbArmed = true;
  }
  return true;
}
"""

_GOTO_JS = r"""
(args) => {
  window.__cbDone = false;
  ASPxClientGridView.Cast(args.name).GotoPage(args.idx);
}
"""

# Lecture de toutes les lignes de la page en UN seul callback serveur.
_READ_JS = r"""
(args) => new Promise((resolve) => {
  let settled = false;
  const fin = (r) => { if (!settled) { settled = true; resolve(r); } };
  try {
    const g = ASPxClientGridView.Cast(args.name);
    const fields = args.fields;
    const total = g.GetVisibleRowsOnPage();
    const watchdog = setTimeout(() => fin({ total, rows: [], timedOut: true }), 25000);
    if (total === 0) { clearTimeout(watchdog); fin({ total, rows: [] }); return; }
    g.UnselectAllRowsOnPage();
    g.SelectAllRowsOnPage();
    g.GetSelectedFieldValues(fields.join(";"), (res) => {
      clearTimeout(watchdog);
      const arr = res || [];
      const rows = arr.map((vals, i) => {
        const o = { __i: i };
        fields.forEach((f, k) => { o[f] = (fields.length === 1 ? vals : vals[k]); });
        return o;
      });
      try { g.UnselectAllRowsOnPage(); } catch (e) {}
      fin({ total, rawCount: arr.length, rows });
    });
  } catch (e) { fin({ error: String(e) }); }
})
"""

_LIST_COLUMNS_JS = r"""
(name) => {
  const g = ASPxClientGridView.Cast(name);
  if (!g) return [];
  const cols = [];
  for (let i = 0; i < g.GetColumnCount(); i++) {
    const col = g.GetColumn(i);
    if (col && col.fieldName) cols.push({ index: i, fieldName: col.fieldName, caption: col.caption || "" });
  }
  return cols;
}
"""

_DOM_DIAG_JS = r"""
(name) => {
  const out = {};
  const row0 = document.getElementById(name + "_DXDataRow0");
  out.hasRow0 = !!row0;
  out.row0Html = row0 ? row0.outerHTML.slice(0, 2500) : null;
  const html = document.documentElement.innerHTML;
  const m = html.match(/lire[a-z]*document[^"'\s)>]{0,90}/gi) || [];
  out.lireMatches = Array.from(new Set(m)).slice(0, 6);
  return out;
}
"""

_PROBE1_JS = r"""
(args) => new Promise((resolve) => {
  try {
    const g = ASPxClientGridView.Cast(args.name);
    const t = setTimeout(() => resolve({ field: args.field, ok: false, timedOut: true }), 8000);
    g.GetRowValues(0, args.field, (v) => { clearTimeout(t); resolve({ field: args.field, ok: true, value: String(v) }); });
  } catch (e) { resolve({ field: args.field, ok: false, error: String(e) }); }
})
"""


# ── Helpers ───────────────────────────────────────────────────────────────────

def _active_fields():
    """Champs à demander au serveur : ceux activés dans COLUMNS + IdDocument si besoin."""
    fields = [k for k, v in COLUMNS.items() if v]
    if FETCH_FILENAMES and "IdDocument" not in fields:
        fields.append("IdDocument")
    return fields


def _csv_fieldnames():
    """Colonnes du CSV dans l'ordre : Reference, [filename,] autres..., url."""
    names = []
    for col in COLUMNS:
        if not COLUMNS[col]:
            continue
        if col == "Reference":
            names.append("Reference")
            if FETCH_FILENAMES:
                names.append("filename")
        elif col == "UrlLectureDocument":
            names.append("url")
        else:
            names.append(col)
    # Si Reference ou UrlLectureDocument était désactivé, on s'assure qu'on n'a pas de trou.
    return names


def _parse_cd(cd):
    """Extrait le nom de fichier d'un header Content-Disposition."""
    m = re.search(r"filename\*=(?:UTF-8'')?([^;]+)", cd, re.IGNORECASE)
    if m:
        return unquote(m.group(1).strip().strip('"'))
    m = re.search(r'filename="?([^";]+)"?', cd, re.IGNORECASE)
    return m.group(1).strip() if m else None


def _fetch_filename(context, doc_id):
    """HEAD docview.aspx?iddoc=<id>&diff=<n> → nom de fichier réel, sans téléchargement."""
    for diff in _DIFF_CANDIDATES:
        url = (f"https://intraqual.lat.corp/intraqual_prod/V5/Doc/"
               f"docview.aspx?iddoc={doc_id}&diff={diff}")
        try:
            resp = context.request.fetch(url, method="HEAD", timeout=10_000)
        except Exception:
            continue
        cd = {k.lower(): v for k, v in resp.headers.items()}.get("content-disposition", "")
        if cd:
            return _parse_cd(cd)
    return None


def _find_grid_frame(page):
    deadline = time.monotonic() + LOGIN_TIMEOUT_S
    announced = False
    while time.monotonic() < deadline:
        for fr in page.frames:
            try:
                grids = fr.evaluate(_DISCOVER_JS)
            except Exception:
                grids = []
            if grids:
                return fr, grids
        if not announced:
            print("   …grille pas encore détectée — connecte-toi / lance la "
                  "recherche, je scrute tous les frames.", flush=True)
            announced = True
        time.sleep(2)
    return None, []


# Libellés de la page de garde à franchir avant que la grille de résultats
# n'apparaisse. Best-effort : si les libellés ne matchent pas (page/version
# différente), on abandonne silencieusement -- l'utilisateur peut toujours
# cliquer à la main, _find_grid_frame attend de toute façon indéfiniment.
_ADVANCED_SEARCH_STEPS = ["Recherche avancée", "Lancer la recherche avancée"]


def _try_auto_advanced_search(page, timeout_s=20):
    """Clique automatiquement les 2 étapes de la page de garde (au lieu de les
    attendre manuellement) : 'Recherche avancée' puis 'Lancer la recherche
    avancée'. Cherche dans tous les frames, par texte visible puis par
    value= (boutons ASPx rendus en <input type=submit>)."""
    for label in _ADVANCED_SEARCH_STEPS:
        deadline = time.monotonic() + timeout_s
        clicked = False
        while time.monotonic() < deadline and not clicked:
            for fr in page.frames:
                for locator in (
                    fr.get_by_text(label, exact=False),
                    fr.locator(f'input[value="{label}"]'),
                    fr.get_by_role("button", name=label, exact=False),
                    fr.get_by_role("link", name=label, exact=False),
                ):
                    try:
                        if locator.count() > 0:
                            locator.first.click(timeout=2000)
                            print(f"   🔎 clic auto : {label!r}")
                            clicked = True
                            break
                    except Exception:
                        continue
                if clicked:
                    break
            if not clicked:
                time.sleep(0.5)
        if not clicked:
            print(f"   ⚠️  bouton {label!r} introuvable après {timeout_s}s "
                  f"— clique-le toi-même si besoin.")
            return False
        time.sleep(1)  # laisser la page suivante se charger avant le clic suivant
    return True


def _read_current_page(frame, grid_name, fields):
    res = frame.evaluate(_READ_JS, {"name": grid_name, "fields": fields})
    if res.get("error"):
        print(f"   ⚠️  lecture page: {res['error']}")
        return [], 0
    rows = sorted(res.get("rows", []), key=lambda r: r.get("__i", 0))
    total = res.get("total", 0)
    raw   = res.get("rawCount", len(rows))
    if res.get("timedOut"):
        print(f"   ⏱️  timeout JS (page de {total} lignes)")
    elif raw != total:
        print(f"   ℹ️  {raw}/{total} lignes renvoyées")
    return rows, total


def _read_page_with_retries(frame, grid_name, fields, attempts=3):
    prev_count = -1
    for a in range(attempts):
        rows, total = _read_current_page(frame, grid_name, fields)
        if (total == 0) or (len(rows) >= total):
            return rows
        # Résultat stable sur 2 tentatives consécutives = row non-data (groupe/template)
        if len(rows) == prev_count:
            return rows
        print(f"   🔄 résultat partiel ({len(rows)}/{total}) — retry {a + 1}/{attempts}")
        prev_count = len(rows)
        if a < attempts - 1:
            time.sleep(1.5)
    return rows


def _diagnose_empty(frame, grid_name, fields):
    for field in fields:
        d = frame.evaluate(_PROBE1_JS, {"name": grid_name, "field": field})
        print(f"   🔬 {field}: {d}")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    fields = _active_fields()

    with sync_playwright() as p:
        browser = p.chromium.launch(channel="msedge", headless=False)
        context = browser.new_context(ignore_https_errors=True)
        page    = context.new_page()
        page.goto(URL, wait_until="domcontentloaded")

        print("\n👉 SSO transparente en cours + clic auto 'Recherche avancée' → "
              "'Lancer la recherche avancée'…", flush=True)
        _try_auto_advanced_search(page)

        print(f"   Scrape démarre dès que la grille apparaît "
              f"(timeout {LOGIN_TIMEOUT_S // 60} min)… "
              f"(si le clic auto a échoué, clique toi-même dans la fenêtre Edge)",
              flush=True)

        frame, grids = _find_grid_frame(page)
        if frame is None:
            print("❌ Aucune grille trouvée.")
            context.close()
            return

        grid_name = grids[0] if grids else GRID_FALLBACK
        info = frame.evaluate(_INFO_JS, grid_name)
        if not info.get("found"):
            grid_name = GRID_FALLBACK
            info = frame.evaluate(_INFO_JS, grid_name)
            if not info.get("found"):
                print("❌ Grille non castable.")
                context.close()
                return

        page_count = info.get("pageCount") or 1
        per_page   = info.get("visibleRows") or 0
        if MAX_PAGES and page_count > MAX_PAGES:
            print(f"   (cap MAX_PAGES={MAX_PAGES} sur {page_count} pages disponibles)")
            page_count = MAX_PAGES

        # ── Modes diagnostics (env vars ponctuels) ──
        if os.getenv("LIST_COLS"):
            cols = frame.evaluate(_LIST_COLUMNS_JS, grid_name)
            print(f"\n📋 Colonnes disponibles dans '{grid_name}' ({len(cols)}) :")
            for c in cols:
                enabled = "✓" if COLUMNS.get(c["fieldName"]) else " "
                print(f"   [{c['index']:2d}] {enabled} {c['fieldName']:<40s} {c['caption']!r}")
            context.close()
            return

        if os.getenv("DIAG"):
            import json as _json
            diag = frame.evaluate(_DOM_DIAG_JS, grid_name)
            print("\n🔬 DIAGNOSTIC DOM :")
            print(_json.dumps(diag, ensure_ascii=False, indent=2)[:4000])
            context.close()
            return

        # ── Scrape ──
        print(f"\n✅ Grille '{grid_name}' — {page_count} page(s), ~{per_page} lignes/page")
        print(f"   Champs extraits ({len(fields)}) : {', '.join(fields)}")
        if FETCH_FILENAMES:
            print(f"   📎 FETCH_FILENAMES — HEAD par doc pour le vrai nom fichier")
        print(f"   → {OUT_JSON}", flush=True)

        frame.evaluate(_ARM_JS, grid_name)

        collected = []
        seen      = set()
        current   = info.get("pageIndex", 0)
        diagnosed = False

        for idx in range(page_count):
            paginated = idx != current
            if paginated:
                frame.evaluate(_GOTO_JS, {"name": grid_name, "idx": idx})
                try:
                    frame.wait_for_function(
                        "(name) => { try { const g = ASPxClientGridView.Cast(name); "
                        "return window.__cbDone === true && g && !g.InCallback(); } "
                        "catch(e){ return false; } }",
                        arg=grid_name,
                        timeout=CALLBACK_TIMEOUT_MS,
                    )
                except Exception:
                    print(f"   ⚠️  page {idx + 1}: callback timeout")
                current = idx
                time.sleep(POLITENESS_DELAY_S)

            rows = _read_page_with_retries(frame, grid_name, fields)
            if not rows and paginated and not diagnosed:
                print("   (page vide après pagination — diagnostic :)")
                _diagnose_empty(frame, grid_name, fields)
                diagnosed = True

            new = 0
            for r in rows:
                ref = (r.get("Reference") or "").strip()
                rel = (r.get("UrlLectureDocument") or "").strip()
                url = urljoin(frame.url, rel) if rel else ""
                key = (ref, url)
                if key in seen:
                    continue
                seen.add(key)

                row = {}
                for col in COLUMNS:
                    if not COLUMNS[col]:
                        continue
                    if col == "Reference":
                        row["Reference"] = ref
                        if FETCH_FILENAMES:
                            row["filename"] = (
                                _fetch_filename(context, r.get("IdDocument")) or ""
                                if r.get("IdDocument") else ""
                            )
                    elif col == "UrlLectureDocument":
                        row["url"] = url
                    else:
                        row[col] = (str(r.get(col) or "")).strip()

                collected.append(row)
                new += 1

            print(f"   📄 page {idx + 1}/{page_count} : +{new} (total {len(collected)})",
                  flush=True)

        with open(OUT_JSON, "w", encoding="utf-8") as f:
            for doc in collected:
                f.write(json.dumps(doc, ensure_ascii=False) + "\n")

        print(f"\n✅ {len(collected)} documents → {OUT_JSON}")
        context.close()


if __name__ == "__main__":
    main()
