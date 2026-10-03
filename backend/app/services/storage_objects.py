"""Canonical stored objects: workspace isolation, atomic finalization (Work 16 §5).

What this module is, and what it wraps
-------------------------------------
``app.services.storage`` already exists and is the canonical abstraction behind
``MediaAsset.storage_key`` -- ``LocalStorage``, ``S3Storage``, ``get_storage()``,
``managed_path()``, ``validate_storage_key()``. This module does not replace it
and does not import ``boto3``: S3 support stays behind that boundary, so there
is exactly one place in the codebase that knows what a backend is. What this
adds is the *object* layer the old module lacks:

* a **stable object id** (``object_key``) derived from workspace + kind +
  logical name, so a retried upload resolves to the same key instead of
  littering ``final-1 (3).mp4``;
* a **lifecycle** (PENDING -> FINALIZED -> DELETED) recorded in the database,
  so "the bytes exist" is a fact with a row behind it;
* **atomic finalization**: stream to a ``.part`` file in the destination
  directory, fsync, ``os.replace``, and only then write the FINALIZED row --
  in that order, so a crash anywhere leaves either the old object or no object,
  never a truncated one that claims to be complete.

The invariant, stated once so it can be tested directly
------------------------------------------------------
    **A temporary file may exist. It may never BECOME canonical state.**

Enforced in two places, because one place is a promise and two is a boundary:

1. :func:`finalize` refuses any source path inside the staging root. The
   bytes have to be written to a destination the caller names, and if that
   destination is the scratch directory the object is still, by definition,
   in progress.
2. :func:`resolve` refuses any key that resolves inside the staging root, and
   refuses any key belonging to another workspace -- checked on BOTH the key's
   workspace prefix and the resolved path, so neither a forged prefix nor a
   traversal can cross the line.

Workspace isolation is enforced on the key (``{workspace}/...``) *and* on the
resolved filesystem path, reusing ``storage.validate_storage_key`` so this
module cannot drift from the boundary every other caller already obeys.

Signed access
-------------
:func:`signed_url` delegates to ``services.public_links`` for the local backend
(that is where the HMAC media tokens already live) and to the backend's own
presigner for S3. The rule is that access tokens are minted by the module that
already owns them, not reimplemented here.

Cleanup policy
--------------
:func:`sweep` removes PENDING rows whose upload never finished (they are
promises, and a promise nobody is keeping is garbage) and expired FINALIZED
objects. It never deletes an object another row references through
``ref_type``/``ref_id`` unless asked to -- the lifecycle in
``services.retention`` owns *that* decision, and duplicating it here would give
two sweeps the authority to delete the same bytes.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import time
from collections.abc import Iterable
from datetime import timedelta
from pathlib import Path, PurePosixPath

from loguru import logger
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import session_scope
from app.models import StorageObject
from app.models.base import utcnow
from app.services import storage as storage_service
from app.services import streaming_io

__all__ = [
    "CanonicalRuleViolation",
    "ObjectNotFound",
    "OBJECT_KINDS",
    "StagingRoot",
    "describe",
    "delete",
    "finalize",
    "get",
    "open_stream",
    "object_key",
    "purge",
    "read_bytes",
    "register_pending",
    "resolve",
    "signed_url",
    "staging_root",
    "sweep",
    "write_object",
]

#: The kinds of thing YMONEY stores. Each is a first-class kind because each
#: has a different lifetime and a different consumer, and collapsing them into
#: "asset" is how a proxy outlives the source it was derived from.
OBJECT_KINDS: tuple[str, ...] = (
    "source", "proxy", "render", "thumbnail", "subtitle",
    "generated", "export", "archive",
)


class CanonicalRuleViolation(ValueError):
    """An attempt to make temporary state canonical, or to cross workspaces."""


class ObjectNotFound(LookupError):
    """No finalized object for this workspace + key."""


# ---------------------------------------------------------------------------
# Staging root
# ---------------------------------------------------------------------------


def staging_root() -> Path:
    """The scratch directory. Resolved, so containment checks are reliable.

    Deliberately a SIBLING of ``data/videos``, not a child: a child would sit
    inside the managed media tree, and any future path check that only
    validated "under STORAGE_ROOT" would happily serve a scratch file.
    """
    from app.core.config import settings

    return Path(getattr(settings, "storage_staging_dir",
                         "data/storage_staging")).resolve()


class StagingRoot:
    """Context manager owning one scratch directory inside the staging root."""

    def __init__(self, *, prefix: str = "obj", root: Path | None = None) -> None:
        self.path = streaming_io.temp_dir(prefix=prefix,
                                          root=root or staging_root())
        self._root = (root or staging_root()).resolve()

    def child(self, name: str) -> Path:
        safe = PurePosixPath(str(name).replace("\\", "/")).name
        if not safe or safe in (".", ".."):
            raise CanonicalRuleViolation(f"unsafe scratch filename {name!r}")
        return self.path / safe

    def close(self) -> None:
        shutil.rmtree(self.path, ignore_errors=True)

    def __enter__(self) -> StagingRoot:
        return self

    def __exit__(self, *_exc) -> None:
        self.close()


def _is_inside(root: Path, candidate: Path) -> bool:
    try:
        candidate.resolve().relative_to(root.resolve())
        return True
    except (ValueError, OSError):
        return False


def assert_not_staging(path: str | Path) -> None:
    """Raise when a path is scratch space. Used by finalize and resolve."""
    root = staging_root()
    if _is_inside(root, Path(path)):
        raise CanonicalRuleViolation(
            f"{path} is inside the staging root {root}; a temporary file may "
            f"exist but must never become canonical state"
        )


# ---------------------------------------------------------------------------
# Stable object keys
# ---------------------------------------------------------------------------


def object_key(workspace_id: str, kind: str, logical_name: str,
               *, extension: str = "") -> str:
    """A stable, workspace-scoped key for one logical object.

    Stable means: the same (workspace, kind, logical name) yields the same key
    on every call, in every process, forever. That is what makes a retried
    upload idempotent -- the second attempt overwrites the first instead of
    creating ``final-1 (2).mp4``, which is the failure mode of any
    ``uuid4``-per-write scheme.

    The human-readable stem is kept in the key (sanitised, truncated) so an
    operator reading a directory listing can tell what a file is; the short
    digest is what makes two different logical names that sanitise to the same
    stem distinct.
    """
    ws = str(workspace_id or "").strip()
    if not ws:
        raise CanonicalRuleViolation("workspace_id is required")
    if str(kind or "") not in OBJECT_KINDS:
        raise CanonicalRuleViolation(
            f"unknown object kind {kind!r}; expected one of {OBJECT_KINDS}")
    safe_ws = PurePosixPath(ws.replace("\\", "/")).name
    if not safe_ws or safe_ws in (".", ".."):
        raise CanonicalRuleViolation(f"unsafe workspace id {workspace_id!r}")

    stem = PurePosixPath(str(logical_name or "").replace("\\", "/")).name
    stem = "".join(ch for ch in stem if ch.isalnum() or ch in "._-")[:60].strip(".")
    if not stem:
        stem = "object"
    if extension and not stem.lower().endswith(extension.lower()):
        stem = f"{stem}{extension}"
    digest = hashlib.sha256(
        f"{safe_ws}|{kind}|{logical_name}".encode()).hexdigest()[:12]
    return f"{safe_ws}/{kind}/{digest}-{stem}"


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


def register_pending(workspace_id: str, kind: str, logical_name: str, *,
                     temp_path: str = "", content_type: str = "",
                     extension: str = "", ref_type: str = "",
                     ref_id: str = "", meta: dict | None = None) -> StorageObject:
    """Record an upload in flight. The row is a PROMISE, never content."""
    ws = str(workspace_id or "").strip()
    key = object_key(ws, kind, logical_name, extension=extension)
    with session_scope() as s:
        row = s.scalar(select(StorageObject).where(
            StorageObject.workspace_id == ws, StorageObject.object_key == key))
        if row is None:
            row = StorageObject(workspace_id=ws, object_key=key)
            s.add(row)
        row.state = StorageObject.PENDING
        row.kind = str(kind)
        row.temp_path = str(temp_path or "")
        row.content_type = str(content_type or "")
        row.ref_type = str(ref_type or "")
        row.ref_id = str(ref_id or "")
        row.backend = str(getattr(storage_service.get_storage(), "backend_name",
                                  "local"))
        if meta:
            row.meta_json = dict(meta)
        s.flush()
        s.expunge(row)
        return row


def resolve(workspace_id: str, object_key_value: str) -> Path:
    """The absolute path of a finalized object, or raise.

    Three refusals, each for a different reason:

    * another workspace's key -- the key's workspace segment is not this one;
    * a staging-root path -- scratch space is not addressable, ever;
    * anything ``storage.validate_storage_key`` rejects -- one boundary check,
      shared with every other caller, so this module cannot become the weak one.

    The stored key is workspace-PREFIXED (that prefix is the isolation check),
    while ``validate_storage_key`` wants a key RELATIVE to the workspace
    directory. Stripping it here, once, in the only function that resolves, is
    what stops the two conventions composing into ``ws/ws/render/...`` paths.
    """
    ws = str(workspace_id or "").strip()
    key = str(object_key_value or "").strip()
    if not ws or not key:
        raise ObjectNotFound("workspace and object key are required")
    normalized = key.replace("\\", "/").lstrip("/")
    head = normalized.split("/", 1)[0]
    if head != PurePosixPath(ws.replace("\\", "/")).name:
        raise CanonicalRuleViolation(
            f"object {key!r} does not belong to workspace {ws!r}")
    assert_not_staging(normalized)
    relative = storage_service.validate_storage_key(
        ws, normalized[len(head) + 1:])
    if relative is None:
        raise CanonicalRuleViolation(
            f"object key {key!r} escapes the managed workspace directory")
    path = storage_service.managed_path(ws, str(storage_service.STORAGE_ROOT
                                               / ws / relative))
    if path is None:
        raise CanonicalRuleViolation(f"object {key!r} did not resolve")
    assert_not_staging(path)
    return path


def finalize(object_id: str, source_path: str | Path, *,
             content_type: str = "", checksum: str = "",
             size_bytes: int | None = None, min_bytes: int = 0) -> StorageObject:
    """Publish a completed file as canonical state. Atomic.

    Order matters and is the whole design:

    1. refuse a source inside the staging root (scratch is not canonical);
    2. stream it to ``<dest>.part``, hashing, then ``os.replace`` into place;
    3. verify the bytes that actually landed, and that there were some;
    4. write the FINALIZED row LAST.

    A crash between 2 and 4 leaves an unreferenced file and a PENDING row, and
    the sweep reclaims the row. A crash during 2 leaves only a ``.part``, which
    ``write_atomic`` removes. In no window is a truncated file addressable as an
    object.

    ``min_bytes`` is the "did the encoder actually produce anything" check. A
    zero-byte artifact passes every existence test and is worthless, so a caller
    publishing a render says ``min_bytes=1`` and gets a refusal instead of an
    empty object that downstream QC will treat as a success.
    """
    from app.models.base import utcnow as _now

    assert_not_staging(source_path)
    with session_scope() as s:
        row = s.get(StorageObject, str(object_id))
        if row is None:
            raise ObjectNotFound(f"no pending object {object_id!r}")
        if row.state == StorageObject.FINALIZED:
            return row
        workspace_id = str(row.workspace_id)
        object_key_text = str(row.object_key)
        destination = resolve(workspace_id, object_key_text)

    report = streaming_io.copy_stream(source_path, destination,
                                      expected_checksum=checksum or None)
    digest = checksum or report.checksum
    size = int(size_bytes if size_bytes is not None else report.size_bytes)
    if digest != report.checksum:
        raise CanonicalRuleViolation(
            f"finalized bytes for {object_key_text} hash {report.checksum}, "
            f"not the declared {digest}")
    if report.size_bytes < int(min_bytes):
        destination.unlink(missing_ok=True)
        raise CanonicalRuleViolation(
            f"refusing to publish {object_key_text}: {report.size_bytes} bytes "
            f"is below the required {int(min_bytes)}")

    with session_scope() as s:
        row = s.get(StorageObject, str(object_id))
        if row is None:
            raise ObjectNotFound(f"no pending object {object_id!r}")
        row.state = StorageObject.FINALIZED
        row.checksum = digest
        row.size_bytes = size
        row.content_type = str(content_type or row.content_type or "")
        row.temp_path = ""
        row.finalized_at = _now()
        row.expires_at = None
        s.flush()
        s.expunge(row)
        return row


def write_object(workspace_id: str, kind: str, source_path: str | Path, *,
                 logical_name: str | None = None, content_type: str = "",
                 extension: str = "", ref_type: str = "", ref_id: str = "",
                 meta: dict | None = None, min_bytes: int = 0) -> StorageObject:
    """Register and finalize in one call -- the common path.

    Still two database writes and one atomic rename; it is not "write the file
    and hope". The two-step form exists so a caller that streams a large
    upload can hold the PENDING row open for as long as the transfer takes.
    """
    source = Path(source_path)
    assert_not_staging(source)
    name = logical_name or source.name
    pending = register_pending(
        workspace_id, kind, name, content_type=content_type,
        extension=extension or source.suffix, ref_type=ref_type, ref_id=ref_id,
        meta=meta)
    return finalize(pending.id, source, content_type=content_type,
                    min_bytes=min_bytes)


def upload_object(workspace_id: str, kind: str, chunks: Iterable[bytes], *,
                  logical_name: str, content_type: str = "",
                  extension: str = "", ref_type: str = "",
                  ref_id: str = "", meta: dict | None = None,
                  min_bytes: int = 0) -> StorageObject:
    """Stream bytes straight to a canonical object, atomically.

    The bytes never exist whole: they go chunk -> ``.part`` -> rename. This is
    the path a multi-GB upload takes, and it is why that upload does not need
    multi-GB of RAM.
    """
    pending = register_pending(
        workspace_id, kind, logical_name, content_type=content_type,
        extension=extension, ref_type=ref_type, ref_id=ref_id, meta=meta)
    with session_scope() as s:
        row = s.get(StorageObject, pending.id)
        workspace_id = str(row.workspace_id)
        key = str(row.object_key)
    destination = resolve(workspace_id, key)
    report = streaming_io.write_atomic(destination, chunks)
    if report.size_bytes < int(min_bytes):
        destination.unlink(missing_ok=True)
        raise CanonicalRuleViolation(
            f"refusing to publish {key}: {report.size_bytes} bytes is below "
            f"the required {int(min_bytes)}")
    with session_scope() as s:
        row = s.get(StorageObject, pending.id)
        row.state = StorageObject.FINALIZED
        row.checksum = report.checksum
        row.size_bytes = report.size_bytes
        row.content_type = str(content_type or row.content_type or "")
        row.temp_path = ""
        row.finalized_at = utcnow()
        s.flush()
        s.expunge(row)
        return row


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


def get(workspace_id: str, object_key_value: str, *, db: Session | None = None) -> StorageObject:
    """The row for a key. Raises unless it is FINALIZED in THIS workspace."""
    ws = str(workspace_id or "")
    key = str(object_key_value or "")
    if not ws or not key:
        raise ObjectNotFound("workspace and object key are required")
    head = key.replace("\\", "/").lstrip("/").split("/", 1)[0]
    if head != PurePosixPath(ws).name:
        raise CanonicalRuleViolation(
            f"object {key!r} does not belong to workspace {ws!r}")

    def _read(s: Session) -> StorageObject:
        row = s.scalar(select(StorageObject).where(
            StorageObject.workspace_id == ws, StorageObject.object_key == key))
        if row is None:
            raise ObjectNotFound(f"no object {key!r} in workspace {ws!r}")
        if row.state != StorageObject.FINALIZED:
            raise ObjectNotFound(
                f"object {key!r} is {row.state}, not finalized; a temporary "
                f"object is never readable as canonical state")
        return row

    if db is not None:
        row = _read(db)
        db.expunge(row)
        return row
    with session_scope() as s:
        return _read(s)


def open_stream(workspace_id: str, object_key_value: str, *,
                chunk_bytes: int | None = None):
    """Iterate an object's bytes in bounded chunks. Raises for a temp object."""
    row = get(workspace_id, object_key_value)
    path = resolve(workspace_id, row.object_key)
    if not path.exists():
        raise ObjectNotFound(f"object bytes are gone for {row.object_key!r}")
    with session_scope() as s:
        live = s.get(StorageObject, row.id)
        live.last_accessed_at = utcnow()
    return streaming_io.iter_chunks(path, chunk_bytes=chunk_bytes)


