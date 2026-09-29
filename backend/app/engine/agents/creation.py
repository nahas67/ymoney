"""Research, strategy and script creation agents."""

from __future__ import annotations

import json
import logging
from typing import ClassVar

from app.engine.agents.base import AgentMeta, BaseAgent
from app.engine.knowledge.context_bridge import build_memory_context
from app.providers import llm

logger = logging.getLogger("ymoney.agents.creation")

_MEMORY_HEADER = "## Known (provenance-tracked) audience/context memory"


def _memory_block(db, ws, topic, *, out=None) -> str:
    """Render provenance-tracked knowledge memories as a prompt block (Lane E).

    Calls ``build_memory_context(db=db, task=topic, topic=topic, max_results=5,
    max_tokens=600)`` and renders one bullet per kept memory in the retriever's
    deterministic order (newline-prefixed so it slots into an existing prompt
    without touching surrounding bytes)::

        \\n## Known (provenance-tracked) audience/context memory
        - <content> [confidence <x>, <effective_status>, <n> sources]

    FAILURE ISOLATION: a falsy ``ws`` or ANY exception (empty/broken
    memory system, bridge failure) returns ``""`` and logs — a memory-system
    failure must never break research or strategize. When ``out`` is a dict it
    is filled in place with ``{"retrieved": int, "used_memory_ids": [...]}``
    from the build result (zeroed on failure) — that is the summary embedded
    as ``output["memory"]`` by the agents.
    """
    summary: dict = {"retrieved": 0, "used_memory_ids": []}
    block = ""
    try:
        if ws:
            result = build_memory_context(
                ws, db=db, task=topic, topic=topic, max_results=5, max_tokens=600
            )
            items = [
                item
                for item in (result.get("items") or [])
                if str(item.get("content") or "").strip()
            ]
            if items:
                lines = [
                    f"- {item['content']} "
                    f"[confidence {item.get('confidence')}, "
                    f"{item.get('effective_status') or item.get('status', '')}, "
                    f"{len(item.get('evidence_ids') or [])} sources]"
                    for item in items
                ]
                block = "\n" + _MEMORY_HEADER + "\n" + "\n".join(lines) + "\n"
            summary = {
                "retrieved": int((result.get("metrics") or {}).get("retrieved") or 0),
                "used_memory_ids": list(result.get("used_memory_ids") or []),
            }
    except Exception:  # noqa: BLE001 — memory is context, never a dependency
        logger.exception("knowledge memory block unavailable for topic %r", str(topic)[:80])
        block = ""
        summary = {"retrieved": 0, "used_memory_ids": []}
    if out is not None:
        out.clear()
        out.update(summary)
    return block


