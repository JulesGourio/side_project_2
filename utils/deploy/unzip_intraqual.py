"""Étape 1/2 — Dézipper les archives Intraqual en ne gardant que les documents utiles.

Usage:
    python utils/deploy/unzip_intraqual.py

Variables d'environnement:
    ZIP_DIR   dossier contenant les .zip   (défaut: Intraqual_files)
    OUT_DIR   dossier de sortie            (défaut: intraqual_extracted)

Structure produite:
    intraqual_extracted/
        D_43/D_43.doc
        D_43/D_43.pdf
        D_44/D_44.docx
        ...
Les dossiers `_fichiers/` et les images/XML sont ignorés.
"""

import os
import sys
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ZIP_DIR = os.getenv("ZIP_DIR", os.path.join(_ROOT, "Intraqual_files"))
OUT_DIR = os.getenv("OUT_DIR", os.path.join(_ROOT, "intraqual_extracted"))

KEEP_EXTS = {".doc", ".docx", ".pdf", ".ppt", ".pptx", ".xls", ".xlsx", ".xlsm", ".docm"}


def _extract_one(zip_path: str, out_dir: str) -> tuple[str, int, int]:
    """Extrait un zip, filtre sur KEEP_EXTS. Retourne (nom, extraits, ignorés)."""
    extracted = skipped = 0
    with zipfile.ZipFile(zip_path) as z:
        members = [m for m in z.infolist() if not m.is_dir()]
        for member in members:
            ext = os.path.splitext(member.filename)[1].lower()
            if ext not in KEEP_EXTS:
                skipped += 1
                continue
            dest = os.path.join(out_dir, member.filename)
            if os.path.exists(dest):
                skipped += 1
                continue
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with z.open(member) as src, open(dest, "wb") as dst:
                dst.write(src.read())
            extracted += 1
    return os.path.basename(zip_path), extracted, skipped


import logging

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("unzip_intraqual")


def main():
    zips = sorted(
        os.path.join(ZIP_DIR, f)
        for f in os.listdir(ZIP_DIR)
        if f.endswith(".zip")
    )
    if not zips:
        logger.info(f"Aucun .zip trouvé dans {ZIP_DIR!r}.")
        return

    os.makedirs(OUT_DIR, exist_ok=True)
    logger.info(f"Dézippage de {len(zips)} archive(s) → {OUT_DIR!r}")
    logger.info(f"Extensions conservées: {', '.join(sorted(KEEP_EXTS))}\n")

    total_extracted = total_skipped = 0
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {pool.submit(_extract_one, z, OUT_DIR): z for z in zips}
        for fut in as_completed(futures):
            name, extracted, skipped = fut.result()
            total_extracted += extracted
            total_skipped += skipped
            logger.info(f"  {name}: {extracted} extraits, {skipped} ignorés/existants")

    logger.info(f"Terminé: {total_extracted} fichiers extraits ({total_skipped} ignorés).")
    logger.info(f"Dossier: {os.path.abspath(OUT_DIR)}")


if __name__ == "__main__":
    main()
