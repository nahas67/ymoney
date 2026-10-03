"""Structural secret redaction for the logging path.

**The rule being enforced:** an API key, access token, password or credential
must never reach a log sink, a trace store or a metrics label -- not because
each call site remembered to be careful, but because the sink itself cannot
emit one.

Three independent layers, because each one misses cases the others catch:

1. **Registered literals.** Whatever the process knows is a secret --
   ``settings.openai_api_key``, a workspace credential from the DB, a test's
   fake key -- is registered here. The scrubber then removes that exact
   substring wherever it appears, including from free text, exception
   messages and third-party dicts nobody in this repo controls.

2. **Key-name patterns.** Any mapping key that looks like a credential has its
   *value* replaced regardless of what that value looks like. This catches the
   credential YMONEY does not know the value of -- a password it merely
   passed through.

3. **Value shapes in free text.** A ``Bearer <token>``, a ``sk-``-prefixed
   key, a JWT, or ``api_key=`` inside a URL is masked even when it is embedded
   in a string the process never classified.

**Deliberate non-redaction, and why.** ``idempotency_key`` is *not* a secret.
It is a reconciliation handle: the whole reason a ``SUBMISSION_UNKNOWN``
submission can be traced back to the request that caused it. Redacting it
because it ends in ``_key`` would destroy exactly the evidence the paid-
execution contract exists to preserve. ``public_key``, ``key_id`` and
``cache_key`` are excluded for the same reason. A redaction list that is too
broad is a correctness bug, not a safety win.

The scrubber is applied *inside the sink*, immediately before formatting, so
no call site can bypass it by logging an already-formatted string.
"""

from __future__ import annotations

import re
import threading
from collections.abc import Mapping, Sequence
from typing import Any

__all__ = [
    "MASK",
    "is_secret_key",
    "redact",
    "redact_text",
    "register_secret",
    "registered_secret_count",
    "reset_registry",
    "scrub_record",
    "secret_placeholder",
]

#: What a redacted value is replaced with. Fixed-length so the output does not
#: leak the secret's length through the shape of the mask.
MASK = "***REDACTED***"

#: Shortest literal we will search for. Registering a 1-character "secret"
#: would scrub the whole log.
_MIN_SECRET_LEN = 6

#: Upper bound on registered literals. Guards against a caller registering one
#: secret per row in a loop, which would make every log line O(n) in a scan.
_MAX_SECRETS = 256

# ---------------------------------------------------------------------------
# key-name patterns
# ---------------------------------------------------------------------------

#: Exact key names that always mean "credential". Matched case-insensitively
#: against the bare key with separators stripped, so ``api_key``, ``API-KEY``
#: and ``apiKey`` all resolve.
_SECRET_KEY_NAMES: frozenset[str] = frozenset({
    "apikey",
    "apisecret",
    "accesstoken",
    "refreshtoken",
    "idtoken",
    "authtoken",
    "bearertoken",
    "authorization",
    "proxyauthorization",
    "password",
    "passwd",
    "pwd",
    "secret",
    "clientsecret",
    "secretkey",
    "privatekey",
    "signingkey",
    "credentials",
    "credential",
    "passphrase",
    "sessionid",
    "sessiontoken",
    "cookie",
    "setcookie",
    "csrftoken",
    "xsrftoken",
    "s3secretkey",
    "awssecretaccesskey",
    "connectionstring",
    "dsncredentials",
    "webhooksecret",
})

#: Substrings anywhere in a key name. Precise on purpose -- see the module
#: docstring about ``idempotency_key``.
_SECRET_KEY_SUBSTRINGS: tuple[str, ...] = (
    "password",
    "passwd",
    "apikey",
    "secret",
    "accesstoken",
    "refreshtoken",
    "authtoken",
    "privatekey",
    "clientsecret",
    "apikeyid",
    "credentials",
    "passphrase",
    "bearer",
)

