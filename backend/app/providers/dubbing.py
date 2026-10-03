"""Video dubbing pipeline (OpenCreator-inspired): translate → bilingual → dub.

Flow: source video → transcript/SRT → LLM translation (with terminology
context) → per-segment TTS in the target language → timing fit (atempo/pad)
→ FFmpeg assembly (portrait crop + bilingual subtitle burn + dubbed mix over
ducked original).

All heavy steps degrade honestly: no LLM key → DubError with remediation,
no TTS voice for the language → fallback voice + warning, no ffmpeg → 503.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from loguru import logger

from app.providers.llm import LLMCompletionError, LLMError
from app.services import provider_settings
from app.services.cost import BudgetExceededError
from app.services.paid_jobs import is_safe_to_retry
from app.services.storage import STORAGE_ROOT

# ISO-639-1 → edge-TTS locale prefix. First listed voice wins.
LANG_LOCALES: dict[str, list[str]] = {
    "es": ["es-"], "fr": ["fr-"], "de": ["de-"], "pt": ["pt-"],
    "hi": ["hi-"], "ar": ["ar-"], "id": ["id-"], "tr": ["tr-"],
    "ru": ["ru-"], "ja": ["ja-"], "ko": ["ko-"], "zh": ["zh-"],
    "it": ["it-"], "nl": ["nl-"], "pl": ["pl-"], "uk": ["uk-"],
    "en": ["en-"],
}


class DubError(Exception):
    pass


@dataclass
class SrtCue:
    index: int
    start: float
    end: float
    text: str


def ffmpeg_present() -> bool:
    return bool(shutil.which("ffmpeg"))


def dry_run_dub(source: str, target_lang: str, voice: str = "",
                srt: str = "", bilingual: bool = True, portrait: bool = True) -> dict:
    """Validate a dub request without downloads, transcription, LLM, TTS or ffmpeg.

    OpenCreator `--dry-run` pattern: check command shape + capability matrix
    only. Never writes files, never calls providers. Returns per-step
    passed/failed/warn rows plus an overall `ok`.
    """
    from pathlib import Path as _Path

    checks: list[dict] = []

    def _row(step: str, status: str, detail: str = "") -> None:
        checks.append({"step": step, "status": status, "detail": detail})

    lang = (target_lang or "").lower().strip()
    if lang and lang in LANG_LOCALES:
        _row("language", "passed", lang)
    else:
        _row("language", "failed", f"unsupported target language: {target_lang!r}")

    src = (source or "").strip()
    if src.startswith(("http://", "https://")):
        from app.providers.clips import yt_dlp_available

        if yt_dlp_available():
            _row("source", "passed", "remote URL (download at run time)")
        else:
            _row("source", "failed", "yt-dlp not installed — URL sources unavailable")
    elif src and _Path(src).exists():
        _row("source", "passed", "local file present (media probed at run time)")
    else:
        _row("source", "failed", "source is neither a URL nor an existing file")

    srt_text = (srt or "").strip()
    if srt_text:
        cues = parse_srt(srt_text)
        if cues:
            _row("subtitles", "passed", f"{len(cues)} supplied cue(s), transcription skipped")
        else:
            _row("subtitles", "failed", "supplied SRT has no parseable cues")
    else:
        from app.providers.clips import whisper_available

        if whisper_available():
            _row("subtitles", "passed", "transcription at run time (faster-whisper)")
        else:
            _row("subtitles", "failed", "no SRT supplied and faster-whisper unavailable")

    from app.providers import llm as llm_mod

    if llm_mod.llm_available():
        _row("translation", "passed", f"{lang} via configured LLM")
    else:
        _row("translation", "failed", "LLM provider not configured")

    if (voice or "").strip():
        _row("voice", "passed", f"explicit voice '{voice.strip()[:60]}'")
    else:
        _row("voice", "warn", "auto-match at run time (voice list queried then)")

    try:
        from app.providers.tts import get_tts_provider

        prov = get_tts_provider()
        if prov.name == "mock":
            _row("tts", "failed", "mock TTS resolves — simulation only")
        else:
            _row("tts", "passed", f"provider '{prov.name}' resolves")
    except Exception as exc:
        _row("tts", "failed", f"TTS provider unavailable: {exc}")

    if ffmpeg_present():
        _row("assemble", "passed", f"ffmpeg present (bilingual={bool(bilingual)}, portrait={bool(portrait)})")
    else:
        _row("assemble", "failed", "ffmpeg not found")

    failed = [c for c in checks if c["status"] == "failed"]
    return {"ok": not failed, "checks": checks,
            "warns": [c for c in checks if c["status"] == "warn"]}


def dub_status() -> dict:
    from app.providers import llm as llm_mod
    from app.providers.tts import get_tts_provider

    tts_ok, tts_name = False, ""
    try:
        prov = get_tts_provider()
        tts_name = prov.name
        tts_ok = prov.name != "mock"
    except Exception:
        pass
    return {
        "ffmpeg": ffmpeg_present(),
        "llm": llm_mod.llm_available(),
        "tts": tts_ok,
        "tts_provider": tts_name,
        "languages": sorted(LANG_LOCALES),
        "ready": bool(ffmpeg_present() and llm_mod.llm_available() and tts_ok),
    }


# ---------------------------------------------------------------------------
# SRT utils (pure — fully unit-tested)
# ---------------------------------------------------------------------------

_TS_RE = re.compile(r"(\d+):(\d+):([\d.,]+)\s*-->\s*(\d+):(\d+):([\d.,]+)")


def _ts_to_seconds(h: str, m: str, s: str) -> float:
    return int(h) * 3600 + int(m) * 60 + float(s.replace(",", "."))


def _seconds_to_ts(sec: float) -> str:
    ms = max(0, int(sec * 1000))
    h, rem = divmod(ms, 3600_000)
    m, rem = divmod(rem, 60_000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def parse_srt(text: str) -> list[SrtCue]:
    """Parse SRT text into cues; malformed blocks are skipped, never fatal."""
    cues: list[SrtCue] = []
    for block in (text or "").replace("\r\n", "\n").split("\n\n"):
        lines = [ln.strip() for ln in block.strip().splitlines() if ln.strip() != ""]
        if len(lines) < 2:
            continue
        m = _TS_RE.search(lines[1] if lines[0].isdigit() else lines[0])
        if not m:
            continue
        start = _ts_to_seconds(*m.groups()[:3])
        end = _ts_to_seconds(*m.groups()[3:])
        body = " ".join(lines[2:] if lines[0].isdigit() else lines[1:])
        if end > start and body:
            cues.append(SrtCue(index=len(cues) + 1, start=start, end=end, text=body))
    return cues


def format_srt(cues: list[SrtCue]) -> str:
    out: list[str] = []
    for i, c in enumerate(cues, 1):
        out.append(f"{i}\n{_seconds_to_ts(c.start)} --> {_seconds_to_ts(c.end)}\n{c.text.strip()}\n")
    return "\n".join(out)


def to_bilingual(original: list[SrtCue], translated: list[str]) -> list[SrtCue]:
    """Merge original + translated lines into bilingual cues (original first)."""
    out: list[SrtCue] = []
    for cue, trans in zip(original, translated, strict=False):
        t = (trans or "").strip()
        text = f"{cue.text.strip()}\n{t}" if t else cue.text.strip()
        out.append(SrtCue(index=cue.index, start=cue.start, end=cue.end, text=text))
    return out


def fit_ratio(audio_seconds: float, window_seconds: float) -> float:
    """atempo ratio to fit dubbed audio into its cue window.

    Clamped to [1.0, 1.35]: never slow down, never chipmunk past 1.35x —
    overflow beyond that is trimmed by the assembler instead.
    """
    if window_seconds <= 0:
        return 1.0
    if audio_seconds <= window_seconds:
        return 1.0
    return round(min(1.35, audio_seconds / window_seconds), 3)


# ---------------------------------------------------------------------------
# Translation (LLM) + synthesis (TTS layer)
# ---------------------------------------------------------------------------

#: Segments per translation REQUEST. The provider receives ONE combined
#: completion per batch, so this is also the unit the budget is accounted in.
TRANSLATION_BATCH_SIZE = 20

#: ``max_tokens`` handed to the completion, and therefore a real upper bound on
#: the output side of the estimate.
TRANSLATION_MAX_TOKENS = 1500

#: Upper bound on characters per token. The vendor owns the tokenizer and will
#: not tell us before the call, so the input side is a bound, never a
#: measurement -- the same honest position ``llm_paid.estimate_request_cost``
#: takes.
_CHARS_PER_TOKEN = 4

#: The ledger category this spend belongs to. "llm", not a private category, so
#: a dub costs against the same daily cap as everything else the workspace buys.
TRANSLATION_CATEGORY = "llm"


def translation_request_count(n_segments: int) -> int:
    """How many billable REQUESTS a run of ``n_segments`` will make.

    Exposed so a caller can budget the whole run before starting it, which is
    the question the audit's "each batch bills separately" note keeps raising.
    """
    return -(-max(0, int(n_segments)) // TRANSLATION_BATCH_SIZE)


def estimate_translation_request_cost(system: str, user: str, *,
                                     model: str = "",
                                     max_tokens: int = TRANSLATION_MAX_TOKENS) -> float:
    """Pre-spend estimate for ONE combined translation request.

    The unit is the request, deliberately. A twenty-segment batch is a single
    POST; pricing it per segment would reserve (and therefore refuse) twenty
    times what the call actually costs, and a budget gate that over-reserves by
    the batch size is a gate that blocks work which fits.
    """
    from app.services.cost import estimate_llm_cost

    prompt_tokens = -(-(len(system or "") + len(user or "")) // _CHARS_PER_TOKEN)
    return float(estimate_llm_cost(model or "", prompt_tokens, int(max_tokens)))


def _translation_model() -> str:
    """The model the ``cheap`` tier will actually use, for the estimate only."""
    try:
        from app.core.config import settings
        from app.providers.llm import _effective

        eff = _effective()
        return str((eff.get("tiers") or {}).get("cheap") or eff.get("model")
                   or settings.llm_model or "")
    except Exception as exc:  # noqa: BLE001 - a price hint must never fail a dub
        logger.debug(f"[dub] translation price hint unavailable: {exc}")
        return ""


def translation_request_payload(batch: list[str], *, target_lang: str,
                                source_lang: str = "auto",
                                glossary: dict[str, str] | None = None
                                ) -> tuple[str, str]:
    """The ``(system, user)`` pair for ONE combined translation request.

    Exists so the estimate and the request are built from the SAME strings. A
    budget gate that prices a different prompt than it sends is a gate priced
    off the wrong number, and nothing would notice until the ledger disagreed
    with the invoice.
    """
    gloss = ""
    if glossary:
        pairs = "; ".join(f"{k} → {v}" for k, v in list(glossary.items())[:20])
        gloss = f"\nTerminology (must be honored): {pairs}"
    system = (
        f"You translate short-form video subtitles to {target_lang}. Keep each line "
        f"punchy and speakable, preserve numbers/names, no preamble in output."
        f"{gloss} Return JSON: {{\"lines\": [\"...\", ...]}} in the same order."
    )
    import json as _json

    return system, _json.dumps({"source_lang": source_lang, "lines": list(batch)})


def translate_segments(texts: list[str], target_lang: str, workspace_id: str = "",
                       glossary: dict[str, str] | None = None,
                       source_lang: str = "auto") -> list[str]:
    """Translate subtitle lines; index-aligned output, same length as input.

    **Every batch is budgeted, per REQUEST (Work 15.8 §5).** The sequence per
    batch is estimate -> reserve -> request -> close the book in place, and a
    reservation is refused BEFORE anything is sent. ``ceil(N/20)`` segments
    therefore cost ``ceil(N/20)`` reservations, never ``N``: the provider is
    asked one question per batch, so that is what the workspace is billed for and
    what the cap must see.

    What the reservation is NOT: a guarantee that one POST happens.
    ``providers.llm.complete_json`` may re-ask once when a reply is unparseable
    (a deliberate SECOND billable request, documented at ``llm.py:198`` and
    counted on ``LLMResult.attempts``). That decision is made inside the
    provider, after this function's reservation has been spent, so no caller can
    pre-reserve it. Reserving for the worst case instead would over-reserve every
    batch that did not need it.

    **Work 15.9 §1: an empty ``workspace_id`` is now refused, not skipped.** It
    used to be passed through as ``""``, where the shared helper logged a
    warning and returned without reserving: a real billable POST that no cap
    ever saw. ``paid_operation.authorize`` raises
    :class:`~app.services.paid_provider.OwnerlessSpendRefused` for that case, and
    it surfaces here as a :class:`DubError` naming the missing owner.
    """
    from app.providers import llm as llm_mod
    from app.services.paid_executor import IdempotencySupport, Reconciliation
    from app.services.paid_provider import paid_operation

    if not texts:
        return []
    if not llm_mod.llm_available():
        raise DubError("LLM provider not configured — add an API key under Settings → Connections")
    if not str(workspace_id or "").strip():
        raise DubError(
            "translation has no workspace to authorise against; refusing "
            "before any batch is sent. Pass the owning workspace id (the "
            "localization row's, the job context's, the request's) instead of "
            "an empty string."
        )
    model = _translation_model()
    out: list[str] = []
    batches = translation_request_count(len(texts))
    for batch_index, batch_start in enumerate(
            range(0, len(texts), TRANSLATION_BATCH_SIZE)):
        batch = texts[batch_start:batch_start + TRANSLATION_BATCH_SIZE]
        system, user = translation_request_payload(
            batch, target_lang=target_lang, source_lang=source_lang,
            glossary=glossary)
        paid = paid_operation(
            provider="openai_compatible_llm",
            operation="dubbing.translate_batch",
            workspace_id=str(workspace_id).strip(),
            category=TRANSLATION_CATEGORY,
            estimated_cost=estimate_translation_request_cost(
                system, user, model=model),
            # OpenAI-compatible gateways document no idempotency header; the
            # reservation plus the refusal below are the protection.
            idempotency=IdempotencySupport.UNSUPPORTED,
            # A chat completion has no remote id and no status endpoint, so the
            # remedy is a human decision about the work, not a lookup.
            reconciliation=Reconciliation.MANUAL_OVERRIDE,
            reservation_extra={
                "target_lang": target_lang,
                "batch_index": batch_index,
                "batch_count": batches,
                # The segment count is recorded so an operator can see that one
                # reservation covered a whole batch. It is NOT a cost unit.
                "segments_in_batch": len(batch),
                "priced_model": model,
            },
        )
        try:
            paid.authorize()
        except BudgetExceededError as exc:
            raise DubError(
                f"translation budget refused before batch "
                f"{batch_index + 1}/{batches}: {exc}. Nothing was sent; raise "
                f"the workspace budget or shorten the source."
            ) from exc

        paid.mark_attempt(detail=f"translating batch {batch_index + 1}/{batches}")
        try:
            res = llm_mod.complete_json(
                system=system,
                user=user,
                workspace_id=workspace_id,
                tier="cheap",
                temperature=0.3,
                max_tokens=TRANSLATION_MAX_TOKENS,
                # This batch already reserved for the exact payload being
                # sent, so the per-leg gate reuses that row instead of
                # reserving the same POST a second time.
                preauthorized_spend=paid.llm_spend_authorization(),
            )
        except LLMCompletionError as exc:
            # The provider may have generated and billed this completion and
            # there is no remote id to look it up by, so the reservation is KEPT
            # and marked as an unknown exposure rather than released.
            paid.mark_unknown(str(exc))
            raise DubError(
                f"translation batch {batch_index + 1}/{batches} is unresolved: "
                f"{exc}"
            ) from exc
        except Exception as exc:  # noqa: BLE001 - classified below
            # Everything that is NOT an ambiguity means the request never
            # produced a billable generation, so the reservation is released and
            # the budget goes back.
            paid.mark_rejected(f"{type(exc).__name__}: {exc}",
                               nothing_billed=_definitely_not_billed(exc))
            raise DubError(
                f"translation batch {batch_index + 1}/{batches} failed: {exc}"
            ) from exc

        paid.mark_succeeded(
            detail=f"batch {batch_index + 1}/{batches}: {len(batch)} segments")
        lines = res.get("lines") or []
        for i in range(len(batch)):
            out.append(str(lines[i]) if i < len(lines) and lines[i] else batch[i])
    return out


def _definitely_not_billed(exc: BaseException) -> bool:
    """True when the failure proves no billable generation happened.

    The unknown case is already handled upstream by the typed
    ``LLMCompletionError.kind``; this is the "everything else" branch, and a
    provider-not-configured error or a malformed local payload never reaches the
    vendor at all.
    """
    if isinstance(exc, (LLMError, BudgetExceededError)):
        return True
    return is_safe_to_retry(exc)


def pick_voice(target_lang: str, explicit: str = "") -> str:
    """Best TTS voice for the target language (explicit wins, else locale match)."""
    if explicit.strip():
        return explicit.strip()
    try:
        from app.providers.tts import get_tts_provider

        prov = get_tts_provider()
        prefixes = LANG_LOCALES.get((target_lang or "").lower(), [])
        try:
            voices = prov.voices()
        except Exception:
            voices = []
        for v in voices:
            vid = str(v.get("id", ""))
            loc = str(v.get("locale", ""))
            if any(vid.startswith(p) or loc.startswith(p) for p in prefixes):
                return vid
        if voices and voices[0].get("id"):
            logger.warning(f"[dub] no '{target_lang}' voice; falling back to {voices[0]['id']}")
            return str(voices[0]["id"])
    except Exception as exc:
        raise DubError(f"TTS provider unavailable: {exc}") from exc
    return ""


def synthesize_segments(texts: list[str], voice: str, work_dir: Path,
                        workspace_id: str = "") -> list[Path]:
    """One audio file per translated line via the configured TTS provider.

    ``workspace_id`` (Work 15.9 §2) is the tenant the speech is billed to. The
    TTS provider resolves its tenant from an explicit argument and only falls
    back to an ambient context variable, so a caller that knows its workspace --
    the localization pipeline always does, on its canonical row -- must pass it
    rather than leave the provider to infer a tenant it cannot see. The
    ``workspace_scope`` below is the fallback made explicit for the same reason:
    within it, a provider reading the context variable gets THIS workspace
    instead of whatever an unrelated outer scope happens to hold.
    """
    from app.providers.tts import TTSError, get_tts_provider

    work_dir.mkdir(parents=True, exist_ok=True)
    prov = get_tts_provider()
    out: list[Path] = []
    for i, text in enumerate(texts):
        if not (text or "").strip():
            out.append(Path(""))
            continue
        try:
            if workspace_id:
                with provider_settings.workspace_scope(workspace_id):
                    res = prov.synthesize(text, voice=voice)
            else:
                res = prov.synthesize(text, voice=voice)
        except TTSError as exc:
            raise DubError(f"TTS synthesis failed on segment {i + 1}: {exc}") from exc
        ext = "wav" if res.format == "wav" else "mp3"
        p = work_dir / f"dub_{i:03d}.{ext}"
        p.write_bytes(res.audio_bytes)
        out.append(p)
    return out


# ---------------------------------------------------------------------------
# Assembly (FFmpeg)
# ---------------------------------------------------------------------------

def _probe_duration(path: Path) -> float:
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json",
             "-show_format", str(path)],
            capture_output=True, text=True, timeout=30,
        )
        import json as _json

        return float(_json.loads(out.stdout or "{}").get("format", {}).get("duration", 0.0))
    except Exception:
        return 0.0


def assemble_dubbed(source_video: Path, cues: list[SrtCue], dub_files: list[Path],
                    workspace_id: str, bilingual_srt: Path | None = None,
                    portrait: bool = True, filename: str | None = None) -> dict:
    """Mix dubbed segments over ducked original + burn bilingual subs.

    Returns {video_path, srt_path}. Dubbed chunks are time-fit per cue;
    the original bed runs at 30% underneath for continuity.
    """
    if not ffmpeg_present():
        raise DubError("ffmpeg not found — install it to assemble dubs")
    total = _probe_duration(source_video)
    if total <= 0:
        raise DubError("source duration unknown; cannot assemble dub")
    out_dir = STORAGE_ROOT / workspace_id
    out_dir.mkdir(parents=True, exist_ok=True)
    inputs: list[str] = ["-i", str(source_video)]
    for p in dub_files:
        if p and Path(p).exists():
            inputs += ["-i", str(p)]

    filt: list[str] = []
    if portrait:
        filt.append("[0:v]crop='min(iw,ih*9/16)':'min(ih,iw*16/9)',scale=1080:1920[vbase]")
        vlabel = "[vbase]"
    else:
        vlabel = "0:v"
    if bilingual_srt and bilingual_srt.exists():
        esc = str(bilingual_srt.resolve()).replace("\\", "/").replace(":", "\\:").replace("'", "\\'")
        filt.append(f"{vlabel}subtitles='{esc}'[vout]")
        vlabel = "[vout]"
    # original bed ducked to 30%
    filt.append("[0:a]volume=0.3[orig]")
    amix_ins = ["[orig]"]
    for i, p in enumerate([p for p in dub_files if p and Path(p).exists()]):
        cue = cues[i] if i < len(cues) else None
        window = (cue.end - cue.start) if cue else total
        dur = _probe_duration(Path(p))
        ratio = fit_ratio(dur, window)
        offset_ms = int((cue.start if cue else 0) * 1000)
        filt.append(f"[{i + 1}:a]atempo={ratio:.3f},adelay={offset_ms}|{offset_ms},apad=whole_dur={total:.2f}[d{i}]")
        amix_ins.append(f"[d{i}]")
    filt.append(f"{''.join(amix_ins)}amix=inputs={len(amix_ins)}:duration=longest:dropout_transition=0[aout]")

    dest = out_dir / (filename or f"dub-{source_video.stem[:40]}.mp4")
    cmd = (["ffmpeg", "-y", "-v", "error"] + inputs +
           ["-filter_complex", ";".join(filt),
            "-map", vlabel, "-map", "[aout]",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
            "-c:a", "aac", "-b:a", "160k", "-shortest", str(dest)])
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=1200)
    except subprocess.TimeoutExpired as exc:
        raise DubError("dub assembly timed out") from exc
    if proc.returncode != 0 or not dest.exists():
        stderr = (proc.stderr or b"").decode(errors="replace")[-400:]
        raise DubError(f"dub assembly failed: {stderr[:300]}")
    return {"video_path": str(dest),
            "srt_path": str(bilingual_srt) if bilingual_srt else ""}


__all__ = [
    "DubError",
    "SrtCue",
    "TRANSLATION_BATCH_SIZE",
    "TRANSLATION_CATEGORY",
    "TRANSLATION_MAX_TOKENS",
    "assemble_dubbed",
    "dub_status",
    "estimate_translation_request_cost",
    "ffmpeg_present",
    "fit_ratio",
    "format_srt",
    "parse_srt",
    "pick_voice",
    "synthesize_segments",
    "to_bilingual",
    "translate_segments",
    "translation_request_count",
    "translation_request_payload",
]
