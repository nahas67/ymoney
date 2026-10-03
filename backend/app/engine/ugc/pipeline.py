"""UGC video pipeline (Work 07 Lane C): brief → editable timeline → render → QC.

One flow, nine presets, ZERO second video engine:

    Brief → Audience → Hook → Script → Presenter/Avatar → Voice
         → Product Assets → B-roll → CTA → Timeline → Render → QC

Reuse map (nothing here re-implements another lane's work):
  * script/strategy — `StrategistAgent` + `ScriptWriterAgent` (creation.py)
  * brand/tone      — workspace brand voice + brief tone (BrandDNA inputs)
  * voice           — `providers/tts.py` (real provider, mock in tests only)
  * b-roll          — `providers/broll.plan_scenes` (+ optional stock/AI fetch)
  * visuals         — user-supplied MediaAssets ONLY (never fabricated)
  * avatar          — `engine.avatar` (consent-gated) when a presenter is set
  * timeline        — canonical `engine/timeline.py` + ContentTimeline rows
  * render          — `providers/video_engine/timeline_render.py`
  * QC              — `engine.ugc.qc.UGCQCReport`

Product/claim safety (hard requirements):
  * product assets come from the workspace upload paths; nothing invents
    features, screenshots or testimonials.
  * testimonial quotes must trace to `brief_json.testimonials[].source_quote`
    — generated claims without a source fail QC and BLOCK rendering.
  * numeric/absolute claims absent from the brief → REVIEW_REQUIRED.

Manual edits are version-protected: `regenerate()` refuses (REVIEW_REQUIRED)
whenever the current timeline no longer matches the manifest hash recorded at
generation, so an editor's changes are never clobbered.
"""

from __future__ import annotations

import logging
import re
import shutil
import time
from collections.abc import Callable
from typing import Any

from app.engine.timeline import (
    TimelineValidationError,
    add_clip,
    create_empty,
    render_manifest,
    validate_timeline,
)
from app.engine.ugc.qc import UGCQCReport, probe_has_audio, run_ugc_qc

# ``_check`` / ``_rollup`` are the SAME private helpers Lane C's QC report uses
# (branding already folds ``_rollup`` in for its own check). Reaching for a
# hand-rolled severity map would give the music check a different vocabulary
# from every other check in the report.
from app.engine.ugc.qc import _check as _check_style  # noqa: PLC2701
from app.engine.ugc.qc import _rollup as _rollup_style  # noqa: PLC2701
from app.engine.ugc.voice import narrate_segments, narrate_text, split_segments
from app.providers.music.base import MUSIC_TRACK_KIND
from app.services.paid_jobs import SubmissionState

UGC_PRESETS: tuple[str, ...] = (
    "PRODUCT_DEMO",
    "TESTIMONIAL",
    "REVIEW",
    "UNBOXING",
    "REACTION",
    "PROBLEM_SOLUTION",
    "FOUNDER_STYLE",
    "TALKING_HEAD",
    "BEFORE_AFTER",
)

#: per-preset starting shape (strategy fallback + script framing)
PRESET_DEFAULTS: dict[str, dict] = {
    "PRODUCT_DEMO": {"hook_type": "curiosity_gap", "duration_seconds": 30,
                     "format": "hands-on product demo over product shots"},
    "TESTIMONIAL": {"hook_type": "story", "duration_seconds": 30,
                    "format": "customer quote led testimonial (sourced quotes only)"},
    "REVIEW": {"hook_type": "question", "duration_seconds": 40,
               "format": "balanced review with pros and cons"},
    "UNBOXING": {"hook_type": "curiosity_gap", "duration_seconds": 35,
                 "format": "first-impressions unboxing"},
    "REACTION": {"hook_type": "bold_claim", "duration_seconds": 30,
                 "format": "react-and-commentary short"},
    "PROBLEM_SOLUTION": {"hook_type": "bold_claim", "duration_seconds": 30,
                         "format": "pain point first, then the fix"},
    "FOUNDER_STYLE": {"hook_type": "story", "duration_seconds": 35,
                      "format": "founder talking-head with a clear POV"},
    "TALKING_HEAD": {"hook_type": "question", "duration_seconds": 30,
                     "format": "direct-to-camera narration"},
    "BEFORE_AFTER": {"hook_type": "statistic", "duration_seconds": 30,
                     "format": "transformation split with proof shots"},
}


class UGCError(Exception):
    """Fatal pipeline input/stage error (bad brief, no voice, no timeline)."""


class UGCBlockedError(UGCError):
    """QC FAIL — the project must not render until a human fixes it."""


class _PaidStateObserver(logging.Handler):
    """Watch the paid-job logger for a submission that may have been billed.

    ``base.music_or_none`` logs ``PaidSubmissionUnconfirmed`` at ERROR precisely
    because it must not be silent, but its contract is to return ``None``. The
    pipeline needs to distinguish that case from an ordinary refusal, and the
    log line is the real signal -- reading it is honest, whereas guessing
    ``None`` means "nothing billed" is how a double charge happens.

    Only messages the paid-job contract emits for an UNCONFIRMED submission
    count; everything else is ignored.
    """

    #: Substring of ``base.music_or_none``'s UNCONFIRMED log line.
    MARKER = "UNCONFIRMED"

    def __init__(self) -> None:
        super().__init__(level=logging.ERROR)
        self.unconfirmed = False
        self.detail = ""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 — a malformed record is not our state
            return
        if self.MARKER in message.upper():
            self.unconfirmed = True
            self.detail = message[:300]


# ---------------------------------------------------------------------------
# brief helpers
# ---------------------------------------------------------------------------


def normalize_brief(preset: str, brief: dict | None) -> dict:
    """Copy the user brief and fill non-claim defaults (tone/audience).

    Nothing product-related is ever invented: product name/features/claims
    stay exactly what the user supplied.
    """
    if preset not in UGC_PRESETS:
        raise UGCError(f"unknown UGC preset '{preset}' "
                       f"(expected one of {', '.join(UGC_PRESETS)})")
    out = dict(brief or {})
    out.setdefault("audience", "")
    out.setdefault("tone", "")
    out.setdefault("product_assets", [])
    out.setdefault("variants", [])
    if not isinstance(out.get("product_assets"), list):
        raise UGCError("brief.product_assets must be a list of workspace assets")
    if not isinstance(out.get("variants"), list):
        raise UGCError("brief.variants must be a list")
    return out


def brief_topic(brief: dict) -> str:
    product = brief.get("product")
    topic = (brief.get("topic") or brief.get("product_name")
             or (product.get("name") if isinstance(product, dict) else "") or "")
    return str(topic).strip()