class ResearchAgent(BaseAgent):
    meta = AgentMeta(
        key="research",
        title="Research Agent",
        description="Gathers facts, angles and visual keywords for a topic.",
        skills=("research",),
        tools=("fetch_url",),
        permissions=("research:read",),
    )

    def research(self, ctx, topic: str) -> dict:
        self.step("collect_brief", f"LLM research brief for '{topic[:60]}'")

        # memory grounding (spec #25): retrieve topic-relevant semantic memories
        # so what the system already learned informs (not replaces) new research.
        memory_block = ""
        if ctx.workspace_id:
            try:
                from app.services import memory as memory_service

                mem = memory_service.retrieve_for_topic(ctx.workspace_id, topic, limit=4)
                if mem:
                    lines = "\n".join(f"- {m['content']} (confidence {m['confidence']})" for m in mem)
                    memory_block = (
                        "\nRelevant knowledge from this workspace's persistent memory "
                        "(treat as prior context, verify independently):\n" + lines + "\n"
                    )
                    self.step_done("ok", f"grounded with {len(mem)} memory record(s)")
                    self.step("collect_brief", f"LLM research brief for '{topic[:60]}'")
            except Exception:
                pass  # memory is context, never a dependency

        # Work 10 knowledge memory (Lane E): provenance-tracked audience/context
        # grounding. Failure-isolated: _memory_block swallows any memory-system
        # error and returns "" so research never breaks; the summary is embedded
        # into the output as output["memory"] below.
        known_out: dict = {"retrieved": 0, "used_memory_ids": []}
        known_block = _memory_block(
            getattr(ctx, "db", None), ctx.workspace_id or "", topic, out=known_out
        )

        res = llm.complete_json(
            system=(
                "You are a meticulous short-form video researcher. Produce a compact research "
                "brief as JSON with keys: summary (<=300 chars), key_facts (3-5 strings), "
                "angles (2-3 strings), visual_keywords (3-6 short stock-footage search terms), "
                "cautions (list), and claims: a list of {claim, status} where status is one of "
                "VERIFIED | LIKELY | UNCERTAIN | CONFLICTING, plus confidence (0-1) and basis "
                "(short string). Only include verifiable general knowledge; mark anything "
                "unverifiable as UNCERTAIN."
            ),
            user=f'Topic: "{topic}"{memory_block}{known_block}\nReturn JSON only.',
            workspace_id=ctx.workspace_id or "",
                        tier="cheap",
            model=self.model_for(ctx.workspace_id) if ctx.workspace_id else None,
        )
        self.step_done("ok", f"{len(res.get('key_facts') or [])} facts, {len(res.get('claims') or [])} claims")
        # normalize claims; derive an overall factual-confidence score
        self.step("verify_claims", "normalize claim statuses and compute factual confidence")
        claims = []
        for c in res.get("claims") or []:
            if not isinstance(c, dict) or not c.get("claim"):
                continue
            status = str(c.get("status", "UNCERTAIN")).upper()
            if status not in ("VERIFIED", "LIKELY", "UNCERTAIN", "CONFLICTING"):
                status = "UNCERTAIN"
            try:
                conf = float(c.get("confidence", 0.5))
            except (TypeError, ValueError):
                conf = 0.5
            claims.append({
                "claim": str(c["claim"])[:400],
                "status": status,
                "confidence": max(0.0, min(1.0, conf)),
                "basis": str(c.get("basis", ""))[:200],
            })
        weights = {"VERIFIED": 1.0, "LIKELY": 0.7, "UNCERTAIN": 0.35, "CONFLICTING": 0.15}
        if claims:
            factual_confidence = sum(weights[c["status"]] * c["confidence"] for c in claims) / len(claims)
        else:
            factual_confidence = 0.4
        conflicting = sum(1 for c in claims if c["status"] == "CONFLICTING")
        uncertain = sum(1 for c in claims if c["status"] == "UNCERTAIN")
        if conflicting:
            fact_status = "CONFLICTING"
        elif factual_confidence < 0.45 or (claims and uncertain > len(claims) / 2):
            fact_status = "INSUFFICIENT"
        else:
            fact_status = "OK"
        self.step_done("ok", f"confidence {factual_confidence:.2f} → {fact_status}")
        return {
            **res,
            "claims": claims,
            "factual_confidence": round(factual_confidence, 2),
            "fact_status": fact_status,
            "memory": {
                "retrieved": known_out["retrieved"],
                "used_memory_ids": list(known_out["used_memory_ids"]),
            },
        }

    def run(self, ctx, topic: str) -> dict:
        return self.execute(ctx, "research", input_summary=topic, fn=lambda: self.research(ctx, topic))


def _style_memory_block(workspace_id: str | None) -> str:
    """Render preference/strategic memories as prompt guidance (spec #25).
    Memory shapes style; it never replaces research or fabricates facts."""
    if not workspace_id:
        return ""
    try:
        from app.services import memory as memory_service

        mem = memory_service.style_context(workspace_id)
        if not mem:
            return ""
        lines = "\n".join(f"- {m['content']}" for m in mem)
        return f"\nWorkspace style guidance (must be honored):\n{lines}\n"
    except Exception:
        return ""


