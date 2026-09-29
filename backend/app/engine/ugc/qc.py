"""UGC + avatar quality gates (Work 07 Lane C).

Two reports, one status vocabulary (PASS | PASS_WITH_WARNINGS |
REVIEW_REQUIRED | FAIL):

  * `UGCQCReport`    — hook present, product assets actually used, CTA, claim
    safety (testimonial quotes must trace to user-supplied `source_quote`
    material; numeric/absolute claims missing from the brief → REVIEW_REQUIRED)
    and timeline completeness.
  * `AvatarQCReport` — missing face, invalid/corrupt/black output frames,
    A/V duration mismatch, and a lip-sync failure flag when Lane B's job
    lineage recorded one (ffprobe/ffmpeg used only when a file exists).

Localization QC is NOT here (Lane A owns it). Reports are stored on
`ugc_projects.qc_json` / `avatar_outputs.lineage_json` — no third QC table.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass, field

from app.engine.campaign.qc import HOOK_WINDOW_SECONDS

QC_STATUSES = ("PASS", "PASS_WITH_WARNINGS", "REVIEW_REQUIRED", "FAIL")
_CHECK_ORDER = {"fail": 3, "review": 2, "warning": 1, "pass": 0}
_STATUS_FOR = {"fail": "FAIL", "review": "REVIEW_REQUIRED",
               "warning": "PASS_WITH_WARNINGS", "pass": "PASS"}

# numeric claims the generator must NOT invent: digits + unit/percent/money
# NOTE: `%` is a non-word char — a trailing \b after it never matches, so the
# percent branch ends without a boundary (word alternations keep their own \b).
NUMERIC_CLAIM_RE = re.compile(
    r"(?:"
    r"\$\s?\d[\d,]*(?:\.\d+)?"
    r"|\b\d[\d,]*(?:\.\d+)?\s?(?:%|percent\b|usd\b|x\b|times\b|days?\b|hours?\b"
    r"|weeks?\b|months?\b|years?\b|minutes?\b)"
    r"|\b\d[\d,]*(?:\.\d+)?\s?faster\b"
    r")",
    re.IGNORECASE,
)

# absolute/superlative marketing claims that need source material behind them
ABSOLUTE_CLAIM_TERMS = (
    "guaranteed", "guarantee", "risk-free", "risk free", "100%", "no side effects",
    "always works", "never fails", "money back", "money-back", "miracle",
    "instant results", "cure", "clinically proven", "best in the world",
    "perfect for everyone",
)

QUOTE_RE = re.compile(r"[\"“”\"‘’](.+?)[\"“”\"‘’]", re.DOTALL)

# attribution cues that turn a sentence into a testimonial claim
ATTRIBUTION_RE = re.compile(
    r"\b(said|says|reviewer|testimonial|according to|one customer|a user)\b",
    re.IGNORECASE,
)

_WS_RE = re.compile(r"\s+")


def _norm(text: str) -> str:
    return _WS_RE.sub(" ", (text or "")).strip().lower()


def _check(status: str, detail: str = "") -> dict:
    return {"status": status, "detail": detail}


def _rollup(checks: dict) -> str:
    worst = max((c.get("status", "pass") for c in checks.values()),
                key=lambda s: _CHECK_ORDER.get(s, 0), default="pass")
    return _STATUS_FOR[worst]


# ---------------------------------------------------------------------------
# claim safety
# ---------------------------------------------------------------------------


def source_corpus(brief: dict | None) -> str:
    """Every user-supplied string in the brief — the ONLY claim backing."""
    parts: list[str] = []

    def walk(node) -> None:
        if isinstance(node, str):
            parts.append(node)
        elif isinstance(node, dict):
            for k, v in node.items():
                parts.append(str(k))
                walk(v)
        elif isinstance(node, (list, tuple)):
            for v in node:
                walk(v)

    walk(brief or {})
    return _norm(" \n ".join(parts))


def extract_claims(text: str) -> list[dict]:
    """Numeric + absolute claims found in generated text."""
    src = text or ""
    out = [{"kind": "numeric", "text": m.group(0).strip()}
           for m in NUMERIC_CLAIM_RE.finditer(src)]
    low = src.lower()
    for term in ABSOLUTE_CLAIM_TERMS:
        if term in low:
            out.append({"kind": "absolute", "text": term})
    return out


def unsupported_claims(script: str, brief: dict | None) -> list[dict]:
    """Generated numeric/absolute claims with no backing in the user brief."""
    corpus = source_corpus(brief)
    unsupported = []
    for claim in extract_claims(script or ""):
        needle = _norm(claim["text"])
        # a claim counts as sourced when its number/term appears in the brief
        digits = re.findall(r"\d+(?:\.\d+)?", claim["text"].replace(",", ""))
        if digits:
            backed = bool(corpus) and all(d in corpus for d in digits)
        else:
            backed = bool(corpus) and needle in corpus
        if not backed:
            unsupported.append(claim)
    return unsupported


def extract_quotes(text: str) -> list[str]:
    return [q.strip() for q in QUOTE_RE.findall(text or "") if q.strip()]


def _traces(quote: str, source: str) -> bool:
    q, s = _norm(quote), _norm(source)
    if not q or not s:
        return False
    if q in s or s in q:
        return True
    qt, st = set(q.split()), set(s.split())
    if not qt:
        return False
    return len(qt & st) / max(1, len(qt)) >= 0.6


def testimonial_claims(script: str, brief: dict | None) -> list[dict]:
    """Testimonial quotes in the script that do NOT trace to a user-supplied
    `source_quote` in brief_json. Generated claims without a source are a
    hard QC FAIL (never published, never silently dropped)."""
    brief = brief or {}
    entries = brief.get("testimonials")
    if not isinstance(entries, list):
        entries = []
    sources = [str(e.get("source_quote") or "") for e in entries if isinstance(e, dict)]
    sources = [s for s in sources if s.strip()]
    out: list[dict] = []
    for quote in extract_quotes(script or ""):
        if not any(_traces(quote, s) for s in sources):
            out.append({"kind": "testimonial", "text": quote,
                        "reason": "quote has no matching source_quote in brief_json"})
    # attribution sentences ("... said a customer") are testimonial claims too
    for sentence in re.split(r"(?<=[.!?])\s+", (script or "").strip()):
        if not ATTRIBUTION_RE.search(sentence):
            continue
        if any(_traces(sentence, s) for s in sources):
            continue
        if any(_traces(q, s) for q in extract_quotes(sentence) for s in sources):
            continue
        out.append({"kind": "testimonial", "text": sentence.strip(),
                    "reason": "attributed claim has no source_quote in brief_json"})
    # de-duplicate (a quoted attributed sentence can hit both passes)
    seen, dedup = set(), []
    for c in out:
        key = c["text"].lower()
        if key not in seen:
            seen.add(key)
            dedup.append(c)
    return dedup


# ---------------------------------------------------------------------------
# UGC report
# ---------------------------------------------------------------------------


@dataclass
class UGCQCReport:
    status: str = "PASS"
    checks: dict = field(default_factory=dict)
    preset: str = ""

    def to_dict(self) -> dict:
        return {"status": self.status, "checks": self.checks, "preset": self.preset,
                "report_type": "ugc"}

    @classmethod
    def from_dict(cls, data: dict | None) -> UGCQCReport:
        raw = dict(data or {})
        status = str(raw.get("status") or "PASS")
        return cls(status=status if status in QC_STATUSES else "PASS",
                   checks=dict(raw.get("checks") or {}),
                   preset=str(raw.get("preset") or ""))

    @property
    def blocking(self) -> bool:
        return self.status == "FAIL"


def _visual_refs(doc: dict) -> list[str]:
    """Every string referenced by a visual clip (asset ids + raw refs)."""
    out: list[str] = []
    for tr in (doc or {}).get("tracks", []):
        if tr.get("kind") not in ("broll", "video", "avatar"):
            continue
        for clip in tr.get("clips", []) or []:
            source = clip.get("source") or {}
            if not isinstance(source, dict):
                continue
            for key in ("asset_id", "ref", "storage_key", "file_path", "asset_ref"):
                val = source.get(key)
                if val:
                    out.append(str(val))
            for val in source.values():
                if isinstance(val, str) and val and not val.startswith("{"):
                    out.append(val)
    return out


def _declared_product_assets(brief: dict | None) -> list[str]:
    brief = brief or {}
    declared = brief.get("product_assets")
    if not isinstance(declared, list):
        product = brief.get("product")
        declared = (product or {}).get("assets") if isinstance(product, dict) else []
    out: list[str] = []
    for entry in declared or []:
        if isinstance(entry, str):
            out.append(entry)
        elif isinstance(entry, dict):
            for key in ("asset_id", "ref", "storage_key", "id", "path", "name"):
                val = entry.get(key)
                if val:
                    out.append(str(val))
                    break
    return out


def _track_clips(doc: dict, kind: str) -> list[dict]:
    for tr in (doc or {}).get("tracks", []):
        if tr.get("kind") == kind:
            return list(tr.get("clips") or [])
    return []


def _label(clip: dict) -> str:
    return f"{clip.get('id', '')} {clip.get('name', '')}".lower()


def run_ugc_qc(*, preset: str = "", brief: dict | None = None,
               script: str = "", doc: dict | None = None,
               file_meta: dict | None = None) -> UGCQCReport:
    """Full UGC gate over the generated script + canonical timeline."""
    brief = dict(brief or {})
    doc = dict(doc or {})
    checks: dict[str, dict] = {}

    # 1) hook lands in the opening window
    hook_ok = any(
        "hook" in _label(c) and float(c.get("start", 99.0)) <= HOOK_WINDOW_SECONDS
        for c in _track_clips(doc, "text") + _track_clips(doc, "caption")
    )
    checks["hook_present"] = (
        _check("pass", f"hook within {HOOK_WINDOW_SECONDS}s") if hook_ok
        else _check("fail", "no hook clip in the first 3.5s")
    )

    # 2) product assets: declared ones must actually appear in the timeline
    declared = _declared_product_assets(brief)
    used = _visual_refs(doc)
    used_blob = " ".join(used).lower()
    missing = [d for d in declared
               if d.lower() not in used_blob and not any(d.lower() in u.lower() for u in used)]
    if not declared:
        checks["product_assets"] = _check(
            "warning", "no product assets supplied in the brief")
    elif missing:
        checks["product_assets"] = _check(
            "fail", f"{len(missing)} declared product asset(s) not in timeline: {missing[:3]}")
    else:
        checks["product_assets"] = _check(
            "pass", f"{len(declared)} declared product asset(s) present")

    # 3) CTA
    cta_text = _norm(str(brief.get("cta") or ""))
    labelled = any(
        "cta" in _label(c)
        for c in _track_clips(doc, "text") + _track_clips(doc, "caption")
    )
    in_script = bool(cta_text) and cta_text in _norm(script)
    checks["cta_present"] = (
        _check("pass", "CTA clip present" if labelled else "CTA text in script")
        if (labelled or in_script) else _check("fail", "no CTA clip or CTA text found")
    )

    # 4) claim safety
    untraced = testimonial_claims(script, brief)
    numeric = unsupported_claims(script, brief)
    testimonial_style = preset == "TESTIMONIAL" or isinstance(brief.get("testimonials"), list)
    sources = [t for t in (brief.get("testimonials") or [])
               if isinstance(t, dict) and str(t.get("source_quote") or "").strip()]
    if testimonial_style and not sources:
        checks["testimonial_sources"] = _check(
            "warning", "testimonial-style brief carries no source_quote material")
    elif testimonial_style:
        checks["testimonial_sources"] = _check(
            "pass", f"{len(sources)} testimonial source_quote(s) supplied")
    if untraced:
        checks["unsupported_claims"] = _check(
            "fail",
            f"{len(untraced)} testimonial claim(s) without source_quote: "
            f"{[u['text'][:60] for u in untraced[:3]]}")
    elif numeric:
        checks["unsupported_claims"] = _check(
            "review",
            f"{len(numeric)} unsupported numeric/absolute claim(s) not in brief source: "
            f"{[n['text'] for n in numeric[:5]]}")
    else:
        checks["unsupported_claims"] = _check("pass", "all claims trace to brief source")

    # 5) timeline completeness
    problems: list[str] = []
    try:
        from app.engine.timeline import validate_timeline

        validate_timeline(doc)
    except Exception as exc:  # noqa: BLE001 — report, never raise from QC
        problems.append(f"validation: {exc}")
    if float(doc.get("duration_seconds") or 0.0) <= 0:
        problems.append("zero duration")
    if not _track_clips(doc, "voice"):
        problems.append("no voice clips")
    if not _track_clips(doc, "caption"):
        problems.append("no caption clips")
    if not any(_track_clips(doc, k) for k in ("broll", "video", "avatar")):
        problems.append("no visual clips")
    checks["timeline_complete"] = (
        _check("pass", "tracks, captions and duration present") if not problems
        else _check("fail", "; ".join(problems))
    )

    # 6) optional post-render file facts
    if file_meta:
        has_audio = file_meta.get("has_audio")
        checks["render"] = (
            _check("pass", "audio present in render") if has_audio
            else _check("warning", "render audio presence unknown")
        )

    return UGCQCReport(status=_rollup(checks), checks=checks, preset=preset)


# ---------------------------------------------------------------------------
# avatar report
# ---------------------------------------------------------------------------


def _ffprobe(path: str) -> dict | None:
    if not shutil.which("ffprobe"):
        return None
    try:
        proc = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json",
             "-show_format", "-show_streams", str(path)],
            capture_output=True, text=True, timeout=30,
        )
        import json as _json

        return _json.loads(proc.stdout or "{}") or None
    except Exception:  # noqa: BLE001 — probe is best-effort
        return None


def _black_fraction(path: str) -> float | None:
    """Share of the clip flagged by blackdetect (None when unavailable)."""
    if not shutil.which("ffmpeg"):
        return None
    try:
        proc = subprocess.run(
            ["ffmpeg", "-hide_banner", "-i", str(path),
             "-vf", "blackdetect=d=0.2:pix_th=0.05", "-an", "-f", "null", "-"],
            capture_output=True, text=True, timeout=120,
        )
        total = 0.0
        black = 0.0
        for m in re.finditer(r"black_start:([\d.]+) black_end:([\d.]+)", proc.stderr or ""):
            start, end = float(m.group(1)), float(m.group(2))
            black += max(0.0, end - start)
        fmt = re.search(r"Duration: (\d+):(\d+):(\d+\.\d+)", proc.stderr or "")
        if fmt:
            total = int(fmt.group(1)) * 3600 + int(fmt.group(2)) * 60 + float(fmt.group(3))
        if total <= 0:
            return None
        return min(1.0, black / total)
    except Exception:  # noqa: BLE001 — best-effort
        return None


def _lip_sync_failed(lineage: dict) -> tuple[bool, str]:
    """Did Lane B's job lineage record a lip-sync failure?"""
    lineage = dict(lineage or {})
    for key in ("lip_sync", "lipsync", "lip_sync_job"):
        job = lineage.get(key)
        if isinstance(job, dict):
            state = str(job.get("status") or job.get("state") or "").upper()
            if state in ("FAILED", "ERROR", "CANCELLED"):
                return True, f"{key} job status={state}"
    for job in lineage.get("jobs") or []:
        if not isinstance(job, dict):
            continue
        kind = str(job.get("type") or job.get("kind") or job.get("name") or "").lower()
        state = str(job.get("status") or job.get("state") or "").upper()
        if "lip" in kind and state in ("FAILED", "ERROR", "CANCELLED"):
            return True, f"{kind} job status={state}"
    return False, "no lip-sync failure recorded"


