"""Voice Designer agent (E2 voices): casting, cloning, emotion, dialogue.

Sits on the workspace voice stack (edge/kokoro/chatterbox/qwen3/mock):
picks voices per scene, clones from workspace-bound reference audio,
directs delivery (exaggeration/instruction), and assembles multi-voice
dialogue into one narration track. Rendered audio lands inside the workspace
storage boundary like any other asset.

Assembly goes through :mod:`app.services.audio_concat`, which decodes every
segment to one PCM format and encodes once. Joining the parts with a stream
copy would splice N encoder delays and drift the narration against its own
captions.

A voice of ``none`` / ``no-voice`` renders a real silent track of the right
length instead of calling a provider, so the rest of the pipeline is
unchanged. An EMPTY voice is not silence — it falls through to the provider
default, because a blank setting is a missing setting (see
:func:`app.services.pause_tags.is_no_voice`).
"""

from __future__ import annotations

import contextlib
import tempfile
import time
from pathlib import Path

from app.engine.agents.base import AgentMeta, BaseAgent
from app.providers.tts import ElevenLabsTTSProvider, TTSError, get_tts_provider
from app.services.audio_concat import AudioConcatError, concat_audio, write_silence
from app.services.pause_tags import (
    NO_VOICE_NAME,
    assert_voice_mode_explicit,
    has_pause_tags,
    is_no_voice,
    parse_script_with_pauses,
    remove_pause_tags,
    resolve_voice_mode,
    silent_duration_seconds,
    total_pause_seconds,
)
from app.services.storage import get_storage, managed_path


def _brand_gate(ws_id: str):
    """Resolve the workspace brand gate (``None`` when unavailable)."""
    try:
        from app.engine.brand_templates import brand_gate

        return brand_gate(None, ws_id)
    except Exception:  # noqa: BLE001 — brand must never break TTS
        return None


def _brand_voice(ws_id: str, requested: str, gate=None) -> str:
    """Filter a voice request through the brand-approved list (never raises).

    Brand hard constraint (Work 08 Lane C): when the effective policy pins
    ``approved_voices`` a non-approved request is swapped for the first
    approved voice; with no policy (or no brand module) the request passes
    through unchanged. Pass a pre-resolved ``gate`` in batch loops so the
    policy is resolved once per call site, not once per part.
    """
    try:
        from app.engine.brand_templates import approved_voice

        return approved_voice(
            gate if gate is not None else _brand_gate(ws_id),
            str(requested or ""))
    except Exception:  # noqa: BLE001 — brand must never break TTS
        return str(requested or "")


