"""LibreOffice-based .docx -> .pdf conversion for exact-layout previews.

Databricks Apps supports no system packages (no apt, no Dockerfile, no init
scripts), so LibreOffice cannot be *installed* — but a portable LibreOffice
tree shipped as a tar.gz in a UC Volume can be downloaded and extracted into
the app's writable temp dir at first use, then run headless as a plain
subprocess. That is what this module does. Discovery order:

1. ``SOFFICE_PATH`` — explicit path to an existing ``soffice`` binary
   (local dev: Windows install, or any pre-provisioned binary).
2. A portable tree already extracted in the local extract dir (fast path
   after the first request following a restart).
3. ``SOFFICE_ARCHIVE_VOLUME_PATH`` — UC Volume path of a portable
   LibreOffice ``.tar.gz`` to download and
   extract once per process lifetime.
4. Well-known system locations (PATH, Program Files) — local dev fallback.

Everything here is synchronous (subprocess + file IO); callers wrap calls in
``asyncio.to_thread``. If no engine can be found the caller gets
``SofficeUnavailable`` with the reason — the preview endpoint then falls
back to client-side docx-preview.
"""

import hashlib
import logging
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
import threading
import time
from pathlib import Path


logger = logging.getLogger(__name__)

# The extension comes from an uploaded file name and becomes part of a path: letters and digits only.
_SAFE_SUFFIX = re.compile(r'^\.[A-Za-z0-9]{1,8}$')
_CONVERT_TIMEOUT_S = int(os.getenv('SOFFICE_CONVERT_TIMEOUT_S', '180'))

# soffice is single-document-per-invocation here (isolated profiles), but each
# invocation is CPU/RAM hungry — cap parallel conversions.
_convert_semaphore = threading.Semaphore(int(os.getenv('SOFFICE_MAX_CONCURRENT', '2')))

_discovery_lock = threading.Lock()
_discovered_path: str | None = None
_discovery_error: str | None = None
# A failed discovery (a download, a probe of up to 120 s) is not retried by every request: it is remembered this long.
_DISCOVERY_RETRY_S = 300.0
_discovery_failed_at: float | None = None
_clock = time.monotonic
_engine_version: str | None = None


class SofficeUnavailable(RuntimeError):
    """No usable LibreOffice binary could be found/provisioned."""


class SofficeConversionError(RuntimeError):
    """LibreOffice ran but did not produce the expected PDF."""


def _extract_root() -> Path:
    base = Path(os.getenv('SOFFICE_EXTRACT_DIR', '') or (Path(tempfile.gettempdir()) / 'qualibot_soffice'))
    # Keyed on the archive basename: a new name forces a fresh extraction, even if /tmp survived a restart.
    archive = os.getenv('SOFFICE_ARCHIVE_VOLUME_PATH', '').strip()
    if archive:
        stem = re.sub(r'[^A-Za-z0-9._-]', '_', archive.rsplit('/', 1)[-1]).removesuffix('.tar.gz')
        return base / stem
    return base


_CACHE_MAX_FILES = 200


def _prune_cache(directory: Path) -> None:
    """Keep the most recently written PDFs of the cache and delete the rest.

    :param directory: cache directory
    """
    try:
        files = sorted(directory.glob('*.pdf'), key=lambda f: f.stat().st_mtime, reverse=True)
        for old in files[_CACHE_MAX_FILES:]:
            old.unlink(missing_ok=True)
    except OSError:
        pass  # cache is best-effort


def _cache_dir() -> Path:
    d = Path(tempfile.gettempdir()) / 'qualibot_pdf_cache'
    d.mkdir(parents=True, exist_ok=True)
    return d


def _find_binary_in_tree(root: Path) -> Path | None:
    """Locate program/soffice inside an extracted portable tree.

    :param root: directory of the extracted tree
    :return: path of the soffice binary, None when the tree has none
    """
    for candidate in root.glob('**/program/soffice'):
        if candidate.is_file():
            return candidate
    # Windows layout of the portable tree (local dev).
    # Prefer soffice.com (console subsystem — sane stdout/exit handling).
    for pattern in ('**/program/soffice.com', '**/program/soffice.exe'):
        for candidate in root.glob(pattern):
            if candidate.is_file():
                return candidate
    return None


def _mark_executable(root: Path) -> None:
    """Sets the executable bit on the files of ``program/``, outside Windows.

    :param root: directory of the extracted tree
    """
    if os.name == 'nt':
        return
    for p in root.glob('**/program/*'):
        try:
            p.chmod(p.stat().st_mode | 0o755)
        except OSError:
            pass


