"""Avatar Director agent (E3 avatars): talking-head presenter clips.

Directs a presenter photo + driving audio into a lip-synced clip through the
avatar provider stack (server|sadtalker|mock). Accepts either a workspace
audio asset or raw script text — text is voiced first through the workspace
voice stack, so script → voice → face composes in one call.
"""

from __future__ import annotations

import time
from pathlib import Path

from app.engine.agents.base import AgentMeta, BaseAgent
from app.providers.avatar import AvatarError, render_avatar
from app.services.storage import get_storage


class AvatarDirectorAgent(BaseAgent):
    meta = AgentMeta(
        key="avatar_director",
        title="Avatar Director",
        description="Directs talking-head presenter clips from photo + audio.",
        skills=("avatar_direction",),
        tools=("render_avatar",),
        permissions=("media:render",),
    )

    def direct(self, ctx, *, image: str, audio: str = "", text: str = "",
               voice: str = "", provider: str = "",
               exaggeration: float = 0.5, backend: str = "") -> dict:
        """Render one presenter clip.

        Provide exactly one of `audio` (workspace asset) or `text` (voiced
        with the workspace voice stack first). `backend` optionally overrides
        the configured avatar lane for this call.
        """

        def work():
            ws = ctx.workspace_id or ""
            if bool(audio) == bool((text or "").strip()):
                raise AvatarError("provide exactly one of audio or text")
            self.step("cast_presenter", f"image: {image[:60]}")
            driving = audio
            used_voice = ""
            if (text or "").strip():
                from app.providers.tts import TTSError, get_tts_provider

                self.step("voice_script", f"{len(text.split())} word(s)")
                try:
                    prov = get_tts_provider(provider) if provider else get_tts_provider()
                    res = prov.synthesize(text, voice=voice, exaggeration=exaggeration)
                except TTSError as exc:
                    self.step_failed(str(exc)[:150])
                    raise
                ext = "wav" if res.format == "wav" else "mp3"
                driving = get_storage().save_media(ws, data=res.audio_bytes,
                                                   filename=f"avatar_voice_{int(time.time())}.{ext}")
                used_voice = voice or getattr(prov, "DEFAULT_VOICE", "")
                self.step_done("ok", f"voiced via {res.provider}")
            self.step("render_avatar", "lip-sync render")
            try:
                clip = render_avatar(image, driving, ws, backend=(backend or "").lower())
            except AvatarError as exc:
                self.step_failed(str(exc)[:150])
                raise
            self.step_done("ok", clip.path)
            # Work 15.7: this used to be ``track_cost(ctx, "video", 0.0, ...)``
            # for EVERY lane, including ``server`` -- a BILLED GPU render. A
            # $0 amount is not "free", it is "no row": services/cost.py drops
            # anything <= 0, so the paid render left no cost entry anywhere.
            # The server lane is now booked by ``providers/avatar.py`` (an
            # estimate, or an UNKNOWN-exposure event when the renderer reported
            # no price), so the agent must NOT book a second, fabricated zero.
            billed = clip.backend == "server"
            self.step(
                "cost",
                "billed GPU render booked by providers.avatar "
                "(paid.submission record)" if billed else
                f"{clip.backend} lane: operator CPU, no vendor invoice")
            return {
                "summary": f"directed presenter clip via {clip.backend}",
                "video_path": clip.path,
                "backend": clip.backend,
                "duration": clip.duration,
                "voice": used_voice,
                "is_mock": clip.is_mock,
                # Surfaced so the operator can see WHICH renders moved money
                # without reading the cost ledger.
                "billable_render": billed,
            }

        return self.execute(ctx, "render_avatar", input_summary=f"avatar: {image[:80]}", fn=work)

    @staticmethod
    def check_image(path: str) -> bool:
        return Path(path).is_file() and Path(path).suffix.lower() in (
            ".jpg", ".jpeg", ".png", ".webp")
