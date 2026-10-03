"""``mediapipe_faces`` -- the face-tracking provider (Work 12 Lane E).

Contracts §1.1/§1.3/§1.4/§8. Registry key ``mediapipe_faces``, capability kind
``face_tracking``; the registry imports this module LAZILY, so nothing heavy is
loaded at app startup.

What this adapter is
    A thin, honest wrapper around ``google-ai-edge/mediapipe``'s face detection.
    It owns three things and nothing else: the backend probe, the raw-result ->
    normalised-box translation, and the license verdict. The tracking brain lives
    in :mod:`app.engine.intel.face_tracking`, which this adapter drives through
    the SAME :func:`detect` seam it uses in production.

Why the backend is injectable
    ``__init__(detector=...)`` accepts any object exposing
    ``detect(frame_path) -> iterable[{x, y, w, h, confidence?, landmarks?}]``.
    That is how the tests prove the identical tracking code over REAL decoded
    frames while CI has zero ML packages installed. Production passes nothing and
    gets the real MediaPipe detector.

Deliberately NOT implemented (and the reason stays visible in
``capabilities()``)

* **No OpenCV Haar cascade fallback.** The cascade XMLs carry per-file
  Intel/contributor terms, not Apache-2.0, so using them would silently change
  the license posture of a derived asset (``docs/oss/MEDIA_INTEL_LICENSES.md``,
  the ``opencv Haar cascades`` row).
* **No identity recognition.** Only box geometry and, when MediaPipe supplies
  them, the six face keypoints. No embedding, no template, no name.

License (contracts §1.4 table): code ``Apache-2.0``; the ``.task``/``.tflite``
bundles publish **no** terms at their download URL, so ``commercial_use`` is
``REVIEW_REQUIRED`` -- never promoted to ``PERMITTED``.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from app.engine.intel import face_tracking as ft
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
)
from app.engine.intel.factory import DEFAULT_REMEDIATION

logger = logging.getLogger("ymoney.intel")

#: registry key (contracts §1.1 chain table)
PROVIDER_KEY = "mediapipe_faces"
#: capability chain this provider serves
PROVIDER_KIND = "face_tracking"
#: package that must be installed for the real backend
BACKEND_PACKAGE = "mediapipe"
#: package the real detector also needs to read a frame
FRAME_READER_PACKAGE = "cv2"

#: MediaPipe's 6 face keypoints (the only landmarks this adapter stores)
KEYPOINT_NAMES: tuple[str, ...] = (
    "right_eye",
    "left_eye",
    "nose_tip",
    "mouth_center",
    "right_ear_tragion",
    "left_ear_tragion",
)

#: default detector confidence threshold
DEFAULT_MIN_DETECTION_CONFIDENCE = 0.5
#: default MediaPipe model selection (0 = short range, 1 = full)
DEFAULT_MODEL_SELECTION = 1
#: rough on-disk size of a ``face_landmarker.task`` bundle
ESTIMATED_MODEL_BYTES = 3 * 1024 * 1024

#: contracts §1.4 audited verdict for this adapter (2026-09-29 audit).
#: Code is Apache-2.0; the model bundles publish NO terms -> REVIEW_REQUIRED.
LICENSE = LicenseInfo(
    code_license="Apache-2.0",
    code_license_url=(
        "https://raw.githubusercontent.com/google-ai-edge/mediapipe/master/LICENSE"
    ),
    model_license="UNVERIFIED",
    model_license_url=(
        "https://storage.googleapis.com/mediapipe-models/face_landmarker/"
        "face_landmarker/float16/latest/face_landmarker.task"
    ),
    model_gated=False,
    commercial_use="REVIEW_REQUIRED",
    audited_on="2026-09-29",
    notes=(
        "mediapipe code is Apache-2.0 but the face_landmarker.task / "
        "face_detection_*.tflite bundles publish no terms at the download URL "
        "(Kaggle's TF.js listing is a secondary source), so commercial use is "
        "REVIEW_REQUIRED until written terms are obtained "
        "(docs/oss/MEDIA_INTEL_LICENSES.md: mediapipe row + open unverified #4)"
    ),
)

#: the operator-facing reason for the verdict above, reused verbatim by tests
LICENSE_REASON = LICENSE.notes

#: why no Haar fallback exists (surfaced in capabilities so it is never silent)
HAAR_FALLBACK_NOTE = (
    "OpenCV Haar cascades are deliberately NOT used as a fallback: the cascade "
    "XMLs carry per-file Intel/contributor terms rather than Apache-2.0 "
    "(docs/oss/MEDIA_INTEL_LICENSES.md, the 'opencv Haar cascades' row). With "
    "no detector the capability is UNAVAILABLE, never a fabricated face box."
)


def build_detector(
    *,
    model_selection: int = DEFAULT_MODEL_SELECTION,
    min_detection_confidence: float = DEFAULT_MIN_DETECTION_CONFIDENCE,
) -> Any:
    """Build the REAL MediaPipe detector (lazy import; raises when unavailable).

    The one seam every caller uses -- the media-intel provider AND
    ``providers/clips.py``'s face-centred reframe -- so detection exists in
    exactly one place (contracts §21 promotion note).
    """
    return _MediaPipeDetector(
        model_selection=model_selection,
        min_detection_confidence=min_detection_confidence,
    )


class _MediaPipeDetector:
    """Real backend: ``mediapipe.solutions.face_detection`` over a decoded frame.

    Constructed lazily (only from :meth:`MediaPipeFaceProvider._resolve_detector`
    or a direct call), so importing this module never imports MediaPipe.
    """

    version = "mediapipe"

    def __init__(
        self,
        *,
        model_selection: int = DEFAULT_MODEL_SELECTION,
        min_detection_confidence: float = DEFAULT_MIN_DETECTION_CONFIDENCE,
    ) -> None:
        import cv2  # noqa: PLC0415 - optional backend, never at module import
        import mediapipe as mp  # noqa: PLC0415 - optional backend

        self._cv2 = cv2
        self._mp = mp
        self._min_confidence = float(min_detection_confidence)
        self._detector = mp.solutions.face_detection.FaceDetection(
            model_selection=int(model_selection),
            min_detection_confidence=self._min_confidence,
        )
        self.version = f"mediapipe {getattr(mp, '__version__', 'unknown')}"

    def detect(self, frame_path: str) -> list[dict]:
        """Boxes + keypoints of one frame file, in that frame's pixel space."""
        image = self._cv2.imread(str(frame_path))
        if image is None:
            return []
        height, width = image.shape[:2]
        rgb = self._cv2.cvtColor(image, self._cv2.COLOR_BGR2RGB)
        result = self._detector.process(rgb)
        return [
            _detection_to_dict(detection, width, height)
            for detection in (getattr(result, "detections", None) or [])
        ]

    def close(self) -> None:  # pragma: no cover - only with the real backend
        closer = getattr(self._detector, "close", None)
        if callable(closer):
            try:
                closer()
            except Exception as exc:  # noqa: BLE001 - cleanup must never raise
                logger.debug("mediapipe close failed: %s", type(exc).__name__)


