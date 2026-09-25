"""Dubbing Localizer agent (E2-voices track, OpenCreator-inspired).

Turns a video into another language: transcribe (or accept SRT) → LLM
translate with terminology context → target-language TTS → time-fit →
portrait + bilingual-subtitle assembly. Every missing dependency fails closed
with remediation instead of silent output.
"""

from __future__ import annotations

from pathlib import Path

from app.engine.agents.base import AgentMeta, BaseAgent
from app.providers.clips import get_repurposer
from app.providers.dubbing import (
    DubError,
    assemble_dubbed,
    format_srt,
    parse_srt,
    pick_voice,
    synthesize_segments,
    to_bilingual,
    translate_segments,
)
from app.services.storage import STORAGE_ROOT


class DubbingLocalizerAgent(BaseAgent):
    meta = AgentMeta(
        key="dubbing_localizer",
        title="Dubbing Localizer",
        description="Translates and re-voices videos into other languages.",
        skills=("dubbing_localization",),
        tools=("dub_video",),
        permissions=("media:render", "llm:generate"),
    )

    def dub(self, ctx, *, source: str, target_lang: str, voice: str = "",
            bilingual: bool = True, portrait: bool = True,
            srt: str = "", glossary: dict | None = None) -> dict:
        def work():
            from app.services import jobs as _jobs

            lang = (target_lang or "").lower().strip()
            if not lang:
                raise DubError("target_lang is required (e.g. es, fr, de, hi)")
            rep = get_repurposer()
            self.step("acquire", f"source: {source[:80]}")
            info = rep.acquire(source, ctx.workspace_id or "")
            self.step_done("ok", info.title[:60])

            if srt.strip():
                self.step("parse_srt", "operator-supplied subtitle track")
                cues = parse_srt(srt)
            else:
                self.step("transcribe", "source-language transcript")
                segs = rep.transcribe_segments(info)
                from app.providers.dubbing import SrtCue

                cues = [SrtCue(index=i + 1, start=s.start, end=s.end, text=s.text)
                        for i, s in enumerate(segs)]
            if not cues:
                raise DubError("no transcript available — install faster-whisper or supply an SRT track")
            self.step_done("ok", f"{len(cues)} cue(s)")
            _jobs.check_cancelled(ctx)

            self.step("translate", f"{len(cues)} line(s) → {lang}")
            translated = translate_segments([c.text for c in cues], lang,
                                            ctx.workspace_id or "", glossary)
            self.step_done("ok", f"{len(translated)} line(s)")

            self.step("voice", "target-language voice match")
            chosen = pick_voice(lang, voice)
            self.step_done("ok", chosen or "provider default")
            _jobs.check_cancelled(ctx)

            self.step("synthesize", f"{len(translated)} segment(s)")
            work_dir = STORAGE_ROOT / "_clipwork" / (ctx.workspace_id or "") / "dub"
            dub_files = [Path(p) if p else Path("")
                         for p in synthesize_segments(translated, chosen, work_dir)]
            self.step_done("ok", f"{sum(1 for p in dub_files if p and p.exists())} clip(s)")
            _jobs.check_cancelled(ctx)

            self.step("assemble", "bilingual burn + dubbed mix")
            bilingual_cues = to_bilingual(cues, translated) if bilingual else None
            srt_path: Path | None = None
            if bilingual_cues:
                work_dir.mkdir(parents=True, exist_ok=True)
                srt_path = work_dir / "bilingual.srt"
                srt_path.write_text(format_srt(bilingual_cues), encoding="utf-8")
            built = assemble_dubbed(info.local_path, cues, dub_files,
                                    ctx.workspace_id or "", srt_path, portrait)
            self.step_done("ok", built["video_path"])
            self.track_cost(ctx, "video", 0.0, provider="ffmpeg_dub")
            return {
                "summary": f"dubbed '{info.title[:60]}' → {lang} ({len(cues)} cues)",
                "video_path": built["video_path"],
                "srt_path": built["srt_path"],
                "target_lang": lang,
                "voice": chosen,
                "cues": len(cues),
            }

        return self.execute(ctx, "dub_video", input_summary=f"{source[:120]} → {target_lang}", fn=work)
