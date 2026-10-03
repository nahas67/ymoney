"""Settings reuse when duplicating a project (Work 15.6 §9).

The flow a user actually performs is: *open last week's project, hit duplicate,
and have next week's project start from the same configuration.* What makes that
safe is that almost nothing in a project's stored state is configuration.

Duplicating a project means copying its **settings**, not its database rows. A
content item carries a publication id, an approval decision, an analytics rollup
and a remote render job id; a timeline carries mutable edit state; both carry
references to objects that belong to the *old* project. Copying any of those
would attach the new project to another project's history, or worse, let two
projects write the same timeline.

So this module is a **copy-from-dict allowlist**, which is the opposite shape
from a denylist:

    for key in COPYABLE_SETTINGS:            # denylist shape: start from
        if key in source:                   # everything the source has,
            out[key] = source[key]          # then subtract the bad ones

    for key in COPYABLE_SETTINGS:           # allowlist shape: start from
        if key in source:                   # the list of things we are
            out[key] = source[key]          # willing to copy

A denylist silently misses the field nobody thought of -- and the fields most
dangerous to copy (``api_key``, ``elevenlabs_api_key``, ``publication_id``,
``remote_job_id``) are exactly the ones nobody thinks of, because they were
added to the settings blob months earlier by an unrelated feature. An allowlist
fails the other way: a *new* field is absent until somebody deliberately adds it
to :data:`COPYABLE_SETTINGS`, which is a diff someone reviews.

The exclusion is therefore **structural**. It is not a filter applied to the
result: there is no code path where a denied key survives because a filter
forgot it, because the denied key is never read in the first place. The one
place this could leak is the *nested* dicts (a brand block, a generation block),
so those get their own per-section allowlists in :data:`SECTION_FIELDS` and the
same copy-from shape applies one level down.

What is deliberately NOT here:

* **No database access.** Every function takes dicts and returns dicts. This
  module cannot clone rows even by accident.
* **No credentials.** Secrets live in the ``api_credentials`` table, not in
  ``settings_json`` -- but a settings blob may still *name* a secret-bearing
  provider, and :func:`is_secret_field` refuses any field whose name looks like
  one regardless of where it came from. Defence in depth against a blob that
  somebody widened by hand.
* **No timeline sharing.** ``timeline``/``timeline_id`` are not on the allowlist,
  so an old timeline id is simply absent from the copy. See
  :data:`NON_SHARED_MARKER` for what "absent" means when a caller does want a
  provenance note.

Per-project settings live under :data:`PROJECT_SETTINGS_KEY` inside the
workspace's ``settings_json``, alongside the other namespaced blocks this
codebase already uses (``brand``, ``music``, ``safety``, ``templates``). That
keeps the storage in the one place workspace settings are already read from
rather than adding a column to ``projects``.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Mapping, Sequence

# ---------------------------------------------------------------------------
# the allowlist
# ---------------------------------------------------------------------------
#
# Top-level sections of a settings blob that are *configuration*: they describe
# how to make content, not what has already been made or published. Each is
# mapped to its own nested allowlist so a surprise key inside an allowed section
# is dropped rather than smuggled through the parent.
#
# The names are the ones this repository already writes into ``settings_json``
# (``brand`` is read by api/v1/workspaces.py:158 and engine/ugc/pipeline.py:396,
# ``music`` by api/v1/music.py:190, ``templates`` by services/templates.py:190).
# A section not listed here does not exist yet as reusable configuration, and an
# unlisted section is dropped rather than guessed at.

SECTION_FIELDS: dict[str, tuple[str, ...]] = {
    # Brand identity: colours, fonts, voice character. Configuration.
    "brand": (
        "name", "primary_color", "secondary_color", "accent_color",
        "font_family", "font_weight", "logo_path", "tagline", "tone",
        "music_prefs", "approved_caption_presets", "banned_caption_presets",
    ),
    # Output format / frame. Configuration.
    "format": (
        "aspect", "resolution", "fps", "max_duration_seconds", "burn_captions",
    ),
    # Narration voice: provider id + voice id + delivery knobs. Configuration,
    # NOT a secret. The provider *key* lives in api_credentials and is never
    # part of a settings blob (and would be refused here anyway).
    "voice": (
        "provider", "voice", "rate", "volume", "language", "exaggeration",
        "clone_allowed", "emotion",
    ),
    # Caption style. Configuration.
    "captions": (
        "preset", "style", "position", "max_words_per_line", "highlight_color",
        "animation", "language",
    ),
    # Which platforms this project targets. Configuration, and deliberately NOT
    # the connected accounts -- see EXCLUDED_SETTINGS.
    "platforms": (
        "selected", "primary", "aspect_overrides", "caption_overrides",
    ),
    # Generation knobs: model tiers, retries, concurrency. Configuration.
    "generation": (
        "llm_tier", "script_variations", "max_retries", "concurrency",
        "quality_tier", "seed", "language",
    ),
}

#: The sections :func:`duplicate_settings` is willing to copy.
COPYABLE_SETTINGS: tuple[str, ...] = tuple(SECTION_FIELDS)

# ---------------------------------------------------------------------------
# what is never copied, and why
# ---------------------------------------------------------------------------
#
# This table is DOCUMENTATION and REPORTING, not enforcement. Enforcement is the
# allowlist above; a denylist that were load-bearing would be a second, weaker
# copy of the same policy that could disagree with it. It exists so the endpoint
# can tell an operator *why* their brand carried over but their posts did not,
# and so a reviewer can see the intent when adding a new section.

EXCLUDED_SETTINGS: dict[str, str] = {
    "publication_ids": "remote publication state; the new project must publish as itself",
    "published_at": "history of the old project",
    "analytics": "measured performance of the old project's posts, not configuration",
    "approvals": "review decisions were about the old project's content",
    "approval_id": "points at another project's review record",
    "review_id": "same",
    "remote_job_ids": "in-flight render/upload jobs on the old project",
    "job_ids": "same",
    "render_id": "same",
    "api_key": "a secret; credentials live in api_credentials, never in settings",
    "api_keys": "same",
    "elevenlabs_api_key": "same",
    "client_secret": "same",
    "access_token": "same",
    "refresh_token": "same",
    "webhook_secret": "same",
    "timeline": "mutable edit state; a shared timeline is two projects fighting",
    "timeline_id": "same -- an old timeline id must never be shared mutable state",
    "content_ids": "rows belonging to the old project",
    "campaign_ids": "same",
    "asset_ids": "same",
    "social_account_ids": "connected accounts are workspace-owned, not project config",
    "secrets": "aggregate credential block if one is ever added",
    "credentials": "same",
}

#: The key under ``settings_json`` that holds per-project settings blocks.
PROJECT_SETTINGS_KEY = "project_settings"

#: A section that must not be shared as mutable state may still be *named* for
#: provenance, and when it is, the name carries this marker so a reader cannot
#: mistake a recorded id for a live link.
NON_SHARED_MARKER = "shared"

# ---------------------------------------------------------------------------
# secret-shaped field names
# ---------------------------------------------------------------------------
#
# Reuses ``services.media_cache.is_credential_param`` rather than declaring a
# second vocabulary: that function already encodes the lesson that a key named
# ``key``/``token``/``secret``/``password``/``credential`` (or any
# ``<prefix>_token`` / ``token_<suffix>`` compound) carries a secret whatever its
# exact spelling. Two lists that disagree about what a secret looks like is how a
# secret gets through one of them.

#: Extra names that are secrets here but are not general credential params.
_EXTRA_SECRET_NAMES: frozenset[str] = frozenset({
    "secret", "secrets", "passphrase", "bearer", "authorization", "cookie",
    "signing_key", "private_key", "service_account",
})


def is_secret_field(name: str) -> bool:
    """True when a field name carries a secret rather than a preference.

    Delegates to :func:`app.services.media_cache.is_credential_param` (the
    canonical detector) and widens it with the few aggregate names that detector
    does not list. Case- and separator-insensitive, so ``API-Key``,
    ``api_key`` and ``apikey`` are one answer.
    """
    from app.services.media_cache import is_credential_param

    key = str(name or "").strip().lower().replace("-", "_")
    if not key:
        return False
    return key in _EXTRA_SECRET_NAMES or is_credential_param(key)


def secret_field_names() -> tuple[str, ...]:
    """Every field name this module refuses, sorted, for the endpoint's report."""
    return tuple(sorted(EXCLUDED_SETTINGS))


