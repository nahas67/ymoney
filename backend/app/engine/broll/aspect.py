"""Orientation verification for stock material (Work 15.5).

A vertical job that receives a horizontal clip does not raise: the compositor
crops it, the subject leaves the frame, and the defect reaches the viewer as
"why is this video half empty". Search endpoints take an ``orientation``
parameter, but it is a *hint*: providers rank loosely, and third-party or
cached responses drift. So orientation is decided from the asset's own pixels
and re-checked at the point the rendition is chosen.

Provider payloads are not uniform. Pexels reports ``width``/``height`` on
every rendition; Pixabay reports them on the variant; Coverr's older responses
carry only an ``is_vertical`` boolean. Precedence is therefore: measured
dimensions first, an explicit orientation flag second, and nothing else. An
asset whose orientation cannot be established is **not** a match — accepting it
would reintroduce exactly the silent mismatch this module exists to prevent,
and "probably fine" is not a thing the compositor can act on.
"""

from __future__ import annotations

#: Orientation vocabulary. ``UNKNOWN`` is a real state, not an alias for
#: landscape: it means "nobody has established this", and it never matches.
PORTRAIT = "portrait"
LANDSCAPE = "landscape"
SQUARE = "square"
UNKNOWN = "unknown"

#: Frame requests we can verify. Anything else is a caller contract this
#: module has no opinion about (``frame_kind`` reports it as ``UNKNOWN``).
_FRAME_KINDS: dict[str, str] = {
    "9:16": PORTRAIT,
    "16:9": LANDSCAPE,
    "1:1": SQUARE,
}


def frame_kind(aspect: str) -> str:
    """The orientation a frame request implies (``"9:16"`` -> ``"portrait"``).

    Unrecognised requests report :data:`UNKNOWN` so the caller can decide to
    keep its legacy behaviour instead of being silently forced into a guess.
    """
    text = str(aspect or "").strip().lower().replace(" ", "")
    if text in ("9:16", "0.5625:1", "vertical", "portrait"):
        return PORTRAIT
    if text in ("16:9", "1.777:1", "horizontal", "landscape"):
        return LANDSCAPE
    if text in ("1:1", "square"):
        return SQUARE
    return _FRAME_KINDS.get(text, UNKNOWN)


def pixel_size(value: object) -> int:
    """Coerce one provider dimension to a positive int, else 0.

    Providers send ints, numeric strings and floats interchangeably, and a
    missing dimension is reported as 0 rather than guessed. ``bool`` is
    rejected explicitly because ``isinstance(True, int)`` is true in Python
    and a stray flag must not become a 1-pixel dimension.
    """
    if value is None or isinstance(value, bool):
        return 0
    try:
        number = int(float(value))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0
    return number if number > 0 else 0


def orientation_of(width: object, height: object, is_vertical: object = None) -> str:
    """Derive an asset's orientation, or :data:`UNKNOWN` when it cannot be."""
    w, h = pixel_size(width), pixel_size(height)
    if w > 0 and h > 0:
        if h > w:
            return PORTRAIT
        if w > h:
            return LANDSCAPE
        return SQUARE
    if isinstance(is_vertical, bool):
        return PORTRAIT if is_vertical else LANDSCAPE
    return UNKNOWN


def matches_aspect(width: object, height: object, aspect: str,
                   *, is_vertical: object = None) -> bool:
    """True only when the asset's orientation is known *and* matches ``aspect``.

    ``aspect`` is a frame request (``"9:16"``). Square is the one frame with
    no boolean fallback: a provider's ``is_vertical`` cannot say "1:1", and
    guessing one of the two is the mismatch we are here to prevent. An asset
    with no usable dimensions and no flag is :data:`UNKNOWN`, which is a
    non-match by construction.
    """
    kind = frame_kind(aspect)
    if kind == UNKNOWN:
        return False
    found = orientation_of(width, height, is_vertical)
    if found == UNKNOWN:
        return False
    return found == kind
