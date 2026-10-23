"""Document conversion utilities using system tools when available.

Priority chain per format:
  .doc  → antiword → olefile stream extraction
  .pptx → LibreOffice headless to PDF, one PNG per page (if available) → empty list
"""

import io
import logging
import os
import re
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path

import mammoth
import olefile
import pymupdf

from server.services.compare.soffice import (
    SofficeConversionError, SofficeUnavailable, _convert_semaphore, _subprocess_env, convert_docx_to_pdf, find_soffice,
)

logger = logging.getLogger(__name__)

# antiword writes in the encoding of its mapping file: UTF-8.txt gives UTF-8, the default mapping gives Windows-1252.
_ANTIWORD_UTF8 = ['-m', 'UTF-8.txt']
_ANTIWORD_TIMEOUT_S = 30
# The raw OLE scan returns every printable run it finds; the cap keeps the text of a noisy file bounded.
_OLEFILE_MAX_RUNS = 400
# Last line of the text of a .doc cut at _OLEFILE_MAX_RUNS fragments: it tells the reader that the end is missing.
DOC_TRUNCATED_NOTE = (f'[Only the first {_OLEFILE_MAX_RUNS} text fragments of this .doc file were read: the rest of '
                      'the document is not shown. Save it as .docx for the full text.]')
_PPT_CONVERT_TIMEOUT_S = 180
_SLIDE_DPI = 96
# A presentation is rendered as one image per slide, embedded in the preview page.
_MAX_SLIDE_IMAGES = 100

# Availability of antiword is checked once per process; the lock keeps two threads from checking at the same time.
_antiword_checked: bool | None = None
_antiword_lock = threading.Lock()


# --- Tool detection ---

def _antiword_available() -> bool:
    """Whether the antiword binary is on the PATH (looked up once per process).

    :return: True when antiword can be called
    """
    global _antiword_checked
    with _antiword_lock:
        if _antiword_checked is None:
            _antiword_checked = shutil.which('antiword') is not None
            if _antiword_checked:
                logger.info('antiword found: %s', shutil.which('antiword'))
            else:
                logger.warning('antiword not found — .doc text extraction will use Python fallback')
        return _antiword_checked


# --- .doc → text via antiword ---

def doc_to_text(doc_bytes: bytes, filename: str = 'document.doc') -> str | None:
    """Extract plain text from a binary .doc file.

    Tries antiword first (best quality), then olefile-based stream extraction
    (pure Python, no system tools required).

    Returns the text string on success, or None if all methods fail.

    :param doc_bytes: content of the .doc file
    :param filename: file name, for the logs
    :return: the text, or None when every method failed
    """
    if _antiword_available():
        result = _antiword_extract(doc_bytes, filename)
        if result:
            return result
        logger.warning('antiword returned no text for %s, trying olefile fallback', filename)

    result = _olefile_extract(doc_bytes, filename)
    if result:
        return result

    return None


