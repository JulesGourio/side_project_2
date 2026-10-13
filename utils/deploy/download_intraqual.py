"""Intraqual downloader — fetch the actual document file(s) for a REF / URL.

Companion to `scrap_ids_intraqual.py`. That scraper collects, per search result,
a `reference` + an absolute document URL (`liredocumentdepuisrecherche?id=<token>`)
into `intraqual_docs.csv` WITHOUT ever opening them. This script does the opposite:
it OPENS those URLs to download the files. Be aware that, unlike the scraper, each
download is one server request AND logs a "consultation" in Intraqual's audit trail.

How it works:
  * Runs headless by default (no Edge window). Intraqual authenticates via
    transparent Windows SSO, so navigating to the app is enough to get the session
    cookies — no password prompt. Set HEADFUL=1 to show the window for debugging or
    if an interactive login is ever required.
  * Uses a PERSISTENT browser profile (scripts/.intraqual_profile) to reuse cookies.
  * Downloads through the authenticated context (`context.request.get`), which
    shares the browser cookies. This returns the raw bytes in a single request and
    sidesteps the in-browser PDF viewer entirely.
  * Names each file from the server's `Content-Disposition`, falling back to the
    REF + an extension guessed from `Content-Type`.

Targets (first match wins):
  1. a CLI argument — a single REF (looked up in the CSV) or a full URL
  2. env TARGET_URL  (+ optional TARGET_REF for the filename) / env TARGET_REF
  3. the first MAX_DOWNLOADS rows of intraqual_docs.csv (default 1)

Download a SINGLE REF (looked up in the CSV):
    python utils/deploy/download_intraqual.py MI-1000

Download a specific URL directly:
    python utils/deploy/download_intraqual.py "https://intraqual.lat.corp/.../liredocumentdepuisrecherche?id=..."

Quick test (first CSV row):
    python utils/deploy/download_intraqual.py

Download the whole CSV:
    PowerShell:  $env:MAX_DOWNLOADS="0"; python utils/deploy/download_intraqual.py
"""

from urllib.parse import urljoin, unquote
from playwright.sync_api import sync_playwright
import csv
import os
import re
import sys
import time

# Windows consoles default to cp1252 and choke on the emojis below.
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

BASE_URL = "https://intraqual.lat.corp/intraqual_prod/V5/doc/cadre_doc.aspx?from=ident"
PROFILE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".intraqual_profile")
IN_CSV = os.getenv("IN_CSV", "intraqual_docs.csv")
OUT_DIR = os.getenv("OUT_DIR", "intraqual_downloads")
MAX_DOWNLOADS = int(os.getenv("MAX_DOWNLOADS", "1"))   # 0 = all rows of the CSV
POLITENESS_DELAY_S = 0.8                                # pause between documents
REQUEST_TIMEOUT_MS = 60_000
LOGIN_TIMEOUT_S = 30 * 60                               # time to log in when non-interactive

# Content-Type -> extension, for when the server gives no filename.
_CT_EXT = {
    "application/pdf": ".pdf",
    "application/msword": ".doc",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/vnd.ms-excel": ".xls",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
    "application/vnd.ms-powerpoint": ".ppt",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": ".pptx",
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/tiff": ".tif",
    "text/plain": ".txt",
    "application/zip": ".zip",
    "text/html": ".html",
}


def _safe_name(s):
    """Make a string usable as a filename on Windows."""
    s = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", (s or "").strip())
    return s[:120] or "document"


def _filename_from_headers(headers, ref, url):
    """Name the file by REF (keeping the real extension). Extension comes from the
    server filename if any, else from Content-Type. With no REF, keep the server
    filename."""
    cd = headers.get("content-disposition", "")
    server_name = None
    # RFC 5987: filename*=UTF-8''percent%20encoded.pdf
    m = re.search(r"filename\*=(?:UTF-8'')?([^;]+)", cd, re.IGNORECASE)
    if m:
        server_name = unquote(m.group(1).strip().strip('"'))
    if not server_name:
        m = re.search(r'filename="?([^";]+)"?', cd, re.IGNORECASE)
        if m:
            server_name = m.group(1).strip()

    ext = ""
    if server_name and os.path.splitext(server_name)[1]:
        ext = os.path.splitext(server_name)[1]
    else:
        ct = (headers.get("content-type", "") or "").split(";")[0].strip().lower()
        ext = _CT_EXT.get(ct, "")

    if ref:
        return _safe_name(ref) + ext
    if server_name:
        return _safe_name(server_name)
    return _safe_name(os.path.basename(url.split("?")[0]) or "document") + ext