def read_bytes(workspace_id: str, object_key_value: str, *,
               max_bytes: int = 8 << 20) -> bytes:
    """Read a SMALL object. Refuses a large one rather than buffering it."""
    row = get(workspace_id, object_key_value)
    path = resolve(workspace_id, row.object_key)
    if int(row.size_bytes or 0) > int(max_bytes):
        raise streaming_io.TempBudgetExceeded(
            f"object {row.object_key!r} is {row.size_bytes} bytes; use "
            f"open_stream() above {int(max_bytes)}")
    return streaming_io.read_all(path, max_bytes=max_bytes)


def checksum(workspace_id: str, object_key_value: str) -> str:
    """Re-hash the stored bytes and compare against the recorded checksum.

    This is the final-artifact verification step: a row that says FINALIZED
    with a checksum is a *claim* until somebody re-reads the bytes.
    """
    row = get(workspace_id, object_key_value)
    path = resolve(workspace_id, row.object_key)
    actual, size = streaming_io.checksum_and_size(path)
    if row.checksum and actual != row.checksum:
        raise CanonicalRuleViolation(
            f"object {row.object_key!r} is corrupt: recorded {row.checksum}, "
            f"on disk {actual}")
    if row.size_bytes and size != int(row.size_bytes):
        raise CanonicalRuleViolation(
            f"object {row.object_key!r} is truncated: recorded "
            f"{row.size_bytes} bytes, on disk {size}")
    return actual


