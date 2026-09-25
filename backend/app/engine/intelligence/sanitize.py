"""Shared secret sanitizer for the intelligence layer (Work 05, Lane B).

Owned by Lane B; other lanes import these helpers. Call ``redact_secrets``
on any payload before it leaves the process (remote LLM/browser calls) or is
persisted to a record/log. Call ``assert_no_secrets`` to fail closed when a
leak is suspected.

Two complementary mechanisms:

1. Key-name based: mapping keys whose normalized name contains a sensitive
   fragment (``token``, ``secret``, ``password``, ``api_key``, ...) have
   scalar values replaced with ``[REDACTED]``. Container values are
   recursed into so non-secret sibling metadata survives.
2. Pattern based: well-known secret shapes inside free text (provider API
   keys, OAuth tokens, ``.env`` assignments, PEM private keys, bearer
   credentials, JWTs) are replaced even when they appear inside prose.

Inputs are never mutated; redacted deep copies are returned.
"""

from __future__ import annotations

import re

REDACTED = "[REDACTED]"

_PLACEHOLDERS = frozenset(
    {"", REDACTED, "****", "***", "••••", "none", "null", "undefined", "n/a", "mock"}
)
_PLACEHOLDERS_LOWER = frozenset(p.lower() for p in _PLACEHOLDERS)


class SecretLeakError(ValueError):
    """Raised by :func:`assert_no_secrets` when a secret is detected."""


# Normalized (lowercase, non-alphanumerics stripped) key fragments that mark a
# mapping key as secret-bearing. Deliberately excludes bare "auth" so keys
# like "author" are untouched, and excludes "mode" fragments so workspace
# settings such as "privacy_mode" survive redaction.
_SENSITIVE_KEY_PARTS = (
    "token",
    "secret",
    "password",
    "passwd",
    "pwd",
    "apikey",
    "api_key",
    "privatekey",
    "private_key",
    "clientsecret",
    "client_secret",
    "accesstoken",
    "access_token",
    "refreshtoken",
    "refresh_token",
    "authorization",
    "bearer",
    "credential",
    "sessionkey",
    "session_key",
    "signingsecret",
    "signing_secret",
    "webhooksecret",
    "webhook_secret",
    "oauth",
)


def _normalize_key(key: str) -> str:
    return re.sub(r"[^a-z0-9]", "", key.lower())


def _is_sensitive_key(key: object) -> bool:
    if not isinstance(key, str):
        return False
    normalized = _normalize_key(key)
    return any(part in normalized for part in _SENSITIVE_KEY_PARTS)


# (compiled pattern, replacement) pairs applied to free text. Order matters:
# the PEM block rule runs first so multi-line keys collapse to one marker.
_VALUE_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (
        re.compile(
            r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?-----END [A-Z0-9 ]*PRIVATE KEY-----",
            re.DOTALL,
        ),
        REDACTED,
    ),
    (
        re.compile(r"\b(sk-ant-[A-Za-z0-9\-_]{20,}|sk-[A-Za-z0-9\-_]{20,})"),
        REDACTED,
    ),
    (re.compile(r"\bsk-or-[A-Za-z0-9\-_]{8,}"), REDACTED),
    (re.compile(r"\bts-[A-Za-z0-9\-_]{8,}"), REDACTED),
    (re.compile(r"\bxox[bpas]-[A-Za-z0-9\-]{8,}"), REDACTED),
    (
        re.compile(
            r"\b(ghp_[A-Za-z0-9]{10,}|gho_[A-Za-z0-9]{10,}|ghu_[A-Za-z0-9]{10,}|"
            r"ghs_[A-Za-z0-9]{10,}|ghr_[A-Za-z0-9]{10,}|github_pat_[A-Za-z0-9_]{10,})"
        ),
        REDACTED,
    ),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), REDACTED),
    (re.compile(r"\bya29\.[A-Za-z0-9\-_]{8,}"), REDACTED),
    (
        re.compile(r"\b(eyJ[A-Za-z0-9\-_]{8,}\.[A-Za-z0-9\-_]{8,}\.[A-Za-z0-9\-_]{8,})"),
        REDACTED,
    ),
    (re.compile(r"(?i)\b(bearer\s+)[A-Za-z0-9\-._~+/=]{8,}"), r"\1" + REDACTED),
    (
        re.compile(
            r'("(?:access_token|refresh_token|api_key|apikey|client_secret|'
            r"private_key|password)\"\\s*:\\s*\")[^\"]+(\")",
            re.IGNORECASE,
        ),
        r"\1" + REDACTED + r"\2",
    ),
    # `.env`-style assignments for secret-looking names, e.g.
    # OPENAI_API_KEY=sk-...  (value may be quoted).
    (
        re.compile(
            r"(?im)^(\s*[A-Z0-9_]*(?:TOKEN|SECRET|PASSWORD|API[_-]?KEY|"
            r"PRIVATE[_-]?KEY|CLIENT[_-]?SECRET)[A-Z0-9_]*\s*=\s*)"
            r"(\"[^\"\n]*\"|'[^'\n]*'|[^\s#\n]+)",
        ),
        r"\1" + REDACTED,
    ),
]

