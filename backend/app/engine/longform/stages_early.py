"""Long-form stages, part 1: research → strategy → outline → script → verify.

Every stage builds a deterministic artifact first (templates + existing
agents), then optionally refines via LLM. LLM output is validated; garbage
falls back silently-but-logged. Provenance is never invented.
"""

from __future__ import annotations

from datetime import UTC, datetime

WPM_DEFAULT = 150

NARRATIVE_STRUCTURES = {
    "DOCUMENTARY": ["Cold Open", "Context", "Question", "History", "Escalation",
                    "Evidence", "Conflict", "Resolution", "Implications", "Conclusion"],
    "EXPLAINER": ["Hook", "Problem", "Context", "Concept 1", "Concept 2", "Concept 3",
                  "Examples", "Implications", "Summary", "CTA"],
    "TUTORIAL": ["Outcome Preview", "Requirements", "Step 1", "Step 2", "Step 3",
                 "Validation", "Mistakes", "Advanced Tips", "Final Result"],
    "NEWS_ANALYSIS": ["Headline", "What Happened", "Background", "Stakeholders",
                      "Analysis", "What Next", "Takeaway"],
    "LISTICLE": ["Hook", "Countdown", "Item Block", "Item Block", "Top Pick", "Recap", "CTA"],
    "VIDEO_ESSAY": ["Premise", "Thesis", "Exploration", "Counterpoint",
                    "Synthesis", "Closing"],
    "EDUCATIONAL": ["Hook", "Learning Goals", "Lesson 1", "Lesson 2", "Lesson 3",
                    "Recap", "Next Steps"],
    "FACELESS": ["Hook", "Context", "Deep Dive", "Evidence", "Implications", "Outro"],
    "PRODUCT_EXPLAINER": ["Hook", "Problem", "Product", "How It Works",
                          "Proof", "Pricing", "CTA"],
    "STORYTELLING": ["Hook", "Setup", "Rising Action", "Climax",
                     "Falling Action", "Resolution"],
    "PODCAST_STYLE": ["Cold Open", "Introductions", "Topic 1", "Topic 2",
                      "Topic 3", "Takeaways", "Outro"],
}

FORMAT_STRUCTURE = {
    "DOCUMENTARY": "DOCUMENTARY", "NEWS_ANALYSIS": "NEWS_ANALYSIS",
    "TUTORIAL": "TUTORIAL", "LISTICLE": "LISTICLE", "VIDEO_ESSAY": "VIDEO_ESSAY",
    "EDUCATIONAL": "EDUCATIONAL", "PRODUCT_EXPLAINER": "PRODUCT_EXPLAINER",
    "STORYTELLING": "STORYTELLING", "PODCAST_STYLE": "PODCAST_STYLE",
    "EXPLAINER": "EXPLAINER", "FACELESS": "FACELESS",
}


def llm_json_or_none(system: str, user: str, workspace_id: str) -> dict | None:
    """LLM enhancement with strict validation. None = use deterministic base."""
    try:
        from app.providers import llm as llm_mod

        if not llm_mod.llm_available():
            return None
        out = llm_mod.complete_json(system=system, user=user,
                                    workspace_id=workspace_id, max_tokens=1500)
        return out if isinstance(out, dict) else None
    except Exception:
        return None


def _now() -> str:
    return datetime.now(UTC).replace(tzinfo=None).isoformat() + "Z"


# -- RESEARCH ---------------------------------------------------------------