#: Raw (lowercased) key names that LOOK credential-shaped but carry
#: operational value. Redacting them would remove reconciliation handles and
#: stable public identifiers. Matched against the raw key BEFORE
#: normalisation, so ``idempotency_key`` is never mistaken for a bare ``key``.
_KEY_NAMES_EXPLICITLY_SAFE: frozenset[str] = frozenset({
    "idempotency_key",
    "idempotencykey",
    "public_key",
    "publickey",
    "key_id",
    "keyid",
    "cache_key",
    "cachekey",
    "partition_key",
    "sort_key",
    "signing_key_id",
    "secret_id",
    "workspace_id",
    "campaign_id",
    "content_id",
    "operation_id",
})


def _normalise_key(key: Any) -> str:
    """``API_KEY`` / ``api-key`` / ``apiKey`` -> ``apikey``."""
    return re.sub(r"[^a-z0-9]", "", str(key).lower())


def is_secret_key(key: Any) -> bool:
    """Whether a mapping key names a credential.

    The answer is about the KEY, never the value: a value that looks like a
    secret under a key that does not mean one is still handled by layer 3,
    while a value that looks ordinary under ``password=`` is still a password.
    """
    raw = str(key).strip().lower()
    if not raw:
        return False
    normalised = _normalise_key(raw)
    if raw in _KEY_NAMES_EXPLICITLY_SAFE:
        return False
    if normalised in _SECRET_KEY_NAMES:
        return True
    if normalised in _KEY_NAMES_EXPLICITLY_SAFE:
        return False
    return any(marker in normalised for marker in _SECRET_KEY_SUBSTRINGS)

# ---------------------------------------------------------------------------
# value-shape patterns for free text
# ---------------------------------------------------------------------------

#: Applied to any string, in this order. Each pattern is (compiled, group
#: index of the prefix to keep).
_TEXT_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # Authorization / Bearer headers in a dump or an error message.
    (re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9\-._~+/=]{8,}"), r"\1 ***"),
    # Vendor-prefixed keys: sk-..., sk-ant-..., ghp_..., xoxb-..., AKIA...
    (re.compile(r"\b(sk-[A-Za-z0-9\-_]{12,})"), "sk-***"),
    (re.compile(r"\b(sk-ant-[A-Za-z0-9\-_]{12,})"), "sk-ant-***"),
    (re.compile(r"\b(gh[pousr]_[A-Za-z0-9]{16,})"), "gh*_***"),
    (re.compile(r"\b(xox[baprs]-[A-Za-z0-9-]{10,})"), "xox*-***"),
    (re.compile(r"\b(AKIA[0-9A-Z]{16})"), "AKIA***"),
    (re.compile(r"\b(AIza[0-9A-Za-z\-_]{30,})"), "AIza***"),
    # JSON Web Tokens (header.payload.signature).
    (re.compile(r"\b(eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,})"), "eyJ***"),
    # query-string credentials: ?api_key=..., &token=...
    (
        re.compile(
            r"(?i)([?&#](?:api[_-]?key|apikey|access[_-]?token|token|secret"
            r"|password|passwd|key|auth|sig|signature)=)[^&\s\"'<>]{4,}"
        ),
        r"\1***",
    ),
    # credentials embedded in a URL authority: scheme://user:pass@host
    (re.compile(r"(://)[^/\s:@\"']+:[^/\s@\"']+@"), r"\1***@"),
)


def secret_placeholder() -> str:
    return MASK


# ---------------------------------------------------------------------------
# registered literal secrets
# ---------------------------------------------------------------------------

_lock = threading.Lock()
_secrets: set[str] = set()


def register_secret(value: Any) -> bool:
    """Remember ``value`` so it is scrubbed from every future log line.

    Accepts any object; non-strings and values shorter than
    :data:`_MIN_SECRET_LEN` are ignored rather than registered, because
    scrubbing a one-character string would redact the alphabet.

    Returns True when the value was actually registered.
    """
    if value is None:
        return False
    text = value if isinstance(value, str) else str(value)
    if len(text) < _MIN_SECRET_LEN:
        return False
    with _lock:
        if len(_secrets) >= _MAX_SECRETS:
            return False
        if text in _secrets:
            return True
        _secrets.add(text)
    return True


