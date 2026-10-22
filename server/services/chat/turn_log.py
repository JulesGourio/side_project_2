"""What one chat turn did, step by step — saved to Lakebase once the turn is over.

The chat route (``routers/chat/ws.py``) creates one ``TurnLog`` per turn and hands it to the engine (``chat/engine/``); both
fill it as the turn runs. ``lakebase.store_chat_turn`` then writes it to three tables:

- ``chat_turns``: one row — ``data`` (columns of ``CHAT_TURN_COLUMNS`` in infra/lakebase/columns.py), the
  durations of every step (``timings_ms``, columns ``<step>_ms``), the outcome and the issue codes;
- ``chat_retrieved_chunks``: one row per passage retrieved (``passages``), handed to the LLM
  (``kept``) or set aside (``drop_reason``);
- ``app_errors``: one row per issue of severity ``error`` or ``warning`` (``issues``), with its detail,
  its origin in the code (``path:line in function``) and the stack trace when there is one.

Outcome (``status``): ``ok`` — answered, nothing went wrong; ``degraded`` — answered, but a step
did not run as configured (rewrite failed, part of the searches failed, fallback model…: the
``warnings`` say which); ``error`` — no answer; ``aborted`` — the browser left before the end.
"""

import re
import time
import traceback
import uuid
from contextlib import contextmanager
from typing import Any, Dict, Iterator, List

from server.services.infra import error_origin

# Issues that are not failures of the app: kept in chat_turns.warnings, not written to app_errors.
INFO = 'info'
WARNING = 'warning'
ERROR = 'error'


_TITLE_RE = re.compile(r'Title:\s*(.*?)\s*\|')


def passage_title(chunk_text: str | None) -> str:
    """Document title from the ``[Source: REF | Title: … |`` prefix every passage starts with.

    :param chunk_text: passage text, None allowed
    :return: the title, or an empty string when the prefix has none
    """
    found = _TITLE_RE.search((chunk_text or '')[:400])
    return found.group(1) if found else ''


def new_trace_id() -> str:
    return f'chat-{uuid.uuid4().hex}'


class TurnLog:
    """Everything recorded about one chat turn (see module doc)."""

    def __init__(self, trace_id: str | None = None):
        """Starts the log of one turn.

        :param trace_id: id of the turn, a new ``chat-`` id when None
        """
        self.trace_id = trace_id or new_trace_id()
        self.started = time.monotonic()
        self.started_at_ns = time.time_ns()
        self.data: Dict[str, Any] = {}
        self.timings_ms: Dict[str, int] = {}
        self.starts_ms: Dict[str, int] = {}      # when each step began, from the start of the turn (MLflow spans)
        self.vs_calls: List[Dict[str, Any]] = []   # every Vector Search call of the search step (MLflow spans)
        self.today = ''                          # the date the answer model was given (MLflow trace)
        self.prompts: List[str] = []             # prompt keys this turn used (chat/prompts.py), linked to the MLflow trace
        self.model_io: Dict[str, Dict[str, Any]] = {}   # per model step: messages sent, tokens, endpoint (MLflow trace only)
        self.answer = ''                         # the text the user saw (MLflow trace output, not stored in Lakebase)
        self.issues: List[Dict[str, Any]] = []
        self.passages: List[Dict[str, Any]] = []
        self.status = ''                         # set by finish()
        self.error: Dict[str, Any] = {}          # stage / type / message of the failure, if any

    # ── durations ────────────────────────────────────────────────────────────
    def elapsed_ms(self) -> int:
        return int((time.monotonic() - self.started) * 1000)

    @contextmanager
    def timed(self, step: str) -> Iterator[None]:
        """Adds the duration of the block to ``timings_ms[step]`` (also when it raises).

        :param step: step name, the key of ``timings_ms``
        """
        t0 = time.monotonic()
        self.starts_ms.setdefault(step, int((t0 - self.started) * 1000))
        try:
            yield
        finally:
            self.timings_ms[step] = self.timings_ms.get(step, 0) + int((time.monotonic() - t0) * 1000)

    def mark(self, step: str) -> None:
        """Time since the start of the turn, recorded once (e.g. the first answer text).

        :param step: step name, the key of ``timings_ms``
        """
        self.timings_ms.setdefault(step, self.elapsed_ms())

    # ── what went wrong ─────────────────────────────────────────────────────
    def issue(self, code: str, stage: str, message: str = '', *, severity: str = WARNING,
              error_type: str = '', http_status: int = 0, upstream: str = '',
              context: Dict[str, Any] | None = None, exc: BaseException | None = None) -> None:
        """Something did not run as configured. ``code`` is a short stable label (warnings column);
        ``exc``, when there is one, gives the origin (where it was raised) and the stack trace.

        :param code: short stable label, stored in the warnings column
        :param stage: step where it happened, e.g. ``search``, ``llm`` or ``rewrite``
        :param message: detail text, cut to 2000 characters
        :param severity: ``warning`` (default), ``error`` or ``info``
        :param error_type: name of the error class, empty when there is none
        :param http_status: HTTP status of the failed call, 0 when there is none
        :param upstream: service or endpoint involved, empty when none
        :param context: extra details stored with the issue, None for none
        :param exc: exception that caused the issue, for its origin and stack trace
        """
        self._add(code, stage, message, severity, error_type, http_status, upstream, context, exc,
                  error_origin.from_exception(exc) or error_origin.caller(1))

    def fail(self, stage: str, error_type: str, message: str, *, http_status: int = 0, upstream: str = '',
             stack_trace: str = '', context: Dict[str, Any] | None = None,
             exc: BaseException | None = None) -> None:
        """The turn produced no answer. The first failure is the one reported.

        :param stage: step where the turn failed, e.g. ``search`` or ``llm``
        :param error_type: name of the error class
        :param message: error text, cut to 2000 characters
        :param http_status: HTTP status of the failed call, 0 when there is none
        :param upstream: service or endpoint involved, empty when none
        :param stack_trace: traceback text, kept up to 4000 characters; empty when there is none
        :param context: extra details stored with the failure, None for none
        :param exc: exception that caused the failure, for its origin and stack trace
        """
        if not self.error:
            self.error = {'stage': stage, 'type': error_type, 'message': (message or '')[:2000]}
        origin = (error_origin.from_exception(exc) or error_origin.from_stack_trace(stack_trace)
                  or error_origin.caller(1))
        self._add('failed', stage, message, ERROR, error_type, http_status, upstream,
                  {**(context or {}), **({'stack_trace': stack_trace[:4000]} if stack_trace else {})}, exc, origin)

    def _add(self, code, stage, message, severity, error_type, http_status, upstream, context, exc, origin) -> None:
        """Appends one issue to ``issues``, with the stack trace of ``exc`` when there is one.

        :param code: issue code
        :param stage: step where it happened
        :param message: detail text
        :param severity: ``warning``, ``error`` or ``info``
        :param error_type: name of the error class, the code when empty
        :param http_status: HTTP status of the failed call, 0 when there is none
        :param upstream: service or endpoint involved
        :param context: extra details, None for none
        :param exc: exception that caused the issue, None for none
        :param origin: where it was raised, as ``path:line in function``, None when unknown
        """
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
        'degraded'. Records the total duration.

        :param outcome: ``ok``, ``error`` or ``aborted``; ``ok`` with warnings becomes ``degraded``
        :return: the final status: ``ok``, ``degraded``, ``error`` or ``aborted``
        """
        self.timings_ms['total'] = self.elapsed_ms()
        if outcome == 'ok' and any(i['severity'] != INFO for i in self.issues):
            outcome = 'degraded'
        self.status = outcome
        return outcome
