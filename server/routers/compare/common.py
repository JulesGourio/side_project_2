"""File helpers shared by the compare routes: names, validation, volume paths. Settings are in ``config.py``."""

import hashlib
import logging
import os
import posixpath
import re

from fastapi import Request, UploadFile

from server.routers.compare.config import MAX_FILE_BYTES
from server.services.compare.processors.factory import SUPPORTED_EXTENSIONS, get_extension
from server.services.infra.lakebase import get_pool
from server.services.infra.user import get_user_identity

logger = logging.getLogger(__name__)


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


def user_session_root(volume_path: str, user_id: str) -> str:
    """The folder of the volume under which the sessions of one user are created.

    :param volume_path: configured volume root
    :param user_id: identity of the user
    :return: ``<volume>/u_<hash of the user id>``
    """
    digest = hashlib.sha256(user_id.encode('utf-8')).hexdigest()[:16]
    return f'{volume_path.rstrip("/")}/u_{digest}'


async def owns_session(request: Request, volume_path: str, session_path: str) -> bool:
    """Whether the caller may read or write a session folder.

    A folder is the caller's when it is under the caller's own root (sessions created now), or when one of the caller's
    saved comparisons names it (sessions created before the per-user root existed).

    :param request: the incoming request, for the caller's identity
    :param volume_path: configured volume root, empty when none is configured
    :param session_path: folder asked for
    :return: True when the folder is the caller's
    """
    user_id = (await get_user_identity(request))['user_id']
    if not user_id or '..' in session_path.split('/'):
        return False
    if volume_path and _is_within_volume(session_path, user_session_root(volume_path, user_id)):
        return True
    pool = get_pool()
    if not pool:
        return False
    try:
        async with pool.acquire() as conn:
            return bool(await conn.fetchval(
                'SELECT 1 FROM compare_messages WHERE user_id = $1 AND volume_session_path = $2 LIMIT 1',
                user_id, session_path))
    except Exception as exc:
        logger.warning('session ownership lookup failed: %s', exc)
        return False
