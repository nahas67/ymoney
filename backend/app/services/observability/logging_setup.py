"""Structured, correlated, redacted logging.

**Correlation.** Every log line carries the IDs needed to reconstruct one unit
of work end to end: ``request_id``, ``workspace_id``, ``job_id``,
``campaign_id``, ``content_id``, ``provider``, ``operation_id``. They come
from two places and both are needed:

  * contextvars, so a log deep in a provider lane inherits the request's
    correlation without any call site threading arguments;
  * loguru's ``bind()``, so an explicit ID at the call site overrides the
    ambient one -- a background job has no inbound request, and the ambient
    value from an unrelated task would be worse than none.

The existing ``app.core.request_context`` contextvar holds ``request_id``. It
is reused rather than replaced: Work 12's middleware, the agent runner and the
events feed all read it, and a second request-id store would mean the same
request has two IDs in two different subsystems.

**Redaction is structural.** The JSON sink calls
:func:`app.services.observability.redaction.scrub_record` as its last step
before formatting, so a secret cannot escape by being interpolated into a
message, bound as an extra, or raised inside an exception. No call site has to
remember.

**Installation is idempotent and additive.** ``install()`` adds a sink; it
never removes loguru's stderr handler and never reconfigures an existing sink,
so a test adding its own capture sink, or a uvicorn worker importing this
module twice, cannot lose logging.
"""

from __future__ import annotations

import contextvars
import json
import sys
import uuid
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from types import MappingProxyType
from typing import Any

from loguru import logger

from app.core.request_context import request_id as current_request_id
from app.services.observability.redaction import (
    register_secret,
    register_settings_secrets,
    scrub_record,
)

__all__ = [
    "CORRELATION_IDS",
    "StructuredFormatter",
    "bind_context",
    "correlation",
    "current_correlation",
    "install",
    "is_installed",
    "new_request_id",
    "request_scope",
]

#: The correlation fields, in a stable order so a log line reads the same way
#: every time. These are the IDs the work order names.
CORRELATION_IDS: tuple[str, ...] = (
    "request_id",
    "workspace_id",
    "job_id",
    "campaign_id",
    "content_id",
    "provider",
    "operation_id",
)

#: Ambient correlation for the current task. A single contextvar holding an
#: immutable mapping rather than seven: one reset token restores everything.
#: ``None`` rather than ``{}``: a mutable dict as a ContextVar default is shared
#: by every context that never sets it, so one context could mutate another's
#: correlation. ``_EMPTY`` is a fresh value per read instead.
_EMPTY: Mapping[str, str] = MappingProxyType({})
_correlation: contextvars.ContextVar[Mapping[str, str] | None] = (
    contextvars.ContextVar("ymoney_correlation", default=None)
)


def new_request_id() -> str:
    """A fresh 16-hex request id.

    16 hex chars is 64 bits -- enough that collisions inside one log window are
    not a practical concern, and short enough to read off a terminal.
    """
    return uuid.uuid4().hex[:16]


def resolve_request_id(inbound: str | None = None) -> str:
    """Accept a caller-supplied id, or mint one.

    An inbound ``X-Request-ID`` is trusted only as an *identifier*, and is
    length-clamped and character-filtered. Echoing an unbounded caller-supplied
    string into every log line is a log-injection and storage-amplification
    vector; a caller cannot make YMONEY write an arbitrary 10 MB field.
    """
    if inbound:
        cleaned = "".join(
            ch for ch in str(inbound).strip() if ch.isalnum() or ch in "-_:"
        )[:64]
        if cleaned:
            return cleaned
    return new_request_id()


def current_correlation() -> dict[str, str]:
    """Correlation for the current task, always including ``request_id``."""
    out = {k: v for k, v in (_correlation.get() or _EMPTY).items() if v}
    out.setdefault("request_id", current_request_id())
    return {k: v for k, v in out.items() if v}


def correlation(**ids: Any) -> dict[str, str]:
    """Merge ``ids`` into the ambient correlation for this task."""
    current = dict(_correlation.get() or _EMPTY)
    for key in CORRELATION_IDS:
        value = ids.get(key)
        if value not in (None, ""):
            current[key] = str(value)
    return current


def _set_correlation(values: Mapping[str, str]) -> contextvars.Token[Mapping[str, str]]:
    return _correlation.set(dict(values))