# ---------------------------------------------------------------------------
# Access + lifecycle
# ---------------------------------------------------------------------------


def signed_url(workspace_id: str, object_key_value: str, *,
               expires_hours: float = 48.0) -> str:
    """A time-limited URL for one object in one workspace.

    Delegates to whichever module already owns token minting for the active
    backend. Never mints a token here: two HMAC schemes in one codebase is one
    too many, and the local one is already used by every publisher.
    """
    row = get(workspace_id, object_key_value)
    backend = str(row.backend or "local")
    if backend == "s3":
        provider = storage_service.get_storage()
        presign = getattr(provider, "presigned_url", None)
        if presign is None:  # pragma: no cover - S3Storage always has it
            return ""
        return str(presign(row.object_key, expires_seconds=expires_hours * 3600))
    from app.services import public_links

    path = resolve(workspace_id, row.object_key)
    return public_links.public_media_url(workspace_id, str(path))


def delete(workspace_id: str, object_key_value: str, *, remove_bytes: bool = True,
           db: Session | None = None) -> bool:
    """Mark DELETED and (by default) unlink the bytes. Idempotent."""

    def _run(s: Session) -> bool:
        ws = str(workspace_id or "")
        row = s.scalar(select(StorageObject).where(
            StorageObject.workspace_id == ws,
            StorageObject.object_key == str(object_key_value or "")))
        if row is None:
            return False
        already = row.state == StorageObject.DELETED
        row.state = StorageObject.DELETED
        row.temp_path = ""
        path: Path | None = None
        if remove_bytes:
            try:
                path = resolve(ws, row.object_key)
            except (CanonicalRuleViolation, ObjectNotFound):
                path = None  # never resolvable: there is nothing to remove
        s.flush()
        if path is not None and path.exists():
            try:
                path.unlink()
            except OSError as exc:  # pragma: no cover - permissions
                logger.warning(f"could not remove object bytes {path}: {exc}")
        return not already

    if db is not None:
        return _run(db)
    with session_scope() as s:
        return _run(s)


