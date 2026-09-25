"""Voice Designer agent (E2 voices): casting, cloning, emotion, dialogue.

Sits on the workspace voice stack (edge/kokoro/chatterbox/qwen3/mock):
picks voices per scene, clones from workspace-bound reference audio,
directs delivery (exaggeration/instruction), and assembles multi-voice
dialogue into one narration track. Rendered audio lands inside the workspace
storage boundary like any other asset.
"""

from __future__ import annotations

import shutil
import subprocess
import time
from pathlib import Path

from app.engine.agents.base import AgentMeta, BaseAgent
from app.providers.tts import ElevenLabsTTSProvider, TTSError, get_tts_provider
from app.services.storage import get_storage, managed_path


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
        """Narrate one block and store it as a workspace asset."""

        def work():
            ws = ctx.workspace_id or ""
            self.step("cast_voice", f"provider={provider or 'default'} voice={voice or 'default'}")
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
            self.step("synthesize", f"{len((text or '').split())} word(s)")
            try:
                res = prov.synthesize(text, voice=voice, rate=rate, language=language,
                                      exaggeration=exaggeration, clone_from=clone_ref)
            except TTSError as exc:
                self.step_failed(str(exc)[:150])
                raise
            ext = "wav" if res.format == "wav" else "mp3"
            stored = get_storage().save_media(ws, data=res.audio_bytes,
                                              filename=f"voice_{int(time.time())}.{ext}")
            self.step_done("ok", stored)
            detail = {"voice": voice or prov.name}
            est_usd = 0.0
            if res.provider == ElevenLabsTTSProvider.name:
                est_usd = round(len(text or "") * ElevenLabsTTSProvider.EST_USD_PER_CHAR, 6)
                detail = {**detail, "chars": len(text or ""), "estimated": True}
            self.track_cost(ctx, "tts", est_usd, provider=res.provider, detail=detail)
            return {
                "summary": f"narrated {len((text or '').split())} word(s) via {res.provider}",
                "audio_path": stored,
                "provider": res.provider,
                "voice": voice or getattr(prov, "DEFAULT_VOICE", ""),
                "is_mock": res.is_mock,
            }

        return self.execute(ctx, "synthesize_speech", input_summary=(text or "")[:200], fn=work)

    def design_batch(self, ctx, *, parts: list[dict], crossfade_ms: int = 0) -> dict:
        """Multi-voice dialogue: [{speaker, text, voice?, provider?, exaggeration?}]."""

        def work():
            from app.services import jobs as _jobs

            if not parts:
                raise TTSError("no dialogue parts provided")
            ws = ctx.workspace_id or ""
            self.step("synthesize_parts", f"{len(parts)} part(s)")
            tmp = Path(f"data/videos/{ws}/_voice_tmp")
            tmp.mkdir(parents=True, exist_ok=True)
            try:
                files: list[Path] = []
                est_usd = 0.0
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
                    prov = get_tts_provider(part.get("provider", "")) if part.get("provider") else get_tts_provider()
                    res = prov.synthesize(
                        text, voice=part.get("voice", ""),
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
                self.step("concat", "single narration track")
                combined = self._concat(files, tmp / "dialogue.mp3")
                stored = get_storage().save_media(ws, data=combined, filename=f"dialogue_{int(time.time())}.mp3")
                self.step_done("ok", stored)
                self.track_cost(ctx, "tts", round(est_usd, 6), provider="voice_batch",
                                detail={"parts": len(files), "estimated": est_usd > 0})
                return {
                    "summary": f"assembled {len(files)}-voice dialogue",
                    "audio_path": stored,
                    "parts": len(files),
                }
            finally:
                for leftover in tmp.glob("part_*.*"):
                    try:
                        leftover.unlink()
                    except OSError:
                        pass

        return self.execute(ctx, "synthesize_speech",
                            input_summary=f"{len(parts or [])} dialogue part(s)", fn=work)

    @staticmethod
    def _concat(files: list[Path], dest: Path) -> bytes:
        """Join same-pipeline audio parts (ffmpeg concat; wav parts convert first)."""
        if not shutil.which("ffmpeg"):
            raise TTSError("ffmpeg not found — cannot assemble dialogue")
        lst = dest.parent / "concat.txt"
        mp3s: list[Path] = []
        try:
            for i, f in enumerate(files):
                target = dest.parent / f"c_{i:03d}.mp3"
                if f.suffix.lower() == ".mp3":
                    mp3s.append(f)
                    continue
                proc = subprocess.run(
                    ["ffmpeg", "-y", "-v", "quiet", "-i", str(f),
                     "-c:a", "libmp3lame", "-b:a", "160k", str(target)],
                    capture_output=True, timeout=120,
                )
                if proc.returncode != 0:
                    raise TTSError(f"dialogue part {i} transcode failed")
                mp3s.append(target)
            lst.write_text("".join(f"file '{p.resolve()}'\n" for p in mp3s), encoding="utf-8")
            proc = subprocess.run(
                ["ffmpeg", "-y", "-v", "quiet", "-f", "concat", "-safe", "0",
                 "-i", str(lst), "-c", "copy", str(dest)],
                capture_output=True, timeout=120,
            )
            if proc.returncode != 0 or not dest.exists():
                raise TTSError("dialogue concat failed")
            return dest.read_bytes()
        finally:
            for p in list(mp3s) + [lst]:
                try:
                    if p.name.startswith("c_") or p.name == "concat.txt":
                        p.unlink(missing_ok=True)
                except OSError:
                    pass

    def list_voices(self, language: str = "", provider: str = "") -> list[dict]:
        prov = get_tts_provider(provider) if provider else get_tts_provider()
        try:
            return prov.voices(language=language)
        except TTSError:
            return []