def _brand_gate(workspace_id: str | None, *, platform: str = "",
                artifact: dict | None = None) -> dict | None:
    """Brand hard-constraint gate (Work 08 Lane C).

    Lazily resolves Lane A's effective policy through
    ``engine.brand_templates`` and NEVER raises: when the brand module is
    absent (mid-merge) or the workspace is unknown, callers get ``None`` and
    behave exactly as before.
    """
    try:
        from app.engine.brand_templates import brand_gate

        return brand_gate(None, workspace_id or "", platform=platform,
                          artifact=artifact)
    except Exception:  # noqa: BLE001 — brand must never break creation
        return None


# template default -> strategy key (template fills gaps; the model still wins)
_TEMPLATE_STRATEGY_MAP = (
    ("aspect_ratio", "aspect_ratio"),
    ("hook_style", "hook_type"),
    ("tone", "tone"),
    ("pacing", "pacing"),
    ("caption_preset", "caption_preset"),
    ("cta_style", "cta_style"),
    ("broll_density", "broll_density"),
    ("music_preference", "music_preference"),
)


class StrategistAgent(BaseAgent):
    meta = AgentMeta(
        key="strategist",
        title="Content Strategist",
        description="Decides angle, format, hook style and platform plan.",
    )

    DEFAULT_STRATEGY: ClassVar[dict] = {
        "angle": "practical explainer with a contrarian hook",
        "target_audience": "viewers seeking quick actionable insight",
        "format": "talking-head narration over b-roll with captions",
        "duration_seconds": 32,
        "hook_type": "question",
        "tone": "confident, direct, friendly",
        "cta": "follow for more",
        "platforms": ["youtube", "tiktok"],
        "aspect_ratio": "9:16",
    }

    def strategize(self, ctx, topic: str, research: dict) -> dict:
        style_block = _style_memory_block(ctx.workspace_id)
        # Brand hard constraints (Work 08 Lane C): resolved BEFORE the model
        # call so tone/vocabulary/forbidden-phrase rules are prompt inputs;
        # the model never sees them as its own choices.
        gate = _brand_gate(ctx.workspace_id)
        style_block += str((gate or {}).get("instructions") or "")

        # Work 10 knowledge memory (Lane E): same failure-isolated block as the
        # Research Agent; summary embedded into the strategy output below.
        known_out: dict = {"retrieved": 0, "used_memory_ids": []}
        known_block = _memory_block(
            getattr(ctx, "db", None), ctx.workspace_id or "", topic, out=known_out
        )

        res = llm.complete_json(
            system=(
                "You are an elite short-form content strategist. Given a topic and research brief, "
                "return a JSON strategy: angle, target_audience, format, duration_seconds (25-60), "
                "hook_type (question|bold_claim|story|statistic|curiosity_gap), tone, cta, "
                "platforms (subset of youtube/tiktok/facebook/instagram), aspect_ratio "
                "(9:16 recommended for shorts/reels), rationale. Retention doctrine: the hook "
                "must land in the first 3 seconds; structure payoffs before attention wanes; "
                "balance educate/entertain/inspire across the plan; titles must work with the "
                "thumbnail as one micro-story (curiosity or extreme value, never clickbait)."
                + style_block
                + known_block
            ),
            user=json.dumps({"topic": topic, "research": research}, ensure_ascii=False),
            workspace_id=ctx.workspace_id or "",
                        tier="reasoning",
            model=self.model_for(ctx.workspace_id) if ctx.workspace_id else None,
        )
        merged = {**self.DEFAULT_STRATEGY, **{k: v for k, v in res.items() if v}}
        # Template inheritance (Work 08 Lane C): creative defaults fill the
        # keys the model did not return; brand still wins over the template.
        try:
            from app.engine.brand_templates import attach_template

            platforms = res.get("platforms") or self.DEFAULT_STRATEGY["platforms"]
            platform = str(platforms[0]) if platforms else ""
            attach_template(gate if gate is not None else {}, platform=platform,
                            artifact={"content_format": "short"},
                            default="shorts", short_form=True)
            defaults = (gate or {}).get("defaults") or {}
            for src, dst in _TEMPLATE_STRATEGY_MAP:
                value = defaults.get(src)
                if value and dst not in res:
                    merged[dst] = value
        except Exception:  # noqa: BLE001 — templates are defaults, never gates
            pass
        try:
            merged["duration_seconds"] = int(max(20, min(90, merged.get("duration_seconds", 32))))
        except (TypeError, ValueError):
            # LLM returned a non-numeric duration — fall back, don't kill the stage
            merged["duration_seconds"] = 32
        # Performance lessons (Work 06 Lane C): advisory recommendations only,
        # gated by the workspace `learning_assist` flag (default off). Guards
        # (duration clamp above, QC downstream) stay authoritative.
        try:
            from app.engine.performance import learning as _lessons

            merged = _lessons.maybe_apply_strategy_lessons(
                ctx.workspace_id or "", topic, merged)
        except Exception:
            pass
        # Brand HARD constraints beat template, model and lessons (Work 08).
        try:
            from app.engine.brand_templates import lineage_markers

            if gate and gate.get("applied_brand") and gate.get("tone"):
                merged["tone"] = gate["tone"]
            merged["brand"] = lineage_markers(gate)
        except Exception:  # noqa: BLE001
            # brand module unavailable — lineage must still be explicit:
            # applied_brand False + degraded marker, never silently absent.
            merged["brand"] = {"applied_brand": False,
                               "degraded": "brand_module_unavailable"}
        # Work 10 Lane E: memory grounding summary (never touches other keys).
        merged["memory"] = {
            "retrieved": known_out["retrieved"],
            "used_memory_ids": list(known_out["used_memory_ids"]),
        }
        return merged

    def run(self, ctx, topic: str, research: dict) -> dict:
        return self.execute(
            ctx, "strategy", input_summary=topic, fn=lambda: self.strategize(ctx, topic, research)
        )

    def plan_from_reference(self, ctx, *, url: str, topic: str = "") -> dict:
        """Reference-driven plan (OpenMontage-adapted): mine a reference video,
        then shape keeps/changes/cost/sample BEFORE any production spend.

        Returns keeps[] (pacing/hook/structure to preserve), changes[]
        (topic/visual/angle shifts for the new video), an honest flat cost
        estimate, and the top moment as the look-and-feel sample.
        """

        def work():
            from app.core.config import settings
            from app.engine.agents import repurpose as rep_mod

            self.step("mine_reference", url[:80])
            mined = rep_mod.LinkMinerAgent().mine(ctx, source=url, max_moments=5)
            moments = mined.get("moments", []) or []
            self.step_done("ok", f"{len(moments)} moment(s) from '{mined.get('source_title', '')[:40]}'")
            self.step("shape_plan", "keeps / changes / cost")
            res = llm.complete_json(
                system=(
                    "You are a reference analyst for short-form video. Given mined moments "
                    "(score/hook/reason/text) from a reference video and an optional new topic, "
                    "return JSON: keeps (up to 3 pacing/hook/structure traits worth preserving), "
                    "changes (up to 3 topic/visual/angle shifts for the new video). "
                    "Reply with JSON only."
                ),
                user=json.dumps(
                    {"topic": topic, "source_title": mined.get("source_title", ""),
                     "moments": moments[:8]}, ensure_ascii=False),
                workspace_id=ctx.workspace_id or "",
                tier="reasoning",
                model=self.model_for(ctx.workspace_id) if ctx.workspace_id else None,
            )
            keeps = [str(k)[:200] for k in (res.get("keeps") or []) if str(k).strip()][:3]
            changes = [str(c)[:200] for c in (res.get("changes") or []) if str(c).strip()][:3]
            sample = max(moments, key=lambda m: float(m.get("score", 0))) if moments else {}
            self.step_done("ok", f"{len(keeps)} keeps, {len(changes)} changes")
            return {
                "summary": f"reference plan from '{mined.get('source_title', '')[:60]}'",
                "source_title": mined.get("source_title", ""),
                "source_duration": mined.get("source_duration"),
                "topic": topic or mined.get("source_title", ""),
                "keeps": keeps,
                "changes": changes,
                "estimated_cost_usd": float(settings.mpt_estimated_render_cost_usd),
                "sample_moment": sample,
                "moments_analyzed": len(moments),
            }

        return self.execute(ctx, "reference_plan", input_summary=url[:200], fn=work)