def _run_antiword(path: str, mapping_args: list[str], filename: str) -> subprocess.CompletedProcess | None:
    """Run antiword on a file.

    :param path: path of the .doc file
    :param mapping_args: antiword options that choose the output encoding
    :param filename: file name, for the logs
    :return: the finished process, or None when antiword could not run or timed out
    """
    try:
        return subprocess.run(
            ['antiword', *mapping_args, '-w', '0', path],
            capture_output=True,
            timeout=_ANTIWORD_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        logger.warning('antiword timed out for %s', filename)
    except FileNotFoundError:
        logger.warning('antiword binary not found at runtime')
    except Exception as exc:
        logger.warning('antiword error: %s', exc)
    return None


def _antiword_extract(doc_bytes: bytes, filename: str) -> str | None:
    """Call antiword subprocess to extract text.

    The output is requested in UTF-8. When the UTF-8 mapping file is not installed antiword exits with an error, and
    the default mapping is used instead (Windows-1252 output).

    :param doc_bytes: content of the .doc file
    :param filename: file name, for the logs
    :return: the text, or None when antiword gives none
    """
    with tempfile.NamedTemporaryFile(suffix='.doc', delete=False) as tmp:
        tmp.write(doc_bytes)
        tmp_path = tmp.name

    try:
        proc, encoding = _run_antiword(tmp_path, _ANTIWORD_UTF8, filename), 'utf-8'
        if proc is not None and proc.returncode != 0:
            logger.info('antiword UTF-8 mapping failed for %s, using the default mapping', filename)
            proc, encoding = _run_antiword(tmp_path, [], filename), 'cp1252'
        if proc is None:
            return None
        if proc.returncode == 0:
            text = proc.stdout.decode(encoding, errors='replace').strip()
            return text if text else None
        logger.warning('antiword exit %d: %s', proc.returncode, proc.stderr.decode('utf-8', errors='replace')[:300])
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass

    return None


def _olefile_extract(doc_bytes: bytes, filename: str) -> str | None:
    """Pure-Python .doc text extraction via olefile.

    Opens the OLE2 compound document and reads text only from the
    'WordDocument', '1Table', and '0Table' streams — avoiding the noise
    from font/style/binary streams in the full file scan.

    :param doc_bytes: content of the .doc file
    :param filename: file name, for the logs
    :return: the text runs joined by newlines, at most ``_OLEFILE_MAX_RUNS`` of them and then ``DOC_TRUNCATED_NOTE``
        when more were found; None when none is found
    """
    try:
        ole = olefile.OleFileIO(io.BytesIO(doc_bytes))
    except Exception as exc:
        logger.debug('olefile could not open %s: %s', filename, exc)
        return None

    seen: set[str] = set()
    results: list[str] = []

    def _scan_stream(stream_name: str) -> None:
        """Adds the text runs of one OLE stream to the results, each run once.

        :param stream_name: name of the OLE stream to read
        """
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

    if len(results) > _OLEFILE_MAX_RUNS:
        logger.warning('olefile: %d text runs found in %s, keeping the first %d', len(results), filename, _OLEFILE_MAX_RUNS)
        return '\n'.join([*results[:_OLEFILE_MAX_RUNS], DOC_TRUNCATED_NOTE])
    return '\n'.join(results)


# --- .docx → HTML body fragment via mammoth ---

def docx_to_html_body(content: bytes) -> str:
    """Return mammoth's HTML body for a .docx — no page wrapper. Shared by
    the compare-tab preview endpoint and the Translate tab's side-by-side
    before/after view.

    :param content: content of the .docx file
    :return: the HTML body fragment, a placeholder paragraph when the document is empty
    """
    with io.BytesIO(content) as f:
        result = mammoth.convert_to_html(f)
    return result.value or '<p class="empty">Document appears empty.</p>'


# --- PPTX → slide PNG images via LibreOffice ---

def pptx_to_slide_images(pptx_bytes: bytes, filename: str = 'presentation.pptx') -> list[bytes]:
    """Render each slide of a presentation to a PNG image.

    LibreOffice converts the presentation to a PDF (its PNG export only renders the first slide), and each page of the
    PDF becomes one image. At most ``_MAX_SLIDE_IMAGES`` slides are rendered.

    :param pptx_bytes: content of the .pptx or .ppt file
    :param filename: file name, which gives LibreOffice the extension to choose its import filter
    :return: the PNG bytes of each slide, in slide order; empty when LibreOffice is unavailable or fails
    """
    suffix = os.path.splitext(filename)[1] or '.pptx'
    try:
        pdf_bytes = convert_docx_to_pdf(pptx_bytes, suffix=suffix)
    except (SofficeUnavailable, SofficeConversionError) as exc:
        logger.info('LibreOffice cannot render %s (%s) — the caller falls back to the Python renderer', filename, exc)
        return []

    with pymupdf.open(stream=pdf_bytes, filetype='pdf') as pdf:
        if pdf.page_count > _MAX_SLIDE_IMAGES:
            logger.warning('%s has %d slides, rendering the first %d', filename, pdf.page_count, _MAX_SLIDE_IMAGES)
        return [pdf[i].get_pixmap(dpi=_SLIDE_DPI).tobytes('png') for i in range(min(pdf.page_count, _MAX_SLIDE_IMAGES))]



def ppt_to_pptx(ppt_bytes: bytes, filename: str = 'presentation.ppt') -> bytes:
    """Convert a binary .ppt presentation to .pptx with LibreOffice.

    :param ppt_bytes: content of the .ppt file
    :param filename: file name, for the error messages
    :return: the .pptx bytes
    :raises ValueError: when LibreOffice is unavailable or cannot convert the file
    """
    try:
        binary = find_soffice()
    except SofficeUnavailable as exc:
        raise ValueError(f'{filename}: the .ppt format is not supported here (no LibreOffice to convert it). '
                         'Save the file as .pptx and compare again.') from exc
    with _convert_semaphore, tempfile.TemporaryDirectory(prefix='soffice_ppt_') as tmp:
        tmp_path = Path(tmp)
        src = tmp_path / 'input.ppt'
        src.write_bytes(ppt_bytes)
        profile = tmp_path / 'profile'
        profile.mkdir()
        cmd = [binary, f'-env:UserInstallation={profile.as_uri()}', '--headless', '--norestore', '--nolockcheck',
               '--nodefault', '--convert-to', 'pptx', '--outdir', str(tmp_path), str(src)]
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=_PPT_CONVERT_TIMEOUT_S,
                                 env=_subprocess_env(binary))
        except subprocess.TimeoutExpired as exc:
            raise ValueError(f'{filename}: the conversion of the .ppt file timed out. '
                             'Save the file as .pptx and compare again.') from exc
        result = tmp_path / 'input.pptx'
        if out.returncode != 0 or not result.is_file():
            logger.warning('ppt conversion failed for %s: %s', filename, (out.stderr or out.stdout).strip()[:300])
            raise ValueError(f'{filename}: the .ppt file could not be converted. '
                             'Save the file as .pptx and compare again.')
        return result.read_bytes()
