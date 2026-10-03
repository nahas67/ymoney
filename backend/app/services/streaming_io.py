"""Chunked file movement: uploads, downloads, copies, checksums (Work 16 §6).

The failure this file prevents
------------------------------
Every unbounded read in a media pipeline eventually meets a file that does not
fit in RAM. ``Path(src).read_bytes()`` is fine for a thumbnail and is an
out-of-memory kill for a 3 GB source master. The tempting middle ground --
``f.read(MAX)`` -- is worse, because it is silently truncated and produces a
plausible-looking artifact that is missing its tail.

So the rule here is one rule: **bytes move in bounded chunks and are never
held whole.** Everything else follows.

* :func:`iter_chunks` is the only way this module reads a file.
* :func:`copy_stream` / :func:`download_to` size the buffer from
  ``settings.storage_stream_chunk_bytes`` and report the count.
* :func:`checksum_and_size` hashes while copying, so verification costs one
  pass rather than two.
* :func:`TempBudget` bounds how much scratch space a job may occupy, and
  :func:`sweep_temp` reclaims it. A crashed render's scratch directory is the
  reason this exists: without a budget, every interrupted 60-minute render
  leaves its intermediates on disk forever.

Streaming uploads are deliberately *write-then-finalize*, never in-place.
:func:`write_atomic` writes to ``<dest>.part`` in the SAME directory and
``os.replace``\\ s it into place. ``os.replace`` is atomic within a filesystem,
so a reader sees either the previous bytes or the complete new bytes and never
a half-written file -- which is the local half of "atomic finalization"; the
storage-object row is the other half.

Nothing here reads a whole file. :func:`read_all` exists only for bounded
inputs and refuses anything above ``max_bytes``.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import time
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path

from loguru import logger

__all__ = [
    "ChunkReport",
    "TempBudget",
    "TempBudgetExceeded",
    "checksum_and_size",
    "copy_stream",
    "counting_iter",
    "download_to",
    "iter_chunks",
    "read_all",
    "sha256_file",
    "sweep_temp",
    "temp_dir",
    "write_atomic",
    "write_stream",
]


class TempBudgetExceeded(RuntimeError):
    """A job tried to allocate more scratch space than its budget allows.

    Raised BEFORE the write, so the refusal costs nothing and leaves no partial
    file behind to clean up.
    """


@dataclass
class ChunkReport:
    """What a streamed move actually did. Evidence, not a promise."""

    path: str
    size_bytes: int = 0
    checksum: str = ""
    chunks: int = 0
    chunk_bytes: int = 0
    seconds: float = 0.0

    @property
    def mb_per_second(self) -> float:
        return (self.size_bytes / (1024 * 1024)) / self.seconds if self.seconds > 0 else 0.0


def _default_chunk() -> int:
    from app.core.config import settings

    return max(64 * 1024, int(getattr(settings, "storage_stream_chunk_bytes", 4 << 20)))


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


def iter_chunks(path: str | Path, *, chunk_bytes: int | None = None,
                start: int = 0, length: int | None = None) -> Iterator[bytes]:
    """Yield ``path`` in bounded chunks. Never loads the file.

    ``start``/``length`` make it a range reader, which is how a chunked render
    pulls one segment out of a long source without reading the other segments.
    """
    size = max(1, int(chunk_bytes or _default_chunk()))
    with Path(path).open("rb") as handle:
        if start:
            handle.seek(int(start))
        remaining = int(length) if length is not None else None
        while True:
            want = size if remaining is None else min(size, remaining)
            if want <= 0:
                return
            block = handle.read(want)
            if not block:
                return
            if remaining is not None:
                remaining -= len(block)
            yield block


def counting_iter(chunks: Iterable[bytes],
                  on_bytes: Callable[[int], None] | None = None) -> Iterator[bytes]:
    """Pass chunks through, reporting the running byte total.

    For callers streaming a network body that need progress or a limit check
    without buffering: ``on_bytes`` fires once per chunk with the cumulative
    total, so an over-large response is refused while it is still arriving
    rather than after it has been written.
    """
    total = 0
    for block in chunks:
        if not block:
            continue
        total += len(block)
        if on_bytes is not None:
            on_bytes(total)
        yield block


def read_all(path: str | Path, *, max_bytes: int = 8 << 20) -> bytes:
    """Read a SMALL file, and refuse a large one.

    The refusal is the feature. ``read_bytes()`` has no upper bound, so a
    caller that means to read a thumbnail will one day read a 3 GB master and
    take the process with it. This raises instead, at a size where the process
    is still healthy.
    """
    size = Path(path).stat().st_size
    if size > int(max_bytes):
        raise TempBudgetExceeded(
            f"read_all refused {size} bytes (limit {int(max_bytes)}); "
            f"use iter_chunks() for anything this size"
        )
    return Path(path).read_bytes()


def sha256_file(path: str | Path, *, chunk_bytes: int | None = None) -> str:
    """Streaming sha256. One pass, bounded memory."""
    digest = hashlib.sha256()
    for block in iter_chunks(path, chunk_bytes=chunk_bytes):
        digest.update(block)
    return digest.hexdigest()


def checksum_and_size(path: str | Path, *, chunk_bytes: int | None = None) -> tuple[str, int]:
    """``(sha256, size)`` in one pass."""
    digest = hashlib.sha256()
    total = 0
    for block in iter_chunks(path, chunk_bytes=chunk_bytes):
        digest.update(block)
        total += len(block)
    return digest.hexdigest(), total


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------


def write_stream(dest: str | Path, chunks: Iterable[bytes], *,
                 chunk_bytes: int | None = None,
                 budget: TempBudget | None = None) -> ChunkReport:
    """Stream chunks to a file, hashing as we go. Not atomic -- see
    :func:`write_atomic` for anything that becomes canonical.

    When a ``budget`` is supplied it is METERED: every block is reserved
    against it before it is written, so exceeding the limit costs the block
    that would have crossed the line rather than the whole file. Checking after
    the write would defeat the point: the disk is already the size the budget
    exists to prevent. A budget that is only *consulted* and never *debited*
    never trips at all.
    """
    target = Path(dest)
    target.parent.mkdir(parents=True, exist_ok=True)
    size = max(1, int(chunk_bytes or _default_chunk()))
    digest = hashlib.sha256()
    total = 0
    count = 0
    started = time.monotonic()
    reserved = 0
    try:
        with target.open("wb") as handle:
            for block in chunks:
                if not block:
                    continue
                if budget is not None:
                    budget.reserve(len(block))
                    reserved += len(block)
                handle.write(block)
                digest.update(block)
                total += len(block)
                count += 1
    except BaseException:
        # The write is abandoned; give the scratch space back so a job that
        # frees its budget and retries is not charged for a dead attempt.
        if budget is not None and reserved:
            budget.release(reserved)
        raise
    return ChunkReport(path=str(target), size_bytes=total,
                       checksum=digest.hexdigest(), chunks=count,
                       chunk_bytes=size, seconds=time.monotonic() - started)


def write_atomic(dest: str | Path, chunks: Iterable[bytes], *,
                 chunk_bytes: int | None = None,
                 budget: TempBudget | None = None,
                 fsync: bool = True) -> ChunkReport:
    """Stream to ``<dest>.part`` then ``os.replace`` into place.

    ``os.replace`` is atomic within a filesystem, so a concurrent reader of
    ``dest`` observes either the whole previous object or the whole new one --
    never a truncated file under a name that claims to be complete. The
    ``.part`` suffix is also the invariant made visible: a partially written
    object is *named* as partial, so anything scanning the tree can tell.

    A failure anywhere removes the ``.part``. That is the interrupted-render
    cleanup: without it every crashed render leaves its half-written output
    behind, and the disk fills with files whose names announce they were never
    finished and which nothing will ever collect.
    """
    target = Path(dest)
    target.parent.mkdir(parents=True, exist_ok=True)
    part = target.with_name(target.name + ".part")
    try:
        report = write_stream(part, chunks, chunk_bytes=chunk_bytes,
                              budget=budget)
        if fsync:
            _fsync_file(part)
        os.replace(part, target)
    except BaseException:
        part.unlink(missing_ok=True)
        raise
    report.path = str(target)
    return report


def _fsync_file(path: Path) -> None:
    """Push the bytes to the platter before the rename.

    Without this the rename can reach the directory entry while the data is
    still in the page cache, and a power cut leaves a correctly-named,
    zero-length "final" object -- the worst possible combination.
    """
    try:
        fd = os.open(str(path), os.O_RDONLY)
    except OSError:  # pragma: no cover - Windows/permissions fallback
        return
    try:
        os.fsync(fd)
    except OSError:  # pragma: no cover - filesystem without fsync
        pass
    finally:
        os.close(fd)


def copy_stream(src: str | Path, dest: str | Path, *,
                chunk_bytes: int | None = None,
                expected_checksum: str | None = None) -> ChunkReport:
    """Copy a file in bounded memory, atomically, verifying as it goes.

    Raises when ``expected_checksum`` is supplied and does not match -- a
    truncated copy that still produced a file is exactly the artifact the
    final-verification step exists to reject, and it is cheapest to reject it
    here, while the bytes are already in hand to hash.
    """
    report = write_atomic(dest, iter_chunks(src, chunk_bytes=chunk_bytes),
                          chunk_bytes=chunk_bytes)
    if expected_checksum and report.checksum != expected_checksum:
        Path(dest).unlink(missing_ok=True)
        raise ValueError(
            f"checksum mismatch for {src}: expected {expected_checksum}, "
            f"got {report.checksum}"
        )
    return report


def download_to(source: Callable[[], Iterable[bytes]], dest: str | Path, *,
                chunk_bytes: int | None = None,
                max_bytes: int | None = None) -> ChunkReport:
    """Stream a remote body to disk through the same atomic path.

    ``source`` is a callable returning an iterable of byte blocks, so the
    caller keeps whatever HTTP client it already uses (and the tests need no
    network). ``max_bytes`` refuses an over-large body *while streaming*, and
    the partial ``.part`` is removed, so an oversized download costs a bounded
    amount of disk rather than the whole file.
    """
    def _bounded() -> Iterator[bytes]:
        seen = 0
        for block in source():
            if not block:
                continue
            seen += len(block)
            if max_bytes is not None and seen > int(max_bytes):
                raise TempBudgetExceeded(
                    f"download exceeded {int(max_bytes)} bytes")
            yield block

    try:
        return write_atomic(dest, _bounded(), chunk_bytes=chunk_bytes)
    except BaseException:
        Path(dest).with_name(Path(dest).name + ".part").unlink(missing_ok=True)
        raise


# ---------------------------------------------------------------------------
# Temporary space
# ---------------------------------------------------------------------------


@dataclass
class TempBudget:
    """A hard ceiling on one job's scratch bytes.

    Reserved BEFORE the write so the refusal happens while the job is still
    recoverable. ``used`` is checked against ``limit`` on every call; nothing
    here consults the free space of the disk, because the number that actually
    protects a shared machine is the number the operator set.
    """

    limit_bytes: int
    used_bytes: int = 0

    def reserve(self, nbytes: int) -> None:
        want = int(nbytes)
        if self.used_bytes + want > int(self.limit_bytes):
            raise TempBudgetExceeded(
                f"temp budget exhausted: {self.used_bytes} + {want} > "
                f"{self.limit_bytes} bytes"
            )
        self.used_bytes += want

    def release(self, nbytes: int) -> None:
        self.used_bytes = max(0, self.used_bytes - int(nbytes))

    @property
    def free_bytes(self) -> int:
        return max(0, int(self.limit_bytes) - int(self.used_bytes))

    def permits(self, nbytes: int) -> bool:
        return self.used_bytes + int(nbytes) <= int(self.limit_bytes)


def temp_dir(*, prefix: str = "w16", root: str | Path | None = None) -> Path:
    """Create a scratch directory under the configured staging root.

    The staging root is deliberately NOT under ``data/videos``: a scratch file
    is not media, and ``storage.managed_path`` resolving a scratch path would
    be a way for temporary state to masquerade as canonical.
    """
    from app.core.config import settings

    base = Path(root or getattr(settings, "storage_staging_dir", "data/storage_staging"))
    path = base / f"{prefix}-{os.getpid()}-{int(time.time() * 1000) % 1_000_000}"
    path.mkdir(parents=True, exist_ok=True)
    return path


def directory_size(path: str | Path) -> int:
    """Bytes under a directory (one level of recursion is enough for scratch)."""
    total = 0
    root = Path(path)
    if not root.exists():
        return 0
    for entry in root.rglob("*"):
        if entry.is_file():
            with_suppressed = getattr(entry, "stat", None)
            if with_suppressed is None:  # pragma: no cover - defensive
                continue
            try:
                total += entry.stat().st_size
            except OSError:  # pragma: no cover - raced deletion
                continue
    return total


def sweep_temp(*, older_than_seconds: float = 3600.0,
               root: str | Path | None = None) -> int:
    """Delete scratch directories older than the cutoff. Returns files removed.

    This is the interrupted-render cleanup: a render that died leaves a scratch
    directory holding its intermediates, and nothing else in the system knows
    they are garbage. Time, not process liveness, is the criterion -- a live
    render's directory is younger than the cutoff by construction.
    """
    from app.core.config import settings

    base = Path(root or getattr(settings, "storage_staging_dir", "data/storage_staging"))
    if not base.exists():
        return 0
    cutoff = time.time() - float(older_than_seconds)
    removed = 0
    for entry in list(base.iterdir()):
        try:
            if not entry.is_dir() or entry.stat().st_mtime > cutoff:
                continue
            removed += sum(1 for _ in entry.rglob("*") if _.is_file())
            shutil.rmtree(entry, ignore_errors=True)
        except OSError as exc:  # pragma: no cover - raced deletion
            logger.warning(f"temp sweep could not remove {entry}: {exc}")
    if removed:
        logger.info("temp sweep removed %d stale scratch file(s)", removed)
    return removed