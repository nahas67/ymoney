"""Segmentation / object-tracking engine + mask compositing (Lane F).

Contracts §9 and §12. This module is the **provider-independent** half of
segmentation: it owns the mask *representation*, the *storage* rule, and the
compositing operations. It never imports ``torch`` or ``sam2`` -- the provider
adapter (:mod:`app.engine.intel.impl.sam2_segmentation`) injects a
:class:`MaskBackend`, so the tests drive this exact code with a deterministic
double while the production path stays honestly unavailable.

Three rules the whole file exists to keep:

1. **Masks are files, never DB blobs.** A mask is written under
   ``STORAGE_ROOT/<workspace_id>/intel/masks/...`` as PNG or RLE JSON and the
   ``mask_assets`` row stores only a reference plus geometry plus a checksum.
   A 1080p PNG mask is ~2 MB; a JSON blob column would make every run listing
   read megabytes of pixels, so the row is deliberately tiny.
2. **Derived only.** Composition writes a NEW ``MediaAsset`` whose
   ``parent_asset_id`` is the source; the source bytes are never rewritten.
   :func:`app.engine.intel.ffmpeg_util.run_filter` additionally refuses
   ``dst == src``, so the derived-only invariant is enforced twice.
3. **Honest unavailability.** When no backend can be resolved the segment and
   every composition helper return ``{"ok": False, "unavailable": True,
   "reason": ...}``. There is no fallback that produces a bad mask -- a blurred
   rectangle over the wrong pixels is worse than a dark capability.

Nothing here reads or guesses a mask: pixels come from the injected backend,
and every number stored is one this module measured.
"""

from __future__ import annotations

import hashlib
import json
import logging
import zlib
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from app.engine.intel import ffmpeg_util
from app.engine.intel.base import CancelFn, ProgressFn, ProviderUnavailable, check_control
from app.services import storage as storage_service

logger = logging.getLogger("ymoney.intel")

#: mask semantics (contracts §9/§12). BACKGROUND is the complement of the
#: foreground, derived locally -- never a second model prompt.
MASK_KINDS: tuple[str, ...] = ("PERSON", "OBJECT", "BACKGROUND", "SUBJECT")

#: on-disk encodings. PNG is the default (compact, ffmpeg-native); RLE_JSON is
#: the auditable text form used when a caller wants to diff masks.
MASK_FORMATS: tuple[str, ...] = ("PNG", "RLE_JSON")

#: sub-directory under the workspace storage root that owns mask files.
MASK_DIR = "intel/masks"

#: default sampling rate for mask extraction.
DEFAULT_FPS = 2.0

#: a mask smaller than this fraction of the frame is reported as suspect (it is
#: still stored -- QC decides, this lane only labels it).
TINY_MASK_AREA_RATIO = 0.005

#: max frames one call may mask. Above this the caller must chunk (contracts §3).
MAX_FRAMES_LIMIT = 600


class SegmentationError(ValueError):
    """A deliberate, reportable segmentation failure (maps to 422 at the API)."""


# ---------------------------------------------------------------------------
# value objects
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MaskFrame:
    """One sampled video frame handed to a backend."""

    index: int
    t_s: float
    path: str
    width: int = 0
    height: int = 0


@dataclass(frozen=True)
class RegionMask:
    """One binary region in one frame, stored as row-major RLE runs.

    ``runs`` are ``(start, length)`` pairs over a ``width * height`` pixel grid
    -- the smallest honest in-memory representation with no image library
    (contracts §0: stdlib + ffmpeg only). ``area_ratio`` is derived, never
    supplied, so a provider cannot mislabel its own coverage.
    """

    label: str
    frame_index: int
    t_s: float
    width: int
    height: int
    runs: tuple[tuple[int, int], ...]
    bbox: tuple[float, float, float, float]
    score: float = 0.0

    @property
    def area(self) -> int:
        """Set pixels (the sum of run lengths)."""
        return int(sum(length for _, length in self.runs))

    @property
    def area_ratio(self) -> float:
        """Coverage of the frame in ``[0, 1]`` -- measured from the runs."""
        total = int(self.width) * int(self.height)
        if total <= 0:
            return 0.0
        return round(self.area / total, 6)


@dataclass(frozen=True)
class MaskRecord:
    """A mask FILE plus the geometry/provenance stored in the DB row.

    ``storage_key`` is workspace-relative (what ``MediaAsset.storage_key`` stores);
    :func:`resolve_mask_path` turns it back into an absolute path.
    """

    label: str
    frame_index: int
    t_s: float
    width: int
    height: int
    area_ratio: float
    bbox: tuple[float, float, float, float]
    storage_key: str
    format: str
    checksum: str
    file_size: int
    provider_key: str
    model_version: str

    def to_dict(self) -> dict:
        return {
            "label": self.label,
            "frame_index": int(self.frame_index),
            "t_s": float(self.t_s),
            "width": int(self.width),
            "height": int(self.height),
            "area_ratio": float(self.area_ratio),
            "bbox": {
                "x": float(self.bbox[0]),
                "y": float(self.bbox[1]),
                "w": float(self.bbox[2]),
                "h": float(self.bbox[3]),
            },
            "storage_key": self.storage_key,
            "format": self.format,
            "checksum": self.checksum,
            "file_size": int(self.file_size),
            "provider_key": self.provider_key,
            "model_version": self.model_version,
        }


# ---------------------------------------------------------------------------
# backend protocol
# ---------------------------------------------------------------------------


class MaskBackend:
    """Injectable mask producer (duck-typed; no ABC on purpose).

    A test double is a handful of lines -- which is the point: the ENGINE is
    identical no matter who produces the pixels, and the production SAM2 adapter
    is the only thing that knows about torch.
    """

    #: registry-visible provider key (provenance on every mask row)
    key: str = "mask_backend"
    #: checkpoint/model identity; empty means "unknown", never a guess
    model_version: str = ""

    def health(self) -> tuple[bool, str]:
        """``(available, reason)``. ``reason`` is required when unavailable."""
        return False, "no mask backend configured"

    def masks_for_frame(
        self,
        frame: MaskFrame,
        *,
        labels: tuple[str, ...],
        progress: ProgressFn | None = None,
        should_cancel: CancelFn | None = None,
        deadline: float | None = None,
    ) -> list[RegionMask]:
        """Masks for one frame. Raise ProviderUnavailable when unavailable."""
        raise ProviderUnavailable("no mask backend configured")


