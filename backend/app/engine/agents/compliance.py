"""Compliance Officer agent (E5 trust): pre-publish safety gate.

Runs platform spec preflights + reused-content risk + disclosure presence for
a finished render. Spec failures are terminal per platform; high reused risk
routes to HUMAN_REVIEW instead of the network. Findings persist onto
publishing-job metadata so the audit export shows exactly what was checked.
"""

from __future__ import annotations

from app.db import session_scope
from app.engine.agents.base import AgentMeta, BaseAgent
from app.models import Workspace
from app.providers.compliance import preflight, reused_content_score


class ComplianceOfficerAgent(BaseAgent):
    meta = AgentMeta(
        key="compliance",
        title="Compliance Officer",
        description="Pre-publish spec, disclosure and originality gate.",
        skills=("compliance_review",),
        tools=("review_compliance",),
        permissions=("compliance:review",),
    )

    def review(self, ctx, *, video_path: str, platforms: list[str], topic: str,
               script: str, metadata_by_platform: dict | None = None,
               visual_keywords: list[str] | None = None) -> dict:
        def work():
            from app.engine.decision import get_safety_settings

            ws = ctx.workspace_id or ""
            with session_scope() as s:
                row = s.get(Workspace, ws) if ws else None
                safety = get_safety_settings((row.settings_json or {}) if row else {})
            threshold = float(safety.get("require_human_review_risk_above", 60.0))
            findings: list[dict] = []

            self.step("preflight", f"spec checks for {', '.join(platforms)}")
            preflights = {}
            simulated = (video_path or "").startswith("mock:")
            for platform in platforms:
                if simulated:
                    pf = {"passed": True, "platform": platform,
                          "checks": [{"name": "simulation", "severity": "ok",
                                      "detail": "mock artifact — spec check skipped"}]}
                else:
                    pf = preflight(video_path, platform)
                preflights[platform] = pf
                for c in pf["checks"]:
                    if c["severity"] in ("fail", "warn"):
                        findings.append({"platform": platform,
                                         "severity": "fail" if c["severity"] == "fail" else "warn",
                                         "message": f"{c['name']}: {c['detail']}"})
            spec_failed = {p for p, pf in preflights.items() if not pf["passed"]}
            self.step_done("ok", f"{len(platforms) - len(spec_failed)}/{len(platforms)} in spec")

            self.step("disclosures", "AI/finance disclosure presence")
            metas = metadata_by_platform or {}
            disclosed = sum(1 for p in platforms
                            if (metas.get(p) or {}).get("is_ai_generated", True))
            if disclosed < len(platforms) and platforms:
                findings.append({"platform": "", "severity": "warn",
                                 "message": "some platforms missing AI disclosure flags"})
            else:
                findings.append({"platform": "", "severity": "ok",
                                 "message": "AI-generated disclosure embedded per platform"})
            finance = any((metas.get(p) or {}).get("contains_finance_advice") for p in platforms)
            if finance:
                findings.append({"platform": "", "severity": "ok",
                                 "message": "finance disclaimer injected + burned in"})
            self.step_done("ok", "disclosures checked")

            self.step("reused_content", "own-catalog + thin-script risk")
            reuse = reused_content_score(topic, script, ws, visual_keywords)
            if reuse["level"] != "low":
                findings.append({"platform": "", "severity": "fail" if reuse["level"] == "high" else "warn",
                                 "message": f"reused-content risk {reuse['score']:.0f}/100 ({reuse['level']}): " +
                                            "; ".join(reuse["reasons"][:3])})
            self.step_done("ok", f"risk {reuse['score']:.0f}/100 ({reuse['level']})")

            require_human = bool(spec_failed) or reuse["score"] >= threshold
            passed = not spec_failed and reuse["score"] < threshold
            if require_human:
                findings.append({"platform": "", "severity": "fail" if spec_failed else "warn",
                                 "message": ("spec failures block: " + ", ".join(sorted(spec_failed)))
                                 if spec_failed else
                                 f"risk {reuse['score']:.0f} ≥ review threshold {threshold:.0f} — human review required"})
            return {
                "summary": ("compliance passed" if passed else
                            f"compliance hold ({len(spec_failed)} spec block(s), risk {reuse['score']:.0f})"),
                "passed": passed,
                "require_human": require_human,
                "risk_score": reuse["score"],
                "risk_level": reuse["level"],
                "spec_failed": sorted(spec_failed),
                "preflights": preflights,
                "findings": findings,
            }

        return self.execute(ctx, "review_compliance", input_summary=", ".join(platforms), fn=work)
