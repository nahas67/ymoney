"""Per-request correlation ID — a lightweight context variable that threads
through middleware, agent runs, events, and job logs.

Usage::

    from app.core.request_context import request_id
    rid = request_id()  # returns str or ""

The middleware in ``main.py`` sets this on every inbound request. Background
tasks and agent workers inherit it automatically when started from a request
context; when started independently (schedule sweep, worker loop), it defaults
to ``""``.
"""

from __future__ import annotations

import contextvars

_request_id: contextvars.ContextVar[str] = contextvars.ContextVar(
    "ymoney_request_id", default=""
)


def request_id() -> str:
    """Return the current request's correlation ID (empty string if none)."""
    return _request_id.get()


def set_request_id(rid: str) -> contextvars.Token[str]:
    """Set the correlation ID for the current context; returns a token for
    ``reset()`` if you need to restore the previous value."""
    return _request_id.set(rid)