class VoiceDesignerAgent(BaseAgent):
    meta = AgentMeta(
        key="voice_designer",
        title="Voice Designer",
        description="Casts, clones and directs narration voices per scene.",
        skills=("voice_design",),
        tools=("synthesize_speech",),
        permissions=("tts:synthesize",),
    )

    def design(self, ctx, *, text: str, voice: str = "", provider: str = "",
               exaggeration: float = 0.5, clone_from: str = "",
               language: str = "", rate: float = 1.0) -> dict:
        """Narrate one block and store it as a workspace asset.

        Three input shapes are honoured: a script carrying pause tags (each
        speech run is synthesized on its own and joined with exact silence), an
        explicit no-voice sentinel (a real silent track of the estimated
        length, no provider call, no cost), and plain speech.
        """
        text = (text or "").strip()
        if not text:
            raise TTSError("text is empty")

        def work():
            ws = ctx.workspace_id or ""
            # Brand hard constraint: non-approved voices are swapped out.
            resolved_voice = _brand_voice(ws, voice)
            self.step("cast_voice", f"provider={provider or 'default'} voice={resolved_voice or 'default'}")

            # No-voice mode. The sentinel must be explicit: a blank voice is a
            # missing setting and falls through to the provider default below,
            # never to silence.
            if is_no_voice(resolved_voice):
                assert_voice_mode_explicit(resolved_voice)
                return self._render_silent(ctx, text, ws=ws, voice=resolved_voice)

            if has_pause_tags(text):
                return self._render_with_pauses(
                    ctx, text, ws=ws, voice=resolved_voice, provider=provider,
                    rate=rate, language=language, exaggeration=exaggeration)

            try:
                prov = get_tts_provider(provider) if provider else get_tts_provider()
            except TTSError as exc:
                self.step_failed(str(exc)[:150])
                raise
            clone_ref = ""
            if clone_from:
                resolved = managed_path(ws, clone_from)
                if not resolved or not resolved.exists():
                    raise TTSError(
                        "clone reference must be a workspace asset — upload it under Assets first"
                    )
                clone_ref = str(resolved)
            self.step_done("ok", prov.name)
            self.step("synthesize", f"{len(text.split())} word(s)")
            # Pause tags must never reach a provider: it would narrate them.
            spoken = remove_pause_tags(text) or text
            try:
                res = prov.synthesize(spoken, voice=resolved_voice, rate=rate, language=language,
                                      exaggeration=exaggeration, clone_from=clone_ref)
            except TTSError as exc:
                self.step_failed(str(exc)[:150])
                raise
            ext = "wav" if res.format == "wav" else "mp3"
            stored = get_storage().save_media(ws, data=res.audio_bytes,
                                              filename=f"voice_{int(time.time())}.{ext}")
            self.step_done("ok", stored)
            detail = {"voice": resolved_voice or prov.name}
            est_usd = 0.0
            if res.provider == ElevenLabsTTSProvider.name:
                est_usd = round(len(text) * ElevenLabsTTSProvider.EST_USD_PER_CHAR, 6)
                detail = {**detail, "chars": len(text), "estimated": True}
            self.track_cost(ctx, "tts", est_usd, provider=res.provider, detail=detail)
            return {
                "summary": f"narrated {len(text.split())} word(s) via {res.provider}",
                "audio_path": stored,
                "provider": res.provider,
                "voice": resolved_voice or getattr(prov, "DEFAULT_VOICE", ""),
                "is_mock": res.is_mock,
                "silent": False,
            }

        return self.execute(ctx, "synthesize_speech", input_summary=text[:200], fn=work)

    def _render_silent(self, ctx, text: str, *, ws: str, voice: str) -> dict:
        """Store a real silent track so every later stage keeps working.

        No provider is contacted and no cost is booked: the track exists only to
        give clip trimming, the caption timeline and the final mux something
        with a duration to read.
        """
        self.step_done("ok", "silent")
        seconds = silent_duration_seconds(text)
        self.step("silent_track", f"{seconds:.2f}s (no voice: {voice})")
        try:
            with tempfile.TemporaryDirectory(prefix="ymoney-silent-") as tmp:
                produced = write_silence(Path(tmp) / "silent.wav", seconds)
                stored = get_storage().save_media(
                    ws, data=Path(tmp, "silent.wav").read_bytes(),
                    filename=f"voice_silent_{int(time.time())}.wav")
        except AudioConcatError as exc:
            self.step_failed(str(exc)[:150])
            raise TTSError(f"silent narration could not be produced: {exc}") from exc
        self.step_done("ok", stored)
        return {
            "summary": f"silent track ({produced:.2f}s) — no narration requested",
            "audio_path": stored,
            "provider": "silent",
            "voice": voice,
            "is_mock": False,
            "silent": True,
            "duration_seconds": round(produced, 3),
        }

    def _render_with_pauses(self, ctx, text: str, *, ws: str, voice: str,
                            provider: str, rate: float, language: str,
                            exaggeration: float) -> dict:
        """Synthesize each speech run separately and join with exact silence.

        Offsets come from decoded sample counts, so a caption placed after the
        second run lands where the audio actually is.
        """
        segments = parse_script_with_pauses(text)
        runs = [s for s in segments if s.is_speech]
        if not runs:
            # Every run was swallowed by a malformed tag: there is nothing to
            # speak, so this is a silent track, not a failed synthesis.
            return self._render_silent(ctx, remove_pause_tags(text), ws=ws,
                                       voice=resolve_voice_mode(voice, default=NO_VOICE_NAME))
        self.step("parse_pauses", f"{len(runs)} run(s), {total_pause_seconds(segments):.2f}s silence")
        self.step_done("ok")
        try:
            prov = get_tts_provider(provider) if provider else get_tts_provider()
        except TTSError as exc:
            self.step_failed(str(exc)[:150])
            raise
        self.step("synthesize_parts", f"via {prov.name}")
        # WAV out: the joined track is written straight from spliced PCM, so
        # there is no encoder at all and therefore no encoder delay to absorb.
        ext = "wav"
        est_usd = 0.0
        parts: list[Path] = []
        with tempfile.TemporaryDirectory(prefix="ymoney-pauses-") as tmp:
            work = Path(tmp)
            for seg in segments:
                if seg.is_speech:
                    res = prov.synthesize(seg.text, voice=voice, rate=rate,
                                          language=language, exaggeration=exaggeration)
                    if res.provider == ElevenLabsTTSProvider.name:
                        est_usd += len(seg.text) * ElevenLabsTTSProvider.EST_USD_PER_CHAR
                    part_ext = "wav" if res.format == "wav" else "mp3"
                    path = work / f"speech_{len(parts):03d}.{part_ext}"
                    path.write_bytes(res.audio_bytes)
                    parts.append(path)
                else:
                    # Pause silence is PCM, written straight from samples —
                    # no encoder, so no added length to absorb later.
                    chunk = work / f"silence_{len(parts):03d}.wav"
                    write_silence(chunk, seg.seconds)
                    parts.append(chunk)
            self.step_done("ok", f"{len(parts)} chunk(s)")
            self.step("concat", "sample-accurate join")
            try:
                seconds = concat_audio(parts, work / f"narration.{ext}")
            except AudioConcatError as exc:
                self.step_failed(str(exc)[:150])
                raise TTSError(f"paused narration could not be assembled: {exc}") from exc
            stored = get_storage().save_media(ws, data=(work / f"narration.{ext}").read_bytes(),
                                              filename=f"voice_pauses_{int(time.time())}.{ext}")
        self.step_done("ok", stored)
        self.track_cost(ctx, "tts", round(est_usd, 6), provider="voice_pauses",
                        detail={"chunks": len(parts), "estimated": est_usd > 0})
        return {
            "summary": f"narrated {len(runs)} run(s) with "
                       f"{total_pause_seconds(segments):.2f}s of pauses via {prov.name}",
            "audio_path": stored,
            "provider": prov.name,
            "voice": voice or getattr(prov, "DEFAULT_VOICE", ""),
            "is_mock": getattr(prov, "name", "") == "mock",
            "silent": False,
            "duration_seconds": round(seconds, 3),
            "runs": len(runs),
            "pause_seconds": total_pause_seconds(segments),
        }

    def design_batch(self, ctx, *, parts: list[dict], crossfade_ms: int = 0) -> dict:
        """Multi-voice dialogue: [{speaker, text, voice?, provider?, exaggeration?}]."""

        def work():
            from app.services import jobs as _jobs

            if not parts:
                raise TTSError("no dialogue parts provided")
            ws = ctx.workspace_id or ""
            # Brand hard constraint: every part's voice is filtered through
            # approved_voices (gate resolved once for the whole batch).
            brand_gate = _brand_gate(ws)
            self.step("synthesize_parts", f"{len(parts)} part(s)")
            tmp = Path(f"data/videos/{ws}/_voice_tmp")
            tmp.mkdir(parents=True, exist_ok=True)
            try:
                files: list[Path] = []
                est_usd = 0.0
                silent_parts = 0
                for i, part in enumerate(parts):
                    _jobs.check_cancelled(ctx)
                    text = (part.get("text") or "").strip()
                    if not text:
                        continue
                    try:
                        rate = float(part.get("rate", 1.0))
                        exaggeration = float(part.get("exaggeration", 0.5))
                    except (TypeError, ValueError):
                        raise TTSError(f"dialogue part {i} has non-numeric rate/exaggeration")
                    part_voice = _brand_voice(ws, part.get("voice", ""), gate=brand_gate)
                    # An explicit no-voice line in a dialogue becomes real
                    # silence, so the speaker's timing still advances. A blank
                    # voice is not silence and reaches the provider below.
                    if is_no_voice(part_voice):
                        assert_voice_mode_explicit(part_voice)
                        chunk = tmp / f"part_{i:03d}.wav"
                        write_silence(chunk, silent_duration_seconds(text))
                        files.append(chunk)
                        silent_parts += 1
                        continue
                    prov = get_tts_provider(part.get("provider", "")) if part.get("provider") else get_tts_provider()
                    res = prov.synthesize(
                        # A pause tag is not speech: strip before synthesizing.
                        remove_pause_tags(text) or text,
                        voice=part_voice,
                        rate=rate,
                        language=part.get("language", ""),
                        exaggeration=exaggeration,
                    )
                    if res.provider == ElevenLabsTTSProvider.name:
                        est_usd += len(text) * ElevenLabsTTSProvider.EST_USD_PER_CHAR
                    ext = "wav" if res.format == "wav" else "mp3"
                    p = tmp / f"part_{i:03d}.{ext}"
                    p.write_bytes(res.audio_bytes)
                    files.append(p)
                if not files:
                    raise TTSError("no speakable dialogue parts")
                self.step_done("ok", f"{len(files)} part(s)")
                self.step("concat", "single narration track (sample-accurate)")
                combined, seconds = self._concat(files, tmp / "dialogue.mp3")
                stored = get_storage().save_media(ws, data=combined, filename=f"dialogue_{int(time.time())}.mp3")
                self.step_done("ok", stored)
                self.track_cost(ctx, "tts", round(est_usd, 6), provider="voice_batch",
                                detail={"parts": len(files), "estimated": est_usd > 0})
                out = {
                    "summary": f"assembled {len(files)}-voice dialogue",
                    "audio_path": stored,
                    "parts": len(files),
                    "duration_seconds": round(seconds, 3),
                }
                if silent_parts:
                    out["silent_parts"] = silent_parts
                return out
            finally:
                # Best-effort cleanup of the intermediate PCM parts. A part
                # still held open on Windows cannot be unlinked, and that must
                # not mask the real result of the narration.
                for leftover in tmp.glob("part_*.*"):
                    with contextlib.suppress(OSError):
                        leftover.unlink()

        return self.execute(ctx, "synthesize_speech",
                            input_summary=f"{len(parts or [])} dialogue part(s)", fn=work)

    @staticmethod
    def _concat(files: list[Path], dest: Path) -> tuple[bytes, float]:
        """Join dialogue parts into one track. Returns (bytes, exact seconds).

        Every part is decoded to one PCM format and the result is encoded
        once. The previous implementation joined with a stream copy, which
        spliced each part's MP3 encoder delay into the output: three 1.000 s
        parts measured 3.090 s, and the error grew with every extra speaker.

        A part that cannot be decoded aborts the join. Returning a track that
        is quietly missing a line is worse than failing the dialogue.
        """
        try:
            seconds = concat_audio(files, dest)
        except AudioConcatError as exc:
            raise TTSError(str(exc)) from exc
        return dest.read_bytes(), seconds

    def list_voices(self, language: str = "", provider: str = "") -> list[dict]:
        prov = get_tts_provider(provider) if provider else get_tts_provider()
        try:
            return prov.voices(language=language)
        except TTSError:
            return []