def _registry_hooks() -> dict[str, str]:
    try:
        from app.services.templates import list_templates

        out = {}
        for t in list_templates("hooks"):
            tpl = (t.get("payload") or {}).get("template")
            if tpl:
                out[t["id"]] = tpl
        if out:
            return out
    except Exception:
        pass
    return {
        "question": 'What if {topic} could change how you handle money — starting today?',
        "bold_claim": 'Nobody talks about this side of {topic}.',
        "story": 'Three months ago I knew nothing about {topic}. Then this happened.',
        "statistic": '{topic} just changed everything — here are the numbers.',
        "curiosity_gap": 'The truth about {topic} that nobody explains properly.',
    }


HOOK_TEMPLATES = _registry_hooks()


class ScriptWriterAgent(BaseAgent):
    meta = AgentMeta(
        key="script_writer",
        title="Script Agent",
        description="Writes retention-optimized scripts with multiple variations.",
        skills=("scriptwriting",),
        tools=("generate_script",),
        permissions=("llm:generate",),
    )

    def write_script(self, ctx, topic: str, strategy: dict, research: dict) -> str:
        duration = strategy.get("duration_seconds", 30)
        style_block = _style_memory_block(ctx.workspace_id)
        # Brand hard constraints (Work 08 Lane C): tone/vocabulary/forbidden
        # phrases are prompt INPUTS; the deterministic post-check below is the
        # enforcement (the model can still slip — the text never does).
        gate = _brand_gate(ctx.workspace_id, artifact={"content_format": "short"})
        style_block += str((gate or {}).get("instructions") or "")

        res = llm.complete(
            system=(
                f"You write short-form video scripts (~{duration}s when spoken, roughly "
                f"{int(duration * 2.6)} words). Start with a scroll-stopping hook of type "
                f"'{strategy.get('hook_type', 'question')}'. Keep momentum, concrete specifics, "
                f"end with CTA: '{strategy.get('cta', 'follow for more')}'. Tone: "
                f"{strategy.get('tone', 'direct and friendly')}. Retention mechanics: no intro "
                f"logo or dead air — open on the hook; a pattern interrupt (new visual, cut, "
                f"caption emphasis) roughly every 8-12 seconds; end on the CTA directly, never "
                f"'thanks for watching'. Output ONLY the script text."
                + style_block
            ),
            user=json.dumps({"topic": topic, "research_brief": research}, ensure_ascii=False),
            workspace_id=ctx.workspace_id or "",
                        tier="reasoning",
            model=self.model_for(ctx.workspace_id) if ctx.workspace_id else None,
            temperature=0.85,
            max_tokens=400,
        )
        script = res.text.strip()
        # Enforce the documented ~2.6 words/second pacing: QC's word-count
        # component and the duration strategy both assume it. Free models
        # routinely blow past the budget (218 words observed on a 30s brief),
        # so trim to the nearest sentence boundary within budget.
        budget = int(duration * 2.6)
        words = script.split()
        if len(words) > budget + 20:
            trimmed = " ".join(words[:budget])
            # keep whole sentences: cut back to the last terminator
            for sep in (". ", "! ", "? "):
                idx = trimmed.rfind(sep)
                if idx > 0:
                    trimmed = trimmed[: idx + 1]
                    break
            script = trimmed.strip()
        # Deterministic brand post-check (Work 08 Lane C): forbidden phrases
        # are stripped from the script itself; hits are recorded on the
        # caller's per-run strategy copy for the variant/lineage audit trail.
        try:
            from app.engine.brand_templates import enforce_forbidden_phrases

            script, stripped = enforce_forbidden_phrases(script, gate)
            if stripped and isinstance(strategy, dict):
                strategy["brand_stripped"] = stripped
        except Exception:  # noqa: BLE001 — brand never breaks scriptwriting
            pass
        return script

    def run_variations(self, ctx, topic: str, strategy: dict, research: dict, count: int = 3,
                       regeneration_instruction: str | None = None) -> list[dict]:
        """Create N script+hook variants.

        When `regeneration_instruction` is set (QC rejection feedback), it is
        injected into every regenerated variant so the new scripts address the
        failure instead of repeating it.
        """
        variants = []

        def work():
            eff_strategy = dict(strategy)
            # Performance lessons (Work 06 Lane C): scope-matched guidance is
            # folded into a strategy copy when `learning_assist` is enabled
            # (default off); the caller's strategy dict is never mutated.
            lesson_keys: list[str] = []
            try:
                from app.engine.performance import learning as _lessons

                eff_strategy, lesson_keys = _lessons.script_guidance(
                    ctx.workspace_id or "", topic, eff_strategy)
            except Exception:
                eff_strategy, lesson_keys = dict(strategy), []
            if regeneration_instruction:
                base_prompt = str(eff_strategy.get("custom_system_prompt") or "")
                eff_strategy["custom_system_prompt"] = (
                    f"{base_prompt}\nIMPORTANT — QC regeneration notes: {regeneration_instruction}"
                ).strip()
            self.step("generate_variants", f"writing {count} variant(s) for '{topic[:60]}'")
            try:
                from app.engine.brand_templates import lineage_markers

                brand_markers = lineage_markers(
                    _brand_gate(ctx.workspace_id, artifact={"content_format": "short"}))
            except Exception:  # noqa: BLE001
                brand_markers = {}
            for i in range(count):
                jobs_service_check_cancelled(ctx)
                script = self.write_script(ctx, topic, eff_strategy, research)
                # Guard: some free models return meta-text or empty output.
                # A variant without a usable script would fail the render later
                # ("tts failed: text is empty"); retry once, then fall back to
                # a deterministic minimal script so the cycle can proceed.
                if not script or len(script.split()) < 8:
                    self.step("variant_retry", f"variant {i + 1} unusable ({len(script.split()) if script else 0} words), retrying")
                    script = self.write_script(ctx, topic, eff_strategy, research)
                if not script or len(script.split()) < 8:
                    script = (
                        f"Here is what you need to know about {topic}. "
                        f"This changes how you think about it — and most people miss why. "
                        f"Follow for more on {topic}."
                    )
                variant: dict = {"label": f"v{i + 1}", "script": script}
                if lesson_keys:
                    variant["applied_lessons"] = list(lesson_keys)
                if brand_markers:
                    variant["brand"] = dict(brand_markers)
                stripped = (eff_strategy or {}).pop("brand_stripped", None)
                if stripped:
                    variant["brand_stripped"] = list(stripped)
                variants.append(variant)
            self.step_done("ok", f"{len(variants)} variant(s) written")
            return variants

        return self.execute(
            ctx, "script_variations",
            input_summary=f"{count} variations for {topic}"
                          + (f" [regen: {regeneration_instruction[:80]}]" if regeneration_instruction else ""),
            fn=work,
        )