def _get(context, url):
    """GET helper returning (response, lowercased_headers, content_type) or (None,...)."""
    try:
        resp = context.request.get(url, timeout=REQUEST_TIMEOUT_MS)
    except Exception as e:
        return None, {}, str(e)
    headers = {k.lower(): v for k, v in resp.headers.items()}
    ct = (headers.get("content-type", "") or "").split(";")[0].strip().lower()
    return resp, headers, ct


def _download_one(context, ref, url, out_dir):
    """Fetch one document through the authenticated context. Returns the path or None.

    The `liredocumentdepuisrecherche` URL returns a viewer frameset; the real file
    is served by its `docview.aspx?iddoc=..&diff=..` frame, which we follow here."""
    label = ref or url
    resp, headers, ct = _get(context, url)
    if resp is None:
        print(f"   ⚠️  {label}: requête échouée — {ct}")
        return None
    if not resp.ok:
        print(f"   ⚠️  {label}: HTTP {resp.status} {resp.status_text}")
        return None

    # Viewer frameset → follow the docview frame to the actual binary.
    if ct == "text/html":
        m = _DOCVIEW_RE.search(resp.text())
        if not m:
            print(f"   ⚠️  {label}: page HTML sans frame docview — viewer ou login ? "
                  f"Sauvegardé en .html pour inspection.")
            body = resp.body()
            path = os.path.join(out_dir, _safe_name(ref or "document") + ".html")
            with open(path, "wb") as f:
                f.write(body)
            return None
        file_url = urljoin(url, m.group(1).replace("&amp;", "&"))
        resp, headers, ct = _get(context, file_url)
        if resp is None:
            print(f"   ⚠️  {label}: docview échoué — {ct}")
            return None
        if not resp.ok:
            print(f"   ⚠️  {label}: docview HTTP {resp.status} {resp.status_text}")
            return None

    body = resp.body()
    name = _filename_from_headers(headers, ref, url)
    path = os.path.join(out_dir, name)
    # Avoid clobbering when several REFs resolve to the same filename.
    base, ext = os.path.splitext(path)
    n = 1
    while os.path.exists(path):
        path = f"{base}({n}){ext}"
        n += 1

    with open(path, "wb") as f:
        f.write(body)
    print(f"   ✅ {label} → {path}  ({len(body)} octets, {ct or 'type inconnu'})")
    return path


def _has_password_field(page):
    """True if any frame is showing a password input (i.e. a login screen)."""
    try:
        if page.query_selector("input[type=password]"):
            return True
        for fr in page.frames:
            try:
                if fr.query_selector("input[type=password]"):
                    return True
            except Exception:
                pass
    except Exception:
        pass
    return False


def _ensure_session(page, headful):
    """Intraqual uses transparent Windows SSO — there is no password prompt, so
    simply navigating to the app obtains the session cookies. Only if a real login
    form appears do we need a visible window (HEADFUL=1) to complete it."""
    deadline = time.monotonic() + (LOGIN_TIMEOUT_S if headful else 15)
    while time.monotonic() < deadline:
        if "intraqual_prod" in (page.url or "") and not _has_password_field(page):
            return True
        if not headful:
            break
        print("   …connecte-toi dans la fenêtre Edge.", flush=True)
        time.sleep(3)
    if _has_password_field(page):
        print("   ⚠️  Connexion interactive nécessaire — relance avec HEADFUL=1 "
              "pour te connecter dans la fenêtre Edge.")
        return False
    return True


_FRAME_SRC_RE = re.compile(r'src="([^"]+\.aspx[^"]*)"', re.IGNORECASE)
# Inside the viewer frameset, this frame serves the real binary file.
_DOCVIEW_RE = re.compile(r'src="([^"]*docview\.aspx[^"]*)"', re.IGNORECASE)


