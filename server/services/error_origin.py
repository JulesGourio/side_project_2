"""Where an error comes from, as ``path:line in function`` — saved in Lakebase ``errors.origin``.

For an exception: the deepest frame of its traceback that is in this repository (the place that
raised or received it, not a library frame). Otherwise: the code that reported the problem.
``fingerprint`` groups identical errors (same route, step, type and origin) across requests.
"""

import hashlib
import os
import re
import sys
import traceback
from typing import Optional

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_TB_LINE_RE = re.compile(r'File "([^"]+)", line (\d+), in (\S+)')


def _ours(path: str) -> bool:
    path = os.path.abspath(path)
    return path.startswith(_ROOT + os.sep) and f'{os.sep}site-packages{os.sep}' not in path


def _fmt(path: str, line: int, func: str) -> str:
    return f'{os.path.relpath(os.path.abspath(path), _ROOT)}:{line} in {func}'


def from_exception(exc: Optional[BaseException]) -> str:
    """Deepest frame of ``exc`` (or of its cause) that is in this repository; '' when none."""
    while exc is not None:
        frames = [f for f in traceback.extract_tb(exc.__traceback__) if _ours(f.filename)]
        if frames:
            return _fmt(frames[-1].filename, frames[-1].lineno, frames[-1].name)
        exc = exc.__cause__ or exc.__context__
    return ''


def from_stack_trace(text: str) -> str:
    """Same as ``from_exception``, from a ``traceback.format_exc()`` string."""
    frames = [m for m in _TB_LINE_RE.finditer(text or '') if _ours(m.group(1))]
    return _fmt(frames[-1].group(1), int(frames[-1].group(2)), frames[-1].group(3)) if frames else ''


def caller(depth: int = 1) -> str:
    """The code ``depth`` frames above the caller of this function."""
    try:
        frame = sys._getframe(depth + 1)
    except ValueError:
        return ''
    return _fmt(frame.f_code.co_filename, frame.f_lineno, frame.f_code.co_name)


def fingerprint(*parts: Optional[str]) -> str:
    """Short stable key of an error kind (line numbers left out: a code change keeps the group)."""
    clean = [re.sub(r':\d+ in ', ' in ', p or '') for p in parts]
    return hashlib.sha1('|'.join(clean).encode('utf-8')).hexdigest()[:12]
