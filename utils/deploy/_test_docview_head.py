"""Diagnostic: get the real server filename without downloading the file body.

Strategy:
  1. Read URLs from intraqual_docs.csv (already scraped — no extra server hits).
  2. GET liredocumentdepuisrecherche?id=<token>  → HTML frameset (logs consultation ⚠️)
     Parse out the docview.aspx?iddoc=...&diff=... frame URL.
  3. HEAD docview.aspx?iddoc=...&diff=...  → Content-Disposition header only (no body).
     This is the real filename without downloading the file.

Step 2 very likely logs a "consultation" in Intraqual's audit trail.
Step 3 (HEAD) does not transfer the file body, so it's lightweight.

Run:  python utils/deploy/_test_docview_head.py
"""

import csv
import os
import re
import sys
from urllib.parse import unquote
from playwright.sync_api import sync_playwright

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

BASE_URL    = "https://intraqual.lat.corp/intraqual_prod/V5/doc/cadre_doc.aspx?from=ident"
PROFILE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".intraqual_profile")
IN_CSV      = os.getenv("IN_CSV", "intraqual_docs.csv")
N_DOCS      = 3

_DOCVIEW_RE = re.compile(r'src="([^"]*docview\.aspx[^"]*)"', re.IGNORECASE)


def _parse_cd(cd):
    """Extract filename from Content-Disposition header."""
    m = re.search(r"filename\*=(?:UTF-8'')?([^;]+)", cd, re.IGNORECASE)
    if m:
        return unquote(m.group(1).strip().strip('"'))
    m = re.search(r'filename="?([^";]+)"?', cd, re.IGNORECASE)
    return m.group(1).strip() if m else None


def main():
    if not os.path.exists(IN_CSV):
        print(f"❌ CSV introuvable : {IN_CSV} — lance d'abord scrap_ids_intraqual.py.")
        return

    with open(IN_CSV, newline="", encoding="utf-8-sig") as f:
        rows = [r for r in csv.DictReader(f) if r.get("url")][:N_DOCS]

    if not rows:
        print("❌ CSV vide.")
        return

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            PROFILE_DIR,
            channel="msedge",
            headless=True,
            ignore_https_errors=True,
            args=["--auth-server-allowlist=*.lat.corp",
                  "--auth-negotiate-delegate-allowlist=*.lat.corp"],
        )
        page = context.pages[0] if context.pages else context.new_page()
        page.goto(BASE_URL, wait_until="domcontentloaded")

        print(f"\n{'—'*60}")
        for row in rows:
            ref  = row.get("reference", "?")
            url  = row["url"]
            print(f"\n[{ref}]")
            print(f"  Étape 1 — GET liredocumentdepuisrecherche (⚠️ log consultation)")
            print(f"  {url}")

            try:
                r1 = context.request.get(url, timeout=20_000)
            except Exception as e:
                print(f"  ❌ GET échoué : {e}")
                continue

            h1  = {k.lower(): v for k, v in r1.headers.items()}
            ct1 = h1.get("content-type", "").split(";")[0].strip().lower()
            print(f"  → HTTP {r1.status}  ct={ct1}")

            if ct1 != "text/html":
                cd = h1.get("content-disposition", "")
                print(f"  ✅ Binaire direct  cd={cd or '(absent)'}")
                if cd:
                    print(f"  Nom fichier : {_parse_cd(cd)}")
                continue

            html1 = r1.text()
            m = _DOCVIEW_RE.search(html1)
            if not m:
                print(f"  ⚠️  Pas de frame docview trouvée dans le HTML.")
                print(f"  Snippet : {html1[:400]!r}")
                continue

            docview_url = m.group(1).replace("&amp;", "&")
            if not docview_url.startswith("http"):
                docview_url = "https://intraqual.lat.corp" + (
                    docview_url if docview_url.startswith("/") else "/" + docview_url
                )
            print(f"  Étape 2 — HEAD docview (pas de corps, pas de log)")
            print(f"  {docview_url}")

            try:
                r2 = context.request.fetch(docview_url, method="HEAD", timeout=15_000)
            except Exception as e:
                print(f"  ❌ HEAD échoué : {e}")
                continue

            h2  = {k.lower(): v for k, v in r2.headers.items()}
            ct2 = h2.get("content-type", "").split(";")[0].strip().lower()
            cd2 = h2.get("content-disposition", "")
            print(f"  → HTTP {r2.status}  ct={ct2}")
            print(f"  Content-Disposition : {cd2 or '(absent)'}")
            if cd2:
                name = _parse_cd(cd2)
                print(f"  ✅ Nom fichier : {name}")
            else:
                print(f"  ℹ️  Pas de Content-Disposition sur HEAD — essai GET pour vérifier…")
                try:
                    r2g = context.request.get(docview_url, timeout=15_000)
                    h2g = {k.lower(): v for k, v in r2g.headers.items()}
                    cd2g = h2g.get("content-disposition", "")
                    ct2g = h2g.get("content-type", "").split(";")[0].strip().lower()
                    print(f"  GET → HTTP {r2g.status}  ct={ct2g}  cd={cd2g or '(absent)'}")
                    if cd2g:
                        print(f"  ✅ Nom fichier (via GET) : {_parse_cd(cd2g)}")
                except Exception as e:
                    print(f"  ❌ GET fallback échoué : {e}")

        print(f"\n{'—'*60}")
        context.close()
        context.close()


if __name__ == "__main__":
    main()
