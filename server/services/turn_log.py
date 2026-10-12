"""What one chat turn did, step by step — saved to Lakebase once the turn is over.

``chat.py`` creates one ``TurnLog`` per turn and hands it to the engine (``chat_vsi.py``); both
fill it as the turn runs. ``lakebase.store_chat_turn`` then writes it to three tables:

- ``chat_turns``: one row — ``data`` (columns of ``CHAT_TURN_COLUMNS`` in lakebase.py), the
  durations of every step (``timings_ms``, columns ``<step>_ms``), the outcome and the issue codes;
- ``chat_retrieved_chunks``: one row per passage retrieved (``passages``), handed to the LLM
  (``kept``) or set aside (``drop_reason``);
- ``errors``: one row per issue of severity ``error`` or ``warning`` (``issues``), with its detail,
  its origin in the code (``path:line in function``) and the stack trace when there is one.

Outcome (``status``): ``ok`` — answered, nothing went wrong; ``degraded`` — answered, but a step
did not run as configured (rewrite failed, part of the searches failed, fallback model…: the
``warnings`` say which); ``error`` — no answer; ``aborted`` — the browser left before the end.
"""

import time
import traceback
import uuid
from contextlib import contextmanager
from typing import Any, Dict, Iterator, List, Optional

from . import error_origin

# Issues that are not failures of the app: kept in chat_turns.warnings, not written to errors.
INFO = 'info'
WARNING = 'warning'
ERROR = 'error'


def new_trace_id() -> str:
    return f'vsi-{uuid.uuid4().hex}'


class TurnLog:
    """Everything recorded about one chat turn (see module doc)."""

    def __init__(self, trace_id: Optional[str] = None):
        self.trace_id = trace_id or new_trace_id()
        self.started = time.monotonic()
        self.data: Dict[str, Any] = {}
        self.timings_ms: Dict[str, int] = {}
        self.issues: List[Dict[str, Any]] = []
        self.passages: List[Dict[str, Any]] = []
        self.status = ''                         # set by finish()
        self.error: Dict[str, Any] = {}          # stage / type / message of the failure, if any

    # ── durations ────────────────────────────────────────────────────────────
    def elapsed_ms(self) -> int:
        return int((time.monotonic() - self.started) * 1000)

    @contextmanager
    def timed(self, step: str) -> Iterator[None]:
        """Adds the duration of the block to ``timings_ms[step]`` (also when it raises)."""
        t0 = time.monotonic()
        try:
            yield
        finally:
            self.timings_ms[step] = self.timings_ms.get(step, 0) + int((time.monotonic() - t0) * 1000)

    def mark(self, step: str) -> None:
        """Time since the start of the turn, recorded once (e.g. the first answer text)."""
        self.timings_ms.setdefault(step, self.elapsed_ms())

    # ── what went wrong ─────────────────────────────────────────────────────
    def issue(self, code: str, stage: str, message: str = '', *, severity: str = WARNING,
              error_type: str = '', http_status: int = 0, upstream: str = '',
              context: Optional[Dict[str, Any]] = None, exc: Optional[BaseException] = None) -> None:
        """Something did not run as configured. ``code`` is a short stable label (warnings column);
        ``exc``, when there is one, gives the origin (where it was raised) and the stack trace."""
        self._add(code, stage, message, severity, error_type, http_status, upstream, context, exc,
                  error_origin.from_exception(exc) or error_origin.caller(1))

    def fail(self, stage: str, error_type: str, message: str, *, http_status: int = 0, upstream: str = '',
             stack_trace: str = '', context: Optional[Dict[str, Any]] = None,
             exc: Optional[BaseException] = None) -> None:
        """The turn produced no answer. The first failure is the one reported."""
        if not self.error:
            self.error = {'stage': stage, 'type': error_type, 'message': (message or '')[:2000]}
        origin = (error_origin.from_exception(exc) or error_origin.from_stack_trace(stack_trace)
                  or error_origin.caller(1))
        self._add('failed', stage, message, ERROR, error_type, http_status, upstream,
                  {**(context or {}), **({'stack_trace': stack_trace[:4000]} if stack_trace else {})}, exc, origin)

    def _add(self, code, stage, message, severity, error_type, http_status, upstream, context, exc, origin):
        if exc is not None and not (context or {}).get('stack_trace'):
            trace = ''.join(traceback.format_exception(type(exc), exc, exc.__traceback__))
            context = {**(context or {}), 'stack_trace': trace[-4000:]}
        self.issues.append({'code': code, 'stage': stage, 'severity': severity, 'message': (message or '')[:2000],
                            'error_type': error_type or code, 'http_status': http_status or None,
                            'upstream': upstream or None, 'context': context or None,
                            'origin': origin, 'at_ms': self.elapsed_ms()})

    @property
    def codes(self) -> List[str]:
        """Issue codes, in order, without duplicates (the failure itself excluded)."""
        return list(dict.fromkeys(i['code'] for i in self.issues if i['code'] != 'failed'))

    def finish(self, outcome: str) -> str:
        """Closes the turn: ``outcome`` is 'ok', 'error' or 'aborted'; 'ok' with warnings becomes
        'degraded'. Records the total duration."""
        self.timings_ms['total'] = self.elapsed_ms()
        if outcome == 'ok' and any(i['severity'] != INFO for i in self.issues):
            outcome = 'degraded'
        self.status = outcome
        return outcome