def sweep(*, now=None, workspace_id: str = "",
          pending_ttl_seconds: float | None = None) -> dict:
    """Reclaim objects whose promises were never kept, plus expired ones.

    A PENDING row older than the TTL is an upload that died. Its bytes, if any,
    are a ``.part`` file the writer abandoned, and its row is a lie the rest of
    the system would otherwise read as "an object exists". Both go.
    """
    from app.core.config import settings

    stamp = now or utcnow()
    ttl = float(pending_ttl_seconds if pending_ttl_seconds is not None
                else getattr(settings, "storage_pending_ttl_seconds", 3600.0))
    pending_cutoff = stamp - timedelta(seconds=max(0.0, ttl))
    result = {"pending_dropped": 0, "expired_dropped": 0, "part_removed": 0}
    with session_scope() as s:
        q = select(StorageObject).where(
            StorageObject.state == StorageObject.PENDING,
            StorageObject.created_at < pending_cutoff)
        if workspace_id:
            q = q.where(StorageObject.workspace_id == workspace_id)
        for row in list(s.scalars(q).all()):
            if row.temp_path:
                _unlink_quietly(Path(row.temp_path))
                result["part_removed"] += 1
            s.delete(row)
            result["pending_dropped"] += 1
        q2 = select(StorageObject).where(
            StorageObject.state == StorageObject.FINALIZED,
            StorageObject.expires_at.is_not(None),
            StorageObject.expires_at <= stamp)
        if workspace_id:
            q2 = q2.where(StorageObject.workspace_id == workspace_id)
        for row in list(s.scalars(q2).all()):
            try:
                path = resolve(str(row.workspace_id), str(row.object_key))
                _unlink_quietly(path)
            except (CanonicalRuleViolation, ObjectNotFound):
                pass
            s.delete(row)
            result["expired_dropped"] += 1
    return result