@dataclass(frozen=True)
class StructuredFormatter:
    """Renders a loguru record as one JSON object per line.

    Loguru's ``record["extra"]`` holds everything ``bind()`` added. Merging it
    under ``extra`` (rather than flattening it into the root) keeps a bound key
    from silently overwriting a first-class field such as ``message``.
    """

    include_exception: bool = True

    def format(self, record: Mapping[str, Any]) -> str:
        moment = record["time"]
        # loguru 0.7.x hands ``record["time"]`` over as an aware datetime, not
        # an epoch float. Normalise both shapes so a loguru upgrade cannot
        # silently break every log line.
        if isinstance(moment, datetime):
            stamped = (moment if moment.tzinfo else moment.replace(tzinfo=UTC))
        else:
            stamped = datetime.fromtimestamp(float(moment), tz=UTC)
        payload: dict[str, Any] = {
            "ts": stamped.isoformat(timespec="milliseconds"),
            "level": record["level"].name,
            "logger": record["name"],
            "module": f'{record["module"]}.{record["function"]}',
            "line": record["line"],
            "message": record["message"],
        }
        # Ambient correlation first, so an explicitly bound ID overrides it.
        for key, value in current_correlation().items():
            payload[key] = value
        extra = dict(record.get("extra") or {})
        for key, value in extra.items():
            if key == "correlation" and isinstance(value, Mapping):
                for sub_key, sub_value in value.items():
                    if str(sub_key) in CORRELATION_IDS and sub_value:
                        payload[str(sub_key)] = str(sub_value)
                continue
            payload[key] = value
        if record.get("exception") and self.include_exception:
            exc = record["exception"]
            # TYPE only. The message can embed a URL with a credential, and
            # tracebacks get written to files that outlive the incident.
            payload["exception_type"] = (
                exc.type.__name__ if exc.type else "Exception")
            payload["exception_frames"] = max(
                len(getattr(exc, "stacktrace", []) or []), 0)
        # The last gate: nothing above may emit a credential.
        return json.dumps(scrub_record(payload), default=str, ensure_ascii=False)


def json_sink(stream: Any = None, formatter: StructuredFormatter | None = None):
    """Build a loguru **sink function** that emits one JSON object per line.

    A sink function, not a ``format=`` callable, because loguru memoises a
    callable ``format`` as a :class:`string.Formatter` template and runs
    ``format_map`` over it -- which parses the *source text* of the callable as
    format fields. A formatter that contains a dict literal or an f-string
    then raises ``KeyError`` on its own source code. The sink receives the
    record through loguru's ``Message.record`` attribute, which is the
    supported way to reach it.

    ``format="{message}"`` is passed so loguru hands the sink a ``Message``
    carrying ``.record`` rather than a bare string.
    """
    target = stream if stream is not None else sys.stderr
    renderer = formatter or StructuredFormatter()

    def _emit(message: Any) -> None:
        record = getattr(message, "record", None)
        if record is None:  # pragma: no cover - loguru always sets it
            target.write(str(message) + "\n")
            return
        target.write(renderer.format(record) + "\n")
        flush = getattr(target, "flush", None)
        if callable(flush):
            flush()

    return _emit


#: Format string that keeps loguru's ``Message.record`` available to a sink.
MESSAGE_FORMAT = "{message}"


@dataclass(frozen=True)
class TextFormatter:
    """Human-readable one-liner, still structurally redacted.

    Used when ``observability_json_logs`` is off. It is NOT a redaction
    bypass: both formatters end at :func:`scrub_record`, so switching
    presentation cannot switch off secret protection.
    """

    def format(self, record: Mapping[str, Any]) -> str:
        moment = record["time"]
        if isinstance(moment, datetime):
            stamped = moment if moment.tzinfo else moment.replace(tzinfo=UTC)
        else:
            stamped = datetime.fromtimestamp(float(moment), tz=UTC)
        ids = current_correlation()
        suffix = " ".join(f"{k}={ids[k]}" for k in CORRELATION_IDS if ids.get(k))
        line = (f"{stamped.isoformat(timespec='milliseconds')} "
                f"{record['level'].name:<8} {record['name']} | "
                f"{record['message']}")
        if suffix:
            line = f"{line} | {suffix}"
        extra = {
            k: v for k, v in (record.get("extra") or {}).items()
            if k not in CORRELATION_IDS
        }
        if extra:
            line = f"{line} | {json.dumps(scrub_record(extra), default=str)}"
        return scrub_record({"message": line})["message"]


