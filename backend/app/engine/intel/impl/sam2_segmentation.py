"""SAM2 segmentation adapter (Work 12 Lane F) -- contracts §1.1/§1.3/§9.

The ONLY provider key the ``segmentation`` chain can resolve today. Everything
this module does is orchestration around an injectable
:class:`~app.engine.intel.segmentation.MaskBackend`:

* ``health()`` probes ``torch`` + ``sam2`` INSIDE the function, never at module
  import, so ``from app.main import app`` succeeds in a venv with zero ML
  packages (contracts §0 "boot without models"). It never raises: in the CI venv
  it returns ``available=False`` with the ImportError reason, which the registry
  turns into the terminal ``UNAVAILABLE`` run state and the API into a dark
  capability with a reason -- never a fabricated mask (contracts §9).
* ``run()`` delegates to the SAME provider-independent engine the tests drive
  with a deterministic double (:func:`app.engine.intel.segmentation.segment`), so
  the production path and the test path execute identical orchestration code.
  Only the pixels differ.

License (contracts §1.4, audited 2026-09-29 in
``docs/oss/MEDIA_INTEL_LICENSES.md``): SAM2 code **and** checkpoints are
``Apache-2.0`` and the checkpoints are ungated
(``facebook/sam2-hiera-*``: ``license: apache-2.0``, ``gated: false``). This is
the only Work 12 backend cleared end to end, so ``commercial_use`` is
``PERMITTED`` -- and it is the ONLY provider in the layer that may say so.
Nothing else is upgraded by this file.
"""

from __future__ import annotations

import logging

from app.engine.intel import segmentation as segmentation_engine
from app.engine.intel.base import (
    MODE_LOCAL,
    CancelFn,
    IntelRequest,
    LicenseInfo,
    MediaIntelProvider,
    ProgressFn,
    ProviderHealth,
    ProviderResult,
    ProviderUnavailable,
    ResourceSpec,
    check_control,
    deadline_in,
)
from app.engine.intel.segmentation import MaskBackend, MaskFrame, RegionMask

logger = logging.getLogger("ymoney.intel")

#: audited values (contracts §1.4 / docs/oss/MEDIA_INTEL_LICENSES.md)
CODE_LICENSE = "Apache-2.0"
CODE_LICENSE_URL = "https://raw.githubusercontent.com/facebookresearch/sam2/main/LICENSE"
MODEL_LICENSE = "Apache-2.0"
MODEL_LICENSE_URL = "https://huggingface.co/facebook/sam2-hiera-tiny"
MODEL_LICENSE_NOTE = (
    "SAM 2 checkpoints are released under Apache 2.0 and are NOT gated "
    "(HF cards for facebook/sam2-hiera-* report license=apache-2.0, gated=false)"
)
AUDITED_ON = "2026-09-29"
COMMERCIAL_USE_PERMITTED = "PERMITTED"

#: SAM2 checkpoint variants this adapter knows how to name. The variant is a
#: request parameter, never a guess: an unknown one is refused, not substituted.
SUPPORTED_VARIANTS: tuple[str, ...] = (
    "sam2.1_hiera_tiny",
    "sam2.1_hiera_small",
    "sam2.1_hiera_base_plus",
    "sam2.1_hiera_large",
)

#: labels SAM2's own prompt vocabulary covers (person/object); BACKGROUND is the
#: engine's complement of the foreground, never a second model prompt.
DEFAULT_LABELS: tuple[str, ...] = ("PERSON",)


