"""Minimal, dependency-free span recorder for the API -> service -> job ->
provider -> DB/storage flow.

**What was checked before building this.** ``backend/.venv/Lib/site-packages``
contains no ``opentelemetry*`` distribution and no ``prometheus*``
distribution. Adding OpenTelemetry is outside the Work 16 dependency budget,
so this module provides the trace *shape* -- parent/child spans, timing,
status, structured attributes -- with no exporter and no wire format claim.

**What this is not.** It is not OTLP, and ``/internal/traces`` does not emit
an OTLP payload. Presenting this as OpenTelemetry-compatible would be false.
The honest position: it answers "which span was slow, and what was its parent"
without shipping a collector, an exporter or a sampler.

**The swap point is :class:`Tracer`.** When OpenTelemetry is added, replace the
recorder behind it; ``start_span``/``current_span`` are the only API call sites
use. The ID scheme deliberately resembles W3C trace-context (32-hex trace, 16-hex
span) so a future bridge has something to map onto.
"""

from __future__ import annotations

import contextvars
import threading
import time
import uuid
from collections import deque
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum

from app.services.observability.redaction import redact

__all__ = [
    "Span",
    "SpanKind",
    "SpanStatus",
    "Tracer",
    "current_span",
    "trace_id",
    "tracer",
]


class SpanKind(StrEnum):
    """The layer a span represents in the request flow."""

    INTERNAL = "internal"
    SERVER = "server"
    CLIENT = "client"
    PRODUCER = "producer"
    CONSUMER = "consumer"
    SERVICE = "service"
    JOB = "job"
    PROVIDER = "provider"
    DATABASE = "database"
    STORAGE = "storage"


class SpanStatus(StrEnum):
    UNSET = "unset"
    OK = "ok"
    ERROR = "error"


@dataclass
class Span:
    """One timed unit of work."""

    name: str
    kind: SpanKind = SpanKind.INTERNAL
    trace_id: str = ""
    span_id: str = ""
    parent_span_id: str = ""
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    ended_at: datetime | None = None
    duration_ms: float = 0.0
    status: SpanStatus = SpanStatus.UNSET
    attributes: dict[str, object] = field(default_factory=dict)
    #: Error TYPE only. A message can embed a URL with credentials; the
    #: redactor runs over attributes, but the type is enough to triage and
    #: cannot leak.
    error_type: str = ""

    @property
    def duration_seconds(self) -> float:
        return self.duration_ms / 1000.0

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "kind": str(self.kind),
            "trace_id": self.trace_id,
            "span_id": self.span_id,
            "parent_span_id": self.parent_span_id,
            "started_at": self.started_at.isoformat(),
            "ended_at": self.ended_at.isoformat() if self.ended_at else None,
            "duration_ms": round(self.duration_ms, 3),
            "status": str(self.status),
            "error_type": self.error_type,
            # Attributes are redacted on the way out, so a provider span that
            # recorded a request header cannot publish it.
            "attributes": redact(self.attributes),
        }


#: The active span, one per asyncio task / thread via contextvars.
_current_span: contextvars.ContextVar[Span | None] = contextvars.ContextVar(
    "ymoney_current_span", default=None
)


