"""Link Miner + Repurpose Editor agents (E1: link-to-shorts).

Link Miner turns an operator-supplied long-form URL/file into ranked viral
moments (score + hook + reason each). Repurpose Editor assembles ranked moments
into captioned vertical shorts. Both degrade gracefully offline: ranking falls
back to a deterministic heuristic, cutting falls back to center-crop.
"""

from __future__ import annotations

from app.engine.agents.base import AgentMeta, BaseAgent
from app.providers.clips import ViralMoment, get_repurposer, probe_source_quality


def _maybe_sync_clips(ctx, clips: list) -> list[str]:
    """Adapter: when the job carries a timeline_id, persist cut ranges as
    Scene rows on the source timeline. Best-effort — assembly never depends
    on it."""
    from loguru import logger

    from app.db import session_scope

    try:
        payload = getattr(ctx, "payload", None) or {}
        timeline_id = payload.get("timeline_id")
        if not timeline_id or not ctx.workspace_id:
            return []
        from app.engine import scene_sync as sync_mod
        from app.models import ContentTimeline

        with session_scope() as s:
            row = s.get(ContentTimeline, timeline_id)
            if row is None or row.workspace_id != ctx.workspace_id:
                return []
            rows = sync_mod.sync_from_segments(
                s, workspace_id=ctx.workspace_id, timeline_id=timeline_id,
                content_item_id=row.content_item_id,
                segments=[{"start": c.start, "end": c.end,
                           "text": c.hook or ""} for c in clips],
                source="repurpose")
            s.commit()
            return [r.id for r in rows]
    except Exception as exc:  # noqa: BLE001 — sync must never break assembly
        logger.debug(f"[repurpose] scene sync skipped: {type(exc).__name__}")
        return []


class LinkMinerAgent(BaseAgent):
    meta = AgentMeta(
        key="link_miner",
        title="Link Miner",
        description="Mines ranked viral moments from long-form URLs and files.",
        skills=("clip_mining",),
        tools=("mine_moments",),
        permissions=("research:read", "llm:generate"),
    )

    def mine(self, ctx, *, source: str, max_moments: int = 5) -> dict:
        def work():
            rep = get_repurposer()
            self.step("probe", "source quality pre-flight")
            quality = probe_source_quality(source)
            if quality.get("warning"):
                self.step_done("warn", quality["warning"])
            else:
                detail = str(quality.get("mode") or "unknown source")
                if quality.get("max_height"):
                    detail += f" {quality['max_height']}p"
                self.step_done("ok", detail)
            self.step("acquire", f"source: {source[:80]}")
            info = rep.acquire(source, ctx.workspace_id or "")
            self.step_done("ok", f"{info.title[:60]} ({info.duration or 0:.0f}s)")
            self.step("transcribe", "word-grouped transcript segments")
            segments = rep.transcribe_segments(info)
            self.step_done("ok", f"{len(segments)} segment(s)" if segments else "no transcription (offline)")
            self.step("detect_scenes", "shot boundaries")
            scenes = rep.detect_scenes(info)
            self.step_done("ok", f"{len(scenes)} scene(s)" if scenes else "fixed windows")
            self.step("rank", "viral-moment scoring")
            base = segments if segments else [
                {"start": s, "end": e, "text": f"Segment {i + 1} of {info.title}"}
                for i, (s, e) in enumerate(scenes)
            ]
            moments = rep.rank_moments(base, max_moments, ctx.workspace_id or "")
            self.step_done("ok", f"{len(moments)} moment(s) ranked")
            # Intelligence advisory (Work 05, Lane A): shadow-only; ranking stays authoritative.
            try:
                from app.engine.intelligence.integrations import advise_repurpose_moments

                advise_repurpose_moments(
                    [{"hook": m.hook, "text": m.text, "score": m.score,
                      "start": m.start, "end": m.end} for m in moments],
                    workspace_id=ctx.workspace_id or "",
                )
            except Exception:
                pass
            return {
                "summary": f"mined {len(moments)} viral moment(s) from '{info.title[:60]}'",
                "source_title": info.title,
                "source_duration": info.duration,
                "source_path": str(info.local_path),
                "source_quality": quality,
                "moments": [
                    {"start": m.start, "end": m.end, "score": m.score,
                     "hook": m.hook, "reason": m.reason, "text": m.text}
                    for m in moments
                ],
            }

        return self.execute(ctx, "mine_moments", input_summary=source[:200], fn=work)


class RepurposeEditorAgent(BaseAgent):
    meta = AgentMeta(
        key="repurpose_editor",
        title="Repurpose Editor",
        description="Assembles ranked moments into captioned vertical shorts.",
        skills=("clip_assembly",),
        tools=("assemble_clips",),
        permissions=("media:render",),
    )

    def assemble(self, ctx, *, source: str, moments: list[dict] | None = None,
                 max_clips: int = 5, vertical: bool = True,
                 caption_preset: str = "minimal", face_track: bool = False) -> dict:
        def work():
            from app.services import jobs as _jobs

            rep = get_repurposer()
            self.step("acquire", f"source: {source[:80]}")
            info = rep.acquire(source, ctx.workspace_id or "")
            self.step_done("ok", info.title[:60])
            viral = []
            for m in (moments or []):
                # Parse first: comparing raw values mis-sorts numeric strings
                # ("100" > "20" is False lexicographically) and lets garbage
                # through to float() below.
                try:
                    start = float(m["start"])
                    end = float(m["end"])
                    score = float(m.get("score", 0))
                except (KeyError, TypeError, ValueError) as exc:
                    raise ValueError(f"invalid moment in request: {exc}") from exc
                if not end > start:
                    continue
                viral.append(ViralMoment(
                    start=start, end=end, score=score,
                    hook=str(m.get("hook", "")), reason=str(m.get("reason", "")),
                    text=str(m.get("text", ""))))
            self.step("cut", f"{len(viral) or max_clips} clip(s), preset={caption_preset}")
            captions = {i + 1: (m.text or m.hook) for i, m in enumerate(viral)} or None
            clips = rep.cut_segments(
                info, ctx.workspace_id or "",
                moments=viral or None, max_clips=max_clips, vertical=vertical,
                caption_preset=caption_preset, captions=captions, face_track=face_track,
            )
            for _c in clips:
                _jobs.check_cancelled(ctx)
            self.step_done("ok", f"{len(clips)} clip(s) rendered")
            synced = _maybe_sync_clips(ctx, clips)
            self.track_cost(ctx, "video", 0.0, provider="ffmpeg_clips")
            return {
                "summary": f"assembled {len(clips)} short(s) from '{info.title[:60]}'",
                "synced_scene_ids": synced,
                "clips": [
                    {"path": c.path, "start": c.start, "end": c.end,
                     "duration": c.duration, "score": c.score, "hook": c.hook,
                     "reason": c.reason, "preset": c.preset}
                    for c in clips
                ],
            }

        return self.execute(ctx, "assemble_clips", input_summary=source[:200], fn=work)