# Leak-detection patterns for assert_no_secrets (search-only variants).
_LEAK_PATTERNS: list[re.Pattern[str]] = [
    re.compile(
        r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?-----END [A-Z0-9 ]*PRIVATE KEY-----",
        re.DOTALL,
    ),
    re.compile(r"\b(sk-ant-[A-Za-z0-9\-_]{20,}|sk-[A-Za-z0-9\-_]{20,})"),
    re.compile(r"\bsk-or-[A-Za-z0-9\-_]{8,}"),
    re.compile(r"\bts-[A-Za-z0-9\-_]{8,}"),
    re.compile(r"\bxox[bpas]-[A-Za-z0-9\-]{8,}"),
    re.compile(
        r"\b(ghp_[A-Za-z0-9]{10,}|gho_[A-Za-z0-9]{10,}|ghu_[A-Za-z0-9]{10,}|"
        r"ghs_[A-Za-z0-9]{10,}|ghr_[A-Za-z0-9]{10,}|github_pat_[A-Za-z0-9_]{10,})"
    ),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bya29\.[A-Za-z0-9\-_]{8,}"),
    re.compile(r"\beyJ[A-Za-z0-9\-_]{8,}\.[A-Za-z0-9\-_]{8,}\.[A-Za-z0-9\-_]{8,}"),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9\-._~+/=]{8,}"),
]

_ENV_LINE_RE = re.compile(
    r"(?im)^\s*[A-Z0-9_]*(?:TOKEN|SECRET|PASSWORD|API[_-]?KEY|"
    r"PRIVATE[_-]?KEY|CLIENT[_-]?SECRET)[A-Z0-9_]*\s*=\s*"
    r"(\"[^\"\n]*\"|'[^'\n]*'|[^\s#\n]+)"
)


def _scrub_text(text: str) -> str:
    for pattern, replacement in _VALUE_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def _is_placeholder(value: object) -> bool:
    if value is None:
        return True
    if isinstance(value, bool):
        return True
    return isinstance(value, str) and value.strip().lower() in _PLACEHOLDERS_LOWER


def redact_secrets(obj):
    """Return a deep copy of ``obj`` with secrets replaced by ``[REDACTED]``."""
    if isinstance(obj, dict):
        redacted = {}
        for key, value in obj.items():
            if _is_sensitive_key(key):
                if isinstance(value, (dict, list, tuple)):
                    redacted[key] = redact_secrets(value)
                elif isinstance(value, str):
                    if not value or _is_placeholder(value):
                        redacted[key] = value
                    else:
                        redacted[key] = REDACTED
                elif value is None or isinstance(value, bool):
                    redacted[key] = value
                elif isinstance(value, (int, float)):
                    # Numeric secrets are rare; a non-zero number under a
                    # secret-bearing key is still scrubbed.
                    redacted[key] = REDACTED if value else value
                else:
                    redacted[key] = redact_secrets(value)
            else:
                redacted[key] = redact_secrets(value)
        return redacted
    if isinstance(obj, list):
        return [redact_secrets(item) for item in obj]
    if isinstance(obj, tuple):
        return tuple(redact_secrets(item) for item in obj)
    if isinstance(obj, str):
        return _scrub_text(obj)
    return obj


def _iter_leaks(obj, path: str = "$") -> list[str]:
    """Collect human-readable leak locations without returning secret values."""
    leaks: list[str] = []
    if isinstance(obj, dict):
        for key, value in obj.items():
            child = f"{path}.{key}"
            if _is_sensitive_key(key):
                if isinstance(value, (dict, list, tuple)):
                    leaks.extend(_iter_leaks(value, child))
                elif isinstance(value, str):
                    if value and not _is_placeholder(value):
                        leaks.append(f"{child} (secret-bearing key with a value)")
                    leaks.extend(
                        f"{child}[pattern:{i}]" for i in _match_indexes(value) if value
                    )
                elif isinstance(value, (int, float)) and not isinstance(value, bool) and value:
                    leaks.append(f"{child} (secret-bearing key with a value)")
                # None/bool/empty values are not leaks.
            else:
                leaks.extend(_iter_leaks(value, child))
    elif isinstance(obj, (list, tuple)):
        for i, item in enumerate(obj):
            leaks.extend(_iter_leaks(item, f"{path}[{i}]"))
    elif isinstance(obj, str):
        leaks.extend(f"{path}[pattern:{i}]" for i in _match_indexes(obj))
    return leaks


def _match_indexes(text: str) -> list[int]:
    found = []
    for i, pattern in enumerate(_LEAK_PATTERNS):
        if pattern.search(text):
            found.append(i)
    return found


def assert_no_secrets(obj) -> None:
    """Raise :class:`SecretLeakError` if ``obj`` still contains secrets.

    Succeeds on redacted payloads (``[REDACTED]`` and masked placeholders are
    explicitly allowed) so callers can verify scrubbing worked.
    """
    leaks = _iter_leaks(obj)
    if isinstance(obj, str):
        for match in _ENV_LINE_RE.finditer(obj):
            raw = match.group(1).strip().strip("\"'")
            if raw and raw.strip().lower() not in _PLACEHOLDERS_LOWER:
                leaks.append("$.env-assignment")
                break
    elif isinstance(obj, (dict, list, tuple)):
        for _path, text in _iter_texts(obj):
            for match in _ENV_LINE_RE.finditer(text):
                raw = match.group(1).strip().strip("\"'")
                if raw and raw.strip().lower() not in _PLACEHOLDERS_LOWER:
                    leaks.append(f"{_path}.env-assignment")
                    break
    if leaks:
        raise SecretLeakError(
            f"secret leak detected at {len(leaks)} location(s): "
            + ", ".join(leaks[:5])
            + ("..." if len(leaks) > 5 else "")
        )


def _iter_texts(obj, path: str = "$"):
    if isinstance(obj, dict):
        for key, value in obj.items():
            yield from _iter_texts(value, f"{path}.{key}")
    elif isinstance(obj, (list, tuple)):
        for i, item in enumerate(obj):
            yield from _iter_texts(item, f"{path}[{i}]")
    elif isinstance(obj, str):
        yield path, obj