def register_settings_secrets(settings: Any = None) -> int:
    """Register every credential the process configuration holds.

    Reads the settings object reflectively rather than naming fields, so a
    secret added to :class:`~app.core.config.Settings` later is covered by
    the sink automatically instead of needing a second edit here.
    """
    if settings is None:
        from app.core.config import settings as resolved

        settings = resolved
    registered = 0
    for name in dir(settings):
        if name.startswith("_"):
            continue
        if not is_secret_key(name):
            continue
        try:
            value = getattr(settings, name)
        except Exception:  # pragma: no cover - defensive
            continue
        if register_secret(value):
            registered += 1
    return registered


def registered_secret_count() -> int:
    with _lock:
        return len(_secrets)


def reset_registry() -> None:
    """Forget every registered literal. Tests use this."""
    with _lock:
        _secrets.clear()


def _registered_snapshot() -> tuple[str, ...]:
    with _lock:
        # Longest first so a secret that contains another is masked whole
        # rather than leaving a tail behind.
        return tuple(sorted(_secrets, key=len, reverse=True))


# ---------------------------------------------------------------------------
# redaction
# ---------------------------------------------------------------------------

#: Depth cap for nested structures. A credential nested ten levels down in a
#: provider response is not a realistic logging need, and unbounded recursion
#: over an attacker-shaped payload is a denial-of-service vector in a log call.
_MAX_DEPTH = 12


def redact_text(text: str) -> str:
    """Mask every known credential shape in a free-text string."""
    if not text:
        return text
    out = str(text)
    for secret in _registered_snapshot():
        if secret in out:
            out = out.replace(secret, MASK)
    for pattern, replacement in _TEXT_PATTERNS:
        out = pattern.sub(replacement, out)
    return out


def redact(value: Any, *, _depth: int = 0) -> Any:
    """Recursively redact an arbitrary value.

    Mappings are walked by key name, sequences element-wise, and every string
    -- key or value -- goes through :func:`redact_text`. The original type is
    preserved for JSON-ish structures; an unknown object is stringified rather
    than passed through, because ``repr(obj)`` can surface attributes.
    """
    if _depth >= _MAX_DEPTH:
        return MASK
    if value is None or isinstance(value, bool | int | float):
        return value
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, Mapping):
        out: dict[str, Any] = {}
        for raw_key, raw_value in value.items():
            key = redact_text(str(raw_key))
            if is_secret_key(raw_key):
                out[key] = MASK
                continue
            out[key] = redact(raw_value, _depth=_depth + 1)
        return out
    if isinstance(value, Sequence | set | frozenset):
        return [redact(item, _depth=_depth + 1) for item in value]
    return redact_text(str(value))


#: Keys whose values are dropped outright from a log record: a traceback with
#: ``diagnose=True`` serialises locals, and locals hold request objects.
_DROP_RECORD_KEYS: frozenset[str] = frozenset({
    "request",
    "response",
    "headers",
    "cookies",
    "authorization",
    "api_key",
    "apikey",
    "password",
    "token",
    "access_token",
    "secret",
    "client_secret",
    "db",
    "session",
    "engine",
    "pool",
})

#: Record keys that carry correlation IDs and must survive verbatim. They are
#: IDs, not credentials, and dropping them would defeat the whole point of
#: correlated logs.
_CORRELATION_KEYS: tuple[str, ...] = (
    "request_id", "workspace_id", "job_id", "campaign_id", "content_id",
    "provider", "operation_id", "operation", "phase", "state", "status",
    "method", "route", "status_class", "platform", "kind", "engine",
)


def scrub_record(record: Mapping[str, Any]) -> dict[str, Any]:
    """The last gate before a record is formatted.

    Drops whole objects that carry credentials (a ``Request`` carries the
    ``Authorization`` header), keeps correlation IDs verbatim, and redacts
    everything else structurally.
    """
    out: dict[str, Any] = {}
    for raw_key, raw_value in record.items():
        key = str(raw_key)
        lowered = key.lower()
        if lowered in _DROP_RECORD_KEYS and lowered not in _CORRELATION_KEYS:
            out[key] = f"<{type(raw_value).__name__} withheld>"
            continue
        if is_secret_key(key):
            out[key] = MASK
            continue
        if lowered in _CORRELATION_KEYS:
            out[key] = redact_text(str(raw_value)) if raw_value is not None else None
            continue
        out[key] = redact(raw_value)
    return out


__all__ += ["register_settings_secrets"]