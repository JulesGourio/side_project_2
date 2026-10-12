"""Document conversion utilities using system tools when available.

Priority chain per format:
  .doc  → antiword → olefile stream extraction
  .pptx → LibreOffice headless (if available) → empty list
"""

import io
import logging
import os
import re
import shutil
import subprocess
import tempfile
from typing import Optional

import mammoth
import olefile

logger = logging.getLogger(__name__)

# Cache availability checks so we only shell-out once per process lifetime.
_antiword_checked: Optional[bool] = None
_lo_cmd: Optional[str] = None
_lo_checked: bool = False


# --- Tool detection ---

def _antiword_available() -> bool:
    global _antiword_checked
    if _antiword_checked is None:
        _antiword_checked = shutil.which('antiword') is not None
        if _antiword_checked:
            logger.info('antiword found: %s', shutil.which('antiword'))
        else:
            logger.warning('antiword not found — .doc text extraction will use Python fallback')
    return _antiword_checked


def _libreoffice_cmd() -> Optional[str]:
    global _lo_cmd, _lo_checked
    if not _lo_checked:
        _lo_checked = True
        for name in ('libreoffice', 'libreoffice7', 'libreoffice24', 'soffice'):
            path = shutil.which(name)
            if path:
                _lo_cmd = path
                logger.info('LibreOffice found: %s', path)
                break
        if not _lo_cmd:
            # No system install — fall back to the portable tree the exact-PDF
            # preview engine provisions from a UC Volume (soffice.py). This is
            # what makes PPTX slide rendering work on Databricks Apps.
            from .soffice import SofficeUnavailable, find_soffice
            try:
                _lo_cmd = find_soffice()
                logger.info('LibreOffice found via portable tree: %s', _lo_cmd)
            except SofficeUnavailable as e:
                logger.info('LibreOffice not found (%s) — PPTX/DOC rendering uses Python fallback', e)
    return _lo_cmd


# --- .doc → text via antiword ---

def doc_to_text(doc_bytes: bytes, filename: str = 'document.doc') -> Optional[str]:
    """Extract plain text from a binary .doc file.

    Tries antiword first (best quality), then olefile-based stream extraction
    (pure Python, no system tools required).

    Returns the text string on success, or None if all methods fail.
    """
    # Method 1: antiword
    if _antiword_available():
        result = _antiword_extract(doc_bytes, filename)
        if result:
            return result
        logger.warning('antiword returned no text for %s, trying olefile fallback', filename)

    # Method 2: olefile — directly reads the WordDocument OLE stream
    result = _olefile_extract(doc_bytes, filename)
    if result:
        return result

    return None


def _antiword_extract(doc_bytes: bytes, filename: str) -> Optional[str]:
    """Call antiword subprocess to extract text."""
    with tempfile.NamedTemporaryFile(suffix='.doc', delete=False) as tmp:
        tmp.write(doc_bytes)
        tmp_path = tmp.name

    try:
        proc = subprocess.run(
            ['antiword', '-w', '0', tmp_path],
            capture_output=True,
            timeout=30,
        )
        if proc.returncode == 0:
            text = proc.stdout.decode('utf-8', errors='replace').strip()
            return text if text else None
        stderr = proc.stderr.decode('utf-8', errors='replace')[:300]
        logger.warning('antiword exit %d: %s', proc.returncode, stderr)
    except subprocess.TimeoutExpired:
        logger.warning('antiword timed out for %s', filename)
    except FileNotFoundError:
        logger.warning('antiword binary not found at runtime')
    except Exception as exc:
        logger.warning('antiword error: %s', exc)
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass

    return None


