"""Local workspace files connector — lists this workspace's MediaAsset rows.

No network, no credentials: ``list()`` reads the ``media_assets`` table for
ONE workspace and renders each row as a document. The workspace scope is
held-config like everything else (``config["workspace_id"]``); the sync
layer injects it from the connector row after a workspace-hard-filtered
lookup, so persisted config can never widen the scope.

Path safety: every non-empty ``storage_key`` must pass
``services.storage.validate_storage_key`` (workspace-relative, no ``..``,
no absolute paths). Keys that fail are refused — a list skips them, an
explicit ``fetch`` raises — so this adapter can never point outside the
workspace storage root.
"""

from __future__ import annotations

from pathlib import PurePosixPath

from sqlalchemy import select

from app.db import session_scope
from app.engine.sources.base import (
    SourceConnector,
    SourceDocumentDoc,
    SourceError,
)
from app.models import MediaAsset
from app.services.storage import validate_storage_key


def _filename(key: str) -> str:
    return PurePosixPath((key or "").replace("\\", "/")).name


class LocalConnector(SourceConnector):
    """Lists the workspace's stored media assets (snapshot single page)."""

    kind = "local"
    snapshot = True

    def _workspace_id(self) -> str:
        return str(self.config.get("workspace_id") or "").strip()

    def connect(self) -> None:
        """No I/O, no credentials, no network — local is always AVAILABLE.

        Workspace scope itself is enforced on every query: ``list``/
        ``fetch`` raise :class:`SourceError` when no workspace context was
        injected (the sync layer always injects one), so a context-less
        instance never fakes an empty listing.
"""

    def _require_workspace(self) -> str:
        workspace_id = self._workspace_id()
        if not workspace_id:
            raise SourceError("local connector has no workspace context")
        return workspace_id

    def _doc_for(self, asset: MediaAsset) -> SourceDocumentDoc | None:
        key = (asset.storage_key or "").strip()
        if key and validate_storage_key(self._workspace_id(), key) is None:
            return None  # absolute / traversal / foreign path — refused
        return SourceDocumentDoc(
            remote_id=str(asset.id),
            title=_filename(key) or str(asset.id),
            mime_type=str(asset.mime_type or ""),
            created_at=asset.created_at,
            updated_at=asset.updated_at,
            checksum=str(asset.checksum or ""),
            asset_reference=key,
            content="",
            meta={
                "type": str(asset.type or ""),
                "file_size": asset.file_size,
                "provider": str(asset.provider or ""),
            },
        )

    def list(self, *, cursor: str | None = None) -> tuple[list[SourceDocumentDoc], str | None]:
        workspace_id = self._require_workspace()
        with session_scope() as db:
            rows = db.scalars(
                select(MediaAsset)
                .where(MediaAsset.workspace_id == workspace_id)
                .order_by(MediaAsset.created_at.asc(), MediaAsset.id.asc())
            ).all()
            docs = []
            for asset in rows:
                doc = self._doc_for(asset)
                if doc is not None:
                    docs.append(doc)
        # one complete page: snapshot pass, cursorless by design
        return docs, ""

    def fetch(self, remote_id: str) -> SourceDocumentDoc:
        workspace_id = self._require_workspace()
        with session_scope() as db:
            asset = db.scalar(
                select(MediaAsset).where(
                    MediaAsset.id == str(remote_id),
                    MediaAsset.workspace_id == workspace_id,
                )
            )
            if asset is None:
                raise SourceError(f"asset not found in workspace: {str(remote_id)[:120]}")
            doc = self._doc_for(asset)
        if doc is None:
            raise SourceError("asset storage key escapes the workspace storage root")
        return doc

    def search(self, query: str, *, limit: int = 20) -> list[SourceDocumentDoc]:
        # titles only (asset bytes are never ingested here), so the base
        # paged substring scan is exact — short-circuit the common case
        needle = (query or "").strip().lower()
        if not needle:
            return []
        docs, _ = self.list()
        found = [doc for doc in docs if needle in (doc.title or "").lower()]
        return found[:limit]


__all__ = ["LocalConnector"]
