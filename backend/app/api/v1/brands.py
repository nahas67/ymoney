"""Brand system API (Work 08 Lane A): identities, DNA, assets, presets,
effective policy and the consistency verifier.

Routes (every one workspace-scoped -- a brand/asset/preset from another
workspace is 404, never a hint that it exists):

    GET    /workspaces/{ws}/brands                  list brands (+ DNA payload)
    POST   /workspaces/{ws}/brands                  create brand (+ optional DNA)
    GET    /workspaces/{ws}/brands/presets          creative template presets
    GET    /workspaces/{ws}/brands/effective        resolved policy + provenance
    GET    /workspaces/{ws}/brands/{id}             detail (DNA editor payload)
    PUT    /workspaces/{ws}/brands/{id}             update name/status/default/DNA
    POST   /workspaces/{ws}/brands/{id}/assets      link a MediaAsset ref (role)
    DELETE /workspaces/{ws}/brands/{id}/assets      unlink a MediaAsset ref
    POST   /workspaces/{ws}/brands/{id}/verify      brand consistency report

The legacy white-label kit (`GET|POST /workspaces/{ws}/brand`, Work 01) is
untouched: this router adds the creative-identity surface beside it. Brand DNA
stores MediaAsset REFERENCES only -- bytes never travel through these routes.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.engine.brand import (
    BrandConfigError,
    BrandDNA,
    BrandDNASchemaError,
    BrandNotFound,
    resolve_effective_policy,
    verify_artifact,
)
from app.models import (
    Brand,
    BrandAsset,
    BrandDNARow,
    BrandPreset,
    MediaAsset,
    Workspace,
)
from app.models.brand import BRAND_ASSET_ROLES
from app.services.auth_service import require_workspace_role

brand_router = APIRouter(prefix="/workspaces/{workspace_id}/brands", tags=["brands"])


# ---------------------------------------------------------------------------
# bodies
# ---------------------------------------------------------------------------


class BrandCreate(BaseModel):
    name: str = Field(default="", max_length=160)
    is_default: bool = False
    dna: dict | None = None


class BrandUpdate(BaseModel):
    name: str | None = Field(default=None, max_length=160)
    status: str | None = Field(default=None, max_length=20)
    is_default: bool | None = None
    dna: dict | None = None  # full DNA replacement when provided


class BrandAssetLink(BaseModel):
    media_asset_id: str = Field(min_length=1, max_length=36)
    asset_role: str = Field(default="logo", max_length=30)
    label: str = Field(default="", max_length=160)


class BrandVerifyBody(BaseModel):
    artifact: Any = Field(default_factory=dict)
    artifact_kind: str = Field(default="clip", max_length=40)
    campaign_id: str | None = Field(default=None, max_length=36)
    content_id: str | None = Field(default=None, max_length=36)
    platform: str | None = Field(default=None, max_length=30)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _brand_or_404(db: Session, workspace_id: str, brand_id: str) -> Brand:
    row = db.scalar(
        select(Brand).where(Brand.id == brand_id, Brand.workspace_id == workspace_id)
    )
    if row is None:
        raise HTTPException(status_code=404, detail="brand not found")
    return row


def _dna_row(db: Session, workspace_id: str, brand_id: str) -> BrandDNARow | None:
    return db.scalar(
        select(BrandDNARow).where(
            BrandDNARow.workspace_id == workspace_id,
            BrandDNARow.brand_id == brand_id,
        )
    )


def _store_dna(db: Session, workspace_id: str, brand_id: str, dna: dict) -> dict:
    """Validate + persist a full DNA document for one brand."""
    try:
        validated = BrandDNA.model_validate(dict(dna or {})).as_dict()
    except (BrandDNASchemaError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=f"invalid BrandDNA: {exc}") from exc
    row = _dna_row(db, workspace_id, brand_id)
    if row is None:
        row = BrandDNARow(workspace_id=workspace_id, brand_id=brand_id, dna_json=validated)
        db.add(row)
    else:
        row.dna_json = validated
    db.flush()
    return validated


def _asset_dto(row: BrandAsset) -> dict:
    return {
        "id": row.id,
        "brand_id": row.brand_id,
        "asset_role": row.asset_role,
        "media_asset_id": row.media_asset_id,
        "label": row.label or "",
        "created_at": row.created_at.isoformat() + "Z" if row.created_at else "",
    }


def _brand_dto(row: Brand, dna: dict | None = None, assets: list[BrandAsset] | None = None) -> dict:
    return {
        "id": row.id,
        "workspace_id": row.workspace_id,
        "name": row.name or "",
        "is_default": bool(row.is_default),
        "status": row.status,
        "dna": dict(dna or {}),
        "assets": [_asset_dto(a) for a in (assets or [])],
        "created_at": row.created_at.isoformat() + "Z" if row.created_at else "",
        "updated_at": row.updated_at.isoformat() + "Z" if row.updated_at else "",
    }


def _load_assets(db: Session, workspace_id: str, brand_id: str) -> list[BrandAsset]:
    return list(
        db.scalars(
            select(BrandAsset)
            .where(BrandAsset.workspace_id == workspace_id, BrandAsset.brand_id == brand_id)
            .order_by(BrandAsset.created_at)
        )
    )


def _make_default(db: Session, workspace_id: str, keep_id: str) -> None:
    rows = db.scalars(
        select(Brand).where(Brand.workspace_id == workspace_id, Brand.is_default.is_(True))
    ).all()
    for row in rows:
        if row.id != keep_id:
            row.is_default = False
    db.flush()


# ---------------------------------------------------------------------------
# brands
# ---------------------------------------------------------------------------


@brand_router.get("", summary="List workspace brands (with DNA payload)")
def list_brands(
    ws: Workspace = Depends(require_workspace_role("viewer")),
    db: Session = Depends(get_db),
):
    rows = db.scalars(
        select(Brand)
        .where(Brand.workspace_id == ws.id, Brand.status == "active")
        .order_by(Brand.is_default.desc(), Brand.created_at)
    ).all()
    out = []
    for row in rows:
        dna_row = _dna_row(db, ws.id, row.id)
        out.append(_brand_dto(row, dict(dna_row.dna_json or {}) if dna_row else {},
                               _load_assets(db, ws.id, row.id)))
    return {"brands": out}


@brand_router.post("", summary="Create a brand (+ optional BrandDNA)", status_code=201)
def create_brand(
    body: BrandCreate,
    ws: Workspace = Depends(require_workspace_role("admin")),
    db: Session = Depends(get_db),
):
    row = Brand(workspace_id=ws.id, name=body.name.strip()[:160], is_default=False)
    db.add(row)
    db.flush()
    if body.is_default:
        _make_default(db, ws.id, row.id)
        row.is_default = True
    dna = _store_dna(db, ws.id, row.id, body.dna or {}) if body.dna is not None else {}
    db.commit()
    return {"brand": _brand_dto(row, dna, _load_assets(db, ws.id, row.id))}


@brand_router.get("/presets", summary="Creative template presets for this workspace")
def list_presets(
    ws: Workspace = Depends(require_workspace_role("viewer")),
    db: Session = Depends(get_db),
):
    rows = db.scalars(
        select(BrandPreset)
        .where(BrandPreset.workspace_id == ws.id)
        .order_by(BrandPreset.builtin.desc(), BrandPreset.name)
    ).all()
    return {
        "presets": [
            {
                "id": r.id,
                "name": r.name or "",
                "builtin": bool(r.builtin),
                "preset": dict(r.preset_json or {}),
            }
            for r in rows
        ]
    }


@brand_router.get("/effective", summary="Resolved effective policy + provenance")
def effective_policy(
    campaign_id: str | None = Query(default=None, max_length=36),
    content_id: str | None = Query(default=None, max_length=36),
    platform: str | None = Query(default=None, max_length=30),
    brand_id: str | None = Query(default=None, max_length=36),
    ws: Workspace = Depends(require_workspace_role("viewer")),
    db: Session = Depends(get_db),
):
    try:
        policy = resolve_effective_policy(
            db,
            ws.id,
            campaign_id=campaign_id,
            content_id=content_id,
            platform=platform,
            brand_id=brand_id,
        )
    except BrandNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except BrandConfigError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    db.commit()
    return {
        "policy": policy.as_dict(),
        "hard_constraints": policy.hard_constraints_dict(),
        "provenance": dict(policy.provenance),
        "effective_config_id": policy.effective_config_id,
        "dna_version": policy.dna_version,
        "subject": dict(policy.subject),
    }


@brand_router.get("/{brand_id}", summary="Brand detail (DNA editor payload)")
def get_brand(
    brand_id: str,
    ws: Workspace = Depends(require_workspace_role("viewer")),
    db: Session = Depends(get_db),
):
    row = _brand_or_404(db, ws.id, brand_id)
    dna_row = _dna_row(db, ws.id, row.id)
    return {"brand": _brand_dto(row, dict(dna_row.dna_json or {}) if dna_row else {},
                                _load_assets(db, ws.id, row.id))}


@brand_router.put("/{brand_id}", summary="Update brand name/status/default/DNA")
def update_brand(
    brand_id: str,
    body: BrandUpdate,
    ws: Workspace = Depends(require_workspace_role("admin")),
    db: Session = Depends(get_db),
):
    row = _brand_or_404(db, ws.id, brand_id)
    if body.name is not None:
        row.name = body.name.strip()[:160]
    if body.status is not None:
        if body.status not in ("active", "archived"):
            raise HTTPException(status_code=422, detail="status must be active|archived")
        row.status = body.status
    if body.is_default is not None:
        row.is_default = bool(body.is_default)
        if body.is_default:
            _make_default(db, ws.id, row.id)
            row.is_default = True
    if body.dna is not None:
        dna = _store_dna(db, ws.id, row.id, body.dna)
    else:
        dna_row = _dna_row(db, ws.id, row.id)
        dna = dict(dna_row.dna_json or {}) if dna_row else {}
    db.commit()
    return {"brand": _brand_dto(row, dna, _load_assets(db, ws.id, row.id))}


# ---------------------------------------------------------------------------
# brand assets (MediaAsset references only -- bytes stay in storage)
# ---------------------------------------------------------------------------


@brand_router.post("/{brand_id}/assets", summary="Link a MediaAsset to a brand role")
def link_asset(
    brand_id: str,
    body: BrandAssetLink,
    ws: Workspace = Depends(require_workspace_role("member")),
    db: Session = Depends(get_db),
):
    row = _brand_or_404(db, ws.id, brand_id)
    if body.asset_role not in BRAND_ASSET_ROLES:
        raise HTTPException(
            status_code=422,
            detail=f"asset_role must be one of {list(BRAND_ASSET_ROLES)}",
        )
    media = db.scalar(
        select(MediaAsset).where(
            MediaAsset.id == body.media_asset_id, MediaAsset.workspace_id == ws.id
        )
    )
    if media is None:
        raise HTTPException(status_code=404, detail="media asset not found")
    existing = db.scalar(
        select(BrandAsset).where(
            BrandAsset.workspace_id == ws.id,
            BrandAsset.brand_id == row.id,
            BrandAsset.asset_role == body.asset_role,
            BrandAsset.media_asset_id == media.id,
        )
    )
    if existing is not None:
        raise HTTPException(status_code=409, detail="asset already linked for this role")
    link = BrandAsset(
        workspace_id=ws.id,
        brand_id=row.id,
        asset_role=body.asset_role,
        media_asset_id=media.id,
        label=body.label.strip()[:160],
    )
    db.add(link)
    db.commit()
    return {"asset": _asset_dto(link)}


@brand_router.delete("/{brand_id}/assets", summary="Unlink a MediaAsset from a brand")
def unlink_asset(
    brand_id: str,
    media_asset_id: str = Query(min_length=1, max_length=36),
    asset_role: str | None = Query(default=None, max_length=30),
    ws: Workspace = Depends(require_workspace_role("member")),
    db: Session = Depends(get_db),
):
    row = _brand_or_404(db, ws.id, brand_id)
    stmt = select(BrandAsset).where(
        BrandAsset.workspace_id == ws.id,
        BrandAsset.brand_id == row.id,
        BrandAsset.media_asset_id == media_asset_id,
    )
    if asset_role:
        stmt = stmt.where(BrandAsset.asset_role == asset_role)
    links = list(db.scalars(stmt).all())
    if not links:
        raise HTTPException(status_code=404, detail="brand asset link not found")
    for link in links:
        db.delete(link)
    db.commit()
    return {"removed": len(links)}


# ---------------------------------------------------------------------------
# verifier
# ---------------------------------------------------------------------------


@brand_router.post("/{brand_id}/verify", summary="Brand consistency report for an artifact")
def verify(
    brand_id: str,
    body: BrandVerifyBody,
    ws: Workspace = Depends(require_workspace_role("viewer")),
    db: Session = Depends(get_db),
):
    row = _brand_or_404(db, ws.id, brand_id)
    try:
        policy = resolve_effective_policy(
            db,
            ws.id,
            campaign_id=body.campaign_id,
            content_id=body.content_id,
            platform=body.platform,
            brand_id=row.id,
        )
        report = verify_artifact(
            db, ws.id, body.artifact_kind, body.artifact, policy=policy
        )
    except BrandNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except BrandConfigError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    db.commit()
    return {"report": report.to_dict()}