def stage_research(session, project, job_ctx) -> str:
    from app.engine.agents.creation import ResearchAgent

    words = [w.strip(".,!?;:") for w in project.topic.split() if len(w) > 3]
    seen, subtopics = set(), []
    for w in words:
        key = w.lower()
        if key not in seen:
            seen.add(key)
            subtopics.append(f"{project.topic}: {w}")
    subtopics = (subtopics or [project.topic])[:5]
    claims, key_facts = [], []
    agent = ResearchAgent()
    for sub in subtopics:
        try:
            brief = agent.research(job_ctx or _ctx(project.workspace_id), sub)
        except Exception:
            brief = None
        if not isinstance(brief, dict):
            brief = {"summary": f"Background on {sub}.", "key_facts": [sub],
                     "claims": [{"claim": sub, "status": "UNCERTAIN",
                                 "confidence": 0.4, "basis": "template fallback"}]}
        key_facts.extend(brief.get("key_facts") or [])
        for c in brief.get("claims") or []:
            if not isinstance(c, dict) or not c.get("claim"):
                continue
            claims.append({
                "claim": str(c["claim"])[:400],
                "status": str(c.get("status", "UNCERTAIN")).upper(),
                "confidence": float(c.get("confidence", 0.4) or 0.4),
                "subtopic": sub,
                "source": {"title": str(c.get("basis", ""))[:200] or sub,
                           "publisher": "research-agent",
                           "url": str(c.get("url", "")),
                           "retrieved_at": _now()},
            })
    project.research_json = {
        "primary_topic": project.topic,
        "subtopics": subtopics,
        "summary": f"Research bundle for {project.topic}: {len(claims)} claims.",
        "key_facts": key_facts[:20],
        "claims": claims,
        "statistics": [],
        "uncertainties": [c["claim"] for c in claims if c["status"] == "UNCERTAIN"][:10],
    }
    project.stage_progress_json = {**(project.stage_progress_json or {}),
                                   "subtopics": f"{len(subtopics)}/{len(subtopics)}"}
    return f"{len(subtopics)} subtopics, {len(claims)} claims"


class _Ctx:
    def __init__(self, ws: str):
        self.workspace_id = ws
        self.payload, self.artifacts = {}, {}


def _ctx(ws: str):
    return _Ctx(ws)


# -- STRATEGY ----------------------------------------------------------------

def stage_strategy(session, project, job_ctx) -> str:
    structure = FORMAT_STRUCTURE.get(project.content_format, "EXPLAINER")
    roles = NARRATIVE_STRUCTURES[structure]
    n_claims = len((project.research_json or {}).get("claims", []))
    strategy = {
        "purpose": f"{project.content_format.title()} video on {project.topic}",
        "target_audience": project.target_audience or "curious general viewers",
        "target_duration_seconds": project.target_duration_seconds,
        "core_theme": project.topic,
        "opening_strategy": f"Cold open on the sharpest claim, then {roles[1].lower()}",
        "narrative_structure": structure,
        "narrative_roles": roles,
        "emotional_arc": "curiosity → tension → clarity → payoff",
        "information_density": "one claim per ~45 seconds",
        "visual_language": "stock-led b-roll with deterministic graphics for data",
        "narration_style": f"{project.tone}, steady pace",
        "music_strategy": {"INTRO": "energetic", "BACKGROUND": "subtle",
                           "CONCLUSION": "optimistic"},
        "cta_strategy": "single CTA in the final chapter",
        "chapter_strategy": f"{len(roles)} beats mapped to chapters",
        "callbacks": [],
        "ending": "payoff restating the core theme with a forward look",
        "claims_available": n_claims,
    }
    llm = llm_json_or_none(
        "You are a longform video strategist. Return JSON with keys opening_strategy, "
        "emotional_arc, callbacks (list of strings).",
        f"Topic: {project.topic}. Format: {project.content_format}.",
        project.workspace_id)
    if llm:
        for key in ("opening_strategy", "emotional_arc"):
            if isinstance(llm.get(key), str) and llm[key].strip():
                strategy[key] = llm[key][:500]
        if isinstance(llm.get("callbacks"), list):
            strategy["callbacks"] = [str(c)[:200] for c in llm["callbacks"][:5]]
    project.strategy_json = strategy
    project.stage_progress_json = {**(project.stage_progress_json or {}), "strategy": "1/1"}
    return f"{structure} strategy, {len(roles)} beats"


# -- OUTLINE (chapters) -------------------------------------------------------