class Sam2MaskBackend(MaskBackend):
    """Real SAM2 backend. Loads torch + sam2 lazily, inside :meth:`health`.

    The prompt is a point/box per label taken from the request ``params``; SAM2
    returns per-object logits which are binarised at ``mask_threshold`` into a
    single :class:`RegionMask` per prompt. No package is imported until
    :meth:`health` or :meth:`masks_for_frame` runs.
    """

    key = "sam2_segmentation"
    model_version = "sam2.1"

    def __init__(self, params: dict | None = None) -> None:
        self._params = dict(params or {})

    # -- availability ----------------------------------------------------

    def _load(self) -> tuple[object | None, str]:
        """Import the backend inside the call, never at module scope.

        Returns ``(module, "")`` or ``(None, reason)``. Both imports are lazy, so
        the module stays importable with no ML packages installed.
        """
        try:
            import torch  # noqa: PLC0415 - optional heavy backend, probed only
        except Exception as exc:  # noqa: BLE001 - any import failure is honest
            return None, f"torch not installed ({type(exc).__name__})"
        try:
            from sam2.automatic_mask_generator import SAM2AutomaticMaskGenerator  # noqa: PLC0415
            from sam2.build_sam import build_sam2  # noqa: PLC0415
        except Exception as exc:  # noqa: BLE001
            return None, f"sam2 not installed ({type(exc).__name__})"
        return {"torch": torch, "build_sam2": build_sam2, "generator": SAM2AutomaticMaskGenerator}, ""

    def health(self) -> tuple[bool, str]:
        loaded, reason = self._load()
        if loaded is None:
            return False, reason
        variant = str(self._params.get("variant") or SUPPORTED_VARIANTS[0])
        if variant not in SUPPORTED_VARIANTS:
            return False, f"unknown SAM2 checkpoint variant {variant!r}"
        return True, ""

    def model_version_for(self, params: dict | None = None) -> str:
        """Checkpoint identity recorded in provenance (variant, never invented)."""
        merged = {**self._params, **dict(params or {})}
        variant = str(merged.get("variant") or SUPPORTED_VARIANTS[0])
        return f"{self.model_version}+{variant}"

    # -- inference -------------------------------------------------------

    def masks_for_frame(
        self,
        frame: MaskFrame,
        *,
        labels: tuple[str, ...],
        progress: ProgressFn | None = None,
        should_cancel: CancelFn | None = None,
        deadline: float | None = None,
    ) -> list[RegionMask]:
        """Run SAM2 on one extracted frame and binarise its masks.

        Raises :class:`ProviderUnavailable` when the backend is absent -- the
        engine converts that into an honest ``unavailable`` payload, so no caller
        can mistake it for "no person in frame".
        """
        loaded, reason = self._load()
        if loaded is None:
            raise ProviderUnavailable(reason)
        check_control(should_cancel, deadline)
        torch = loaded["torch"]
        params = {**self._params}
        variant = str(params.get("variant") or SUPPORTED_VARIANTS[0])
        threshold = float(params.get("mask_threshold") or 0.0)
        # Automatic prompting: the point grid is the request parameter, so a
        # caller can trade latency for mask recall without a code change.
        grid = max(1, int(params.get("points_per_side") or 8))
        if progress is not None:
            progress(0.5)
        model = loaded["build_sam2"](variant, params.get("checkpoint") or None)
        generator = loaded["generator"](model, points_per_side=grid)
        image = _read_frame_rgb(frame.path)
        results = generator.generate(image)
        check_control(should_cancel, deadline)
        masks: list[RegionMask] = []
        for entry in results:
            logits = entry.get("segmentation", {}).get("logits")
            if logits is None:
                continue
            binary = (logits > threshold).to(dtype=torch.uint8).cpu().numpy()
            masks.append(
                segmentation_engine.mask_from_binary(
                    binary,
                    label=str(entry.get("label") or labels[0] if labels else "PERSON"),
                    frame=frame,
                    score=float(entry.get("predicted_iou") or 0.0),
                )
            )
        return masks


def _read_frame_rgb(path: str):
    """Decode one frame to an HxWx3 uint8 array (SAM2's expected layout).

    Only reached when torch/sam2 exist; ``numpy`` arrives as torch's dependency,
    so no new runtime dependency is introduced by Work 12.
    """
    import numpy as np  # noqa: PLC0415 - only importable alongside torch
    from PIL import Image  # noqa: PLC0415 - SAM2's own image reader dependency

    with Image.open(path) as handle:
        return np.asarray(handle.convert("RGB"))