def _unlink_quietly(path: Path) -> None:
    try:
        if path.exists():
            path.unlink()
    except OSError as exc:  # pragma: no cover - permissions
        logger.warning(f"could not remove {path}: {exc}")


def purge(*, older_than_seconds: float = 3600.0,
          root: str | Path | None = None) -> int:
    """Remove stale scratch directories. Thin wrapper over the IO module."""
    return streaming_io.sweep_temp(older_than_seconds=older_than_seconds, root=root)


def describe(workspace_id: str) -> dict:
    """An operator's view of one workspace's stored objects."""
    ws = str(workspace_id or "")
    with session_scope() as s:
        rows = list(s.scalars(select(StorageObject).where(
            StorageObject.workspace_id == ws)).all())
        by_kind: dict[str, dict[str, int]] = {}
        for row in rows:
            bucket = by_kind.setdefault(str(row.kind), {"count": 0, "bytes": 0})
            bucket["count"] += 1
            bucket["bytes"] += int(row.size_bytes or 0)
        return {
            "workspace_id": ws,
            "objects": len(rows),
            "finalized": sum(1 for r in rows if r.state == StorageObject.FINALIZED),
            "pending": sum(1 for r in rows if r.state == StorageObject.PENDING),
            "total_bytes": sum(int(r.size_bytes or 0) for r in rows),
            "by_kind": by_kind,
        }


def _now_epoch() -> float:  # pragma: no cover - diagnostics
    return time.time()


def _os_link_supported() -> bool:  # pragma: no cover - diagnostics
    return hasattr(os, "replace")