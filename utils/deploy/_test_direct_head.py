"""Test: HEAD docview.aspx?iddoc=<id>&diff=<n> directement, sans passer par liredocumentdepuisrecherche.

Si ça marche, on peut récupérer le vrai nom de fichier avec ZERO consultation loggée,
juste en utilisant l'IdDocument de la grille + diff fixe.
"""
import sys, re, os
from urllib.parse import unquote
from playwright.sync_api import sync_playwright

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

PROFILE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".intraqual_profile")
BASE_URL    = "https://intraqual.lat.corp/intraqual_prod/V5/doc/cadre_doc.aspx?from=ident"

TESTS = [
    (43, "MI-1000"),
    (44, "MIT-1016"),
    (45, "MR-1000"),
]


def parse_cd(cd):
    m = re.search(r"filename\*=(?:UTF-8'')?([^;]+)", cd, re.IGNORECASE)
    if m:
        return unquote(m.group(1).strip().strip('"'))
    m = re.search(r'filename="?([^";]+)"?', cd, re.IGNORECASE)
    return m.group(1).strip() if m else None


with sync_playwright() as p:
    ctx = p.chromium.launch_persistent_context(
        PROFILE_DIR,
        channel="msedge",
        headless=True,
        ignore_https_errors=True,
        args=["--auth-server-allowlist=*.lat.corp",
              "--auth-negotiate-delegate-allowlist=*.lat.corp"],
    )
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    page.goto(BASE_URL, wait_until="domcontentloaded")

    print(f"\n{'—'*60}")
    print("Test : HEAD direct sur docview.aspx?iddoc=<id>&diff=<n>")
    print("(sans passer par liredocumentdepuisrecherche)")
    print(f"{'—'*60}\n")

    for iddoc, ref in TESTS:
        print(f"[{ref}] iddoc={iddoc}")
        found = False
        for diff in [3, 1, 2, 4, 0]:
            url = f"https://intraqual.lat.corp/intraqual_prod/V5/Doc/docview.aspx?iddoc={iddoc}&diff={diff}"
            try:
                r = ctx.request.fetch(url, method="HEAD", timeout=10_000)
            except Exception as e:
                print(f"  diff={diff} HEAD échoué : {e}")
                continue
            h  = {k.lower(): v for k, v in r.headers.items()}
            ct = h.get("content-type", "").split(";")[0].strip()
            cd = h.get("content-disposition", "")
            name = parse_cd(cd) if cd else None
            print(f"  diff={diff} → HTTP {r.status}  ct={ct}  cd={cd or '(absent)'}")
            if name:
                print(f"  ✅ Nom fichier : {name}")
                found = True
                break
        if not found:
            print("  ❌ Aucun diff n'a retourné un Content-Disposition.")
        print()

    ctx.close()