def _fallback_script(brief: dict, topic: str, cta: str) -> str:
    """Deterministic, claim-free script built ONLY from user-supplied fields.

    Used when no LLM/script agent is reachable — it never adds a feature,
    number or testimonial the brief did not state.
    """
    parts: list[str] = []
    hook = str(brief.get("hook") or "").strip()
    description = ""
    product = brief.get("product")
    if isinstance(product, dict):
        description = str(product.get("description") or "").strip()
    if hook:
        parts.append(hook)
    else:
        parts.append(f"{topic}: {description}" if description else f"{topic}.")
    problem = str(brief.get("problem") or "").strip()
    solution = str(brief.get("solution") or "").strip()
    if problem:
        parts.append(problem)
    if solution:
        parts.append(solution)
    features = brief.get("features") or (
        product.get("features") if isinstance(product, dict) else None) or []
    if isinstance(features, list) and features:
        parts.append("What you get: " + ", ".join(str(f) for f in features[:4]) + ".")
    if cta:
        parts.append(cta)
    return " ".join(p for p in parts if p).strip()


# ---------------------------------------------------------------------------
# pipeline
# ---------------------------------------------------------------------------


class UGCVideoPipeline:
    """Runs one UGC project through the documented stage flow."""

    def __init__(self, session: Any, workspace_id: str, project: Any, *,
                 job_ctx: Any = None,
                 strategy_fn: Callable[..., dict] | None = None,
                 script_fn: Callable[..., str] | None = None,
                 voice_fn: Callable[[], Any] | None = None,
                 broll_fn: Callable[..., list] | None = None,
                 render_fn: Callable[..., dict] | None = None) -> None:
        self.session = session
        self.workspace_id = workspace_id
        self.project = project
        self.job_ctx = job_ctx
        # injectable stages (tests + future providers); None → real implementations
        self._strategy_fn = strategy_fn
        self._script_fn = script_fn
        self._voice_fn = voice_fn
        self._broll_fn = broll_fn
        self._render_fn = render_fn

        self.preset: str = str(project.preset or "")
        self.brief: dict = normalize_brief(self.preset, project.brief_json)
        self.topic: str = brief_topic(self.brief)
        self.lineage: dict = dict(project.lineage_json or {})
        self.script: str = ""
        self.strategy: dict = {}
        self.segments: list[dict] = []
        self.presenter: dict = {"type": "none"}
        self.cta_text: str = ""
        self.product_assets: list[dict] = []
        self.broll_plan: list[dict] = []
        #: Work 15.6: the generated soundtrack, when one was produced.
        #: ``{}`` means "no bed" and is always explained in
        #: ``lineage_json["music"]["reason"]``.
        self.music: dict[str, Any] = {}

    # -- context -----------------------------------------------------------

    def _ctx(self):
        if self.job_ctx is not None:
            return self.job_ctx
        from app.services.jobs import JobContext

        self.job_ctx = JobContext(
            job_id=f"ugc-{self.project.id}", type="ugc_pipeline",
            workspace_id=self.workspace_id, cycle_id=None,
            payload={"project_id": self.project.id, "preset": self.preset},
            attempt=1, cancelled=lambda: False)
        return self.job_ctx

    def _save_lineage(self) -> None:
        self.project.lineage_json = dict(self.lineage)

    # -- stages ------------------------------------------------------------

    def stage_brief(self) -> dict:
        """BRIEF: validate the request, resolve the topic/product framing."""
        if not self.topic:
            raise UGCError(
                "brief needs a topic or product name "
                "(brief_json.topic / product_name / product.name)")
        defaults = PRESET_DEFAULTS.get(self.preset, {})
        self.lineage["preset"] = self.preset
        self.lineage["topic"] = self.topic
        self._save_lineage()
        return {"topic": self.topic, "preset": self.preset, "defaults": defaults}

    def stage_audience(self) -> dict:
        """AUDIENCE: audience + tone from the brief (BrandDNA), strategy via
        StrategistAgent with a deterministic fallback (never fabricates)."""
        brief = self.brief
        research = {
            "topic": self.topic,
            "audience": str(brief.get("audience") or ""),
            "product_name": self.topic,
            "product_description": (
                brief.get("product", {}) or {}).get("description", "")
            if isinstance(brief.get("product"), dict) else "",
            "problem": str(brief.get("problem") or ""),
            "solution": str(brief.get("solution") or ""),
            "features": brief.get("features") or [],
            "claims": brief.get("claims") or [],
            "keywords": brief.get("keywords") or [],
            "constraints": ("Only state what this brief states: no invented "
                            "features, numbers, results or testimonials."),
        }
        brand_voice = self._brand_voice()
        if brand_voice:
            research["brand_voice"] = brand_voice

        strategy: dict = {}
        if self._strategy_fn is not None:
            strategy = dict(self._strategy_fn(self._strategy_input(research)) or {})
        else:
            from app.engine.agents.creation import StrategistAgent

            try:
                strategy = dict(StrategistAgent().strategize(
                    self._ctx(), self.topic, research) or {})
            except Exception:  # noqa: BLE001 — strategy must not kill the cycle
                strategy = {}
        merged = {**PRESET_DEFAULTS.get(self.preset, {}), **strategy}
        # Creative template defaults (Work 08 Lane C): fill the keys neither
        # the preset nor the model set — brand DNA overrides them next, and
        # the operator's brief still wins over both (user > brand > template).
        try:
            from app.engine.brand_templates import (
                apply_template_defaults,
                get_template,
                template_for,
            )
            from app.engine.brand_templates import brand_gate as _brand_gate
            from app.engine.brand_templates import (
                lineage_markers as _lineage_markers,
            )

            gate = _brand_gate(self.session, self.workspace_id,
                               artifact={"content_format": "ugc"})
            tpl_key = template_for({"content_format": "ugc"},
                                   platform=str(brief.get("platform") or ""),
                                   default="ugc")
            tpl_defaults = apply_template_defaults(
                (gate or {}).get("policy"), get_template(tpl_key))
            for src, dst in (
                ("aspect_ratio", "aspect_ratio"),
                ("hook_style", "hook_type"),
                ("pacing", "pacing"),
                ("caption_preset", "caption_preset"),
                ("broll_density", "broll_density"),
                ("music_preference", "music_preference"),
                ("duration_seconds", "duration_seconds"),
            ):
                value = tpl_defaults.get(src)
                if value and dst not in merged:
                    merged[dst] = value
            if gate and gate.get("applied_brand") and gate.get("tone"):
                merged["tone"] = gate["tone"]
            if gate:
                merged["brand"] = _lineage_markers(gate)
        except Exception:  # noqa: BLE001 — templates are defaults, never gates
            pass
        # user brief always wins over the model (audience/tone/cta are theirs)
        for key in ("audience", "tone", "cta"):
            if brief.get(key):
                merged[key] = brief[key]
        if brief.get("hook_type"):
            merged["hook_type"] = brief["hook_type"]
        merged.setdefault("hook_type", "question")
        merged.setdefault("cta", "follow for more")
        merged.setdefault("aspect_ratio", str(brief.get("aspect_ratio") or "9:16"))
        try:
            merged["duration_seconds"] = int(max(20, min(90, int(
                merged.get("duration_seconds") or 30))))
        except (TypeError, ValueError):
            merged["duration_seconds"] = 30
        self.strategy = merged
        self.lineage["strategy"] = merged
        self.lineage["audience"] = str(merged.get("audience") or "")
        self._save_lineage()
        return merged

    def _strategy_input(self, research: dict) -> tuple[str, dict]:
        return self.topic, research

    def _brand_voice(self) -> str:
        """BrandDNA (workspace brand voice + white-label brand kit).

        Work 08 Lane C: the RESOLVED brand policy (tone + vocabulary) is
        listed first; the legacy workspace `brand_voice` / `app_name` fields
        remain the fallback whenever no policy is configured yet.
        """
        from app.models import Workspace

        ws = self.session.get(Workspace, self.workspace_id)
        if ws is None:
            return ""
        parts: list[str] = []
        try:
            from app.engine.brand_templates import brand_gate, lineage_markers

            gate = brand_gate(self.session, self.workspace_id,
                              artifact={"content_format": "short"})
            if gate and gate.get("brand_available"):
                if gate.get("tone"):
                    parts.append(f"tone: {gate['tone']}")
                vocab = gate.get("vocabulary") or {}
                if vocab.get("preferred"):
                    parts.append("use: " + ", ".join(str(t) for t in vocab["preferred"][:8]))
                if vocab.get("avoid"):
                    parts.append("avoid: " + ", ".join(str(t) for t in vocab["avoid"][:8]))
                self.lineage["brand"] = lineage_markers(gate)
                self._save_lineage()
        except Exception:  # noqa: BLE001 — brand never breaks the UGC stage
            pass
        parts.append(str(ws.brand_voice or "").strip())
        brand = ((ws.settings_json or {}).get("brand") or {})
        if brand.get("app_name"):
            parts.append(f"brand: {brand['app_name']}")
        return "; ".join(p for p in parts if p)

    def stage_hook(self) -> dict:
        """HOOK: pick the opening line (brief hook wins when supplied)."""
        hook = str(self.brief.get("hook") or "").strip()
        if not hook:
            hook_type = str(self.strategy.get("hook_type") or "question")
            hook = f"[{hook_type}] {self.topic}"
        self.lineage["hook"] = hook
        self._save_lineage()
        return {"hook": hook, "hook_type": self.strategy.get("hook_type")}

    def stage_script(self) -> str:
        """SCRIPT: ScriptAgent, a supplied script, or a claim-free fallback."""
        supplied = str(self.brief.get("script") or "").strip()
        script = ""
        source = "supplied"
        if supplied:
            script = supplied
        elif self._script_fn is not None:
            script = str(self._script_fn(
                self.topic, self.strategy, self.brief) or "").strip()
            source = "injected"
        else:
            from app.engine.agents.creation import ScriptWriterAgent

            try:
                script = str(ScriptWriterAgent().write_script(
                    self._ctx(), self.topic, self.strategy,
                    {"brief": self.brief}) or "").strip()
                source = "script_agent"
            except Exception:  # noqa: BLE001 — fall back, never fabricate facts
                script = ""
        if not script:
            script = _fallback_script(self.brief, self.topic,
                                      str(self.brief.get("cta") or ""))
            source = "fallback_brief_only"
        if not script:
            raise UGCError("script is empty — supply brief.script or configure an LLM")
        hook = str(self.lineage.get("hook") or "")
        if (self.brief.get("hook") and hook
                and not script.lower().startswith(hook.lower()[:24])):
            script = f"{hook} {script}".strip()
        self.script = script
        self.lineage["script"] = script
        self.lineage["script_source"] = source
        self._save_lineage()
        return script

    def stage_presenter(self) -> dict:
        """PRESENTER/AVATAR: resolve the presenter and GATE on consent.

        A profile whose consent_state != authorized raises here — before any
        voice render or avatar work happens.
        """
        raw = self.brief.get("presenter")
        profile_id = (raw or {}).get("avatar_profile_id") if isinstance(raw, dict) \
            else (self.brief.get("avatar_profile_id") or "")
        if not profile_id:
            self.presenter = {"type": "product_assets"}
            self._save_lineage()
            return self.presenter
        from app.engine.avatar.profile import AvatarProfile, require_authorized
        from app.models.avatar import AvatarProfileRow

        row = self.session.get(AvatarProfileRow, profile_id)
        if row is None or row.workspace_id != self.workspace_id:
            raise UGCError(f"avatar profile '{profile_id}' not found in this workspace")
        profile = AvatarProfile.from_row(row)
        require_authorized(profile, action="UGC presenter render")
        self.presenter = {"type": "avatar", "profile_id": profile.id,
                          "profile_name": profile.name,
                          "source_asset_ref": profile.source_asset_ref,
                          "consent_state": profile.consent_state,
                          "provider": profile.provider}
        self._save_lineage()
        return self.presenter

    def stage_voice(self) -> list[dict]:
        """VOICE: narrate each segment through the workspace TTS provider."""

        # Brand-approved voices (Work 08 Lane C): filter through the policy
        # when it declares an approved list; degrade to the provider default
        # if that voice yields no audio (provider mismatch never blocks).
        voice = ""
        try:
            from app.engine.brand_templates import approved_voice, brand_gate

            gate = brand_gate(self.session, self.workspace_id,
                              artifact={"content_format": "ugc"})
            voice = approved_voice(gate, "")
            if voice:
                self.lineage["brand_voice"] = voice
                self._save_lineage()
        except Exception:  # noqa: BLE001 — brand never breaks the voice stage
            voice = ""
        self.segments = narrate_segments(
            self.session, self.workspace_id, self.script,
            provider_factory=self._voice_fn, voice=voice)
        if not self.segments and voice:
            self.segments = narrate_segments(
                self.session, self.workspace_id, self.script,
                provider_factory=self._voice_fn, voice="")
        if not self.segments:
            raise UGCError("voice stage produced no audio — configure TTS "
                           "or supply brief.script narration")
        self.lineage["voice"] = [
            {"asset_id": s.get("asset_id"), "duration": s.get("duration"),
             "words": s.get("words")} for s in self.segments]
        self._save_lineage()
        return self.segments

    # -- music (Work 15.6) --------------------------------------------------

    def _brand_dna(self) -> Any:
        """The effective BrandDNA for this workspace, or None.

        Read through the EXISTING resolver so a workspace-default document, a
        brand row and campaign/content/platform overrides all behave as they do
        everywhere else. A resolver failure yields None -- an unreadable brand is
        "no preference stated", never a licence to invent one.
        """
        try:
            from app.engine.brand.dna import effective_dna
            from app.engine.brand.inheritance import brand_layer

            dna, _brand_id = brand_layer(self.session, self.workspace_id)
            return effective_dna(dna)
        except Exception as exc:  # noqa: BLE001 — brand never breaks the pipeline
            self.lineage["music_brand_error"] = f"{type(exc).__name__}: {exc}"[:200]
            return None

    def _music_target_duration(self) -> float:
        """The video length a bed must fit: the narration it will sit under."""
        return round(sum(float(s.get("duration") or 0.0) for s in self.segments), 3)

    def _music_source_asset(self) -> Any:
        """The video the music is derived from, if one exists.

        The provider derives audio from a rendered video. The presenter output is
        the canonical video for this project; when there is no presenter, a
        previously rendered project asset is used. Returning a row (not bytes)
        keeps the lineage link a REFERENCE, like every other MediaAsset edge.
        """
        from app.models.assets import MediaAsset

        asset_id = str(self.presenter.get("output_asset_id") or "")
        if not asset_id:
            asset_id = str(self.project.render_asset_ref or "")
        if not asset_id:
            return None
        row = self.session.get(MediaAsset, asset_id)
        if row is None or row.workspace_id != self.workspace_id:
            return None
        return row

    def stage_music(self) -> dict:
        """MUSIC: opt-in, brand-governed AI soundtrack on the canonical track.

        Order of refusals, strongest first: BrandDNA hard rule -> workspace
        policy -> budget -> cancellation -> the provider. Every refusal is
        RECORDED (``lineage_json["music"]["reason"]``) and returns a no-bed
        result, because a missing soundtrack degrades the video, it never fails
        it.

        Generated audio is a NEW, derived asset. It is never written over a
        source asset: the ``MediaAsset`` row is created with
        ``origin="generated"``, ``parent_asset_id`` pointing at the video it was
        derived from, and the bytes land under their own storage key.
        """
        from app.providers.music.base import MusicRequest, music_timeline_clip
        from app.providers.music.policy import (
            MusicPolicyRefused,
            duration_within_tolerance,
            enforce_forbidden_genres,
            music_policy,
            recommend_style,
        )

        state: dict[str, Any] = {"track": MUSIC_TRACK_KIND, "generated": False}

        def record(**fields: Any) -> dict:
            state.update(fields)
            self.lineage["music"] = dict(state)
            self._save_lineage()
            return dict(state)

        # 1. policy gate: opt-in only, unset means NO generation
        dna = self._brand_dna()
        policy = music_policy(getattr(self.project, "workspace_settings", None)
                              or self._workspace_settings(), dna,
                              workspace_id=self.workspace_id)
        if not policy.generate:
            return record(reason=policy.reason, policy=policy.to_dict())

        # 2. brand hard rules beat any recommendation. Checked against the RAW
        #    requested genre first: `recommend_style` drops a forbidden one, and
        #    dropping it silently would hide that the brief asked for something
        #    the brand forbids.
        candidate = dict(self.brief.get("music") or {})
        raw_genre = str(candidate.get("genre") or "").strip().lower()
        try:
            enforce_forbidden_genres([raw_genre] if raw_genre else [], policy)
        except MusicPolicyRefused as exc:
            return record(reason="forbidden_genre", detail=str(exc),
                          policy=policy.to_dict())
        recommend_style(candidate, policy)   # filtered suggestion; brand wins

        duration = self._music_target_duration()
        if duration <= 0:
            return record(reason="no_timeline_duration")

        source = self._music_source_asset()
        source_path = ""
        if source is not None:
            from app.services.storage import managed_path

            resolved = managed_path(self.workspace_id, str(source.storage_key or ""))
            source_path = str(resolved or "")

        request = MusicRequest(
            workspace_id=self.workspace_id,
            duration_seconds=duration,
            video_path=source_path,
            video_asset_id=str(getattr(source, "id", "") or ""),
            video_title=self.topic,
            script_excerpt=self.script[:600],
            campaign_id=str(self.brief.get("campaign_id") or ""),
            brand_music_preference=str(self.brief.get("music_preference") or ""),
            brand_tone=str(self.brief.get("tone") or ""),
            keywords=[str(k) for k in (self.brief.get("keywords") or [])][:6],
        )
        policy.apply_to(request)

        # 3. budget gate BEFORE any billable call
        estimated = self._music_estimate(request, policy.provider_key)
        try:
            from app.services.cost import assert_can_spend

            assert_can_spend(self.workspace_id, estimated)
        except Exception as exc:  # noqa: BLE001 — BudgetExceededError and lookup failures
            from app.services.cost import BudgetExceededError

            reason = ("budget_rejected" if isinstance(exc, BudgetExceededError)
                      else "budget_check_failed")
            return record(reason=reason, detail=str(exc)[:200],
                          estimated_cost_usd=estimated)

        # 4. cancellation: never spend money on an abandoned job
        try:
            cancelled = bool(self._ctx().cancelled())
        except Exception:  # noqa: BLE001
            cancelled = False
        if cancelled:
            return record(reason="cancelled")

        # 5. the paid provider.
        result, outcome = self._music_submit(policy.provider_key, request)
        state_name = str(outcome.state)
        if state_name == SubmissionState.SUBMISSION_UNKNOWN:
            # Billed-or-not: loud, permanent, and never a retry trigger. This is
            # recorded from the SAME classifier the provider uses, so a lost
            # submit response can never be re-sent by a later run.
            return record(reason="submission_unknown", provider=policy.provider_key,
                          state=state_name, remote_id=outcome.remote_id,
                          detail=outcome.detail, must_not_resubmit=True,
                          may_resubmit=outcome.may_resubmit)
        if result is None:
            return record(reason="unavailable_or_failed",
                          provider=policy.provider_key, state=state_name,
                          detail=outcome.detail)

        measured = float(result.duration_seconds or 0.0)
        if not duration_within_tolerance(measured, duration, tolerance=0.25):
            return record(reason="duration_mismatch", provider=result.provider,
                          state=state_name, measured_duration=measured,
                          expected_duration=duration)

        asset = self._persist_music_asset(result, source, estimated, request)
        self.music = {"asset_id": asset.id, "asset": asset,
                      "duration": measured, "provider": result.provider,
                      "clip": music_timeline_clip(asset.id, measured)}
        return record(generated=True, reason="generated",
                      provider=result.provider, state=state_name,
                      asset_id=asset.id, storage_key=asset.storage_key,
                      duration_seconds=measured, parent_asset_id=asset.parent_asset_id,
                      estimated_cost_usd=estimated,
                      prompt=result.prompt, provenance=dict(result.provenance),
                      policy=policy.to_dict())

    def _music_submit(self, provider_key: str, request: Any):
        """ONE paid submission through the EXISTING ``generate_music`` entry point.

        Returns ``(MusicResult | None, SubmissionRecord)``.

        ``generate_music`` is the package's never-raises contract: it returns
        ``None`` for a refusal AND for a submission that may already have been
        billed. Collapsing those two is exactly what the paid-job contract
        forbids, so this wrapper observes the loud ``ERROR`` that
        ``base.music_or_none`` already emits for ``PaidSubmissionUnconfirmed``
        and records ``SUBMISSION_UNKNOWN`` -- read from the real signal, never
        inferred. Any other no-track outcome is recorded as a failure.

        Exactly ONE submit is attempted. A lost response ends the stage here and
        is never retried, by this stage or by a later run.
        """
        from app.services.paid_jobs import SubmissionRecord

        record = SubmissionRecord(workspace_id=self.workspace_id,
                                  provider=str(provider_key))
        observer = _PaidStateObserver()
        music_logger = logging.getLogger("ymoney.music")
        music_logger.addHandler(observer)
        try:
            # Resolved from the module (not a captured local) so an injected
            # provider at the package seam is honoured, exactly as the lane's
            # tests and any future provider swap require.
            from app.providers.music import generate_music

            result = generate_music(provider_key, request,
                                    workspace_id=self.workspace_id)
        finally:
            music_logger.removeHandler(observer)

        if observer.unconfirmed:
            record.state = SubmissionState.SUBMISSION_UNKNOWN
            record.detail = observer.detail
        elif result is None:
            record.state = SubmissionState.FAILED
            record.detail = ("no track returned; no UNCONFIRMED submission was "
                             "reported, but this is not proof of a clean refusal")
        else:
            record.state = getattr(result, "state", SubmissionState.SUCCEEDED)
            record.remote_id = str(getattr(result, "remote_id", "") or "")
            record.detail = ""
        return result, record

    def _music_estimate(self, request: Any, provider_key: str) -> float:
        """The provider's own estimate, or an honest refusal of the call."""
        try:
            from app.providers.music import get_music_provider

            provider = get_music_provider(provider_key,
                                          workspace_id=self.workspace_id)
            return max(0.0, float(provider.estimate_cost(request)))
        except Exception:  # noqa: BLE001 — an unconfigured provider costs nothing
            return 0.0

    def _persist_music_asset(self, result: Any, source: Any,
                             estimated: float, request: Any) -> Any:
        """Copy the generated audio into workspace storage as a NEW MediaAsset.

        Lineage is explicit: ``parent_asset_id`` names the video this bed was
        derived from, and ``derivation_json`` carries the provider, model, prompt,
        submission state and cost so a later reader can tell a generated bed from
        a licensed one. The source asset row is never read-modify-written.
        """
        from pathlib import Path

        from app.models.assets import MediaAsset
        from app.providers.music.base import MUSIC_TRACK_KIND
        from app.services.storage import get_storage

        data = Path(str(result.path)).read_bytes()
        name = f"music_{result.provider}_{int(time.time() * 1000)}.mp3"
        key = get_storage().save_media(self.workspace_id, data=data, filename=name)
        row = MediaAsset(
            workspace_id=self.workspace_id,
            type="audio",                 # canonical asset type: every consumer
            origin="generated",           # ... already understands it
            provider=str(result.provider or ""),
            storage_key=str(key),
            mime_type="audio/mpeg",
            duration_seconds=float(result.duration_seconds or 0.0),
            file_size=int(result.file_size or len(data)),
            audio_codec=str(result.audio_codec or ""),
            sample_rate=int(result.sample_rate or 0) or None,
            channels=int(result.channels or 0) or None,
            checksum=str(result.checksum or ""),
            parent_asset_id=str(getattr(source, "id", "") or "") or None,
            derivation_json={
                "kind": "music_bed",
                "provider": str(result.provider or ""),
                "state": str(getattr(getattr(result, "state", None), "value",
                                     getattr(result, "state", "")) or ""),
                "remote_id": str(result.remote_id or ""),
                "model": str((result.provenance or {}).get("model_id") or ""),
                "prompt": str(result.prompt or "")[:1000],
                "requested_duration_seconds": float(
                    getattr(request, "duration_seconds", 0.0) or 0.0),
                "measured_duration_seconds": float(result.duration_seconds or 0.0),
                "estimated_cost_usd": round(float(estimated), 6),
                "cost_is_estimate": True,
                "warnings": [str(w)[:200] for w in (result.warnings or [])][:10],
            },
            meta_json={"work": "15.6", "track": MUSIC_TRACK_KIND,
                       "provenance": dict(result.provenance or {})},
        )
        self.session.add(row)
        self.session.flush()
        return row

    def _workspace_settings(self) -> dict:
        from app.models import Workspace

        row = self.session.get(Workspace, self.workspace_id)
        return dict(getattr(row, "settings_json", None) or {})

    def stage_product_assets(self) -> list[dict]:
        """PRODUCT ASSETS: resolve user-uploaded MediaAssets (never invented)."""
        from app.engine.ugc.assets import resolve_product_assets

        resolved, unresolved = resolve_product_assets(
            self.session, self.workspace_id, self.brief.get("product_assets") or [])
        self.product_assets = resolved
        self.lineage["product_assets"] = {
            "resolved": resolved, "unresolved": unresolved}
        self._save_lineage()
        return resolved

    def stage_broll(self, *, fetch: bool = False) -> list[dict]:
        """B-ROLL: plan scenes (planner reuse); fetch only when asked."""
        from app.providers.broll import plan_scenes

        keywords = [str(k) for k in (self.brief.get("keywords") or [])][:6]
        if not keywords:
            product = self.brief.get("product")
            if isinstance(product, dict):
                keywords = [str(f) for f in (product.get("features") or [])][:4]
        plan_fn = self._broll_fn or (
            lambda: plan_scenes(self.topic, keywords,
                                max(2, min(len(self.segments) or 3, 8)),
                                self.workspace_id))
        try:
            plan = plan_fn() or []
        except Exception as exc:  # noqa: BLE001 — planning is advisory
            plan = []
            self.lineage["broll_error"] = str(exc)[:200]
        self.broll_plan = [
            {"index": getattr(p, "index", i), "query": getattr(p, "query", ""),
             "prompt": getattr(p, "prompt", ""), "source": getattr(p, "source", "")}
            for i, p in enumerate(plan)]
        if fetch:
            self._fetch_broll()
        self.lineage["broll_plan"] = self.broll_plan
        self._save_lineage()
        return self.broll_plan

    def _fetch_broll(self) -> None:
        """Optional stock/AI fetch (off by default — no network in tests)."""
        from app.providers import broll as broll_mod

        mode = str(self.brief.get("broll") or "").lower()
        fetched: list[str] = []
        errors: list[str] = []
        for scene in self.broll_plan[:4]:
            try:
                if mode == "stock":
                    ref = broll_mod.fetch_stock_clip(
                        scene.get("query", ""), self.workspace_id)
                elif mode == "ai":
                    ref = broll_mod.generate_clip(scene.get("prompt", ""),
                                                  self.workspace_id)
                else:
                    return
                if ref:
                    fetched.append(ref)
            except Exception as exc:  # noqa: BLE001 — one source never kills
                errors.append(str(exc)[:160])
        if fetched or errors:
            self.lineage["broll_fetched"] = {"refs": fetched, "errors": errors}
            self._save_lineage()

    def stage_cta(self) -> dict:
        """CTA: spoken + on-screen call to action (user CTA wins)."""

        cta = str(self.brief.get("cta") or self.strategy.get("cta") or "").strip()
        if not cta:
            cta = "follow for more"
        spoken = cta not in self.script
        segment = None
        if spoken:
            segment = narrate_text(self.session, self.workspace_id, cta,
                                   provider_factory=self._voice_fn)
            if segment:
                self.segments.append(segment)
        self.cta_text = cta
        self.lineage["cta"] = {"text": cta, "spoken": bool(segment)}
        self._save_lineage()
        return {"text": cta, "spoken": bool(segment)}

    def stage_presenter_render(self) -> dict:
        """Render the avatar clip (after voice) when a presenter is set."""
        if self.presenter.get("type") != "avatar":
            return {"rendered": False}
        from app.engine.avatar.service import render_profile_output

        audio_ref = next((s.get("asset_id") for s in self.segments
                          if s.get("asset_id")), "")
        try:
            out = render_profile_output(
                self.session, self.workspace_id, self.presenter["profile_id"],
                audio_ref=audio_ref, timeline=False)
        except Exception as exc:  # noqa: BLE001 — consent errors surface in
            # stage_presenter; render failures degrade to product-asset visuals
            self.lineage["presenter_error"] = str(exc)[:200]
            self._save_lineage()
            return {"rendered": False, "error": str(exc)[:200]}
        self.presenter.update({"rendered": True,
                               "output_asset_id": out.get("asset_id"),
                               "output_id": out.get("output_id"),
                               "duration": out.get("duration")})
        self._save_lineage()
        return {"rendered": True, "asset_id": out.get("asset_id")}

    # -- timeline ----------------------------------------------------------

    def stage_timeline(self) -> Any:
        """TIMELINE: canonical ContentTimeline + Scenes on a ContentItem.

        Everything stays individually editable (voice/caption/hook/CTA clips,
        visual assets, scenes) and validates through the same
        `validate_timeline` the editor API uses.
        """
        from app.models import ContentTimeline
        from app.models.assets import Scene

        if not self.segments:
            raise UGCError("no voice segments — run the voice stage first")
        aspect = str(self.strategy.get("aspect_ratio") or "9:16")
        doc = create_empty(self.workspace_id, aspect=aspect)
        t = 0.0
        for i, seg in enumerate(self.segments):
            dur = float(seg.get("duration") or 0.0)
            if dur <= 0:
                continue
            name = str(seg.get("text") or "")[:200]
            if seg.get("asset_id"):
                add_clip(doc, track="voice", clip_id=f"v_{i}", name=name,
                         start=t, duration=dur,
                         source={"asset_id": seg["asset_id"]})
            add_clip(doc, track="caption", clip_id=f"c_{i}", name=name,
                     start=t, duration=dur)
            t += dur
        total = float(doc.get("duration_seconds") or 0.0)
        if total <= 0:
            raise UGCError("timeline has zero duration")

        # hook + CTA share the text track: size them together so a short
        # video (< 6s of narration) still validates — overlapping clips are
        # rejected by the same `validate_timeline` the editor API runs.
        hook = str(self.lineage.get("hook") or self.topic)
        hook_dur = min(3.0, total / 2.0)
        cta_dur = min(3.0, total - hook_dur)
        cta_start = total - cta_dur
        add_clip(doc, track="text", clip_id="hook", name=f"hook: {hook[:80]}",
                 start=0.0, duration=hook_dur,
                 text={"content": hook[:120], "size": 64})
        if cta_dur > 0 and cta_start >= hook_dur - 1e-6:
            add_clip(doc, track="text", clip_id="cta",
                     name=f"cta: {self.cta_text[:80]}", start=cta_start,
                     duration=cta_dur,
                     text={"content": self.cta_text[:120], "size": 56})

        # the AI soundtrack, if one was generated: a clip on the EXISTING music
        # track, so the existing render (timeline_render.py mixes
        # voice/music/sfx) picks it up with no new render path.
        music = self.music or {}
        clip = dict(music.get("clip") or {})
        if clip.get("source", {}).get("asset_id"):
            add_clip(doc, track=MUSIC_TRACK_KIND, clip_id=str(clip.get("id") or "music_0"),
                     name=str(clip.get("name") or "AI music bed"),
                     start=float(clip.get("start") or 0.0),
                     duration=float(clip.get("duration") or 0.0),
                     source=dict(clip.get("source") or {}),
                     volume=float(clip.get("volume", 0.18)))

        # visuals: user product assets tiled under an optional presenter clip
        visual_refs = self._visual_assets(total)
        cursor = 0.0
        idx = 0
        while cursor < total - 1e-6 and visual_refs:
            dur = min(4.0, total - cursor)
            ref = visual_refs[idx % len(visual_refs)]
            add_clip(doc, track="broll", clip_id=f"b_{idx}",
                     name=str(ref.get("role") or f"product-asset {idx}"),
                     start=cursor, duration=dur,
                     source={"asset_id": ref["asset_id"]})
            cursor += dur
            idx += 1

        validate_timeline(doc)

        content_item = self._content_item()
        previous = (self.session.get(ContentTimeline, self.project.timeline_id)
                    if self.project.timeline_id else None)
        row = ContentTimeline(
            workspace_id=self.workspace_id,
            content_item_id=content_item.id,
            name=f"{self.topic} ({self.preset})"[:200],
            fps=float(doc.get("fps") or 30.0),
            duration_seconds=float(doc.get("duration_seconds") or 0.0),
            tracks_json=doc,
            version=(int(previous.version or 1) + 1) if previous else 1,
            parent_timeline_id=previous.id if previous else None,
        )
        self.session.add(row)
        self.session.flush()

        # scenes: one editable narrative unit per voiced segment
        for i, seg in enumerate(self.segments):
            start = sum(float(s.get("duration") or 0.0) for s in self.segments[:i])
            end = start + float(seg.get("duration") or 0.0)
            scene = Scene(
                workspace_id=self.workspace_id,
                content_item_id=content_item.id,
                timeline_id=row.id,
                index=i,
                title=(str(seg.get("text") or "")[:60] or f"{self.preset} {i}"),
                script_segment=str(seg.get("text") or ""),
                narration=str(seg.get("text") or ""),
                visual_intent=(self.broll_plan[i]["query"] if i < len(self.broll_plan)
                               else self.topic),
                start_seconds=start, end_seconds=end,
                assets_json=([{"asset_id": seg["asset_id"], "role": "voice"}]
                             if seg.get("asset_id") else []),
                captions_json=[str(seg.get("text") or "")[:200]],
            )
            self.session.add(scene)
        self.session.flush()

        self.project.timeline_id = row.id
        self.lineage["content_item_id"] = content_item.id
        self.lineage["generated_version"] = int(row.version or 1)
        self.lineage["manifest_hash"] = render_manifest(doc)["manifest_hash"]
        self._save_lineage()
        return row

    def _visual_assets(self, total: float) -> list[dict]:
        out: list[dict] = []
        if self.presenter.get("rendered") and self.presenter.get("output_asset_id"):
            out.append({"asset_id": self.presenter["output_asset_id"],
                        "role": "presenter"})
            return out
        for ref in self.product_assets:
            out.append({"asset_id": ref["asset_id"],
                        "role": str(ref.get("role") or "product")})
        return out

    def _content_item(self) -> Any:
        from app.models import ContentItem

        existing = self.lineage.get("content_item_id")
        if existing:
            item = self.session.get(ContentItem, existing)
            if item is not None and item.workspace_id == self.workspace_id:
                return item
        item = ContentItem(workspace_id=self.workspace_id,
                           topic=f"{self.topic} (UGC {self.preset})",
                           status="PRODUCTION", derivation_type="other")
        self.session.add(item)
        self.session.flush()
        return item

    # -- QC + render -------------------------------------------------------

    def run_qc(self, *, file_meta: dict | None = None) -> UGCQCReport:
        doc = {}
        row = None
        if self.project.timeline_id:
            from app.models import ContentTimeline

            row = self.session.get(ContentTimeline, self.project.timeline_id)
            doc = dict((row.tracks_json if row else None) or {})
        report = run_ugc_qc(preset=self.preset, brief=self.brief,
                            script=self.script or str(self.lineage.get("script") or ""),
                            doc=doc, file_meta=file_meta)
        # Brand consistency (Work 08 Lane C): Lane A's verifier folded into
        # the UGC report as a `brand` check and re-rolled into PASS /
        # PASS_WITH_WARNINGS / REVIEW_REQUIRED / FAIL. A verifier crash maps
        # to `warning` — the brand check alone can never FAIL the project.
        try:
            from app.engine.brand_templates import brand_qc_check
            from app.engine.ugc.qc import _rollup

            brand_check = brand_qc_check(
                self.session, self.workspace_id,
                artifact={"text": self.script or str(self.lineage.get("script") or ""),
                          "artifact_kind": "ugc"})
            if brand_check:
                report.checks["brand"] = brand_check
                report.status = _rollup(report.checks)
                self.lineage["brand_qc"] = {
                    k: brand_check.get(k) for k in ("status", "detail",
                                                    "effective_config_id")
                    if k in brand_check}
        except Exception:  # noqa: BLE001 — brand never breaks UGC QC
            pass
        # Music (Work 15.6): the soundtrack decision is a VISIBLE QC check so an
        # operator can see whether the bed is present and, when it is not, the
        # recorded reason. A missing bed is never a failure -- the render is
        # still valid -- so this reports `pass` or `warning` only.
        music_state = dict(self.lineage.get("music") or {})
        if music_state:
            clips = [c for tr in doc.get("tracks", [])
                     if tr.get("kind") == MUSIC_TRACK_KIND
                     for c in tr.get("clips", [])]
            if music_state.get("generated"):
                report.checks["music_decision"] = _check_style(
                    "pass" if clips else "warning",
                    (f"{len(clips)} music clip(s) on the canonical track"
                     if clips else
                     "a bed was generated but no clip reached the timeline"))
            else:
                # A DECLINED soundtrack is the system working, so it must not
                # warn: warning here would downgrade every policy-refused run to
                # PASS_WITH_WARNINGS for something nobody asked for. The reason
                # is carried in the detail so the fact stays visible.
                report.checks["music_decision"] = _check_style(
                    "pass",
                    f"no soundtrack by policy: "
                    f"{music_state.get('reason') or 'not attempted'}")
            report.status = _rollup_style(report.checks)
        self.project.qc_json = report.to_dict()
        self.project.status = _status_for(report.status)
        self._save_lineage()
        self.session.flush()
        return report

    def render(self, *, out_name: str = "ugc.mp4") -> dict:
        """RENDER: timeline → MP4 → MediaAsset (refuses on QC FAIL)."""
        from app.models import ContentTimeline
        from app.models.assets import MediaAsset

        report = UGCQCReport.from_dict(self.project.qc_json)
        if report.status == "FAIL":
            raise UGCBlockedError(
                f"project {self.project.id} is BLOCKED by QC FAIL "
                f"({', '.join(k for k, v in report.checks.items() if v.get('status') == 'fail')}) "
                f"— fix the brief/script and regenerate before rendering")
        row = self.session.get(ContentTimeline, self.project.timeline_id) if \
            self.project.timeline_id else None
        if row is None:
            raise UGCError("project has no timeline — run the pipeline first")
        doc = dict(row.tracks_json or {})
        doc.setdefault("fps", row.fps)
        doc.setdefault("duration_seconds", row.duration_seconds)

        if self._render_fn is not None:
            out = self._render_fn(doc)
        else:
            from app.providers.video_engine.timeline_render import (
                TimelineRenderError,
                render_timeline,
            )

            try:
                out = render_timeline(self.workspace_id, self.session, doc,
                                      out_name=out_name)
            except TimelineRenderError as exc:
                raise UGCError(f"render failed: {exc}") from exc

        storage_key = self._store_output(out["path"], out_name)
        asset = MediaAsset(
            workspace_id=self.workspace_id, type="video", origin="render",
            provider="ugc_pipeline", storage_key=storage_key,
            mime_type="video/mp4",
            duration_seconds=out.get("duration_seconds"),
            width=out.get("width"), height=out.get("height"),
            meta_json={"project_id": self.project.id, "preset": self.preset,
                       "timeline_id": row.id, "timeline_version": row.version,
                       "warnings": out.get("warnings", [])})
        self.session.add(asset)
        self.session.flush()
        self.project.render_asset_ref = asset.id
        meta = {"has_audio": probe_has_audio(
            _workspace_storage_path(self.workspace_id, storage_key))}
        report = self.run_qc(file_meta=meta)
        if report.status == "FAIL":
            raise UGCBlockedError(
                f"rendered output failed QC ({report.status}) — output retained "
                f"as asset {asset.id} but the project stays BLOCKED")
        self.project.status = "RENDERED"
        self.session.flush()
        return {"asset_id": asset.id, "storage_key": storage_key,
                "duration_seconds": out.get("duration_seconds"),
                "timeline_id": row.id, "qc": report.to_dict()}

    def _store_output(self, path: str, out_name: str) -> str:
        from app.services.storage import STORAGE_ROOT

        dest_dir = STORAGE_ROOT / self.workspace_id
        dest_dir.mkdir(parents=True, exist_ok=True)
        safe = re.sub(r"[^A-Za-z0-9._-]", "_", out_name) or "ugc.mp4"
        dest = dest_dir / f"ugc_{self.project.id[:8]}_{int(time.time())}_{safe}"
        if dest.exists():
            dest.unlink()
        shutil.copyfile(path, dest)
        return dest.name

    # -- orchestration -----------------------------------------------------

    def run(self, *, render: bool = False, fetch_broll: bool = False) -> dict:
        """Brief → … → Timeline → (Render) → QC. Returns the project summary."""
        self.project.status = "RUNNING"
        self.session.flush()
        # Stage commits: hook/script/b-roll stages call the LLM, whose cost
        # recording writes from a second connection — holding one write
        # transaction across those calls stalls SQLite ~5s per attempt.
        self.session.commit()
        try:
            self.stage_brief()
            self.session.commit()
            self.stage_audience()
            self.session.commit()
            self.stage_hook()
            self.session.commit()
            self.stage_script()
            self.session.commit()
            self.stage_presenter()          # consent gate (raises if unauthorized)
            self.session.commit()
            self.stage_voice()
            self.session.commit()
            self.stage_product_assets()
            self.session.commit()
            self.stage_broll(fetch=fetch_broll)
            self.session.commit()
            self.stage_cta()
            self.session.commit()
            self.stage_presenter_render()
            self.session.commit()
            # MUSIC after voice/CTA (the narration it must fit is known) and
            # before the timeline (whose doc the bed is placed on).
            self.stage_music()
            self.session.commit()
            self.stage_timeline()
            self.session.commit()
        except Exception as exc:
            self.project.status = "FAILED"
            lineage = dict(self.project.lineage_json or {})
            lineage["error"] = f"{type(exc).__name__}: {str(exc)[:300]}"
            self.project.lineage_json = lineage
            self.session.flush()
            raise
        report = self.run_qc()
        result = {"project_id": self.project.id,
                  "timeline_id": self.project.timeline_id,
                  "status": self.project.status,
                  "qc": report.to_dict(),
                  "script": self.script,
                  "stages": ["brief", "audience", "hook", "script", "presenter",
                             "voice", "product_assets", "broll", "cta",
                             "music", "timeline", "qc"],
                  "music": dict(self.lineage.get("music") or {})}
        if render and report.status != "FAIL":
            result["render"] = self.render()
        return result

    def regenerate(self) -> dict:
        """Re-run the flow — NEVER over a manually edited timeline.

        If the live timeline no longer matches the manifest hash/version
        recorded at generation, refuse with REVIEW_REQUIRED and leave every
        row untouched (append-only history, human fixes win).
        """
        from app.models import ContentTimeline

        current = (self.session.get(ContentTimeline, self.project.timeline_id)
                   if self.project.timeline_id else None)
        lineage = dict(self.project.lineage_json or {})
        if current is not None:
            doc = dict(current.tracks_json or {})
            try:
                current_hash = render_manifest(doc)["manifest_hash"]
            except TimelineValidationError as exc:
                current_hash = f"invalid:{exc}"
            expected = str(lineage.get("manifest_hash") or "")
            expected_version = int(lineage.get("generated_version") or 1)
            if current_hash != expected or int(current.version or 1) != expected_version:
                reason = ("timeline was edited manually after generation "
                          "(manifest/version mismatch) — regenerate would clobber it")
                self.project.status = "REVIEW_REQUIRED"
                qc = dict(self.project.qc_json or {})
                qc["regeneration"] = {
                    "status": "REVIEW_REQUIRED", "refused": True, "reason": reason,
                    "expected_manifest_hash": expected,
                    "current_manifest_hash": current_hash,
                    "expected_version": expected_version,
                    "current_version": int(current.version or 1),
                    "timeline_id": current.id,
                }
                self.project.qc_json = qc
                self.session.flush()
                return {"refused": True, "status": "REVIEW_REQUIRED",
                        "reason": reason, "timeline_id": current.id,
                        "project_id": self.project.id}
        # untouched (or no timeline yet): build a NEW child timeline row
        before = self.project.timeline_id
        out = self.run(render=False)
        out["refused"] = False
        out["previous_timeline_id"] = before
        out["status"] = self.project.status
        return out


def _status_for(qc_status: str) -> str:
    return {"PASS": "READY", "PASS_WITH_WARNINGS": "READY",
            "REVIEW_REQUIRED": "REVIEW_REQUIRED", "FAIL": "BLOCKED"}.get(
                qc_status, "REVIEW_REQUIRED")


def _workspace_storage_path(workspace_id: str, storage_key: str):
    from app.services.storage import managed_path

    return str(managed_path(workspace_id, storage_key) or "")


__all__ = [
    "PRESET_DEFAULTS",
    "UGC_PRESETS",
    "UGCBlockedError",
    "UGCError",
    "UGCVideoPipeline",
    "normalize_brief",
    "split_segments",
]