# ---------------------------------------------------------------------------
# the pure projection
# ---------------------------------------------------------------------------


def _copy_section(source: Mapping, allowed: Sequence[str]) -> tuple[dict, list[str]]:
    """Copy ``allowed`` keys out of one section. Returns ``(copied, dropped)``.

    Copy-from-shape: the loop walks the ALLOWLIST, so a key present in the
    source but absent from the allowlist is never read and cannot appear in the
    result. ``dropped`` reports those names (never their values) so the caller
    can explain the omission.
    """
    copied: dict = {}
    dropped: list[str] = []
    for key in allowed:
        if key not in source:
            continue
        value = source[key]
        # Defence in depth: the allowlist is the policy, but a future edit that
        # put a secret-shaped name on the allowlist must not silently ship a
        # secret. Refuse here too, and say so in the report.
        if is_secret_field(key):
            dropped.append(str(key))
            continue
        copied[key] = copy.deepcopy(value)
    for key in source:
        name = str(key)
        if name not in copied and name not in allowed and name not in dropped:
            dropped.append(name)
    return copied, dropped


def duplicate_settings(source: Mapping | None) -> tuple[dict, dict]:
    """Project the reusable settings out of ``source``.

    Pure: reads only its argument, returns new objects, never mutates the input,
    never touches the database, the network or the clock.

    Returns ``(settings, report)``. ``report`` names what was left behind and
    why -- names only. A denied field's *value* is never copied into the report,
    because "this key was dropped" is useful and "this key's value" is the leak.
    """
    src = source if isinstance(source, Mapping) else {}
    out: dict = {}
    dropped: list[str] = []

    for section in COPYABLE_SETTINGS:
        raw = src.get(section)
        if not isinstance(raw, Mapping):
            if raw is not None and section in src:
                # Present but not a mapping: nothing safe to copy, and guessing a
                # shape here is how a list of publication ids becomes a list of
                # publication ids in the copy.
                dropped.append(section)
            continue
        copied, section_dropped = _copy_section(raw, SECTION_FIELDS[section])
        if copied:
            out[section] = copied
        dropped.extend(f"{section}.{name}" for name in section_dropped)

    # Top-level keys that are not allowlisted sections are reported as dropped.
    # This is reporting only -- they were never candidates for the copy.
    for key in src:
        name = str(key)
        if name not in COPYABLE_SETTINGS:
            dropped.append(name)

    report = {
        "copied_sections": sorted(out),
        "dropped": sorted(dict.fromkeys(dropped)),
        "dropped_count": len(dict.fromkeys(dropped)),
        "exclusion_policy": "allowlist",
    }
    return out, report