def _detection_to_dict(detection: Any, width: int, height: int) -> dict:
    """One MediaPipe detection -> a normalised-box mapping (pixel space)."""
    location = getattr(detection, "location_data", None)
    box = getattr(location, "relative_bounding_box", None)
    if box is None:
        return {}
    scores = getattr(detection, "score", None) or []
    confidence = float(scores[0]) if scores else None
    keypoints = getattr(location, "relative_keypoints", None) or []
    landmarks = [
        {
            # "key" (never "name"): a landmark POINT is not a person, and the
            # DTO vocabulary check rejects name-like keys (contracts §0)
            "key": KEYPOINT_NAMES[i] if i < len(KEYPOINT_NAMES) else f"keypoint_{i}",
            "x": round(float(point.x) * width, 3),
            "y": round(float(point.y) * height, 3),
        }
        for i, point in enumerate(keypoints)
    ]
    return {
        "x": float(box.xmin) * width,
        "y": float(box.ymin) * height,
        "w": float(box.width) * width,
        "h": float(box.height) * height,
        "confidence": confidence,
        "landmarks": landmarks or None,
    }


class MediaPipeFaceProvider(MediaIntelProvider):
    """Face tracking through MediaPipe, or an honest unavailable verdict.

    ``key``/``kind`` are class attributes (they satisfy the ABC's abstract
    properties) because the registry instantiates the class with no arguments:
    ``MediaPipeFaceProvider()`` is the production configuration and reports
    ``available=False`` with a reason whenever MediaPipe (or a model bundle) is
    missing.
    """

    key = PROVIDER_KEY
    kind = PROVIDER_KIND
    kinds: tuple[str, ...] = ()

    def __init__(
        self,
        detector: Any = None,
        *,
        model_selection: int = DEFAULT_MODEL_SELECTION,
        min_detection_confidence: float = DEFAULT_MIN_DETECTION_CONFIDENCE,
        model_path: str | Path | None = None,
    ) -> None:
        self._detector = detector
        self._model_selection = int(model_selection)
        self._min_confidence = float(min_detection_confidence)
        self._model_path = Path(model_path) if model_path else None

    # -- probes ---------------------------------------------------------

    def _backend_version(self) -> str:
        """Imported backend version, or "" (never raises)."""
        try:
            import mediapipe  # noqa: PLC0415 - optional backend

            return str(getattr(mediapipe, "__version__", "") or "")
        except Exception as exc:  # noqa: BLE001 - a probe must never raise
            logger.debug("mediapipe import failed: %s", type(exc).__name__)
            return ""

    def _resolved_model(self) -> Path | None:
        """The configured model bundle when it exists on disk, else None."""
        candidate = self._model_path
        if candidate is None:
            return None
        try:
            return candidate if candidate.is_file() else None
        except OSError:  # pragma: no cover - unreadable mount
            return None

    def health(self) -> ProviderHealth:
        """Availability verdict. NEVER raises, NEVER imports eagerly into it."""
        try:
            if self._detector is not None:
                return ProviderHealth(
                    available=True,
                    reason="",
                    version=str(getattr(self._detector, "version", "") or "injected"),
                    mode=MODE_LOCAL,
                    detail={
                        "backend": "injected",
                        "min_detection_confidence": self._min_confidence,
                    },
                )
            try:
                import mediapipe  # noqa: PLC0415 - optional backend
            except Exception as exc:  # noqa: BLE001 - honest unavailability
                return ProviderHealth(
                    available=False,
                    reason=(
                        f"{BACKEND_PACKAGE} not installed "
                        f"({type(exc).__name__}); face tracking cannot run"
                    ),
                    mode=MODE_LOCAL,
                    detail={
                        "backend": BACKEND_PACKAGE,
                        "frame_reader": FRAME_READER_PACKAGE,
                        "error_type": type(exc).__name__,
                        "haar_fallback": False,
                        "remediation": DEFAULT_REMEDIATION,
                    },
                )
            version = str(getattr(mediapipe, "__version__", "") or "unknown")
            model = self._resolved_model()
            if model is None:
                return ProviderHealth(
                    available=False,
                    reason=(
                        f"{BACKEND_PACKAGE} {version} is installed but no face "
                        "landmarker model bundle is configured on this host"
                    ),
                    version=version,
                    mode=MODE_LOCAL,
                    detail={
                        "backend": BACKEND_PACKAGE,
                        "model_bundle": str(self._model_path or ""),
                        "model_license": LICENSE.model_license,
                        "commercial_use": LICENSE.commercial_use,
                        "haar_fallback": False,
                        "remediation": (
                            "Place the face_landmarker.task bundle on the worker host "
                            "and pass model_path= to the provider."
                        ),
                    },
                )
            return ProviderHealth(
                available=True,
                reason="",
                version=f"{version} + {model.name}",
                mode=MODE_LOCAL,
                detail={
                    "backend": BACKEND_PACKAGE,
                    "model_bundle": model.name,
                    "model_license": LICENSE.model_license,
                    "commercial_use": LICENSE.commercial_use,
                    "min_detection_confidence": self._min_confidence,
                },
            )
        except Exception as exc:  # noqa: BLE001 - health must never propagate
            return ProviderHealth(
                available=False,
                reason=f"health probe failed: {type(exc).__name__}",
                mode=MODE_LOCAL,
                detail={"error_type": type(exc).__name__, "remediation": DEFAULT_REMEDIATION},
            )

    def capabilities(self) -> dict:
        """Honest feature/limit map (never claims what the probe did not confirm)."""
        health = self.health()
        return {
            "available": bool(health.available),
            "reason": health.reason,
            "backend": "injected" if self._detector is not None else BACKEND_PACKAGE,
            "sample_fps_default": ft.DEFAULT_SAMPLE_FPS,
            "sample_fps_range": [ft.MIN_SAMPLE_FPS, ft.MAX_SAMPLE_FPS],
            "max_samples_per_track_default": ft.DEFAULT_MAX_SAMPLES,
            "max_faces_per_frame": None,  # unbounded: MediaPipe reports what it sees
            "landmarks": True,
            "landmark_names": list(KEYPOINT_NAMES),
            "multi_face": True,
            "anonymous_tracks": True,
            "track_label_format": "FT_%02d",
            "identity_recognition": False,
            "association": {
                "metric": "iou",
                "min_iou": ft.DEFAULT_MIN_IOU,
                "ambiguity_margin": ft.DEFAULT_AMBIGUITY_MARGIN,
                "size_tolerance": ft.DEFAULT_SIZE_TOLERANCE,
                "center_tolerance": ft.DEFAULT_CENTER_TOLERANCE,
                "max_gap_frames": ft.DEFAULT_MAX_GAP_FRAMES,
                "reentry_window_s": ft.DEFAULT_REENTRY_WINDOW_S,
                "uncertain_crossing_policy": "new_track_flagged",
            },
            "haar_fallback": False,
            "haar_note": HAAR_FALLBACK_NOTE,
            "commercial_use": LICENSE.commercial_use,
        }

    def resource_requirements(self) -> ResourceSpec:
        return ResourceSpec(
            gpu=False,
            vram_mb=0,
            ram_mb=512,
            model_bytes=ESTIMATED_MODEL_BYTES,
            cpu_seconds_per_audio_minute=0.0,
            notes=(
                "CPU-only per-frame inference; cost is time x frames, not GPU time "
                "(contract §1.4: this provider is never PERMITTED for commercial use "
                "until its model terms are written down)"
            ),
        )

    def license_info(self) -> LicenseInfo:
        """Code AND model terms, exactly as the §1.4 audit recorded them."""
        return LICENSE

    # -- work -----------------------------------------------------------

    def _resolve_detector(self) -> Any:
        """The injected double, or a freshly built real MediaPipe detector."""
        if self._detector is not None:
            return self._detector
        try:
            return build_detector(
                model_selection=self._model_selection,
                min_detection_confidence=self._min_confidence,
            )
        except ImportError as exc:
            raise ProviderUnavailable(
                f"{BACKEND_PACKAGE} backend unavailable: {type(exc).__name__}"
            ) from exc

    def run(
        self,
        request: IntelRequest,
        *,
        progress: ProgressFn,
        should_cancel: CancelFn,
        deadline: float | None,
    ) -> ProviderResult:
        """Detect + track over the request's media file; writes nothing but tracks."""
        params: Mapping[str, Any] = dict(request.params or {})
        detector = self._resolve_detector()
        policy = ft.TrackParams.from_params(params)
        sample_fps = ft._clamp_float(
            params.get("sample_fps"),
            ft.DEFAULT_SAMPLE_FPS,
            ft.MIN_SAMPLE_FPS,
            ft.MAX_SAMPLE_FPS,
        )
        check_control(should_cancel, deadline)
        progress(0.0)
        started = time.perf_counter()
        result = ft.analyze(
            request.storage_path,
            detector=detector,
            fps=sample_fps,
            params=policy,
            progress=lambda fraction: progress(0.05 + 0.9 * max(0.0, min(1.0, fraction))),
            should_cancel=should_cancel,
            deadline=deadline,
        )
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        metrics = result.metrics()
        metrics["elapsed_ms"] = elapsed_ms
        metrics["min_detection_confidence"] = self._min_confidence
        metrics["backend"] = "injected" if self._detector is not None else BACKEND_PACKAGE
        return ProviderResult(
            ok=True,
            artifacts={"face_tracks": {"payload": result.payload()}},
            metrics=metrics,
            warnings=list(result.warnings),
        )

    def cost(self, spec: ResourceSpec) -> dict:
        """CPU-only local inference: no GPU slot, no billed provider."""
        return {
            "gpu_ms": 0,
            "cpu_ms": 0,
            "cost_micros": 0,
            "billed": False,
            "model_bytes": int(spec.model_bytes or 0),
            "note": "local CPU inference; elapsed time is measured per run, not billed",
        }


#: the registry loads this symbol first (contracts §1.1)
PROVIDER = MediaPipeFaceProvider


def detect_boxes(detector: Any, frame_path: str, width: int, height: int) -> Sequence[ft.Detection]:
    """Normalised boxes of one frame for any detector backend (test seam)."""
    return ft.detect_frame(detector, frame_path, width, height)


__all__ = [
    "BACKEND_PACKAGE",
    "DEFAULT_MIN_DETECTION_CONFIDENCE",
    "DEFAULT_MODEL_SELECTION",
    "FRAME_READER_PACKAGE",
    "HAAR_FALLBACK_NOTE",
    "KEYPOINT_NAMES",
    "LICENSE",
    "LICENSE_REASON",
    "PROVIDER",
    "PROVIDER_KEY",
    "PROVIDER_KIND",
    "MediaPipeFaceProvider",
    "build_detector",
    "detect_boxes",
]