def _olefile_extract(doc_bytes: bytes, filename: str) -> Optional[str]:
    """Pure-Python .doc text extraction via olefile.

    Opens the OLE2 compound document and reads text only from the
    'WordDocument', '1Table', and '0Table' streams — avoiding the noise
    from font/style/binary streams in the full file scan.
    """
    try:
        ole = olefile.OleFileIO(io.BytesIO(doc_bytes))
    except Exception as exc:
        logger.debug('olefile could not open %s: %s', filename, exc)
        return None

    seen: set[str] = set()
    results: list[str] = []

    def _scan_stream(stream_name: str) -> None:
        if not ole.exists(stream_name):
            return
        try:
            data = ole.openstream(stream_name).read()
        except Exception:
            return
        # UTF-16LE runs — how Word stores paragraph text
        for m in re.finditer(rb'(?:[\x20-\x7e]\x00){5,}', data):
            text = m.group().decode('utf-16-le', errors='ignore').strip()
            if len(text) >= 4 and text not in seen:
                seen.add(text)
                results.append(text)
        # Plain ASCII sequences (captions, metadata embedded in text body)
        for m in re.finditer(rb'[\x20-\x7e]{10,}', data):
            text = m.group().decode('ascii', errors='ignore').strip()
            if len(text) >= 10 and text not in seen:
                seen.add(text)
                results.append(text)

    _scan_stream('WordDocument')
    _scan_stream('1Table')
    _scan_stream('0Table')
    ole.close()

    if not results:
        return None

    return '\n'.join(results[:400])


# --- .docx → HTML body fragment via mammoth ---

def docx_to_html_body(content: bytes) -> str:
    """Return mammoth's HTML body for a .docx — no page wrapper. Shared by
    the compare-tab preview endpoint and the Translate tab's side-by-side
    before/after view."""
    with io.BytesIO(content) as f:
        result = mammoth.convert_to_html(f)
    return result.value or '<p class="empty">Document appears empty.</p>'


# --- PPTX → slide PNG images via LibreOffice ---

def pptx_to_slide_images(pptx_bytes: bytes, filename: str = 'presentation.pptx') -> list[bytes]:
    """Render each PPTX slide to a PNG image using LibreOffice headless.

    Returns an ordered list of PNG bytes (one per slide), or an empty list
    if LibreOffice is unavailable or conversion fails.
    """
    lo = _libreoffice_cmd()
    if not lo:
        return []

    with tempfile.TemporaryDirectory() as tmp_dir:
        pptx_path = os.path.join(tmp_dir, filename)
        with open(pptx_path, 'wb') as f:
            f.write(pptx_bytes)

        # Isolated profile per invocation (same rationale as soffice.py):
        # concurrent conversions must not fight over a shared profile lock.
        profile_dir = os.path.join(tmp_dir, 'profile')
        os.makedirs(profile_dir, exist_ok=True)
        profile_uri = 'file:///' + profile_dir.replace('\\', '/').lstrip('/')
        try:
            proc = subprocess.run(
                [lo, f'-env:UserInstallation={profile_uri}',
                 '--headless', '--convert-to', 'png', '--outdir', tmp_dir, pptx_path],
                capture_output=True,
                timeout=120,
            )
        except subprocess.TimeoutExpired:
            logger.warning('LibreOffice PPTX→PNG timed out')
            return []
        except Exception as exc:
            logger.warning('LibreOffice failed: %s', exc)
            return []

        if proc.returncode != 0:
            logger.warning(
                'LibreOffice exit %d: %s',
                proc.returncode,
                proc.stderr.decode('utf-8', errors='replace')[:300],
            )

        base = os.path.splitext(filename)[0]
        found: list[tuple[int, bytes]] = []

        for fn in os.listdir(tmp_dir):
            if not fn.lower().endswith('.png'):
                continue
            m = re.match(rf'^{re.escape(base)}(\d*)\.png$', fn, re.IGNORECASE)
            if not m:
                continue
            num_str = m.group(1)
            num = int(num_str) if num_str else 0
            with open(os.path.join(tmp_dir, fn), 'rb') as fh:
                found.append((num, fh.read()))

        found.sort(key=lambda x: x[0])
        return [img for _, img in found]