def resolve_backend(backend: MaskBackend | None = None) -> tuple[MaskBackend | None, str]:
    """Pick the mask backend: injected one, else the registry's ``segmentation``.

    Never raises. Returns ``(backend, reason)`` where ``reason`` is empty only
    when a backend is present.
    """
    if backend is not None:
        try:
            available, reason = backend.health()
        except Exception as exc:  # noqa: BLE001 - a probe must never propagate
            return None, f"mask backend probe failed: {type(exc).__name__}"
        if not available:
            return None, reason or "mask backend unavailable"
        return backend, ""
    from app.engine.intel import registry as intel_registry

    provider, reasons = intel_registry.resolve("segmentation")
    if provider is None:
        detail = "; ".join(f"{k}: {v}" for k, v in reasons.items())
        return None, detail or "no segmentation provider configured"
    resolved = getattr(provider, "backend", None)
    if resolved is None:
        # A provider that does not expose a backend cannot drive this engine;
        # saying so is honest, silently returning no masks is not.
        return None, f"provider {getattr(provider, 'key', '?')} exposes no mask backend"
    available, reason = resolved.health()
    if not available:
        return None, reason or "segmentation provider backend unavailable"
    return resolved, ""


# ---------------------------------------------------------------------------
# mask construction helpers (pure, stdlib)
# ---------------------------------------------------------------------------


def mask_from_runs(
    runs: Iterable[Sequence[int]],
    *,
    label: str,
    frame: MaskFrame,
    width: int = 0,
    height: int = 0,
    score: float = 0.0,
) -> RegionMask:
    """Build a validated :class:`RegionMask` from row-major RLE runs.

    ``width``/``height`` default to the frame's own geometry (a backend masks
    the frame it was handed). Out-of-bounds or negative runs raise
    :class:`SegmentationError` rather than being clamped: a provider that reports
    geometry outside the frame is broken, and silently clipping it would produce
    a subtly wrong mask.
    """
    cols, rows = int(width or frame.width), int(height or frame.height)
    if cols <= 0 or rows <= 0:
        raise SegmentationError(f"mask geometry must be positive, got {width}x{height}")
    total = cols * rows
    clean: list[tuple[int, int]] = []
    for run in runs:
        start, length = int(run[0]), int(run[1])
        if start < 0 or length <= 0:
            continue
        if start + length > total:
            raise SegmentationError(
                f"mask run [{start},{length}) exceeds {cols}x{rows}"
            )
        clean.append((start, length))
    clean.sort()
    return RegionMask(
        label=str(label),
        frame_index=int(frame.index),
        t_s=float(frame.t_s),
        width=cols,
        height=rows,
        runs=tuple(clean),
        bbox=bbox_from_runs(clean, cols),
        score=float(score),
    )


def mask_from_binary(
    binary: Any,
    *,
    label: str,
    frame: MaskFrame,
    score: float = 0.0,
) -> RegionMask:
    """Build a :class:`RegionMask` from a 2-D boolean-ish grid.

    Indexing only (``binary[y][x]``), so it works with a numpy array from a
    provider as well as with a plain list-of-lists from a test double -- the
    engine itself never imports numpy (contracts §0).
    """
    try:
        rows = int(len(binary))
        cols = int(len(binary[0])) if rows else 0
    except (TypeError, IndexError) as exc:
        raise SegmentationError("binary mask must be a 2-D indexable grid") from exc
    runs: list[tuple[int, int]] = []
    for y in range(rows):
        row = binary[y]
        start: int | None = None
        for x in range(cols):
            filled = bool(row[x])
            if filled and start is None:
                start = x
            elif not filled and start is not None:
                runs.append((y * cols + start, x - start))
                start = None
        if start is not None:
            runs.append((y * cols + start, cols - start))
    return mask_from_runs(
        runs, label=label, frame=frame, width=cols, height=rows, score=score
    )


def rect_runs(x: int, y: int, w: int, h: int, width: int, height: int) -> list[tuple[int, int]]:
    """Row-major RLE for an axis-aligned rectangle (the fixtures' shape)."""
    cols, rows = int(width), int(height)
    if w <= 0 or h <= 0:
        return []
    left = max(0, min(cols - 1, int(x)))
    top = max(0, min(rows - 1, int(y)))
    right = max(left, min(cols, left + int(w)))
    bottom = max(top, min(rows, top + int(h)))
    runs: list[tuple[int, int]] = []
    for row in range(top, bottom):
        runs.append((row * cols + left, right - left))
    return runs


def bbox_from_runs(
    runs: Sequence[tuple[int, int]], width: int
) -> tuple[float, float, float, float]:
    """``(x, y, w, h)`` bounding box of row-major runs; zeros when empty."""
    cols = max(1, int(width))
    xs0: list[int] = []
    xs1: list[int] = []
    ys0: list[int] = []
    ys1: list[int] = []
    for start, length in runs:
        if length <= 0:
            continue
        row = start // cols
        col = start % cols
        xs0.append(col)
        xs1.append(col + length - 1)
        ys0.append(row)
        ys1.append(row)
    if not xs0:
        return (0.0, 0.0, 0.0, 0.0)
    return (
        float(min(xs0)),
        float(min(ys0)),
        float(max(xs1) - min(xs0) + 1),
        float(max(ys1) - min(ys0) + 1),
    )


# ---------------------------------------------------------------------------
# mask serialisation (PNG / RLE JSON) -- deterministic bytes, so a checksum
# recorded today still matches a file fetched tomorrow.
# ---------------------------------------------------------------------------


def _png_chunk(tag: bytes, data: bytes) -> bytes:
    return (
        len(data).to_bytes(4, "big")
        + tag
        + data
        + (zlib.crc32(tag + data) & 0xFFFFFFFF).to_bytes(4, "big")
    )


