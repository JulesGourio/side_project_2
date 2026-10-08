"""Étape 2/2 — Uploader les fichiers dézippés vers un Unity Catalog Volume Databricks.

Prérequis: avoir lancé unzip_intraqual.py d'abord.

Usage:
    python utils/deploy/upload_intraqual.py

Variables d'environnement:
    SRC_DIR        dossier local source        (défaut: intraqual_extracted)
    VOLUME_PATH    chemin Unity Catalog        (défaut: /Volumes/dev_lab/lab_jules/doc_compare/intraqual)
    MAX_WORKERS    threads parallèles          (défaut: 8)

Le chemin VOLUME_PATH doit pointer vers un volume existant. Adapter pour UAT/prod:
    /Volumes/uat_landingzone/qualibot/doc_compare/intraqual
"""

import io
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

_ROOT            = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC_DIR          = os.getenv("SRC_DIR",          os.path.join(_ROOT, "intraqual_extracted"))
VOLUME_PATH      = os.getenv("VOLUME_PATH",      "/Volumes/dev_lab/lab_jules/doc_compare/intraqual")
DATABRICKS_HOST  = os.getenv("DATABRICKS_HOST",  "https://dbc-c623749d-731b.cloud.databricks.com")
MAX_WORKERS      = int(os.getenv("MAX_WORKERS",  "8"))

_REPORT_INTERVAL = 3  # secondes entre chaque ligne de progression


import logging

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("upload_intraqual")


class _Progress:
    """Compteurs thread-safe + reporter de fond."""

    def __init__(self, total_files: int, total_bytes: int):
        self._lock         = threading.Lock()
        self.total_files   = total_files
        self.total_bytes   = total_bytes
        self.done_files    = 0
        self.done_bytes    = 0
        self.errors        = 0
        self._start        = time.monotonic()
        self._stop_evt     = threading.Event()
        self._thread       = threading.Thread(target=self._reporter, daemon=True)

    def record(self, size: int):
        with self._lock:
            self.done_files += 1
            self.done_bytes += size

    def record_error(self):
        with self._lock:
            self.done_files += 1
            self.errors     += 1

    def start(self):
        self._thread.start()

    def stop(self):
        self._stop_evt.set()
        self._thread.join()

    def _reporter(self):
        while not self._stop_evt.wait(timeout=_REPORT_INTERVAL):
            self._print_line()

    def _print_line(self):
        with self._lock:
            done_f  = self.done_files
            done_b  = self.done_bytes
            errs    = self.errors

        elapsed = time.monotonic() - self._start
        speed   = done_b / elapsed if elapsed > 0 else 0          # octets/s
        pct     = done_f / self.total_files * 100 if self.total_files else 0

        if speed > 0 and done_f < self.total_files:
            remaining_b = self.total_bytes - done_b
            eta_s       = remaining_b / speed
            eta_str     = _fmt_duration(eta_s)
        else:
            eta_str = "--"

        bar_len   = 30
        filled    = int(bar_len * done_f / self.total_files) if self.total_files else 0
        bar       = "█" * filled + "░" * (bar_len - filled)

        err_str = f"  ⚠ {errs} erreurs" if errs else ""
        logger.info(f"  [{bar}] {done_f}/{self.total_files} ({pct:.1f}%)"
            f"  {done_b/1024/1024:.1f}/{self.total_bytes/1024/1024:.1f} Mo"
            f"  {speed/1024/1024:.2f} Mo/s"
            f"  ETA {eta_str}"
            f"{err_str}")


def _fmt_duration(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m{seconds % 60:02d}s"
    return f"{seconds // 3600}h{(seconds % 3600) // 60:02d}m"


def _upload_one(w, local_path: str, remote_path: str) -> tuple[str, int]:
    with open(local_path, "rb") as f:
        data = f.read()
    w.files.upload(remote_path, io.BytesIO(data), overwrite=True)
    return remote_path, len(data)


def _collect_files(src_dir: str) -> list[tuple[str, str, int]]:
    """Retourne (local_path, remote_path, size) pour chaque fichier."""
    pairs = []
    base = Path(src_dir)
    for local in sorted(base.rglob("*")):
        if local.is_file():
            rel    = local.relative_to(base).as_posix()
            remote = f"{VOLUME_PATH.rstrip('/')}/{rel}"
            pairs.append((str(local), remote, local.stat().st_size))
    return pairs


def main():
    if not os.path.isdir(SRC_DIR):
        logger.info(f"Dossier source introuvable: {SRC_DIR!r}  (lance unzip_intraqual.py d'abord).")
        sys.exit(1)

    from databricks.sdk import WorkspaceClient
    w = WorkspaceClient(host=DATABRICKS_HOST)

    logger.info("Collecte des fichiers...")
    pairs = _collect_files(SRC_DIR)
    if not pairs:
        logger.info(f"Aucun fichier trouvé dans {SRC_DIR!r}.")
        return

    total_bytes = sum(s for _, _, s in pairs)
    logger.info(f"Upload de {len(pairs)} fichiers ({total_bytes/1024/1024:.1f} Mo) → {VOLUME_PATH}")
    logger.info(f"Parallélisme: {MAX_WORKERS} threads\n")

    try:
        w.files.create_directory(VOLUME_PATH)
    except Exception:
        pass

    progress = _Progress(len(pairs), total_bytes)
    progress.start()

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {
            pool.submit(_upload_one, w, local, remote): (local, remote)
            for local, remote, _ in pairs
        }
        for fut in as_completed(futures):
            local, remote = futures[fut]
            try:
                _, size = fut.result()
                progress.record(size)
            except Exception as e:
                progress.record_error()
                logger.info(f"  {os.path.basename(local)}: {e}")

    progress.stop()

    elapsed = time.monotonic() - progress._start
    speed   = progress.done_bytes / elapsed if elapsed > 0 else 0
    logger.info(f"Terminé en {_fmt_duration(elapsed)}"
        f"  |  {progress.done_files - progress.errors} uploadés"
        f"  |  {progress.done_bytes/1024/1024:.1f} Mo"
        f"  |  moy. {speed/1024/1024:.2f} Mo/s"
        f"  |  {progress.errors} erreurs")
    logger.info(f"Volume: {VOLUME_PATH}")


if __name__ == "__main__":
    main()