def _extract_archive_from_volume(volume_path: str) -> Path:
    """Download the portable tar.gz from the UC Volume and extract it locally.

    Runs at most once per process (marker file); a crashed half-extract is
    detected by the missing marker and redone from scratch.

    :param volume_path: UC Volume path of the portable ``.tar.gz``
    :return: path of the soffice binary inside the extracted tree
    """
    from databricks.sdk import WorkspaceClient

    root = _extract_root()
    # Written last: its absence means a previous extraction was interrupted and the tree cannot be trusted.
    marker = root / '.extract_complete'
    if marker.exists():
        binary = _find_binary_in_tree(root)
        if binary:
            return binary
        # Marker without binary — corrupted; start over.
        shutil.rmtree(root, ignore_errors=True)

    # Start from an empty folder: leftovers of an interrupted extraction would mix with the new files.
    if root.exists():
        shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True, exist_ok=True)

    started = time.monotonic()
    logger.info('soffice: downloading portable archive from %s', volume_path)
    w = WorkspaceClient()
    resp = w.files.download(volume_path)
    archive_path = root / 'archive.tar.gz'
    # Streamed to disk in 8 MB blocks: the archive is large and must not sit in memory.
    with open(archive_path, 'wb') as f:
        shutil.copyfileobj(resp.contents, f, length=8 * 1024 * 1024)
    logger.info(
        'soffice: archive downloaded (%.0f MB in %.0fs), extracting…',
        archive_path.stat().st_size / 1e6, time.monotonic() - started,
    )

    # The archive is the operator's own file on the volume, not user input, so its member paths are trusted.
    with tarfile.open(archive_path, 'r:gz') as tar:
        tar.extractall(root)
    archive_path.unlink(missing_ok=True)  # frees the disk space of the temp dir
    _mark_executable(root)  # the executable bits do not survive every archive tool

    binary = _find_binary_in_tree(root)
    if not binary:
        raise SofficeUnavailable(
            f'archive at {volume_path} extracted but contains no program/soffice binary'
        )
    marker.write_text('ok')
    logger.info('soffice: portable tree ready at %s (%.0fs total)', binary, time.monotonic() - started)
    return binary


def _well_known_paths() -> list:
    paths = []
    found = shutil.which('soffice')
    if found:
        paths.append(Path(found))
    if os.name == 'nt':
        for pf in (os.getenv('ProgramFiles'), os.getenv('ProgramFiles(x86)')):
            if pf:
                paths.append(Path(pf) / 'LibreOffice' / 'program' / 'soffice.exe')
    else:
        paths.extend([
            Path('/usr/bin/soffice'),
            Path('/opt/libreoffice/program/soffice'),
        ])
    return [p for p in paths if p.is_file()]


def _probe_version(binary: str) -> str | None:
    """Run `soffice --headless --version`. Also serves as the executability
    check — a present-but-unrunnable binary (noexec mount, missing shared
    libs) fails here with a useful message instead of at first conversion.
    Headless + isolated profile: without them, a pristine Windows soffice.exe
    blocks on first-run profile/dialog handling.

    :param binary: path of the soffice binary
    :return: the first line of its version output, ``unknown version`` when it prints nothing
    """
    try:
        with tempfile.TemporaryDirectory(prefix='soffice_probe_') as tmp:
            profile = Path(tmp) / 'profile'
            profile.mkdir()
            out = subprocess.run(
                [binary, f'-env:UserInstallation={profile.as_uri()}', '--headless', '--version'],
                capture_output=True, text=True, timeout=120,
                env=_subprocess_env(binary),
            )
            if out.returncode == 0:
                # Windows soffice.exe is a GUI-subsystem binary: rc=0 but no
                # console output. Success is rc=0; the version string is a bonus.
                return out.stdout.strip().splitlines()[0] if out.stdout.strip() else 'unknown version'
            raise SofficeUnavailable(
                f'{binary} --version failed (rc={out.returncode}): {(out.stderr or out.stdout).strip()[:500]}'
            )
    except (OSError, subprocess.TimeoutExpired) as e:
        raise SofficeUnavailable(f'{binary} not runnable: {e}')


def _subprocess_env(binary: str | None = None) -> dict:
    """Environment of a soffice run: headless defaults, and the library path of the portable tree.

    :param binary: path of the soffice binary, None for the plain environment
    :return: the environment as a dict
    """
    env = dict(os.environ)
    # Headless-friendly defaults; harmless when already set.
    env.setdefault('HOME', tempfile.gettempdir())
    env.setdefault('SAL_USE_VCLPLUGIN', 'svp')  # pure-software rendering, no X11
    if binary and os.name != 'nt':
        # The portable tarball ships the Ubuntu system libs LibreOffice needs (absent from slim containers) inside program/:
        # $ORIGIN covers the executables, LD_LIBRARY_PATH covers anything dlopen'd through another path.
        program_dir = str(Path(binary).parent)
        env['LD_LIBRARY_PATH'] = program_dir + (
            ':' + env['LD_LIBRARY_PATH'] if env.get('LD_LIBRARY_PATH') else ''
        )
    return env