def jobs_service_check_cancelled(ctx) -> None:
    from app.services import jobs as _jobs

    _jobs.check_cancelled(ctx)


class HookOptimizerAgent(BaseAgent):
    meta = AgentMeta(
        key="hook_optimizer",
        title="Hook Optimizer",
        description="Generates and ranks hooks to maximize retention.",
    )

    HOOK_SIGNALS = (
        ("question", 82),
        ("bold_claim", 78),
        ("curiosity_gap", 80),
        ("statistic", 74),
        ("command", 76),
        ("story", 70),
    )
    # First-120-chars markers per hook type (TikTok/Video-Opt persona signals:
    # commands convert like questions; concrete numbers beat abstractions).
    HOOK_MARKERS: ClassVar[dict[str, tuple[str, ...]]] = {
        "question": ("?",),
        "bold_claim": ("nobody",),
        "curiosity_gap": ("truth",),
        "statistic": ("numbers",),
        "command": ("stop", "never", "don't"),
        "story": ("ago",),
    }
    NUMBER_BONUS = 4.0

    def run(self, ctx, variants: list[dict], patterns: list[dict] | None = None,
            scope: dict | None = None) -> list[dict]:
        return self.rank_hooks(ctx, variants, patterns, scope=scope)

    def rank_hooks(self, ctx, variants: list[dict], patterns: list[dict] | None = None,
                   scope: dict | None = None) -> list[dict]:
        """Attach predicted hook strength to each variant and sort desc.

        When the workspace enables `learning_assist` and a scope is given,
        scope-matched lessons add a small deterministic prior bonus and audit
        tags; otherwise scoring is byte-identical to the legacy rubric.
        """

        def work():
            pattern_hook_bonus = 0.0
            active_patterns = [
                p for p in (patterns or [])
                if p.get("active", True) and p.get("confidence") in ("medium", "high")
            ]
            for p in active_patterns:
                if "hook" in (p.get("pattern_key") or ""):
                    pattern_hook_bonus += p.get("observed_improvement_pct", 0) / 10.0
            for v in variants:
                base = 60.0
                opening = v.get("script", "").lower()[:120]
                for kind, score in self.HOOK_SIGNALS:
                    if any(m in opening for m in self.HOOK_MARKERS[kind]):
                        base = float(score)
                        break
                if any(c.isdigit() for c in opening):
                    base += self.NUMBER_BONUS
                v["predicted_score"] = round(min(100.0, base + pattern_hook_bonus), 1)
            variants.sort(key=lambda v: -(v.get("predicted_score") or 0))
            # Performance lessons (Work 06 Lane C): thin, default-off.
            ranked = variants
            if scope:
                try:
                    from app.engine.performance import learning as _lessons

                    ranked = _lessons.apply_hook_lesson_bonus(
                        ctx.workspace_id or "", scope, ranked)
                except Exception:
                    pass
            # Brand hard constraints (Work 08 Lane C): a hook carrying a
            # forbidden phrase can NEVER win — it is scored 0 and flagged
            # (kept in the list for auditability, always sorted last).
            try:
                from app.engine.brand_templates import hook_rejection_reason, lineage_markers

                gate = _brand_gate(ctx.workspace_id,
                                   artifact={"content_format": "short"})
            except Exception:  # noqa: BLE001
                gate = None
            if gate and gate.get("brand_available"):
                markers = lineage_markers(gate)
                for v in ranked:
                    opening = str(v.get("hook") or v.get("script") or "")[:240]
                    reason = hook_rejection_reason(opening, gate)
                    if reason:
                        v["predicted_score"] = 0.0
                        v["brand_blocked"] = True
                        v["brand_rejection_reason"] = reason
                    v["brand"] = dict(markers)
                ranked.sort(key=lambda v: -(v.get("predicted_score") or 0))
            return ranked

        return self.execute(ctx, "rank_hooks", input_summary=f"{len(variants)} candidates", fn=work)