@dataclass
class AvatarQCReport:
    status: str = "PASS"
    checks: dict = field(default_factory=dict)
    flags: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"status": self.status, "checks": self.checks, "flags": self.flags,
                "report_type": "avatar"}

    @classmethod
    def from_dict(cls, data: dict | None) -> AvatarQCReport:
        raw = dict(data or {})
        status = str(raw.get("status") or "PASS")
        return cls(status=status if status in QC_STATUSES else "PASS",
                   checks=dict(raw.get("checks") or {}),
                   flags=dict(raw.get("flags") or {}))

    @property
    def blocking(self) -> bool:
        return self.status == "FAIL"


def run_avatar_qc(*, output_path: str = "", audio_duration: float | None = None,
                  lineage: dict | None = None, expected_duration: float | None = None,
                  probe_black: bool = True) -> AvatarQCReport:
    """QC one avatar output. Every probe degrades to a WARNING when its tool
    is missing — only a measured failure becomes FAIL."""
    import os

    lineage = dict(lineage or {})
    checks: dict[str, dict] = {}
    flags: dict[str, bool] = {}

    exists = bool(output_path) and os.path.exists(output_path)
    probe = _ffprobe(output_path) if exists else None

    # consent snapshot (defensive: outputs must be authorized)
    consent_state = str((lineage.get("consent") or {}).get("state") or "")
    checks["consent"] = (
        _check("pass", "authorized at render time") if consent_state == "authorized"
        else _check("fail", f"consent state '{consent_state or 'missing'}' is not authorized")
        if consent_state else _check("warning", "no consent snapshot in lineage")
    )

    # output file: present, decodable, has frames
    if not exists:
        checks["output_file"] = _check("fail", "output file missing")
        duration = None
    elif probe is None:
        checks["output_file"] = _check("warning", "ffprobe unavailable — file not verified")
        duration = None
    else:
        fmt = probe.get("format") or {}
        try:
            duration = float(fmt.get("duration") or 0.0) or None
        except (TypeError, ValueError):
            duration = None
        streams = probe.get("streams") or []
        has_video = any(s.get("codec_type") == "video" for s in streams)
        width = next((int(s.get("width") or 0) for s in streams if s.get("codec_type") == "video"), 0)
        height = next((int(s.get("height") or 0) for s in streams if s.get("codec_type") == "video"), 0)
        if not has_video or not width or not height:
            checks["output_file"] = _check(
                "fail", f"invalid frames (video={has_video} {width}x{height})")
        elif not duration or duration <= 0:
            checks["output_file"] = _check("fail", "unmeasurable duration — corrupt output")
        else:
            checks["output_file"] = _check("pass", f"{width}x{height} {duration:.2f}s")

    # face present (detector info when supplied; backend-enforced otherwise)
    detection = lineage.get("face_detection")
    if isinstance(detection, dict) and "detected" in detection:
        checks["face_present"] = (
            _check("pass", "face detected in source portrait") if detection.get("detected")
            else _check("fail", detection.get("detail") or "no face found in source portrait")
        )
    elif str(lineage.get("backend") or "") == "mock":
        checks["face_present"] = _check("warning", "mock output — face presence not verified")
    else:
        checks["face_present"] = _check(
            "pass", f"backend '{lineage.get('backend') or 'unknown'}' enforces face detection "
                    f"(render succeeded)")

    # black / fully-dark output
    if exists and probe_black and shutil.which("ffmpeg"):
        frac = _black_fraction(output_path)
        if frac is None:
            checks["black_output"] = _check("warning", "blackdetect unavailable")
        elif frac >= 0.9:
            checks["black_output"] = _check("fail", f"{frac:.0%} of output is black")
        else:
            checks["black_output"] = _check("pass", f"{frac:.0%} black (within tolerance)")
    else:
        checks["black_output"] = _check(
            "warning", "black-frame check skipped (no file or ffmpeg)")

    # A/V duration mismatch (driving audio vs rendered video)
    ref = audio_duration if audio_duration else expected_duration
    try:
        ref_f = float(ref) if ref else None
    except (TypeError, ValueError):
        ref_f = None
    if duration and ref_f:
        delta = abs(float(duration) - ref_f)
        if delta > max(1.5, 0.25 * ref_f):
            checks["av_duration"] = _check(
                "fail", f"duration mismatch {delta:.2f}s vs driving audio {ref_f:.2f}s")
        elif delta > 0.5:
            checks["av_duration"] = _check("warning", f"duration drift {delta:.2f}s")
        else:
            checks["av_duration"] = _check("pass", f"within {delta:.2f}s of audio")
    else:
        checks["av_duration"] = _check(
            "warning", "driving audio duration unknown — A/V match not verified")

    # lip-sync failure flag (Lane B job lineage)
    failed, detail = _lip_sync_failed(lineage)
    flags["lip_sync_failure"] = failed
    checks["lip_sync"] = _check("fail", detail) if failed else _check("pass", detail)

    return AvatarQCReport(status=_rollup(checks), checks=checks, flags=flags)


def probe_has_audio(path: str) -> bool | None:
    """True/False when ffprobe can read the file; None when unknown.

    Best-effort post-render fact for `run_ugc_qc(file_meta=...)` — a missing
    file or missing ffprobe degrades to None (QC warns, never invents).
    """
    import os

    if not path or not os.path.exists(path):
        return None
    probe = _ffprobe(path)
    if probe is None:
        return None
    streams = probe.get("streams") or []
    if not streams:
        return None
    return any(s.get("codec_type") == "audio" for s in streams)


__all__ = [
    "AvatarQCReport",
    "QC_STATUSES",
    "UGCQCReport",
    "extract_claims",
    "extract_quotes",
    "probe_has_audio",
    "run_avatar_qc",
    "run_ugc_qc",
    "source_corpus",
    "testimonial_claims",
    "unsupported_claims",
]