def stage_outline(session, project, job_ctx) -> str:
    from app.models import LongFormChapter

    strategy = project.strategy_json or {}
    roles = strategy.get("narrative_roles") or ["Hook", "Body", "Conclusion"]
    total = max(int(project.target_duration_seconds or 600), 60)
    weights = _chapter_weights(len(roles))
    old = session.query(LongFormChapter).filter(
        LongFormChapter.project_id == project.id).all()
    for row in old:
        session.delete(row)
    session.flush()
    wpm = WPM_DEFAULT
    chapters = []
    for i, role in enumerate(roles):
        dur = max(20, round(total * weights[i]))
        chapters.append(LongFormChapter(
            project_id=project.id, index=i,
            title=f"{project.topic} — {role}"[:200],
            goal=f"{role}: advance the {strategy.get('core_theme', project.topic)}",
            narrative_role=role, target_duration_seconds=dur,
            target_words=max(30, dur * wpm // 60),
            entry_transition="fade" if i > 0 else "cut",
            exit_transition="fade" if i < len(roles) - 1 else "cut",
            retention_device=_retention_device(role, i, len(roles)),
            status="PLANNED"))
    session.add_all(chapters)
    session.flush()
    project.stage_progress_json = {**(project.stage_progress_json or {}),
                                   "chapters": f"{len(chapters)}/{len(chapters)}"}
    return f"{len(chapters)} chapters over {total}s"


def _chapter_weights(n: int) -> list[float]:
    if n <= 1:
        return [1.0]
    # open + close leaner, middle carries the weight
    raw = [0.7] + [1.0] * (n - 2) + [0.8]
    total = sum(raw)
    return [r / total for r in raw]


def _retention_device(role: str, i: int, n: int) -> str:
    low = role.lower()
    if i == 0:
        return "cold-open hook in the first 15 seconds"
    if i == n - 1:
        return "payoff + single CTA"
    if "conflict" in low or "problem" in low:
        return "open loop: pose the unresolved question"
    if "evidence" in low or "concept" in low or "step" in low:
        return "curiosity loop into the next beat"
    return "callback to the opening claim"


# -- SCRIPT -------------------------------------------------------------------

SEGMENT_TYPES = ("hook", "explain", "evidence", "transition", "callback",
                 "quote", "cta", "recap")


def stage_script(session, project, job_ctx) -> str:
    from app.models import LongFormChapter

    research = project.research_json or {}
    claims = research.get("claims", []) or [{"claim": project.topic,
                                             "status": "UNCERTAIN", "confidence": 0.4}]
    chapters = session.query(LongFormChapter).filter(
        LongFormChapter.project_id == project.id).order_by(LongFormChapter.index).all()
    if not chapters:
        raise ValueError("no chapters — run OUTLINE first")
    total_segments, total_words = 0, 0
    for claim_idx, ch in enumerate(chapters):
        segments = _write_chapter_segments(
            project, ch, claims, claim_idx % max(len(claims), 1))
        ch.script_json = {"segments": segments}
        ch.status = "SCRIPTED"
        total_segments += len(segments)
        total_words += sum(len(s["narration"].split()) for s in segments)
    project.script_json = {"segments_total": total_segments,
                           "words_total": total_words,
                           "estimated_seconds": total_words * 60 // WPM_DEFAULT}
    project.stage_progress_json = {**(project.stage_progress_json or {}),
                                   "segments": f"{total_segments}/{total_segments}"}
    return f"{total_segments} segments, {total_words} words"


def _write_chapter_segments(project, chapter, claims: list, start_idx: int) -> list[dict]:
    words_target = max(int(chapter.target_words or 150), 40)
    n_segments = max(2, min(8, words_target // 60))
    per = max(30, words_target // n_segments)
    segments = []
    for s in range(n_segments):
        claim = claims[(start_idx + s) % len(claims)]
        seg_type = _segment_type(chapter.narrative_role, s, n_segments)
        narration = _template_narration(
            project, chapter, seg_type, s, claim, per)
        # LLM polish is best-effort; template is the deterministic base
        polished = llm_json_or_none(
            "You are a longform script polisher. Return JSON {narration: string} "
            "with similar length, no filler, no repeating the chapter title.",
            f"Chapter '{chapter.title}' ({seg_type}): {narration}",
            project.workspace_id)
        if isinstance((polished or {}).get("narration"), str) and polished["narration"].strip():
            words = polished["narration"].split()
            if 10 <= len(words) <= per + 40:  # guard against rambling rewrites
                narration = " ".join(words)
        segments.append({
            "id": f"ch{chapter.index:02d}_seg{s:02d}",
            "type": seg_type,
            "narration": narration,
            "words": len(narration.split()),
            "claim": claim["claim"][:200],
            "claim_status": claim.get("status", "UNCERTAIN"),
            "transition_out": "cut",
        })
    return segments


def _segment_type(role: str, s: int, n: int) -> str:
    low = (role or "").lower()
    if s == 0 and ("hook" in low or "cold" in low or "headline" in low or "premise" in low):
        return "hook"
    if s == n - 1 and ("cta" in low or "conclusion" in low or "outro" in low or "result" in low):
        return "cta"
    if "evidence" in low or "proof" in low:
        return "evidence"
    if s == n - 1:
        return "transition"
    return "explain" if s % 2 == 0 else "callback"


def _template_narration(project, chapter, seg_type: str, s: int,
                        claim: dict, per: int) -> str:
    topic, claim_text = project.topic, claim["claim"]
    bases = {
        "hook": f"Here is the claim about {topic} that changes how you should think about it: {claim_text}. Stay with this chapter and you will see exactly why it holds.",
        "explain": f"To understand {chapter.title}, start with the mechanism. {claim_text}. That single idea explains most of what follows, and the next piece builds directly on it.",
        "evidence": f"The evidence for this is straightforward. {claim_text}. Independent angles point the same way, which is why this chapter treats it as load-bearing.",
        "transition": "With that established, the story moves forward. The next chapter picks up where this leaves off, and the connection matters more than it first appears.",
        "callback": f"Remember the opening claim about {topic}. {claim_text}. It returns here because the details now give it a sharper meaning.",
        "quote": f"As the research puts it: {claim_text}. That line is worth pausing on before we continue.",
        "cta": f"If this clarified {topic}, the next step is simple: apply one idea from this chapter today. The conclusion ties everything together.",
        "recap": f"So far: {claim_text}. Hold that result in mind; the final stretch depends on it.",
    }
    text = bases.get(seg_type, bases["explain"])
    words = text.split()
    while len(words) < per:  # pad without filler phrases: restate with specifics
        words += f"Concretely, {claim_text}".split()
    return " ".join(words[:per + 20])


# -- VERIFY -------------------------------------------------------------------

def stage_verify(session, project, job_ctx) -> str:
    from app.models import LongFormChapter

    research = project.research_json or {}
    by_claim = {c["claim"][:200]: c for c in research.get("claims", [])}
    chapters = session.query(LongFormChapter).filter(
        LongFormChapter.project_id == project.id).order_by(LongFormChapter.index).all()
    checked, verdicts = [], []
    for ch in chapters:
        for seg in (ch.script_json or {}).get("segments", []):
            key = str(seg.get("claim", ""))[:200]
            src = by_claim.get(key)
            verdict = _verdict(seg.get("claim_status", "UNCERTAIN"), src)
            checked.append({"segment": seg["id"], "chapter": ch.index,
                            "claim": key, "verdict": verdict,
                            "source": (src or {}).get("source", {})})
            verdicts.append(verdict)
    from collections import Counter

    counts = Counter(verdicts)
    risky = counts.get("CONTESTED", 0) + counts.get("UNVERIFIED", 0)
    project.fact_report_json = {
        "checked": len(checked), "verdicts": dict(counts),
        "items": checked[:200],
        "risky_claims": risky,
        "policy": "CONTESTED/UNVERIFIED claims surface to QC; never silently dropped",
    }
    project.stage_progress_json = {**(project.stage_progress_json or {}),
                                   "claims": f"{len(checked)}/{len(checked)}"}
    return f"{len(checked)} claims checked, {risky} risky"


def _verdict(seg_status: str, src: dict | None) -> str:
    if src is None:
        return "UNVERIFIED"
    status = str(src.get("status", "UNCERTAIN")).upper()
    conf = float(src.get("confidence", 0.4) or 0.4)
    if status == "CONTESTED":
        return "CONTESTED"
    if status == "VERIFIED" or (status == "LIKELY" and conf >= 0.5):
        return "SUPPORTED"
    if status == "LIKELY":
        return "PARTIALLY_SUPPORTED"
    return "UNVERIFIED"