# ---------------------------------------------------------------------------
# per-project storage (a thin namespaced wrapper, still no row cloning)
# ---------------------------------------------------------------------------


def load_project_settings(workspace_settings: Mapping | None, project_id: str) -> dict:
    """The stored settings block for one project, or ``{}``.

    Reads ``settings_json[PROJECT_SETTINGS_KEY][project_id]``. A missing or
    malformed block is ``{}`` rather than an error: an operator with no saved
    settings must still be able to open the project.
    """
    ws = workspace_settings if isinstance(workspace_settings, Mapping) else {}
    block = ws.get(PROJECT_SETTINGS_KEY)
    if not isinstance(block, Mapping):
        return {}
    row = block.get(str(project_id or ""))
    return dict(row) if isinstance(row, Mapping) else {}


def store_project_settings(workspace_settings: Mapping | None, project_id: str,
                           settings: Mapping | None) -> dict:
    """Return a NEW ``settings_json`` with one project's block written.

    Pure -- the caller assigns the result. ``workspace_settings`` is never
    mutated, so a caller that ignores the return value changes nothing, which is
    the property that makes "did this write happen?" answerable from the value.
    """
    merged = dict(workspace_settings or {})
    block = merged.get(PROJECT_SETTINGS_KEY)
    block = dict(block) if isinstance(block, Mapping) else {}
    block[str(project_id or "")] = copy.deepcopy(dict(settings or {}))
    merged[PROJECT_SETTINGS_KEY] = block
    return merged


