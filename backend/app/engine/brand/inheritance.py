"""Brand inheritance resolver (Work 08 Lane A).

Deterministic precedence -- LAST WINS, each level only overrides the keys it
actually sets::

    workspace defaults  ->  BrandDNA  ->  campaign override
                      ->  content override  ->  platform override

Guarantees:

  * Campaign/content/platform overrides NEVER mutate stored BrandDNA: patches
    live in `brand_overrides`, the DNA document is read-only input.
  * HARD constraints (`forbidden_phrases`, `required_disclaimers`,
    `logo_safe_zone`, `approved_voices`, `approved_avatars`) survive merging:
    a level that sets one to an empty value does not erase the inherited value.
  * Every resolution persists a `brand_effective_configs` snapshot
    (effective JSON + dna version) so any generated artifact can record the
    exact config it was produced under and reproduce it later.

Contract used by Lane C::

    resolve_effective_policy(session, workspace_id, *, campaign_id=None,
                             content_id=None, platform=None, overrides=None)
        -> EffectiveCreativePolicy
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.engine.brand.dna import (
    HEX_COLOR_RE,
    BrandDNA,
    deep_merge,
    dna_version,
    normalize_platform,
)
from app.engine.brand.policy import (
    HARD_CONSTRAINT_KEYS,
    LEVELS,
    WORKSPACE_DEFAULTS,
    EffectiveCreativePolicy,
)
from app.models import (
    Brand,
    BrandDNARow,
    BrandEffectiveConfig,
    BrandOverride,
    Campaign,
    ContentItem,
    Workspace,
)
from app.models.brand import OVERRIDE_SUBJECT_TYPES

LEVEL_KEYS = frozenset(LEVELS)


class BrandError(Exception):
    """Base class for brand resolution errors (routes map these to 4xx)."""


class BrandNotFound(BrandError):
    """Unknown workspace / brand / campaign / content in this workspace."""


class BrandConfigError(BrandError):
    """Stored BrandDNA (or an override patch) is structurally invalid."""


# ---------------------------------------------------------------------------
# level helpers
# ---------------------------------------------------------------------------


def _prune(level: dict[str, Any]) -> dict[str, Any]:
    """Drop keys a level did not really set: None / blank / [] / {}.

    This is what makes "each level only overrides keys it sets" true, and --
    together with HARD_CONSTRAINT_KEYS -- what keeps hard constraints alive
    across the chain (an empty list never clears inherited phrases/voices).
    """
    out: dict[str, Any] = {}
    for key, value in (level or {}).items():
        if value is None:
            continue
        if isinstance(value, str):
            if not value.strip():
                continue
            out[key] = value
        elif isinstance(value, (list, tuple)):
            if key in HARD_CONSTRAINT_KEYS:
                items = [i for i in value if isinstance(i, str) and i.strip()]
                if not items:
                    continue
                out[key] = items
            elif not value:
                continue
            else:
                out[key] = list(value)
        elif isinstance(value, dict):
            if not value:
                continue
            out[key] = value
        else:
            out[key] = value
    return out


def _leaves(prefix: tuple[str, ...], value: Any):
    if isinstance(value, dict):
        if not value and prefix:
            yield prefix, value
            return
        for key, nested in value.items():
            yield from _leaves(prefix + (str(key),), nested)
        return
    if not prefix:
        return
    yield prefix, value


def merge_levels(levels: list[tuple[str, dict[str, Any]]]) -> tuple[dict, dict[str, str]]:
    """Deep-merge levels in precedence order; returns (merged, leaf provenance).

    Provenance maps dotted leaf paths ("caption_style.size") to the highest
    precedence level that set them.
    """
    order = {name: index for index, name in enumerate(LEVELS)}
    merged: dict[str, Any] = {}
    provenance: dict[str, str] = {}
    for name, patch in levels:
        level_index = order.get(name, -1)
        for path, value in _leaves((), patch):
            node = merged
            for part in path[:-1]:
                child = node.get(part)
                if not isinstance(child, dict):
                    child = {}
                    node[part] = child
                node = child
            node[path[-1]] = value
            dotted = ".".join(path)
            previous = provenance.get(dotted)
            if previous is None or level_index >= order.get(previous, -1):
                provenance[dotted] = name
    return merged, provenance


# ---------------------------------------------------------------------------
# loaders (all workspace-scoped; cross-workspace ids raise BrandNotFound)
# ---------------------------------------------------------------------------


def workspace_defaults(session: Session, workspace_id: str) -> dict[str, Any]:
    """Level 1: built-in baseline + white-label kit + `settings.brand_defaults`."""
    ws = session.get(Workspace, workspace_id)
    if ws is None:
        raise BrandNotFound(f"workspace {workspace_id} not found")
    settings = dict(ws.settings_json or {})
    base = deep_merge({}, WORKSPACE_DEFAULTS)
    kit = settings.get("brand") or {}
    accent = kit.get("accent") if isinstance(kit, dict) else None
    if isinstance(accent, str) and HEX_COLOR_RE.match(accent.strip()):
        base = deep_merge(base, {"brand_colors": {"accent": accent.strip().lower()}})
    voice = str(getattr(ws, "brand_voice", "") or "").strip()
    if voice:
        base = deep_merge(base, {"tone": voice[:400]})
    extra = settings.get("brand_defaults")
    if isinstance(extra, dict):
        base = deep_merge(base, extra)
    return _prune(base)


def _dna_from_row(row: BrandDNARow | None) -> dict[str, Any]:
    if row is None:
        return {}
    try:
        return BrandDNA.model_validate(dict(row.dna_json or {})).as_dict()
    except Exception as exc:  # schema/serialization problems are config errors
        raise BrandConfigError(f"invalid stored BrandDNA: {exc}") from exc


def brand_layer(
    session: Session, workspace_id: str, brand_id: str | None = None
) -> tuple[dict[str, Any], str]:
    """Level 2: the BrandDNA document. Returns (dna dict, brand_id used).

    With no explicit brand: workspace-default DNA (brand_id NULL) wins, then
    the workspace's default brand, then any active brand.
    """
    if brand_id:
        brand = session.get(Brand, brand_id)
        if brand is None or brand.workspace_id != workspace_id:
            raise BrandNotFound(f"brand {brand_id} not found")
        row = session.scalar(
            select(BrandDNARow).where(
                BrandDNARow.workspace_id == workspace_id,
                BrandDNARow.brand_id == brand_id,
            )
        )
        return _dna_from_row(row), brand.id

    row = session.scalar(
        select(BrandDNARow)
        .where(
            BrandDNARow.workspace_id == workspace_id,
            BrandDNARow.brand_id.is_(None),
        )
        .order_by(BrandDNARow.created_at)
    )
    if row is not None:
        return _dna_from_row(row), ""

    default_brand = session.scalar(
        select(Brand).where(
            Brand.workspace_id == workspace_id,
            Brand.is_default.is_(True),
            Brand.status == "active",
        )
    )
    if default_brand is None:
        default_brand = session.scalar(
            select(Brand)
            .where(Brand.workspace_id == workspace_id, Brand.status == "active")
            .order_by(Brand.created_at)
        )
    if default_brand is None:
        return {}, ""
    row = session.scalar(
        select(BrandDNARow).where(
            BrandDNARow.workspace_id == workspace_id,
            BrandDNARow.brand_id == default_brand.id,
        )
    )
    return _dna_from_row(row), default_brand.id


def override_row(
    session: Session, workspace_id: str, subject_type: str, subject_id: str
) -> dict[str, Any]:
    """Persisted override patch for a subject ({} when none)."""
    row = session.scalar(
        select(BrandOverride).where(
            BrandOverride.workspace_id == workspace_id,
            BrandOverride.subject_type == subject_type,
            BrandOverride.subject_id == str(subject_id or ""),
        )
    )
    return dict(row.override_json or {}) if row is not None else {}


def set_override(
    session: Session,
    workspace_id: str,
    subject_type: str,
    subject_id: str,
    patch: dict[str, Any],
) -> BrandOverride:
    """Upsert an override patch (deep-merged over any existing patch).

    Overrides live OUTSIDE `brand_dna`: applying one never mutates the stored
    DNA document -- that isolation is what lets a campaign diverge safely.
    """
    if subject_type not in OVERRIDE_SUBJECT_TYPES:
        raise BrandConfigError(
            f"subject_type must be one of {OVERRIDE_SUBJECT_TYPES}, got {subject_type!r}"
        )
    if not str(subject_id or "").strip():
        raise BrandConfigError("subject_id is required")
    if not isinstance(patch, dict) or not patch:
        raise BrandConfigError("override patch must be a non-empty mapping")
    row = session.scalar(
        select(BrandOverride).where(
            BrandOverride.workspace_id == workspace_id,
            BrandOverride.subject_type == subject_type,
            BrandOverride.subject_id == str(subject_id),
        )
    )
    if row is None:
        row = BrandOverride(
            workspace_id=workspace_id,
            subject_type=subject_type,
            subject_id=str(subject_id),
            override_json={},
        )
        session.add(row)
    row.override_json = deep_merge(dict(row.override_json or {}), _prune(patch))
    session.flush()
    return row


def clear_override(
    session: Session, workspace_id: str, subject_type: str, subject_id: str
) -> bool:
    """Delete an override patch. Returns True when a row was removed."""
    row = session.scalar(
        select(BrandOverride).where(
            BrandOverride.workspace_id == workspace_id,
            BrandOverride.subject_type == subject_type,
            BrandOverride.subject_id == str(subject_id),
        )
    )
    if row is None:
        return False
    session.delete(row)
    session.flush()
    return True


def get_effective_config(
    session: Session, workspace_id: str, effective_config_id: str
) -> dict[str, Any]:
    """Load a persisted snapshot (404-equivalent across workspaces)."""
    row = session.get(BrandEffectiveConfig, effective_config_id)
    if row is None or row.workspace_id != workspace_id:
        raise BrandNotFound(f"effective config {effective_config_id} not found")
    return dict(row.effective_json or {})


# ---------------------------------------------------------------------------
# resolution
# ---------------------------------------------------------------------------


def _split_overrides(
    overrides: dict[str, Any] | None,
) -> tuple[dict[str, dict], dict[str, Any]]:
    """Accept either level-keyed ({"campaign": {...}}) or flat patches.

    A flat patch is an artifact/content-level override (applied at the
    content level); level-keyed patches are applied inside their level.
    """
    if not isinstance(overrides, dict) or not overrides:
        return {}, {}
    if set(overrides) <= LEVEL_KEYS and all(
        isinstance(value, dict) for value in overrides.values()
    ):
        return dict(overrides), {}
    return {}, dict(overrides)


def _as_patch(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _require_campaign(session: Session, workspace_id: str, campaign_id: str) -> None:
    row = session.get(Campaign, campaign_id)
    if row is None or row.workspace_id != workspace_id:
        raise BrandNotFound(f"campaign {campaign_id} not found")


def _require_content(session: Session, workspace_id: str, content_id: str) -> ContentItem:
    row = session.get(ContentItem, content_id)
    if row is None or row.workspace_id != workspace_id:
        raise BrandNotFound(f"content {content_id} not found")
    return row


def resolve_effective_policy(
    session: Session,
    workspace_id: str,
    *,
    campaign_id: str | None = None,
    content_id: str | None = None,
    platform: str | None = None,
    overrides: dict[str, Any] | None = None,
    brand_id: str | None = None,
) -> EffectiveCreativePolicy:
    """Resolve the effective creative policy for one subject and snapshot it.

    Precedence (last wins): workspace defaults -> BrandDNA -> campaign ->
    content -> platform. Stored BrandDNA is never mutated by any override.
    Every call persists a `brand_effective_configs` snapshot and returns the
    policy carrying its `effective_config_id`.
    """
    defaults = workspace_defaults(session, workspace_id)  # raises when unknown ws
    nested, flat = _split_overrides(overrides)

    dna, selected_brand_id = brand_layer(session, workspace_id, brand_id)

    # -- campaign level (auto-derived from content when not given) --------
    effective_campaign_id = campaign_id
    content_row = (
        _require_content(session, workspace_id, content_id) if content_id else None
    )
    if content_row is not None and not effective_campaign_id:
        effective_campaign_id = content_row.campaign_id or None
    campaign_patch: dict[str, Any] = {}
    if effective_campaign_id:
        _require_campaign(session, workspace_id, effective_campaign_id)
        campaign_patch = override_row(
            session, workspace_id, "campaign", effective_campaign_id
        )
    campaign_patch = deep_merge(campaign_patch, _as_patch(nested.get("campaign")))

    # -- content level ----------------------------------------------------
    content_patch: dict[str, Any] = {}
    if content_row is not None:
        content_patch = override_row(session, workspace_id, "content", content_id)
    content_patch = deep_merge(content_patch, _as_patch(nested.get("content")))
    content_patch = deep_merge(content_patch, flat)  # caller/artifact-level patch

    # -- platform level (DNA platform fragment, applied LAST) -------------
    token = normalize_platform(platform)
    platform_patch: dict[str, Any] = {}
    if token:
        fragments = dna.get("platform_overrides") or {}
        platform_patch = _as_patch(fragments.get(token))
        platform_patch = deep_merge(platform_patch, _as_patch(nested.get("platform")))

    levels: list[tuple[str, dict[str, Any]]] = [
        ("workspace", defaults),
        ("brand", _prune(dna)),
    ]
    if campaign_patch:
        levels.append(("campaign", _prune(campaign_patch)))
    if content_patch:
        levels.append(("content", _prune(content_patch)))
    if platform_patch:
        levels.append(("platform", _prune(platform_patch)))

    merged, leaf_provenance = merge_levels(levels)
    version = dna_version(dna)

    if content_id:
        subject = {"subject_type": "content", "subject_id": content_id}
    elif effective_campaign_id:
        subject = {"subject_type": "campaign", "subject_id": effective_campaign_id}
    else:
        subject = {"subject_type": "workspace", "subject_id": ""}

    policy = EffectiveCreativePolicy.build(
        merged,
        leaf_provenance,
        platform=token or None,
        workspace_id=workspace_id,
        brand_id=selected_brand_id,
        subject=subject,
        dna_version=version,
    )

    # -- persist the snapshot (reproducibility ledger) --------------------
    snapshot = {
        "effective": merged,
        "provenance": dict(policy.provenance),
        "levels": {name: sorted(patch) for name, patch in levels},
        "subject": dict(subject),
        "platform": token or None,
        "brand_id": selected_brand_id,
        "dna_version": version,
    }
    row = BrandEffectiveConfig(
        workspace_id=workspace_id,
        subject_type=str(subject["subject_type"]),
        subject_id=str(subject["subject_id"]),
        platform=token or None,
        effective_json=snapshot,
        dna_version=version,
        note="resolve_effective_policy",
    )
    session.add(row)
    session.flush()
    policy.effective_config_id = row.id
    # Commit the snapshot to release the SQLite write lock — resolve runs at
    # the START of long stages (jobs, LLM calls) and an open txn here would
    # starve every other writer ("database is locked"). BUT never commit
    # inside a caller's savepoint: that would release the savepoint and blow
    # up atomic units (ResourceClosedError); their own unit/stage commit
    # persists the snapshot moments later (stage-commit pattern, Works 06/07).
    try:
        in_nested = session.get_nested_transaction() is not None
    except Exception:  # noqa: BLE001 — never fail resolution over the guard
        in_nested = False
    if not in_nested:
        session.commit()
    return policy