class Sam2SegmentationProvider(MediaIntelProvider):
    """``segmentation`` chain provider. Provider-independent by construction.

    ``backend`` is injectable so the tests exercise the real engine with a
    deterministic double. The production path (no injected backend) builds the
    real :class:`Sam2MaskBackend` and stays honestly unavailable in a venv with
    no ML packages.
    """

    key = "sam2_segmentation"
    kind = "segmentation"

    def __init__(self, backend: MaskBackend | None = None) -> None:
        self._backend = backend

    # -- engine plumbing -------------------------------------------------

    @property
    def backend(self) -> MaskBackend:
        """The mask backend this provider drives (never raises)."""
        if self._backend is not None:
            return self._backend
        return Sam2MaskBackend()

    def health(self) -> ProviderHealth:
        """Availability verdict. Never raises, never imports eagerly."""
        try:
            backend = self.backend
            available, reason = backend.health()
        except Exception as exc:  # noqa: BLE001 - a probe must never propagate
            return ProviderHealth(
                available=False,
                reason=f"mask backend probe failed: {type(exc).__name__}",
                mode=MODE_LOCAL,
                detail={"error_type": type(exc).__name__},
            )
        if not available:
            return ProviderHealth(
                available=False,
                reason=reason or "no segmentation backend available",
                version=str(getattr(backend, "model_version", "") or ""),
                mode=MODE_LOCAL,
                detail={
                    "backend": str(getattr(backend, "key", "") or ""),
                    "remediation": (
                        "install torch + facebookresearch/sam2 and the checkpoint "
                        "on the worker host (the app itself adds no dependency)"
                    ),
                },
            )
        return ProviderHealth(
            available=True,
            version=str(getattr(backend, "model_version", "") or ""),
            mode=MODE_LOCAL,
            detail={"backend": str(getattr(backend, "key", "") or "")},
        )

    def capabilities(self) -> dict:
        """Honest feature/limit map: only what this adapter can actually do."""
        backend = self._backend
        return {
            "available": backend is not None or Sam2MaskBackend().health()[0],
            "labels": list(segmentation_engine.MASK_KINDS),
            "formats": list(segmentation_engine.MASK_FORMATS),
            "variants": list(SUPPORTED_VARIANTS),
            "mask_storage": "file_reference",
            "background_blur": True,
            "background_replace": True,
            "subject_crop": True,
            "tracked_overlays": False,
            "notes": (
                "masks are written as files under STORAGE_ROOT and referenced by "
                "mask_assets; raw masks are never stored in DB rows"
            ),
        }

    def resource_requirements(self) -> ResourceSpec:
        return ResourceSpec(
            gpu=True,
            vram_mb=4096,
            ram_mb=8192,
            model_bytes=160 * 1024 * 1024,
            cpu_seconds_per_audio_minute=0.0,
            notes="SAM2 video inference; acquire a GPU slot via gpu_semaphore_sync",
        )

    def license_info(self) -> LicenseInfo:
        """Code AND checkpoints are Apache-2.0 and ungated -> PERMITTED.

        The only Work 12 provider cleared end to end (contracts §1.4). The note
        keeps the audit date + source next to the claim so a later audit can
        retract it without a code archaeology hunt.
        """
        return LicenseInfo(
            code_license=CODE_LICENSE,
            code_license_url=CODE_LICENSE_URL,
            model_license=MODEL_LICENSE,
            model_license_url=MODEL_LICENSE_URL,
            model_gated=False,
            commercial_use=COMMERCIAL_USE_PERMITTED,
            audited_on=AUDITED_ON,
            notes=(
                "SAM2 code and checkpoints are Apache-2.0, ungated "
                "(docs/oss/MEDIA_INTEL_LICENSES.md, audited 2026-09-29); "
                f"{MODEL_LICENSE_NOTE}"
            ),
        )

    def run(
        self,
        request: IntelRequest,
        *,
        progress: ProgressFn,
        should_cancel: CancelFn,
        deadline: float | None,
    ) -> ProviderResult:
        """Delegate to the shared engine; no mask is ever invented here."""
        params = dict(request.params or {})
        labels = tuple(
            str(v) for v in (params.get("labels") or DEFAULT_LABELS)
        )
        unknown = [v for v in labels if v not in segmentation_engine.MASK_KINDS]
        if unknown:
            raise ValueError(f"unknown mask kind(s): {', '.join(sorted(unknown))}")
        backend = self.backend
        outcome: dict = segmentation_engine.segment(
            workspace_id=request.workspace_id,
            storage_path=request.storage_path,
            run_id=request.run_id,
            backend=backend,
            params=params,
            progress=progress,
            should_cancel=should_cancel,
            deadline=deadline_in(None) if deadline is None else deadline,
        )
        if not outcome.get("ok"):
            return ProviderResult(
                ok=False,
                artifacts={},
                metrics=dict(outcome.get("metrics") or {}),
                warnings=[str(w) for w in (outcome.get("warnings") or [])],
                error=str(outcome.get("error") or "SEGMENTATION_FAILED")[:60],
            )
        return ProviderResult(
            ok=True,
            artifacts={"masks": {"payload": list(outcome.get("masks") or [])}},
            metrics=dict(outcome.get("metrics") or {}),
            warnings=[str(w) for w in (outcome.get("warnings") or [])],
        )

    def cost(self, spec: ResourceSpec) -> dict:
        """Cost for the work described by ``spec``.

        SAM2 is a local model: there is no per-call vendor price to invent, so
        the honest answer is "unbilled, measure it". The run row carries the real
        ``gpu_ms``/``processing_ms`` measured by the caller (contracts §3).
        """
        return {
            "gpu_ms": 0,
            "cpu_ms": 0,
            "cost_micros": 0,
            "billed": False,
            "notes": f"local model; measure gpu_ms on the host (gpu={bool(spec.gpu)})",
        }


PROVIDER = Sam2SegmentationProvider

__all__ = [
    "AUDITED_ON",
    "CODE_LICENSE",
    "CODE_LICENSE_URL",
    "COMMERCIAL_USE_PERMITTED",
    "DEFAULT_LABELS",
    "MODEL_LICENSE",
    "MODEL_LICENSE_URL",
    "PROVIDER",
    "SUPPORTED_VARIANTS",
    "Sam2MaskBackend",
    "Sam2SegmentationProvider",
]
