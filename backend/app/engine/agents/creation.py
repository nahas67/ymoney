"""Research, strategy and script creation agents."""

from __future__ import annotations

import json

from app.engine.agents.base import AgentMeta, BaseAgent
from app.providers import llm


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
            user=f'Topic: "{topic}"{memory_block}\nReturn JSON only.',
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


class StrategistAgent(BaseAgent):
    meta = AgentMeta(
        key="strategist",
        title="Content Strategist",
        description="Decides angle, format, hook style and platform plan.",
    )

    DEFAULT_STRATEGY = {
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

        res = llm.complete_json(
            system=(
                "You are an elite short-form content strategist. Given a topic and research brief, "
                "return a JSON strategy: angle, target_audience, format, duration_seconds (25-60), "
                "hook_type (question|bold_claim|story|statistic|curiosity_gap), tone, cta, "
                "platforms (subset of youtube/tiktok/facebook/instagram), aspect_ratio "
                "(9:16 recommended for shorts/reels), rationale."
                + style_block
            ),
            user=json.dumps({"topic": topic, "research": research}, ensure_ascii=False),
            workspace_id=ctx.workspace_id or "",
                        tier="reasoning",
            model=self.model_for(ctx.workspace_id) if ctx.workspace_id else None,
        )
        merged = {**self.DEFAULT_STRATEGY, **{k: v for k, v in res.items() if v}}
        merged["duration_seconds"] = int(max(20, min(90, merged.get("duration_seconds", 32))))
        return merged

    def run(self, ctx, topic: str, research: dict) -> dict:
        return self.execute(
            ctx, "strategy", input_summary=topic, fn=lambda: self.strategize(ctx, topic, research)
        )


HOOK_TEMPLATES = {
    "question": 'What if {topic} could change how you handle money — starting today?',
    "bold_claim": 'Nobody talks about this side of {topic}.',
    "story": 'Three months ago I knew nothing about {topic}. Then this happened.',
    "statistic": '{topic} just changed everything — here are the numbers.',
    "curiosity_gap": 'The truth about {topic} that nobody explains properly.',
}


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

        res = llm.complete(
            system=(
                f"You write short-form video scripts (~{duration}s when spoken, roughly "
                f"{int(duration * 2.6)} words). Start with a scroll-stopping hook of type "
                f"'{strategy.get('hook_type', 'question')}'. Keep momentum, concrete specifics, "
                f"end with CTA: '{strategy.get('cta', 'follow for more')}'. Tone: "
                f"{strategy.get('tone', 'direct and friendly')}. Output ONLY the script text."
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
            if regeneration_instruction:
                base_prompt = str(eff_strategy.get("custom_system_prompt") or "")
                eff_strategy["custom_system_prompt"] = (
                    f"{base_prompt}\nIMPORTANT — QC regeneration notes: {regeneration_instruction}"
                ).strip()
            self.step("generate_variants", f"writing {count} variant(s) for '{topic[:60]}'")
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
                variants.append({"label": f"v{i + 1}", "script": script})
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
        ("story", 70),
    )

    def run(self, ctx, variants: list[dict], patterns: list[dict] | None = None) -> list[dict]:
        return self.rank_hooks(ctx, variants, patterns)

    def rank_hooks(self, ctx, variants: list[dict], patterns: list[dict] | None = None) -> list[dict]:
        """Attach predicted hook strength to each variant and sort desc."""

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
                for kind, score in self.HOOK_SIGNALS:
                    marker = {"question": "?", "bold_claim": "Nobody", "curiosity_gap": "truth",
                              "statistic": "numbers", "story": "ago"}[kind]
                    if marker.lower() in v.get("script", "").lower()[:120]:
                        base = float(score)
                        break
                v["predicted_score"] = round(min(100.0, base + pattern_hook_bonus), 1)
            variants.sort(key=lambda v: -(v.get("predicted_score") or 0))
            return variants

        return self.execute(ctx, "rank_hooks", input_summary=f"{len(variants)} candidates", fn=work)