def encode_png(mask: RegionMask) -> bytes:
    """8-bit grayscale PNG of the mask (0 background, 255 subject).

    Hand-rolled with ``zlib`` + ``struct`` because Work 12 has no image library
    (contracts §0). Deterministic for a given mask, so the sha256 in
    ``mask_assets.checksum`` is stable.
    """
    width, height = int(mask.width), int(mask.height)
    pixels = bytearray(width * height)
    for start, length in mask.runs:
        pixels[start:start + length] = b"\xff" * length
    raw = bytearray()
    stride = width
    for row in range(height):
        raw.append(0)  # filter type 0 (None) -- keeps the encoder trivial
        raw += pixels[row * stride:(row + 1) * stride]
    header = (
        width.to_bytes(4, "big") + height.to_bytes(4, "big")
        + bytes([8, 0, 0, 0, 0])  # bit depth 8, colour type 0 (grey)
    )
    return (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", header)
        + _png_chunk(b"IDAT", zlib.compress(bytes(raw), 6))
        + _png_chunk(b"IEND", b"")
    )


def encode_rle_json(mask: RegionMask) -> bytes:
    """Auditable text form: geometry + runs + bbox, no binary."""
    payload = {
        "bbox": [float(v) for v in mask.bbox],
        "height": int(mask.height),
        "label": str(mask.label),
        "runs": [[int(start), int(length)] for start, length in mask.runs],
        "score": round(float(mask.score), 4),
        "t_s": round(float(mask.t_s), 4),
        "width": int(mask.width),
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def encode_mask(mask: RegionMask, fmt: str) -> bytes:
    """Encode a mask; an unknown format is a :class:`SegmentationError`."""
    if fmt == "PNG":
        return encode_png(mask)
    if fmt == "RLE_JSON":
        return encode_rle_json(mask)
    raise SegmentationError(f"unsupported mask format {fmt!r}")


# ---------------------------------------------------------------------------
# storage
# ---------------------------------------------------------------------------


def _workspace_dir(workspace_id: str) -> Path:
    """``STORAGE_ROOT/<workspace_id>`` resolved lazily (STORAGE_ROOT is relative)."""
    root = Path(storage_service.STORAGE_ROOT) / str(workspace_id)
    root.mkdir(parents=True, exist_ok=True)
    return root


def resolve_workspace_file(workspace_id: str, storage_key: str) -> Path | None:
    """Absolute path of a workspace storage FILE, or None when it is not ours.

    ``validate_storage_key`` is the gate: a key that is absolute, escapes the
    workspace directory, or is empty resolves to None, so a tampered row can
    never make a caller read (or overwrite) another workspace's file.

    ``MediaAsset.storage_key`` is workspace-RELATIVE (the convention every asset
    writer in the repo follows -- ``engine/longform/proxy.py`` and friends), so
    the key is joined onto ``STORAGE_ROOT/<workspace_id>`` here rather than onto
    the process cwd the way ``services.storage.managed_path`` does.
    """
    key = storage_service.validate_storage_key(str(workspace_id or ""), str(storage_key or ""))
    if key is None:
        return None
    return (_workspace_dir(workspace_id) / key).resolve()


#: Readability alias -- the same gate, named for what callers usually hold.
resolve_mask_path = resolve_workspace_file


def write_mask(
    mask: RegionMask,
    *,
    workspace_id: str,
    run_id: str = "",
    fmt: str = "PNG",
) -> MaskRecord:
    """Encode + write one mask under workspace storage; return its record.

    The file name encodes kind/frame so a listing is readable and two runs never
    collide. ``checksum`` is the sha256 of the exact bytes written.
    """
    payload = encode_mask(mask, fmt)
    stem = f"{str(mask.label).lower()}_f{int(mask.frame_index):05d}"
    key = f"{MASK_DIR}/{run_id or 'adhoc'}/{stem}.{ 'png' if fmt == 'PNG' else 'json'}"
    normalised = storage_service.validate_storage_key(workspace_id, key)
    if normalised is None:
        raise SegmentationError(f"refusing to write a mask outside the workspace root: {key}")
    path = _workspace_dir(workspace_id) / normalised
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return MaskRecord(
        label=str(mask.label),
        frame_index=int(mask.frame_index),
        t_s=round(float(mask.t_s), 4),
        width=int(mask.width),
        height=int(mask.height),
        area_ratio=float(mask.area_ratio),
        bbox=mask.bbox,
        storage_key=normalised,
        format=fmt,
        checksum=hashlib.sha256(payload).hexdigest(),
        file_size=len(payload),
        provider_key=str(getattr(mask, "provider_key", "") or ""),
        model_version=str(getattr(mask, "model_version", "") or ""),
    )


def sha256_file(path: str | Path) -> str:
    """sha256 of a file's bytes (used by the mask-storage verification test)."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(65536), b""):
            digest.update(block)
    return digest.hexdigest()


# ---------------------------------------------------------------------------
# frame extraction
# ---------------------------------------------------------------------------


def probe_geometry(path: str) -> tuple[int, int]:
    """``(width, height)`` of the first video stream, ``(0, 0)`` when unknown."""
    data = ffmpeg_util.probe(path)
    for stream in data.get("streams", []) or []:
        if isinstance(stream, dict) and stream.get("codec_type") == "video":
            try:
                return int(stream.get("width") or 0), int(stream.get("height") or 0)
            except (TypeError, ValueError):
                return 0, 0
    return 0, 0


def extract_mask_frames(
    path: str,
    *,
    fps: float = DEFAULT_FPS,
    max_frames: int = 0,
    workdir: str | Path,
) -> list[MaskFrame]:
    """Sample frames through :func:`ffmpeg_util.extract_frames` into ``workdir``.

    ``fps`` defaults to 2 (contracts §8 uses the same rate for face samples, so
    a mask frame and a face sample can be compared at the same timestamp).
    Returns ``[]`` when nothing decodes -- the caller then reports an honest
    failure instead of an empty mask set.
    """
    width, height = probe_geometry(path)
    limit = int(max_frames or 0)
    if limit <= 0 or limit > MAX_FRAMES_LIMIT:
        limit = MAX_FRAMES_LIMIT
    directory = Path(workdir) / "frames"
    rate = float(fps or DEFAULT_FPS)
    written = ffmpeg_util.extract_frames(path, rate, directory)
    if not written:
        return []
    frames: list[MaskFrame] = []
    for index, item in enumerate(written[:limit]):
        frames.append(
            MaskFrame(
                index=index,
                t_s=round(index / rate, 4) if rate > 0 else 0.0,
                path=str(item),
                width=width,
                height=height,
            )
        )
    return frames


# ---------------------------------------------------------------------------
# the engine
# ---------------------------------------------------------------------------


def _progress(progress: ProgressFn | None, value: float) -> None:
    if progress is not None:
        progress(max(0.0, min(1.0, float(value))))


def unavailable(reason: str, **extra: Any) -> dict:
    """Canonical honest-unavailable payload (contracts §0)."""
    return {"ok": False, "unavailable": True, "reason": str(reason or "unavailable"), **extra}


def segment(
    *,
    workspace_id: str,
    storage_path: str,
    backend: MaskBackend | None = None,
    params: dict | None = None,
    run_id: str = "",
    frames: Sequence[MaskFrame] | None = None,
    workdir: str | Path | None = None,
    progress: ProgressFn | None = None,
    should_cancel: CancelFn | None = None,
    deadline: float | None = None,
) -> dict:
    """Mask a video and persist the masks as FILES.

    The provider-independent entry point: identical code path for the real SAM2
    adapter and for a test double. Returns

    ``{"ok": True, "masks": [MaskRecord, ...], "metrics": {...}}``

    or ``{"ok": False, "unavailable": True, "reason": ...}`` when no mask
    backend can serve the request (contracts §9). ``reason`` is always populated
    in that case -- a dark capability, never a fabricated mask.

    Cooperative: calls ``progress(0..1)`` per frame and polls
    ``should_cancel``/``deadline`` through :func:`check_control`.
    """
    resolved, reason = resolve_backend(backend)
    if resolved is None:
        return unavailable(reason, masks=[], metrics={})

    options = dict(params or {})
    labels = tuple(str(v).upper() for v in (options.get("labels") or ("PERSON",)))
    unknown = sorted(v for v in labels if v not in MASK_KINDS)
    if unknown:
        raise SegmentationError(f"unknown mask kind(s): {', '.join(unknown)}")
    fmt = str(options.get("format") or "PNG").upper()
    if fmt not in MASK_FORMATS:
        raise SegmentationError(f"unsupported mask format {fmt!r}")
    fps = float(options.get("fps") or DEFAULT_FPS)
    max_frames = int(options.get("max_frames") or 0)

    if not str(storage_path or ""):
        return unavailable("no source media path for this asset", masks=[], metrics={})

    work_root = Path(workdir) if workdir else Path(
        Path(storage_service.STORAGE_ROOT) / str(workspace_id) / MASK_DIR / (run_id or "adhoc")
    )
    sampled = list(frames) if frames else extract_mask_frames(
        storage_path, fps=fps, max_frames=max_frames, workdir=work_root
    )
    if not sampled:
        return {
            "ok": False,
            "unavailable": False,
            "reason": f"no frames decoded from {Path(storage_path).name}",
            "masks": [],
            "metrics": {},
        }

    warnings: list[str] = []
    records: list[MaskRecord] = []
    areas: list[float] = []
    provider_key = str(getattr(resolved, "key", "") or "")
    model_version = str(options.get("model_version") or getattr(resolved, "model_version", "") or "")
    total = max(1, len(sampled))

    source_width, source_height = probe_geometry(storage_path)
    for position, frame in enumerate(sampled):
        check_control(should_cancel, deadline, counter=position)
        width = int(frame.width or source_width)
        height = int(frame.height or source_height)
        if width <= 0 or height <= 0:
            warnings.append(f"frame {frame.index}: unknown geometry, skipped")
            continue
        try:
            masks = resolved.masks_for_frame(
                frame, labels=labels, progress=None, should_cancel=should_cancel, deadline=deadline
            )
        except ProviderUnavailable as exc:
            return unavailable(str(exc), masks=[], metrics={"provider_key": provider_key})
        except Exception as exc:  # noqa: BLE001 - one bad frame must not kill the run
            warnings.append(f"frame {frame.index}: backend failed ({type(exc).__name__})")
            continue
        for mask in masks or []:
            area = float(getattr(mask, "area_ratio", 0.0))
            if area <= 0.0:
                warnings.append(f"frame {frame.index}: empty {mask.label} mask skipped")
                continue
            if area < TINY_MASK_AREA_RATIO:
                warnings.append(
                    f"frame {frame.index}: {mask.label} mask covers {area:.4f} "
                    "(below the tiny-mask floor; QC decides)"
                )
            record = write_mask(mask, workspace_id=workspace_id, run_id=run_id, fmt=fmt)
            records.append(
                replace(record, provider_key=provider_key, model_version=model_version)
            )
            areas.append(area)
        _progress(progress, (position + 1) / total)

    _progress(progress, 1.0)
    if not records:
        return {
            "ok": False,
            "unavailable": False,
            "reason": "the backend returned no non-empty mask for any sampled frame",
            "masks": [],
            "metrics": {"frames": len(sampled), "provider_key": provider_key},
            "warnings": warnings,
        }
    return {
        "ok": True,
        "unavailable": False,
        "masks": records,
        "warnings": warnings,
        "metrics": {
            "frames": len(sampled),
            "masks": len(records),
            "labels": sorted({r.label for r in records}),
            "mean_area_ratio": round(sum(areas) / len(areas), 6) if areas else 0.0,
            "provider_key": provider_key,
            "model_version": model_version,
            "format": fmt,
        },
    }


def persist_masks(
    db: Any,
    *,
    workspace_id: str,
    run_id: str,
    input_asset_id: str,
    records: Sequence[MaskRecord],
) -> list[dict]:
    """Write one ``mask_assets`` row per mask FILE.

    The row carries the reference (``mask_asset_id`` -> the written file's
    ``MediaAsset``), the geometry, the checksum and the provenance -- and nothing
    else. No raw mask bytes ever reach a row: that is the whole point of the
    table, and ``tests/test_segmentation.py`` asserts it.
    """
    from app.models import MaskAsset, MediaAsset

    persisted: list[dict] = []
    for record in records or []:
        file_asset = MediaAsset(
            workspace_id=workspace_id,
            type="other",
            origin="generated",
            provider=str(record.provider_key or ""),
            storage_key=str(record.storage_key),
            mime_type="image/png" if record.format == "PNG" else "application/json",
            width=int(record.width),
            height=int(record.height),
            file_size=int(record.file_size),
            checksum=str(record.checksum),
            meta_json={
                "kind": "segmentation_mask",
                "label": record.label,
                "frame_index": int(record.frame_index),
                "t_s": float(record.t_s),
                "format": record.format,
            },
            derivation_json={
                "operation": "segmentation",
                "provider_key": str(record.provider_key or ""),
                "model_version": str(record.model_version or ""),
                "input_asset_id": str(input_asset_id),
                "run_id": str(run_id),
                "area_ratio": float(record.area_ratio),
            },
        )
        db.add(file_asset)
        db.flush()
        row = MaskAsset(
            run_id=run_id,
            workspace_id=workspace_id,
            input_asset_id=input_asset_id,
            kind=record.label,
            mask_asset_id=file_asset.id,
            format=record.format,
            width=int(record.width),
            height=int(record.height),
            area_ratio=float(record.area_ratio),
            provider_key=str(record.provider_key or ""),
            model_version=str(record.model_version or ""),
            checksum=str(record.checksum),
        )
        db.add(row)
        db.flush()
        payload = record.to_dict()
        payload.update({"id": row.id, "mask_asset_id": file_asset.id, "run_id": run_id})
        persisted.append(payload)
    return persisted


def mask_row_dto(row: Any) -> dict:
    """API shape for one mask row (reference + geometry, never pixels)."""
    return {
        "id": row.id,
        "run_id": row.run_id,
        "workspace_id": row.workspace_id,
        "input_asset_id": row.input_asset_id,
        "kind": str(row.kind or ""),
        "mask_asset_id": row.mask_asset_id,
        "format": str(row.format or ""),
        "width": int(row.width) if row.width is not None else None,
        "height": int(row.height) if row.height is not None else None,
        "area_ratio": float(row.area_ratio) if row.area_ratio is not None else None,
        "provider_key": str(row.provider_key or ""),
        "model_version": str(row.model_version or ""),
        "checksum": str(row.checksum or ""),
    }


# ---------------------------------------------------------------------------
# compositing (contracts §12): every one of these writes a NEW derived asset
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CompositionResult:
    """Outcome of a compositing operation."""

    ok: bool
    operation: str
    reason: str = ""
    unavailable: bool = False
    asset: dict | None = None
    mask: dict | None = None
    params: dict = field(default_factory=dict)
    metrics: dict = field(default_factory=dict)
    warnings: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return {
            "ok": bool(self.ok),
            "operation": self.operation,
            "unavailable": bool(self.unavailable),
            "reason": self.reason,
            "asset": dict(self.asset) if self.asset else None,
            "mask": dict(self.mask) if self.mask else None,
            "params": dict(self.params or {}),
            "metrics": dict(self.metrics or {}),
            "warnings": list(self.warnings or ()),
        }


def _resolve_input(db: Any, workspace_id: str, asset_id: str) -> Any:
    """Workspace-scoped source asset; a foreign id is None (-> 404)."""
    from app.models import MediaAsset

    row = db.get(MediaAsset, str(asset_id or ""))
    if row is None or row.workspace_id != str(workspace_id):
        return None
    return row


def extract_single_frame(path: str, t_s: float, out_path: str | Path) -> bool:
    """Write exactly ONE frame at ``t_s`` as a PNG; False when nothing decoded.

    ``-vf trim=start=<t> -frames:v 1`` -- a single-input graph, so it goes through
    :func:`app.engine.intel.ffmpeg_util.run_filter` like every other media call in
    Work 12. Cheap enough for a one-off composition: one PNG instead of a full
    fps-sampled frame directory.
    """
    start = max(0.0, float(t_s or 0.0))
    result = ffmpeg_util.run_filter(
        str(path),
        str(out_path),
        ["-vf", f"trim=start={start:.3f}"],
        extra_args=("-frames:v", "1"),
        video=True,
    )
    return bool(result.get("ok"))


def _one_mask(
    *,
    workspace_id: str,
    backend: MaskBackend | None,
    storage_path: str,
    mask_t_s: float | None,
    labels: tuple[str, ...],
    fmt: str,
    run_id: str,
    workdir: str | Path,
    progress: ProgressFn | None = None,
    should_cancel: CancelFn | None = None,
    deadline: float | None = None,
) -> tuple[dict | None, str, str]:
    """Mask a single sampled frame for a one-off composition.

    Returns ``(mask_record_dict, reason, mask_path)``. ``reason`` is non-empty
    when the operation must report ``unavailable`` instead of writing anything --
    including the case where no backend exists (contracts §12: no silent bad
    mask).
    """
    resolved, reason = resolve_backend(backend)
    if resolved is None:
        return None, reason, ""
    width, height = probe_geometry(storage_path)
    if width <= 0 or height <= 0:
        return None, "source geometry unknown", ""
    root = Path(workdir)
    root.mkdir(parents=True, exist_ok=True)
    frame_png = root / "sample.png"
    if not extract_single_frame(storage_path, float(mask_t_s or 0.0), frame_png):
        return None, f"no frame decodable at t={float(mask_t_s or 0.0):.3f}s", ""
    frame = MaskFrame(index=0, t_s=round(float(mask_t_s or 0.0), 4), path=str(frame_png),
                      width=width, height=height)
    try:
        masks = resolved.masks_for_frame(
            frame, labels=labels, progress=progress, should_cancel=should_cancel, deadline=deadline
        )
    except ProviderUnavailable as exc:
        return None, str(exc), ""
    best = max(masks or [], key=lambda m: float(getattr(m, "area_ratio", 0.0)), default=None)
    if best is None or float(getattr(best, "area_ratio", 0.0)) <= 0.0:
        return None, "the backend found no mask to composite", ""
    record = write_mask(best, workspace_id=workspace_id, run_id=run_id or "adhoc", fmt=fmt)
    resolved_path = resolve_mask_path(workspace_id, record.storage_key)
    payload = record.to_dict()
    payload["provider_key"] = str(getattr(resolved, "key", "") or record.provider_key)
    payload["model_version"] = str(
        getattr(resolved, "model_version", "") or record.model_version
    )
    return payload, "", (str(resolved_path) if resolved_path else "")


def _register_derived(
    db: Any,
    *,
    workspace_id: str,
    source: Any,
    storage_key: str,
    width: int,
    height: int,
    duration: float | None,
    file_size: int,
    checksum: str,
    provider_key: str,
    operation: str,
    params: dict,
    metrics: dict,
) -> Any:
    """Register the composed file as a NEW asset with full lineage.

    ``parent_asset_id`` is the structured lineage; ``derivation_json`` is the
    processing manifest; ``meta_json`` repeats the pointer in the convention the
    other 14 asset-writing sites use. The source row is not touched.
    """
    from app.models import MediaAsset

    row = MediaAsset(
        workspace_id=workspace_id,
        type="video",
        origin="generated",
        provider=provider_key,
        storage_key=storage_key,
        mime_type="video/mp4",
        width=int(width),
        height=int(height),
        duration_seconds=float(duration) if duration else None,
        file_size=int(file_size),
        checksum=checksum,
        parent_asset_id=source.id,
        meta_json={
            "parent_asset_id": source.id,
            "original_key": str(source.storage_key or ""),
            "operation": operation,
            "derived": True,
        },
        derivation_json={
            "operation": operation,
            "provider_key": provider_key,
            "model_version": str(params.get("model_version") or ""),
            "input_asset_id": source.id,
            "input_checksum": str(source.checksum or ""),
            "params": dict(params),
            "metrics": dict(metrics),
        },
    )
    db.add(row)
    db.flush()
    return row


def _compose(
    db: Any,
    *,
    operation: str,
    workspace_id: str,
    asset_id: str,
    backend: MaskBackend | None,
    filter_builder: Callable[[str, dict, int, int], str],
    extra_args: Sequence[str] = (),
    params: dict | None = None,
    mask_t_s: float | None = None,
    labels: tuple[str, ...] = ("PERSON",),
    fmt: str = "PNG",
    run_id: str = "",
    workdir: str | Path | None = None,
    should_cancel: CancelFn | None = None,
    deadline: float | None = None,
) -> CompositionResult:
    """Shared body of every composition: mask -> ffmpeg -> derived asset.

    ``filter_builder(mask_path, mask_record, width, height)`` returns the ``-vf``
    graph. The source is only ever READ; the output is a new file under the
    workspace's derived directory.
    """
    options = dict(params or {})
    # Backend availability FIRST: with no mask provider the honest answer is
    # "unavailable", not "your file is missing" -- the two failures have
    # completely different remediations and the UI must show the right one.
    resolved_backend, backend_reason = resolve_backend(backend)
    if resolved_backend is None:
        return CompositionResult(
            ok=False, operation=operation, unavailable=True,
            reason=backend_reason or "no mask backend available",
        )
    source = _resolve_input(db, workspace_id, asset_id)
    if source is None:
        return CompositionResult(
            ok=False, operation=operation, reason="asset not found in this workspace"
        )
    source_path = resolve_workspace_file(workspace_id, str(source.storage_key or ""))
    if source_path is None or not Path(source_path).exists():
        return CompositionResult(
            ok=False, operation=operation, reason="source media file is missing"
        )
    width, height = probe_geometry(str(source_path))
    if width <= 0 or height <= 0:
        return CompositionResult(
            ok=False, operation=operation, reason="source has no readable video stream"
        )

    root = Path(workdir) if workdir else Path(
        Path(storage_service.STORAGE_ROOT) / workspace_id / f"intel/{operation}"
    )
    mask_payload, reason, mask_path = _one_mask(
        workspace_id=workspace_id,
        backend=resolved_backend,
        storage_path=str(source_path),
        mask_t_s=mask_t_s,
        labels=labels,
        fmt=fmt,
        run_id=run_id or operation,
        workdir=root,
        should_cancel=should_cancel,
        deadline=deadline,
    )
    if mask_payload is None:
        return CompositionResult(
            ok=False, operation=operation, unavailable=True, reason=reason or "mask unavailable"
        )

    derived_key = f"intel/{operation}/{source.id[:8]}-{run_id or 'adhoc'}.mp4"
    normalised = storage_service.validate_storage_key(workspace_id, derived_key)
    if normalised is None:
        return CompositionResult(
            ok=False, operation=operation, reason="derived path escapes the workspace root"
        )
    out_path = _workspace_dir(workspace_id) / normalised
    graph = filter_builder(mask_path, mask_payload, width, height)
    result = ffmpeg_util.run_filter(
        str(source_path), str(out_path), ["-vf", graph], extra_args=tuple(extra_args), video=True
    )
    if not result.get("ok"):
        return CompositionResult(
            ok=False,
            operation=operation,
            reason=f"ffmpeg composition failed: {str(result.get('stderr_tail') or '')[-160:]}",
        )
    out_width, out_height = probe_geometry(str(out_path))
    out_duration = ffmpeg_util.duration_seconds(str(out_path))
    asset = _register_derived(
        db,
        workspace_id=workspace_id,
        source=source,
        storage_key=normalised,
        width=out_width or width,
        height=out_height or height,
        duration=out_duration,
        file_size=int(out_path.stat().st_size),
        checksum=sha256_file(out_path),
        provider_key=str(mask_payload.get("provider_key") or ""),
        operation=operation,
        params={**options, "model_version": str(mask_payload.get("model_version") or "")},
        metrics={"processing_ms": int(result.get("processing_ms") or 0)},
    )
    return CompositionResult(
        ok=True,
        operation=operation,
        asset={
            "id": asset.id,
            "storage_key": asset.storage_key,
            "parent_asset_id": source.id,
            "checksum": asset.checksum,
            "file_size": int(asset.file_size or 0),
            "width": int(asset.width or 0),
            "height": int(asset.height or 0),
        },
        mask=mask_payload,
        params=options,
        metrics={"processing_ms": int(result.get("processing_ms") or 0)},
    )


def _movie_ref(mask_path: str) -> str:
    """``movie=`` source reference for a mask file, escaped for the filtergraph.

    A Windows path carries a drive colon, which the filtergraph parser reads as
    an option separator; both the colon and the backslashes are escaped so the
    same code works on POSIX and Windows.
    """
    escaped = str(mask_path).replace("\\", "\\\\").replace(":", "\\:")
    for char in ("[", "]", ",", ";"):
        escaped = escaped.replace(char, "\\" + char)
    return f"movie=filename='{escaped}'[m]"


def background_blur(
    db: Any,
    *,
    workspace_id: str,
    asset_id: str,
    backend: MaskBackend | None = None,
    radius: float = 12.0,
    strength: float = 2.0,
    params: dict | None = None,
    mask_t_s: float | None = None,
    labels: tuple[str, ...] = ("PERSON",),
    fmt: str = "PNG",
    run_id: str = "",
    workdir: str | Path | None = None,
    should_cancel: CancelFn | None = None,
    deadline: float | None = None,
) -> CompositionResult:
    """Blur everything OUTSIDE the mask; keep the subject sharp.

    ``boxblur`` on the background branch, the mask lifted into an alpha plane with
    ``alphamerge``, then ``overlay`` -- the standard compositing idiom, and the
    only way to express it with a single ffmpeg input (the mask is referenced as
    a ``movie`` source rather than a second ``-i``). Returns an honest
    ``unavailable`` when no mask backend exists; there is no "blur everything"
    fallback, which would be a fake result.
    """
    power = max(0.3, min(3.0, float(strength)))
    blur = max(1, int(radius))

    def build(mask_path: str, _mask: dict, _width: int, _height: int) -> str:
        return (
            f"{_movie_ref(mask_path)};"
            "[0:v]split=2[keep][bg];"
            f"[bg]boxblur=luma_radius={blur}:luma_power={power:g}[blurred];"
            "[keep][m]alphamerge[fg];"
            "[blurred][fg]overlay[out]"
        )

    return _compose(
        db,
        operation="background_blur",
        workspace_id=workspace_id,
        asset_id=asset_id,
        backend=backend,
        filter_builder=build,
        params={"radius": blur, "strength": power, **dict(params or {})},
        mask_t_s=mask_t_s,
        labels=labels,
        fmt=fmt,
        run_id=run_id,
        workdir=workdir,
        should_cancel=should_cancel,
        deadline=deadline,
    )


def background_replace(
    db: Any,
    *,
    workspace_id: str,
    asset_id: str,
    background_path: str,
    backend: MaskBackend | None = None,
    params: dict | None = None,
    mask_t_s: float | None = None,
    labels: tuple[str, ...] = ("PERSON",),
    fmt: str = "PNG",
    run_id: str = "",
    workdir: str | Path | None = None,
    should_cancel: CancelFn | None = None,
    deadline: float | None = None,
) -> CompositionResult:
    """Composite a NEW background behind the masked subject.

    ``background_path`` is a real file (an operator-chosen still or another
    workspace asset); it is read, never written. Same compositing idiom as
    :func:`background_blur`, with the background branch scaled to the source
    frame so any still works.
    """
    escaped = str(background_path).replace("\\", "\\\\").replace(":", "\\:")
    for char in ("[", "]", ",", ";"):
        escaped = escaped.replace(char, "\\" + char)

    def build(mask_path: str, _mask: dict, width: int, height: int) -> str:
        return (
            f"{_movie_ref(mask_path)};"
            f"movie=filename='{escaped}',scale={width}:{height}:force_original_aspect_ratio=increase,"
            f"crop={width}:{height}[bg];"
            "[0:v][m]alphamerge[fg];"
            "[bg][fg]overlay[out]"
        )

    return _compose(
        db,
        operation="background_replace",
        workspace_id=workspace_id,
        asset_id=asset_id,
        backend=backend,
        filter_builder=build,
        params={"background_path": str(background_path), **dict(params or {})},
        mask_t_s=mask_t_s,
        labels=labels,
        fmt=fmt,
        run_id=run_id,
        workdir=workdir,
        should_cancel=should_cancel,
        deadline=deadline,
    )


def subject_crop(
    db: Any,
    *,
    workspace_id: str,
    asset_id: str,
    backend: MaskBackend | None = None,
    aspect: str = "9:16",
    pad_ratio: float = 0.25,
    out_width: int = 0,
    params: dict | None = None,
    mask_t_s: float | None = None,
    labels: tuple[str, ...] = ("PERSON",),
    fmt: str = "PNG",
    run_id: str = "",
    workdir: str | Path | None = None,
    should_cancel: CancelFn | None = None,
    deadline: float | None = None,
) -> CompositionResult:
    """Crop the frame around the masked subject, honouring a target aspect.

    The crop rectangle is derived from the mask's bounding box plus ``pad_ratio``
    of slack, clamped inside the frame so the crop is always a valid rectangle.
    This is a STATIC crop sampled from one masked frame -- time-varying framing
    is lane G's reframe plan (contracts §11), not this operation.
    """
    try:
        target_w, target_h = (int(v) for v in str(aspect).split(":", 1))
    except (TypeError, ValueError) as exc:
        raise SegmentationError(f"aspect must look like '9:16', got {aspect!r}") from exc
    if target_w <= 0 or target_h <= 0:
        raise SegmentationError(f"aspect must be positive, got {aspect!r}")

    def build(_mask_path: str, mask: dict, width: int, height: int) -> str:
        # The rectangle follows the bbox of the SAME RegionMask the written file
        # was encoded from, so the crop cannot drift from the mask on disk.
        box = (mask.get("bbox") or {}) if isinstance(mask, dict) else {}
        bx = float(box.get("x") or 0.0)
        by = float(box.get("y") or 0.0)
        bw = float(box.get("w") or width)
        bh = float(box.get("h") or height)
        slack = max(0.0, float(pad_ratio))
        cw = min(width, max(1, int(round(bw * (1 + slack)))))
        ch = min(height, max(1, int(round(bh * (1 + slack)))))
        want_ratio = target_w / target_h
        if ch / cw < want_ratio:
            ch = min(height, max(1, int(round(cw / want_ratio))))
        else:
            cw = min(width, max(1, int(round(ch * want_ratio))))
        x = max(0, min(width - cw, int(round(bx + bw / 2 - cw / 2))))
        y = max(0, min(height - ch, int(round(by + bh / 2 - ch / 2))))
        scale = f",scale={int(out_width)}:-2" if int(out_width or 0) > 0 else ""
        return f"[0:v]crop={cw}:{ch}:{x}:{y}{scale}[out]"

    return _compose(
        db,
        operation="subject_crop",
        workspace_id=workspace_id,
        asset_id=asset_id,
        backend=backend,
        filter_builder=build,
        params={"aspect": str(aspect), "pad_ratio": float(pad_ratio), **dict(params or {})},
        mask_t_s=mask_t_s,
        labels=labels,
        fmt=fmt,
        run_id=run_id,
        workdir=workdir,
        should_cancel=should_cancel,
        deadline=deadline,
    )


def tracked_overlay(
    db: Any,
    *,
    workspace_id: str,
    asset_id: str,
    boxes: Sequence[dict],
    color: str = "yellow",
    thickness: int = 3,
    params: dict | None = None,
) -> CompositionResult:
    """Draw tracked rectangles on a NEW copy of the clip.

    ``boxes`` are ``{"start_s", "end_s", "x", "y", "w", "h"}`` rectangles --
    exactly what ``face_tracks``/``face_track_samples`` already hold, so a face
    tracker or a mask bbox can drive it without a new concept. Absent geometry
    is a refusal, not an empty overlay.
    """
    windows = [
        b for b in (boxes or [])
        if b.get("x") is not None and b.get("w") and b.get("h")
    ]
    if not windows:
        return CompositionResult(
            ok=False, operation="tracked_overlay", reason="no box geometry supplied"
        )
    source = _resolve_input(db, workspace_id, asset_id)
    if source is None:
        return CompositionResult(
            ok=False, operation="tracked_overlay", reason="asset not found in this workspace"
        )
    source_path = resolve_workspace_file(workspace_id, str(source.storage_key or ""))
    if source_path is None or not Path(source_path).exists():
        return CompositionResult(
            ok=False, operation="tracked_overlay", reason="source media file is missing"
        )
    width, height = probe_geometry(str(source_path))
    if width <= 0 or height <= 0:
        return CompositionResult(
            ok=False, operation="tracked_overlay", reason="source has no readable video stream"
        )
    draw_terms = "+".join(
        "drawbox=x={x}:y={y}:w={w}:h={h}:color={color}:t={t}:"
        "enable='between(t\\,{a}\\,{b})'".format(
            x=int(b["x"]),
            y=int(b["y"]),
            w=int(b["w"]),
            h=int(b["h"]),
            color=str(color),
            t=max(1, int(thickness)),
            a=float(b.get("start_s") or 0.0),
            b=float(b.get("end_s") or 0.0),
        )
        for b in windows
    )
    graph = f"[0:v]{draw_terms}[out]"
    derived_key = f"intel/tracked_overlay/{source.id[:8]}.mp4"
    normalised = storage_service.validate_storage_key(workspace_id, derived_key)
    if normalised is None:
        return CompositionResult(
            ok=False, operation="tracked_overlay", reason="derived path escapes the workspace root"
        )
    out_path = _workspace_dir(workspace_id) / normalised
    result = ffmpeg_util.run_filter(
        str(source_path),
        str(out_path),
        ["-vf", f"{graph}"],
        video=True,
    )
    if not result.get("ok"):
        return CompositionResult(
            ok=False,
            operation="tracked_overlay",
            reason=f"ffmpeg overlay failed: {str(result.get('stderr_tail') or '')[-160:]}",
        )
    asset = _register_derived(
        db,
        workspace_id=workspace_id,
        source=source,
        storage_key=normalised,
        width=width,
        height=height,
        duration=ffmpeg_util.duration_seconds(str(out_path)),
        file_size=int(out_path.stat().st_size),
        checksum=sha256_file(out_path),
        provider_key="ffmpeg",
        operation="tracked_overlay",
        params={"boxes": len(windows), "windows": len(windows), **dict(params or {})},
        metrics={"processing_ms": int(result.get("processing_ms") or 0)},
    )
    return CompositionResult(
        ok=True,
        operation="tracked_overlay",
        asset={
            "id": asset.id,
            "storage_key": asset.storage_key,
            "parent_asset_id": source.id,
            "checksum": asset.checksum,
            "file_size": int(asset.file_size or 0),
        },
        params={"boxes": len(windows)},
        metrics={"processing_ms": int(result.get("processing_ms") or 0)},
    )


__all__ = [
    "DEFAULT_FPS",
    "MASK_DIR",
    "MASK_FORMATS",
    "MASK_KINDS",
    "MAX_FRAMES_LIMIT",
    "TINY_MASK_AREA_RATIO",
    "CompositionResult",
    "MaskBackend",
    "MaskFrame",
    "MaskRecord",
    "RegionMask",
    "SegmentationError",
    "background_blur",
    "background_replace",
    "bbox_from_runs",
    "encode_mask",
    "encode_png",
    "encode_rle_json",
    "extract_mask_frames",
    "extract_single_frame",
    "mask_from_binary",
    "mask_from_runs",
    "mask_row_dto",
    "persist_masks",
    "probe_geometry",
    "rect_runs",
    "resolve_backend",
    "resolve_mask_path",
    "resolve_workspace_file",
    "segment",
    "sha256_file",
    "subject_crop",
    "tracked_overlay",
    "unavailable",
    "write_mask",
]