def find_soffice() -> str:
    """Resolve (once per process) the soffice binary path.

    A failed discovery is remembered for ``_DISCOVERY_RETRY_S`` seconds: during that time the same error is raised
    without probing again.

    :return: the path of the soffice binary
    :raises SofficeUnavailable: when no usable binary was found
    """
    global _discovered_path, _discovery_error, _engine_version, _discovery_failed_at
    if _discovered_path:
        return _discovered_path
    with _discovery_lock:
        if _discovered_path:
            return _discovered_path
        if _discovery_failed_at is not None:
            if _clock() - _discovery_failed_at < _DISCOVERY_RETRY_S:
                raise SofficeUnavailable(_discovery_error or 'LibreOffice discovery failed')
            _discovery_failed_at = None

        errors = []

        explicit = os.getenv('SOFFICE_PATH', '').strip()
        if explicit:
            if Path(explicit).is_file():
                candidates = [Path(explicit)]
            else:
                errors.append(f'SOFFICE_PATH={explicit} does not exist')
                candidates = []
        else:
            candidates = []

        if not candidates:
            local = _find_binary_in_tree(_extract_root()) if (_extract_root() / '.extract_complete').exists() else None
            if local:
                candidates = [local]

        if not candidates:
            volume_archive = os.getenv('SOFFICE_ARCHIVE_VOLUME_PATH', '').strip()
            if volume_archive:
                try:
                    candidates = [_extract_archive_from_volume(volume_archive)]
                except Exception as e:
                    errors.append(f'volume archive: {e}')

        if not candidates:
            candidates = _well_known_paths()

        if not candidates:
            errors.append('no soffice binary found (set SOFFICE_PATH or SOFFICE_ARCHIVE_VOLUME_PATH)')
            _discovery_error = '; '.join(errors)
            _discovery_failed_at = _clock()
            raise SofficeUnavailable(_discovery_error)

        try:
            _engine_version = _probe_version(str(candidates[0]))
        except SofficeUnavailable as e:
            errors.append(str(e))
            _discovery_error = '; '.join(errors)
            _discovery_failed_at = _clock()
            raise SofficeUnavailable(_discovery_error)

        _discovered_path = str(candidates[0])
        _discovery_error = None
        _discovery_failed_at = None
        logger.info('soffice: using %s (%s)', _discovered_path, _engine_version)
        return _discovered_path


def convert_docx_to_pdf(docx_bytes: bytes, timeout_s: int = _CONVERT_TIMEOUT_S, suffix: str = '.docx') -> bytes:
    """Convert .docx (or any soffice-readable format — pass the real
    extension via ``suffix``, soffice picks its import filter from it) to
    PDF bytes.

    Results are cached on disk by content hash — the before/after preview of a
    finished job is immutable, so repeat views cost nothing.

    :param docx_bytes: content of the document
    :param timeout_s: conversion timeout, in seconds
    :param suffix: file extension of the document, with or without the dot
    :return: the PDF bytes
    """
    suffix = suffix if suffix.startswith('.') else '.' + suffix
    if not _SAFE_SUFFIX.match(suffix):
        raise SofficeConversionError(f'unsupported file extension {suffix!r}')
    digest = hashlib.sha256(docx_bytes).hexdigest()
    cached = _cache_dir() / f'{digest}.pdf'
    if cached.is_file() and cached.stat().st_size > 0:
        return cached.read_bytes()

    binary = find_soffice()

    with _convert_semaphore:
        with tempfile.TemporaryDirectory(prefix='soffice_job_') as tmp:
            tmp_path = Path(tmp)
            src = tmp_path / f'input{suffix}'
            src.write_bytes(docx_bytes)
            # Isolated profile per invocation — allows concurrent soffice
            # processes and avoids the shared-profile lock file entirely.
            profile = tmp_path / 'profile'
            profile.mkdir()
            cmd = [
                binary,
                f'-env:UserInstallation={profile.as_uri()}',
                '--headless', '--norestore', '--nolockcheck', '--nodefault',
                '--convert-to', 'pdf',
                '--outdir', str(tmp_path),
                str(src),
            ]
            started = time.monotonic()
            try:
                out = subprocess.run(
                    cmd, capture_output=True, text=True, timeout=timeout_s,
                    env=_subprocess_env(binary),
                )
            except subprocess.TimeoutExpired:
                raise SofficeConversionError(f'conversion timed out after {timeout_s}s')

            pdf = tmp_path / 'input.pdf'
            if out.returncode != 0 or not pdf.is_file():
                raise SofficeConversionError(
                    f'soffice rc={out.returncode}: {(out.stderr or out.stdout).strip()[:800]}'
                )
            pdf_bytes = pdf.read_bytes()
            logger.info(
                'soffice: converted %.0f kB docx -> %.0f kB pdf in %.1fs',
                len(docx_bytes) / 1e3, len(pdf_bytes) / 1e3, time.monotonic() - started,
            )

    tmp_cache = cached.with_suffix('.tmp')
    try:
        tmp_cache.write_bytes(pdf_bytes)
        tmp_cache.replace(cached)
        _prune_cache(cached.parent)
    except OSError:
        pass  # cache is best-effort
    return pdf_bytes
