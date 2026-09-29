"""Workspace autonomy modes + the deterministic hard-bypass escalation gate.

Settings live at ``Workspace.settings_json["community"]``::

    {"mode": "DRAFT_ONLY", "platforms": {"linkedin": "APPROVAL_REQUIRED"},
     "auto_reply_classes": ["POSITIVE"], "daily_cap": 50,
     "rate_per_10min": 5, "cooldown_seconds": 60, "emergency_disable": false,
     "strict_approval": false}

Missing or malformed settings are ALWAYS interpreted conservatively
(``DRAFT_ONLY``, no auto classes, emergency flag off only when explicitly
false). Unknown modes/classes never widen autonomy.

``strict_approval`` (Work 10, default off) is the forced-approval toggle:
when enabled, ``APPROVAL_REQUIRED`` accepts only an explicitly approved
action — the human draft/pending send escape hatch stays closed until a
human approves. Off by default, so shipped Work 09 semantics are unchanged.

``escalation_required`` is the hard-bypass gate: it is deterministic
(keyword/regex + label rules) and overrides every mode — including
``LOW_RISK_AUTO``. Financial commitments, legal claims, refunds,
account/security matters, sensitive complaints, uncertain factual claims and
high-risk moderation never auto-send, and ABUSE always escalates.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from app.models.community import AUTONOMY_MODES, CLASSIFICATION_LABELS

DEFAULT_MODE = "DRAFT_ONLY"
MODES: tuple[str, ...] = AUTONOMY_MODES

DEFAULTS: dict[str, Any] = {
    "mode": DEFAULT_MODE,
    "platforms": {},
    "auto_reply_classes": [],
    "daily_cap": 50,
    "rate_per_10min": 5,
    "cooldown_seconds": 60,
    "emergency_disable": False,
    "strict_approval": False,
}


@dataclass(frozen=True)
class AutonomyConfig:
    """Validated community autonomy for one workspace (conservative defaults)."""

    mode: str = DEFAULT_MODE
    platforms: dict[str, str] = field(default_factory=dict)
    auto_reply_classes: tuple[str, ...] = ()
    daily_cap: int = 50
    rate_per_10min: int = 5
    cooldown_seconds: int = 60
    emergency_disable: bool = False
    strict_approval: bool = False
    warnings: tuple[str, ...] = ()

    def mode_for(self, platform: str | None = "") -> str:
        """Workspace mode with a validated per-platform override."""
        token = (platform or "").strip().lower()
        override = self.platforms.get(token, "")
        return override if override in MODES else self.mode

    def allows_auto(self, platform: str | None, labels: list[str]) -> tuple[bool, str]:
        """May an agent-initiated reply go out for this platform + class set?"""
        mode = self.mode_for(platform)
        if mode == "DISABLED":
            return False, "mode_disabled"
        if mode == "DRAFT_ONLY":
            return False, "mode_draft_only"
        if mode == "APPROVAL_REQUIRED":
            return False, "mode_approval_required"
        # LOW_RISK_AUTO: only explicitly allowed classes.
        allowed = [lb for lb in (labels or []) if lb in self.auto_reply_classes]
        if not allowed:
            return False, "class_not_allowed"
        return True, "allowed:" + ",".join(sorted(allowed))


def _as_int(value: Any, default: int, *, minimum: int = 0) -> tuple[int, str | None]:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default, f"non-integer {value!r} → {default}"
    if number < minimum:
        return minimum, f"value {number} below {minimum} → {minimum}"
    return number, None


def load_autonomy(settings: Any, platform: str | None = None) -> AutonomyConfig:
    """Read + validate autonomy settings (workspace row, settings dict, None).

    Validation is fail-conservative: unknown mode → DRAFT_ONLY, unknown
    class → dropped, bad numbers → defaults.
    """
    warnings: list[str] = []
    raw: dict = {}
    try:
        if settings is None:
            raw = {}
        elif isinstance(settings, dict):
            raw = (settings.get("community") if isinstance(settings.get("community"), dict)
                   else settings) or {}
        else:
            ws_settings = getattr(settings, "settings_json", None) or {}
            raw = ws_settings.get("community", {}) if isinstance(ws_settings, dict) else {}
    except Exception:  # noqa: BLE001 - malformed settings must not crash
        raw = {}
    if not isinstance(raw, dict):
        warnings.append("settings not a dict → defaults")
        raw = {}

    mode = str(raw.get("mode") or DEFAULT_MODE).strip().upper()
    if mode not in MODES:
        warnings.append(f"unknown mode {mode!r} → {DEFAULT_MODE}")
        mode = DEFAULT_MODE

    platforms: dict[str, str] = {}
    raw_platforms = raw.get("platforms")
    if isinstance(raw_platforms, dict):
        for name, value in raw_platforms.items():
            token = str(name).strip().lower()
            token_mode = str(value or "").strip().upper()
            if token_mode in MODES:
                platforms[token] = token_mode
            else:
                warnings.append(f"unknown platform mode {token}={value!r} dropped")

    classes: list[str] = []
    # the Inbox API writes {"classes": [...], "caps": {...}} while older
    # settings use auto_reply_classes / daily_cap — accept BOTH so admin-set
    # caps and class allowlists actually reach the gate instead of silently
    # falling back to defaults.
    raw_classes = raw.get("auto_reply_classes")
    if raw_classes is None:
        raw_classes = raw.get("classes")
    if isinstance(raw_classes, (list, tuple)):
        for item in raw_classes:
            label = str(item or "").strip().upper()
            if label in CLASSIFICATION_LABELS:
                if label not in classes:
                    classes.append(label)
            else:
                warnings.append(f"unknown auto_reply class {label!r} dropped")
    elif raw_classes is not None:
        warnings.append("classes/auto_reply_classes not a list → []")

    caps = raw.get("caps") if isinstance(raw.get("caps"), dict) else {}

    def _cap_int(*keys: str, default: int) -> tuple[int, str | None]:
        """First present key wins: native gate keys, then the API aliases."""
        for key in keys:
            source = raw if key in raw else (caps if key in caps else None)
            if source is None:
                continue
            value, note = _as_int(source.get(key), default, minimum=0)
            return value, (f"{key} {note}" if note else None)
        return default, None

    daily_cap, note = _cap_int("daily_cap", "daily_replies",
                               default=DEFAULTS["daily_cap"])
    if note:
        warnings.append(f"daily_cap {note}")
    rate, note = _cap_int("rate_per_10min", "hourly_replies",
                          default=DEFAULTS["rate_per_10min"])
    if note:
        warnings.append(f"rate_per_10min {note}")
    cooldown, note = _cap_int("cooldown_seconds", "cooldown",
                              default=DEFAULTS["cooldown_seconds"])
    if note:
        warnings.append(f"cooldown_seconds {note}")

    emergency = raw.get("emergency_disable", False)
    if not isinstance(emergency, bool):
        warnings.append(f"emergency_disable {emergency!r} → False")
        emergency = False

    # forced approval (Work 10): off unless explicitly true; malformed → off
    # (the admin can enable it literally — a typo must not silently flip modes)
    strict = raw.get("strict_approval", False)
    if not isinstance(strict, bool):
        warnings.append(f"strict_approval {strict!r} → False")
        strict = False

    cfg = AutonomyConfig(
        mode=mode,
        platforms=platforms,
        auto_reply_classes=tuple(classes),
        daily_cap=daily_cap,
        rate_per_10min=rate,
        cooldown_seconds=cooldown,
        emergency_disable=emergency,
        strict_approval=strict,
        warnings=tuple(warnings),
    )
    if platform:
        # validate the effective mode early so callers see warnings
        cfg.mode_for(platform)
    return cfg


# ---------------------------------------------------------------------------
# hard-bypass escalation gate (deterministic; overrides every mode)
# ---------------------------------------------------------------------------

_ESCALATION_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("financial_commitment", re.compile(
        r"\b(guaranteed (returns?|profit|income)|we(?:'ll| will) pay|"
        r"promise to pay|wire transfer|bank details|routing number|"
        r"double your money|send money|gift card|escrow)\b", re.IGNORECASE)),
    ("legal_claim", re.compile(
        r"\b(lawsuit|legal action|sue(?:ing)?|attorney|lawyer|court|"
        r"defamation|gdpr|class action|legal advice|subpoena|settlement)\b",
        re.IGNORECASE)),
    ("refund_request", re.compile(
        r"\b(refund|money back|chargeback|cancel my (subscription|plan|account)|"
        r"dispute(?:d)? charge|reversal of charge|billing dispute)\b",
        re.IGNORECASE)),
    ("account_security", re.compile(
        r"\b(hacked|compromised|unauthori[sz]ed|stolen|phishing|password|"
        r"security breach|data breach|2fa|two-factor|locked out|"
        r"reset my|account takeover|identity theft)\b", re.IGNORECASE)),
    ("sensitive_complaint", re.compile(
        r"\b(discriminat\w*|harass\w*|racist|sexist|whistleblow\w*|"
        r"bankrupt\w*|regulator|reporting you|report you to)\b", re.IGNORECASE)),
    ("uncertain_factual_claim", re.compile(
        r"\b(guaranteed|100% (accurate|certain|proven|guaranteed)|"
        r"risk[- ]free|proven to (earn|make|grow)|always wins|no risk|"
        r"we promise it works)\b", re.IGNORECASE)),
    ("securities", re.compile(
        r"\b(securities|sec filing|penny stock|stock tip|insider tip|"
        r"ticker symbol|pump and dump|initial coin offering|"
        r"stocks?|investing advice|trading signals?)\b", re.IGNORECASE)),
    ("high_risk_moderation", re.compile(
        r"\b(kill|murder|shoot|beat (you|them) up|hurt you|bomb|"
        r"doxx|swat|self-?harm|suicide)\b", re.IGNORECASE)),
)

_COMPLAINT_RX = re.compile(
    r"\b(complaint|report you|reporting this|report this|warn everyone|"
    r"warning others|stay away|avoid (this|them)|worst company|"
    r"never trusting|fraud(?:ulent)?)\b", re.IGNORECASE)


def escalation_details(text: str, labels: list[str] | None = None) -> list[dict]:
    """All deterministic escalation triggers with evidence (for audit rows)."""
    raw = str(text or "")
    have = set(labels or [])
    found: list[dict] = []
    for reason, pattern in _ESCALATION_RULES:
        match = pattern.search(raw)
        if match:
            found.append({"rule": reason, "trigger": match.group(0)[:80],
                          "source": "keyword"})
    if "ABUSE" in have:
        found.append({"rule": "abuse_label", "trigger": "ABUSE",
                      "source": "label"})
    if "NEGATIVE" in have and _COMPLAINT_RX.search(raw):
        found.append({"rule": "negative_complaint",
                      "trigger": _COMPLAINT_RX.search(raw).group(0)[:80],
                      "source": "label+keyword"})
    if "SUPPORT" in have:
        for reason in ("refund_request", "account_security"):
            pattern = dict(_ESCALATION_RULES)[reason]
            if pattern.search(raw) and not any(f["rule"] == reason for f in found):
                found.append({"rule": reason, "trigger": pattern.search(raw).group(0)[:80],
                              "source": "label+keyword"})
    return found


def escalation_required(text: str, labels: list[str] | None = None) -> tuple[bool, str]:
    """Hard bypass: never auto-send when True. Returns (required, reason).

    ``reason`` names EVERY triggered rule (deduplicated, rule order) so the
    persisted ``escalated:`` error tells the human resolver the full set of
    categories — a securities pitch that also promises "guaranteed returns"
    escalates as ``financial_commitment,uncertain_factual_claim,securities``,
    not just the first pattern that happened to match.
    """
    found = escalation_details(text, labels)
    if not found:
        return False, ""
    rules: list[str] = []
    for item in found:
        rule = str(item["rule"])
        if rule not in rules:
            rules.append(rule)
    return True, ",".join(rules)


__all__ = [
    "DEFAULT_MODE",
    "DEFAULTS",
    "MODES",
    "AutonomyConfig",
    "escalation_details",
    "escalation_required",
    "load_autonomy",
]
