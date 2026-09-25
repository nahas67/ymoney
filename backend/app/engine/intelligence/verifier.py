"""CompletionVerifier (Lane C, Work 05).

``execution_status`` (what happened) is SEPARATE from ``verification_status``
(what was proven):

- execution_status: COMPLETED | FAILED | RUNNING | UNKNOWN
- verification_status: VERIFIED | PARTIALLY_VERIFIED | NOT_VERIFIED | BLOCKED

Checkers (video / publication / campaign / research) record per-check
{name, passed, detail} evidence and append to the ledger. MOCK-labeled
publication results verify ONLY as mock, never live.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

VERIFICATION_STATUSES = ("VERIFIED", "PARTIALLY_VERIFIED", "NOT_VERIFIED", "BLOCKED")
EXECUTION_STATUSES = ("COMPLETED", "FAILED", "RUNNING", "UNKNOWN")
KINDS = ("video", "publication", "campaign", "research")

BLOCKED_CROSS_WORKSPACE = "cross-workspace subject (isolation)"


@dataclass
class CompletionContract:
    kind: str  # video|publication|campaign|research
    subject_id: str
    expectations: dict = field(default_factory=dict)


@dataclass
class CheckResult:
    name: str
    passed: bool
    detail: str = ""
    critical: bool = True

    def as_dict(self) -> dict:
        return {"name": self.name, "passed": self.passed,
                "detail": self.detail, "critical": self.critical}


def _verdict(checks: list[CheckResult]) -> str:
    if not checks:
        return "NOT_VERIFIED"
    critical_failed = [c for c in checks if c.critical and not c.passed]
    if critical_failed:
        return "NOT_VERIFIED"
    if all(c.passed for c in checks):
        return "VERIFIED"
    return "PARTIALLY_VERIFIED"


def _blocked(reason: str) -> tuple[str, list[dict]]:
    return "BLOCKED", [{"name": "isolation", "passed": False,
                        "detail": reason, "critical": True}]


# ---------------------------------------------------------------------------
# video
# ---------------------------------------------------------------------------

def check_video(session, workspace_id: str, contract: CompletionContract) -> tuple[str, str, list[dict]]:
    """Verify a rendered video: real file, non-zero, ffprobe-valid, QC ok."""
    from app.models import MediaAsset, QualityCheck, Video

    video = session.get(Video, contract.subject_id)
    if video is None or video.workspace_id != workspace_id:
        return "UNKNOWN", *_blocked(BLOCKED_CROSS_WORKSPACE)
    exp = contract.expectations or {}
    checks: list[CheckResult] = []
    execution = "COMPLETED" if video.status == "READY" else (
        "FAILED" if video.status == "FAILED" else "RUNNING")

    path = video.file_path or ""
    is_mock = path.startswith("mock:")
    real_path = None if is_mock else Path(path) if path else None
    exists = bool(real_path is not None and real_path.exists())
    size = real_path.stat().st_size if exists else 0
    checks.append(CheckResult("record_registered", True,
                              f"video {video.id} status={video.status}"))
    checks.append(CheckResult("file_real_nonzero", exists and size > 0,
                              f"path={path[:120]} size={size} mock={is_mock}"))

    meta: dict = {}
    if exists and size > 0:
        try:
            from app.services.storage import probe_metadata

            meta = probe_metadata(real_path) or {}
        except Exception as exc:  # noqa: BLE001 — probe is best-effort
            meta = {"probe_error": str(exc)[:120]}
    has_streams = bool(meta.get("duration_seconds") or meta.get("width"))
    checks.append(CheckResult("probe_valid", has_streams,
                              f"duration={meta.get('duration_seconds')} "
                              f"res={meta.get('width')}x{meta.get('height')}"))

    if "duration_seconds" in exp and meta.get("duration_seconds"):
        try:
            want = float(exp["duration_seconds"])
            got = float(meta["duration_seconds"])
            tol = float(exp.get("duration_tolerance", 2.0))
            checks.append(CheckResult("duration_matches", abs(got - want) <= tol,
                                      f"expected {want}s got {got}s tol {tol}s"))
        except (TypeError, ValueError):
            checks.append(CheckResult("duration_matches", False, "bad expectation"))
    if "resolution" in exp and meta.get("width"):
        want_res = str(exp["resolution"])
        got_res = f"{meta.get('width')}x{meta.get('height')}"
        checks.append(CheckResult("resolution_matches", want_res == got_res,
                                  f"expected {want_res} got {got_res}"))

    qc_rows = session.query(QualityCheck).filter(
        QualityCheck.video_id == video.id).all()
    if qc_rows:
        latest = sorted(qc_rows, key=lambda r: r.created_at)[-1]
        min_score = float(exp.get("min_qc_score", 60.0))
        checks.append(CheckResult("qc_acceptable",
                                  bool(latest.passed) and float(latest.overall) >= min_score,
                                  f"overall={latest.overall} passed={latest.passed} min={min_score}"))
    else:
        checks.append(CheckResult("qc_acceptable", False, "no quality check recorded",
                                  critical=False))

    asset = None
    try:
        asset = session.query(MediaAsset).filter(
            MediaAsset.workspace_id == workspace_id,
            MediaAsset.storage_key == Path(path).name if path and not is_mock else "__none__",
        ).first()
    except Exception:
        asset = None
    checks.append(CheckResult("db_asset_registered", asset is not None or is_mock,
                              "mock render" if is_mock else (
                                  f"asset={asset.id}" if asset else "no MediaAsset row"),
                              critical=False))
    return execution, _verdict(checks), [c.as_dict() for c in checks]


# ---------------------------------------------------------------------------
# publication (mock verifies ONLY as mock, never live)
# ---------------------------------------------------------------------------

def check_publication(session, workspace_id: str,
                      contract: CompletionContract) -> tuple[str, str, list[dict]]:
    from app.models import PublishedPost

    post = session.get(PublishedPost, contract.subject_id)
    if post is None or post.workspace_id != workspace_id:
        return "UNKNOWN", *_blocked(BLOCKED_CROSS_WORKSPACE)
    exp = contract.expectations or {}
    checks: list[CheckResult] = []
    execution = "COMPLETED" if post.remote_post_id else "FAILED"

    checks.append(CheckResult("receipt_present", bool(post.remote_post_id),
                              f"remote_post_id={post.remote_post_id[:60]}"))
    checks.append(CheckResult("remote_id_present", bool(post.remote_post_id),
                              f"remote_url={(post.remote_url or '')[:120]}"))
    dupes = session.query(PublishedPost).filter(
        PublishedPost.video_id == post.video_id,
        PublishedPost.platform == post.platform).count()
    checks.append(CheckResult("idempotency_single_record", dupes == 1,
                              f"{dupes} row(s) for video+platform"))
    if "platform" in exp:
        checks.append(CheckResult("platform_match", post.platform == exp["platform"],
                                  f"expected {exp['platform']} got {post.platform}"))
    if "account_id" in exp and exp["account_id"]:
        checks.append(CheckResult("account_match",
                                  (post.account_id or "") == exp["account_id"],
                                  f"expected {exp['account_id']} got {post.account_id}"))

    mode = "mock" if post.is_mock else "live"
    if post.is_mock:
        checks.append(CheckResult("mock_classified", True,
                                  "MOCK-labeled: verifies as mock only, never live",
                                  critical=False))
        if exp.get("live") is True:
            checks.append(CheckResult("live_proof", False,
                                      "mock result cannot verify a live publication"))
            return execution, "NOT_VERIFIED", [c.as_dict() for c in checks]
    elif exp.get("mock") is True:
        checks.append(CheckResult("mock_classified", False,
                                  f"expected mock but post is live ({mode})"))
        return execution, "NOT_VERIFIED", [c.as_dict() for c in checks]
    checks.append(CheckResult("mode", True, f"verified as {mode}", critical=False))
    return execution, _verdict(checks), [c.as_dict() for c in checks]


# ---------------------------------------------------------------------------
# campaign
# ---------------------------------------------------------------------------

def check_campaign(session, workspace_id: str,
                   contract: CompletionContract) -> tuple[str, str, list[dict]]:
    from app.models import Campaign, ContentItem, PublishingPlan
    from app.models.campaign import PlatformVariant

    campaign = session.get(Campaign, contract.subject_id)
    if campaign is None or campaign.workspace_id != workspace_id:
        return "UNKNOWN", *_blocked(BLOCKED_CROSS_WORKSPACE)
    exp = contract.expectations or {}
    checks: list[CheckResult] = []
    items = session.query(ContentItem).filter(
        ContentItem.workspace_id == workspace_id,
        ContentItem.campaign_id == campaign.id).all()
    expected = int(exp.get("expected_derivatives", 0))
    exceptions = list(exp.get("exceptions", []) or [])
    if expected and len(items) >= expected:
        checks.append(CheckResult("derivatives_present", True,
                                  f"{len(items)}/{expected} derivatives"))
    elif exceptions:
        checks.append(CheckResult("derivatives_present", True,
                                  f"{len(items)} items + {len(exceptions)} explicit exception(s)",
                                  critical=False))
    else:
        checks.append(CheckResult("derivatives_present", len(items) > 0 or expected == 0,
                                  f"{len(items)}/{expected} derivatives"))

    bad_lineage = [i.id for i in items
                   if i.derivation_type and not (i.parent_content_id or i.root_content_id)]
    checks.append(CheckResult("lineage_valid", not bad_lineage,
                              f"{len(bad_lineage)} item(s) missing ancestry"))

    required = list(exp.get("required_variants", []) or [])
    if required:
        have = {v.platform for v in session.query(PlatformVariant).filter(
            PlatformVariant.workspace_id == workspace_id,
            PlatformVariant.campaign_id == campaign.id).all()}
        missing = [p for p in required if p not in have]
        checks.append(CheckResult("required_variants", not missing,
                                  f"missing={missing} have={sorted(have)}"))
    else:
        checks.append(CheckResult("required_variants", True, "no variants required",
                                  critical=False))

    plan = session.query(PublishingPlan).filter(
        PublishingPlan.workspace_id == workspace_id,
        PublishingPlan.campaign_id == campaign.id).first()
    if exp.get("require_publishing_plan", True):
        checks.append(CheckResult("publishing_plan_present", plan is not None,
                                  f"plan={plan.id if plan else None}"))
    else:
        checks.append(CheckResult("publishing_plan_present", True, "not required",
                                  critical=False))

    failed = [i.id for i in items if i.status == "FAILED" and not (i.error or "").strip()]
    checks.append(CheckResult("no_unresolved_failures", not failed,
                              f"{len(failed)} unresolved mandatory failure(s)"))
    execution = "FAILED" if failed else ("COMPLETED" if items else "UNKNOWN")
    return execution, _verdict(checks), [c.as_dict() for c in checks]


# ---------------------------------------------------------------------------
# research
# ---------------------------------------------------------------------------

def check_research(session, workspace_id: str,
                   contract: CompletionContract) -> tuple[str, str, list[dict]]:
    from app.models import ContentItem

    item = session.get(ContentItem, contract.subject_id)
    if item is None or item.workspace_id != workspace_id:
        return "UNKNOWN", *_blocked(BLOCKED_CROSS_WORKSPACE)
    exp = contract.expectations or {}
    research = item.research_json or {}
    checks: list[CheckResult] = []
    execution = "COMPLETED" if research else "FAILED"

    checks.append(CheckResult("brief_present", bool(research),
                              f"keys={sorted(research.keys())[:8]}"))
    sources = research.get("sources") or research.get("key_facts") or []
    min_sources = int(exp.get("min_sources", 1))
    checks.append(CheckResult("real_sources", len(sources) >= min_sources,
                              f"{len(sources)}/{min_sources} source(s)"))
    claims = research.get("claims") or []
    with_basis = [c for c in claims if isinstance(c, dict) and c.get("basis")]
    checks.append(CheckResult("claims_with_provenance",
                              bool(claims) and len(with_basis) == len(claims),
                              f"{len(with_basis)}/{len(claims)} claims with basis"))
    surfaced = bool(research.get("fact_status") or research.get("cautions")
                    or any(isinstance(c, dict) and c.get("status") in (
                        "UNCERTAIN", "CONFLICTING") for c in claims))
    checks.append(CheckResult("uncertainty_surfaced", surfaced,
                              f"fact_status={research.get('fact_status')}"))
    return execution, _verdict(checks), [c.as_dict() for c in checks]


_CHECKERS = {
    "video": check_video,
    "publication": check_publication,
    "campaign": check_campaign,
    "research": check_research,
}


def verify(session, workspace_id: str, contract: CompletionContract):
    """Run the checker for contract.kind and append to the evidence ledger."""
    from app.engine.intelligence.ledger import append_evidence

    if contract.kind not in _CHECKERS:
        execution, verification, checks = "UNKNOWN", *_blocked(
            f"unknown verification kind: {contract.kind}")
    else:
        execution, verification, checks = _CHECKERS[contract.kind](
            session, workspace_id, contract)
    return append_evidence(
        session, workspace_id=workspace_id, kind=contract.kind,
        subject_id=contract.subject_id, execution_status=execution,
        verification_status=verification, checks=checks,
    )