def clear_project_settings(workspace_settings: Mapping | None, project_id: str) -> dict:
    """Return a NEW ``settings_json`` with one project's block removed."""
    merged = dict(workspace_settings or {})
    block = merged.get(PROJECT_SETTINGS_KEY)
    if not isinstance(block, Mapping) or str(project_id or "") not in block:
        return merged
    block = {k: v for k, v in block.items() if str(k) != str(project_id or "")}
    merged[PROJECT_SETTINGS_KEY] = block
    return merged


# ---------------------------------------------------------------------------
# the timeline answer
# ---------------------------------------------------------------------------
#
# A duplicated project must never share the old timeline as mutable state. The
# allowlist already guarantees the id is absent, but "absent" is a claim about
# this function; a caller who wants to record where a project came from needs a
# way to say it that cannot be mistaken for a live link. So provenance notes are
# built by this function, never by the caller, and every id in one is emitted
# under :data:`NON_SHARED_MARKER`.


def provenance_note(source_project_id: str, *, timeline_id: str = "",
                    timeline_kind: str = "") -> dict:
    """A from-project note whose ids are explicitly NON-SHARED.

    The keys are namespaced under ``non_shared`` and carry
    ``{NON_SHARED_MARKER: False}``, so no consumer can read a duplicated
    project's note as a reference it should follow, edit, or write through.
    An empty id is omitted rather than stored as ``""`` -- a blank id is a
    placeholder that later code has to learn to ignore, and an absent key is
    already unambiguous.
    """
    note: dict = {
        "from_project_id": str(source_project_id or ""),
        "shared": False,
        "kind": "settings_provenance",
    }
    tid = str(timeline_id or "").strip()
    if tid:
        note["timeline_id"] = tid
        note["timeline_kind"] = str(timeline_kind or "").strip() or "unknown"
        note["timeline_shared"] = False
    return note


def settings_fingerprint(settings: Mapping | None) -> str:
    """A stable, NON-REVERSIBLE tag for a settings block.

    Used for "these two projects have the same configuration" comparisons in a
    log or an event payload. sha256 over canonical JSON: order-independent,
    and one-way, so this can be recorded without carrying the values it covers.
    """
    import hashlib

    block = settings if isinstance(settings, Mapping) else {}
    canonical = json.dumps(block, sort_keys=True, separators=(",", ":"),
                           ensure_ascii=False, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


__all__ = [
    "COPYABLE_SETTINGS",
    "EXCLUDED_SETTINGS",
    "NON_SHARED_MARKER",
    "PROJECT_SETTINGS_KEY",
    "SECTION_FIELDS",
    "clear_project_settings",
    "duplicate_settings",
    "is_secret_field",
    "load_project_settings",
    "provenance_note",
    "secret_field_names",
    "settings_fingerprint",
    "store_project_settings",
]