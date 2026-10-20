"""File helpers shared by the compare routes: names, validation, volume paths. Settings are in ``config.py``."""

import os
import posixpath
import re

from fastapi import UploadFile

from server.routers.compare.config import MAX_FILE_BYTES
from server.services.compare.processors.factory import SUPPORTED_EXTENSIONS, get_extension


def _sanitize_filename(filename: str, fallback: str = 'document') -> str:
    """A file name safe to use in a path or a header.

    :param filename: the name sent by the browser (any directory part is dropped).
    :param fallback: returned when nothing usable is left.
    :return: the base name with every character outside ``A-Za-z0-9._-`` replaced by ``_``.
    """
    base = os.path.basename((filename or '').strip())
    if not base:
        return fallback
    safe = re.sub(r'[^A-Za-z0-9._-]+', '_', base).strip('._')
    return safe or fallback


def _validate_file(file: UploadFile, data: bytes) -> None:
    """Refuse an empty, oversized or unsupported upload.

    :param file: the uploaded file, for its name.
    :param data: its content.
    :raises ValueError: with a message for the user when the file is refused.
    """
    if not data:
        raise ValueError(f'File "{file.filename or "unknown"}" is empty')
    if len(data) > MAX_FILE_BYTES:
        raise ValueError(f'File "{file.filename or "unknown"}" exceeds {MAX_FILE_BYTES // (1024*1024)} MB limit')
    ext = get_extension(file.filename or '')
    if ext not in SUPPORTED_EXTENSIONS:
        raise ValueError(
            f'Unsupported extension "{ext}". Supported: {", ".join(sorted(SUPPORTED_EXTENSIONS))}'
        )


def _is_within_volume(session_path: str, volume_path: str) -> bool:
    """True when session_path is the volume root or a path under it.

    A bare startswith() would also accept '<volume>/../../other' and '<volume>_other',
    both outside the configured volume.

    :param session_path: the path to check.
    :param volume_path: the configured volume root.
    :return: whether ``session_path`` is the root or below it.
    """
    root = posixpath.normpath(volume_path)
    path = posixpath.normpath(session_path)
    climbs_out = '..' in session_path.split('/')
    is_root = path == root
    is_below_root = path.startswith(root + '/')
    return not climbs_out and (is_root or is_below_root)