class Tracer:
    """Bounded, thread-safe recorder of completed spans.

    Bounded because an unbounded trace buffer is a memory leak that only shows
    up under the load that matters. When the deque is full the OLDEST span is
    dropped and the drop is counted -- a trace recorder that silently discards
    data under load should at least admit it.
    """

    def __init__(self, *, max_spans: int = 2000) -> None:
        self._lock = threading.Lock()
        self._spans: deque[Span] = deque(maxlen=max_spans)
        self._dropped = 0
        self._started = 0

    # -- lifecycle --------------------------------------------------------

    def new_trace_id(self) -> str:
        return uuid.uuid4().hex

    def new_span_id(self) -> str:
        return uuid.uuid4().hex[:16]

    @contextmanager
    def start_span(
        self,
        name: str,
        *,
        kind: SpanKind = SpanKind.INTERNAL,
        attributes: Mapping[str, object] | None = None,
        trace_id: str = "",
    ) -> Iterator[Span]:
        """Open a span, parent it to whatever is active, and close it on exit.

        An exception is recorded on the span and **re-raised**. Swallowing it
        would make the tracer lie about whether the traced operation worked,
        which is the one thing a trace must never do.
        """
        parent = current_span()
        span = Span(
            name=name,
            kind=kind,
            trace_id=trace_id or (parent.trace_id if parent else self.new_trace_id()),
            span_id=self.new_span_id(),
            parent_span_id=parent.span_id if parent else "",
            attributes=dict(attributes or {}),
        )
        token = _current_span.set(span)
        started = time.perf_counter()
        try:
            yield span
        except BaseException as exc:
            span.status = SpanStatus.ERROR
            span.error_type = type(exc).__name__
            raise
        finally:
            span.duration_ms = (time.perf_counter() - started) * 1000.0
            span.ended_at = datetime.now(UTC)
            if span.status is SpanStatus.UNSET:
                span.status = SpanStatus.OK
            _current_span.reset(token)
            self.record(span)

    def record(self, span: Span) -> None:
        from app.services.observability.metrics import SPANS_RECORDED

        with self._lock:
            self._started += 1
            if len(self._spans) == self._spans.maxlen:
                self._dropped += 1
            self._spans.append(span)
        SPANS_RECORDED.inc(kind=str(span.kind), status=str(span.status))

    # -- reading ----------------------------------------------------------

    def spans(self, *, limit: int = 200, trace_id: str = "",
              kind: str = "", min_duration_ms: float = 0.0) -> list[Span]:
        """Most recent first, optionally narrowed.

        Returns copies of the dataclass fields needed for a response; the list
        itself is a fresh list, so a caller cannot mutate the buffer.
        """
        with self._lock:
            candidates = list(self._spans)
        wanted_kind = str(kind) if kind else ""
        out: list[Span] = []
        for span in reversed(candidates):
            if trace_id and span.trace_id != trace_id:
                continue
            if wanted_kind and str(span.kind) != wanted_kind:
                continue
            if span.duration_ms < min_duration_ms:
                continue
            out.append(span)
            if len(out) >= limit:
                break
        return out

    def as_dicts(self, *, limit: int = 200, trace_id: str = "",
                 kind: str = "", min_duration_ms: float = 0.0) -> list[dict[str, object]]:
        return [s.to_dict() for s in self.spans(
            limit=limit, trace_id=trace_id, kind=kind,
            min_duration_ms=min_duration_ms)]

    def stats(self) -> dict[str, object]:
        with self._lock:
            buffered = len(self._spans)
            dropped = self._dropped
            started = self._started
        return {
            "buffered": buffered,
            "capacity": self._spans.maxlen,
            "started": started,
            "dropped_oldest": dropped,
            "backend": "in_process_recorder",
            "otel_installed": otel_installed(),
        }

    def clear(self) -> None:
        with self._lock:
            self._spans.clear()
            self._dropped = 0
            self._started = 0

    def grouped_by_trace(self, *, limit: int = 50) -> list[dict[str, object]]:
        """Recent traces with their span trees flattened into one list.

        This is the ``API -> service -> job -> provider -> DB`` view: one entry
        per trace, spans in start order so parent-child nesting is readable.
        """
        with self._lock:
            recent = list(self._spans)
        by_trace: dict[str, list[Span]] = {}
        for span in recent:
            by_trace.setdefault(span.trace_id, []).append(span)
        traces: list[dict[str, object]] = []
        for trace, spans in by_trace.items():
            ordered = sorted(spans, key=lambda s: s.started_at)
            traces.append({
                "trace_id": trace,
                "span_count": len(ordered),
                "root": ordered[0].name,
                "total_duration_ms": round(
                    sum(s.duration_ms for s in ordered), 3),
                "status": (
                    SpanStatus.ERROR.value
                    if any(s.status is SpanStatus.ERROR for s in ordered)
                    else SpanStatus.OK.value
                ),
                "spans": [s.to_dict() for s in ordered],
            })
        traces.sort(key=lambda t: t["total_duration_ms"], reverse=True)
        return traces[:limit]


def otel_installed() -> bool:
    """Whether an OpenTelemetry API is importable.

    Reported rather than assumed, so the ``/internal/traces`` response states
    which backend produced the spans instead of implying a standard one.
    """
    from importlib.util import find_spec

    try:
        return find_spec("opentelemetry") is not None
    except (ImportError, ValueError):  # pragma: no cover - defensive
        return False


#: Process-wide tracer.
tracer = Tracer()


def current_span() -> Span | None:
    """The span currently on this task, or ``None`` outside any span."""
    return _current_span.get()


def trace_id() -> str:
    """Current trace id, or ``""`` outside any span."""
    span = current_span()
    return span.trace_id if span else ""


@contextmanager
def span(name: str, *, kind: SpanKind = SpanKind.INTERNAL,
         **attributes: object) -> Iterator[Span]:
    """Module-level shorthand over the process tracer."""
    with tracer.start_span(name, kind=kind, attributes=attributes) as active:
        yield active


__all__ += ["otel_installed", "span"]