def _diagnose_chain(context, ref, url, depth=0, seen=None):
    """Follow the viewer frameset and report each frame's content-type, so we can
    find where the real binary file is served. Diagnostic only (DIAG=1)."""
    seen = seen if seen is not None else set()
    indent = "   " * (depth + 1)
    if url in seen or depth > 3:
        return
    seen.add(url)
    try:
        resp = context.request.get(url, timeout=REQUEST_TIMEOUT_MS)
    except Exception as e:
        print(f"{indent}⚠️  {url} — {e}")
        return
    h = {k.lower(): v for k, v in resp.headers.items()}
    ct = (h.get("content-type", "") or "").split(";")[0].strip().lower()
    cd = h.get("content-disposition", "")
    body = resp.body()
    print(f"{indent}→ HTTP {resp.status}  {ct or '?'}  size={len(body)}"
          + (f"  cd={cd}" if cd else ""))
    print(f"{indent}  {url}")
    if ct == "text/html" or ct == "":
        html = resp.text()
        srcs = []
        for s in _FRAME_SRC_RE.findall(html):
            full = urljoin(url, s.replace("&amp;", "&"))
            if full not in srcs:
                srcs.append(full)
        if not srcs:
            print(f"{indent}  (pas de sous-frame ; snippet) {html[:400]!r}")
        for s in srcs:
            _diagnose_chain(context, ref, s, depth + 1, seen)


def _load_csv_rows():
    """Read intraqual_docs.csv -> list of {reference, url} rows (URL non-empty)."""
    if not os.path.exists(IN_CSV):
        print(f"❌ CSV introuvable : {IN_CSV} (lance d'abord scrap_ids_intraqual.py).")
        return None
    with open(IN_CSV, newline="", encoding="utf-8-sig") as f:
        return [r for r in csv.DictReader(f) if (r.get("url") or "").strip()]


def _resolve_targets(cli_target=None):
    """Build the (reference, url) list to download. Priority:
       1. a CLI argument — either a single REF (looked up in the CSV) or a full URL;
       2. env TARGET_URL (+ optional TARGET_REF) / env TARGET_REF;
       3. the first MAX_DOWNLOADS rows of the CSV (MAX_DOWNLOADS=0 → all)."""
    target = cli_target or os.getenv("TARGET_URL") or os.getenv("TARGET_REF")
    ref_label = os.getenv("TARGET_REF")

    # A full URL given directly — no CSV lookup needed.
    if target and target.lower().startswith("http"):
        return [(ref_label or "document", target)]

    # A single REF — look it up in the CSV to recover its (tokenised) URL.
    if target:
        rows = _load_csv_rows()
        if rows is None:
            return []
        match = [r for r in rows if (r.get("reference") or "").strip() == target]
        if not match:
            print(f"❌ REF '{target}' absente de {IN_CSV}.")
            return []
        return [((r.get("reference") or "").strip(), (r.get("url") or "").strip()) for r in match]

    # No explicit target — batch the first MAX_DOWNLOADS rows of the CSV.
    rows = _load_csv_rows()
    if rows is None:
        return []
    if MAX_DOWNLOADS:
        rows = rows[:MAX_DOWNLOADS]
    return [((r.get("reference") or "").strip(), (r.get("url") or "").strip()) for r in rows]


def main():
    cli_target = sys.argv[1] if len(sys.argv) > 1 else None
    targets = _resolve_targets(cli_target)
    if not targets:
        return

    os.makedirs(OUT_DIR, exist_ok=True)
    print(f"🎯 {len(targets)} document(s) à télécharger → dossier '{OUT_DIR}'.")

    headful = os.getenv("HEADFUL") == "1"
    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            PROFILE_DIR,
            channel="msedge",
            headless=not headful,
            ignore_https_errors=True,
            accept_downloads=True,
            # Let Edge perform transparent Windows SSO (Negotiate/NTLM) for the
            # intranet host, so it authenticates even when headless.
            args=[
                "--auth-server-allowlist=*.lat.corp",
                "--auth-negotiate-delegate-allowlist=*.lat.corp",
            ],
        )
        page = context.pages[0] if context.pages else context.new_page()
        page.goto(BASE_URL, wait_until="domcontentloaded")

        if not _ensure_session(page, headful):
            context.close()
            return

        if os.getenv("DIAG"):
            ref, url = targets[0]
            url = urljoin(page.url, url)
            print(f"\n🔬 DIAGNOSTIC chaîne de frames pour {ref} :")
            _diagnose_chain(context, ref, url)
            context.close()
            return

        ok = 0
        for i, (ref, url) in enumerate(targets):
            url = urljoin(page.url, url)   # tolerate a relative URL
            if _download_one(context, ref, url, OUT_DIR):
                ok += 1
            if i < len(targets) - 1:
                time.sleep(POLITENESS_DELAY_S)

        print(f"\n✅ Terminé : {ok}/{len(targets)} document(s) téléchargé(s) dans '{OUT_DIR}'.")
        context.close()


if __name__ == "__main__":
    main()