#: Guard so a re-import or a second uvicorn worker cannot double-add.
_installed = False


def is_installed() -> bool:
    return _installed


def install(*, level: str | None = None, serialize: bool = True) -> bool:
    """Add the structured JSON sink. Idempotent.

    Returns True if a sink was added by this call. The stderr sink loguru adds
    by default is left alone: removing it would silence a developer who has not
    opted into JSON, and a sink that deletes other handlers is a sink that
    fights whatever else configures logging.
    """
    global _installed
    if _installed:
        return False
    from app.core.config import settings

    # Register configured credentials BEFORE the first line is emitted, so the
    # very first log line cannot leak one.
    register_settings_secrets(settings)

    log_level = (level or settings.log_level or "INFO").upper()

    renderer: Any = StructuredFormatter() if serialize else TextFormatter()
    logger.add(
        json_sink(sys.stderr, formatter=renderer),
        level=log_level,
        format=MESSAGE_FORMAT,
        # backtrace/diagnose are deliberately OFF: diagnose=True serialises
        # local variables, and a local can hold a credential. Redaction cannot
        # see inside a traceback string it never parses.
        backtrace=False,
        diagnose=False,
        # NO enqueue here. enqueue=True moves writes to a background thread that
        # holds a reference to the stream; if anything later swaps or closes
        # that stream (a test capture, a log rotation handler) the thread keeps
        # writing to a closed file and every later log call raises. The
        # rotating FILE sink in main.py keeps its own enqueue, where it is
        # genuinely the right trade.
        enqueue=False,
    )
    _installed = True
    return True


def register_runtime_secret(value: Any) -> bool:
    """Register a secret discovered at runtime (e.g. a workspace credential).

    Provider credentials are resolved from the database at runtime, so the
    settings scan cannot see them. A lane that loads one must call this.
    """
    return register_secret(value)


@contextmanager
def bind_context(**ids: Any) -> Iterator[dict[str, str]]:
    """Attach correlation IDs for the duration of a block.

    Restores the previous mapping on exit, so a job worker's logs are not
    stamped with whichever request happened to enqueue it.
    """
    merged = correlation(**ids)
    token = _set_correlation(merged)
    try:
        yield merged
    finally:
        _correlation.reset(token)


@contextmanager
def request_scope(inbound_request_id: str | None = None) -> Iterator[str]:
    """Bind a request id for the duration of one inbound request.

    Sets BOTH stores -- ``app.core.request_context`` (which the pre-existing
    middleware, agent runner and events feed read) and this module's ambient
    correlation (which the JSON sink reads). One ID, two stores, one value.
    """
    from app.core import request_context as _rc

    rid = resolve_request_id(inbound_request_id)
    token = _rc.set_request_id(rid)
    corr_token = _set_correlation({**correlation(), "request_id": rid})
    try:
        yield rid
    finally:
        _correlation.reset(corr_token)
        # Same-package private: this is the only place that may restore the
        # pre-existing contextvar, and reusing the token ``set_request_id``
        # returned is what keeps the two stores in lockstep.
        _rc._request_id.reset(token)  # noqa: SLF001


def log_correlated(level: str, message: str, **fields: Any) -> None:
    """Emit ONE structured line with ``fields`` merged into the payload.

    The call shape every lane should use instead of string interpolation. An
    interpolated secret is unredactable by key name -- but layer 3 of the
    redactor still catches its shape, whereas a bound field is additionally
    redacted structurally by key name.

    Exactly one emission: correlation IDs are bound as ambient context for the
    duration of the call, not used to justify a second log line.
    """
    bound = {k: v for k, v in fields.items() if k not in CORRELATION_IDS}
    ids = {k: v for k, v in fields.items() if k in CORRELATION_IDS}
    with bind_context(**ids) if ids else _null_context():
        logger.bind(**bound).opt(colors=False).log(level, message)


@contextmanager
def _null_context() -> Iterator[None]:
    yield None


def correlation_extra() -> dict[str, str]:
    """The correlation block to hand to ``logger.bind(correlation=...)``."""
    return current_correlation()