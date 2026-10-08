"""CompletionVerifier (Lane C, Work 05).

``execution_status`` (what happened) is SEPARATE from ``verification_status``
(what was proven):

- execution_status: COMPLETED | FAILED | RUNNING | UNKNOWN
- verification_status: VERIFIED | PARTIALLY_VERIFIED | NOT_VERIFIED | BLOCKED

Checkers (video / publication / campaign / research / community_reply) record
per-check {name, passed, detail} evidence and append to the ledger.
MOCK-labeled publication results verify ONLY as mock, never live; MOCK/fixture
community-reply receipts NEVER verify (no VERIFIED status at all) — a live
reply is proven by four independent proofs: provider receipt, remote reply id,
a persisted CommunityAction row, and account/platform agreement.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

VERIFICATION_STATUSES = ("VERIFIED", "PARTIALLY_VERIFIED", "NOT_VERIFIED", "BLOCKED")
EXECUTION_STATUSES = ("COMPLETED", "FAILED", "RUNNING", "UNKNOWN")
KINDS = ("video", "publication", "campaign", "research", "community_reply",
         "export")

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
    # W11.5 D-F5: prefer the storage boundary -- a raw DB path must not be
    # stat'ed blindly (a row carrying an absolute path outside workspace
    # storage would otherwise be trusted). Fall back to the path AS-IS only
    # when the boundary refuses it, because real installs store engine output
    # under a provider-owned path outside data/videos; the boundary result is
    # reported in the detail so the provenance stays auditable.
    real_path = None
    via_boundary = False
    if path and not is_mock:
        try:
            from app.services.storage import managed_path

            real_path = managed_path(workspace_id, path)
            via_boundary = real_path is not None
        except Exception:
            real_path = None
        # No raw-path fallback. ``managed_path`` is the storage boundary: it
        # refuses a path outside STORAGE_ROOT/<workspace> precisely so a tampered
        # row cannot make this checker read another workspace's bytes. Re-adopting
        # the same path with a bare ``Path(...).exists()`` made that refusal
        # advisory -- any file that happened to exist on the host, such as
        # /etc/passwd on Linux, was verified anyway. A refused path stays refused.
    exists = bool(real_path is not None and real_path.exists())
    size = real_path.stat().st_size if exists else 0
    checks.append(CheckResult("record_registered", True,
                              f"video {video.id} status={video.status}"))
    checks.append(CheckResult("file_real_nonzero", exists and size > 0,
                              f"path={path[:120]} size={size} mock={is_mock} "
                              f"via_storage_boundary={via_boundary}"))

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
    if path and not is_mock:
        try:
            # W11.5 D-F5: storage keys are workspace-relative paths
            # (data/videos/<ws>/...), so a bare-basename comparison can never
            # match. Try the exact key first, then a same-workspace basename
            # suffix (the workspace filter keeps the suffix from leaking across).
            from sqlalchemy import or_

            wanted = Path(path).name
            # exact key, the workspace-relative suffix form, or the legacy
            # bare-basename key that pre-boundary rows still carry
            asset = session.query(MediaAsset).filter(
                MediaAsset.workspace_id == workspace_id,
                or_(MediaAsset.storage_key == path,
                    MediaAsset.storage_key == wanted,
                    MediaAsset.storage_key.like(f"%/{wanted}")),
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
    from app.engine.distribution.modes import PublicationMode
    from app.models import PublishedPost

    post = session.get(PublishedPost, contract.subject_id)
    if post is None or post.workspace_id != workspace_id:
        return "UNKNOWN", *_blocked(BLOCKED_CROSS_WORKSPACE)
    exp = contract.expectations or {}
    checks: list[CheckResult] = []

    # -- Work 14 §9: the FOUR modes, resolved before anything else --------
    # `publication_mode` is authoritative. `is_mock` is a backstop for rows
    # whose mode was never written: a row that claims LIVE while its own
    # is_mock flag says otherwise is a contradiction, resolved in favour of
    # MOCK (fail closed) -- never in favour of LIVE.
    raw_mode = str(getattr(post, "publication_mode", "") or "").strip().upper()
    if raw_mode not in {m.value for m in PublicationMode}:
        raw_mode = PublicationMode.UNAVAILABLE.value
    contradicted = bool(post.is_mock and raw_mode == PublicationMode.LIVE.value)
    if post.is_mock and raw_mode != PublicationMode.MOCK.value:
        raw_mode = PublicationMode.MOCK.value
    try:
        mode = PublicationMode(raw_mode)
    except ValueError:  # pragma: no cover - guarded above
        mode = PublicationMode.UNAVAILABLE
    _ = contradicted

    execution = ("COMPLETED" if mode is PublicationMode.LIVE
                 else "PENDING" if mode is PublicationMode.HANDOFF
                 else "FAILED")

    # A remote id is the evidence of a LIVE post -- but ONLY a live one. A mock
    # or handoff carrying one is a contradiction and must be surfaced, because
    # that is exactly the confusion this check exists to catch.
    has_remote = bool(post.remote_post_id)
    if has_remote and mode in (PublicationMode.HANDOFF,
                               PublicationMode.UNAVAILABLE):
        # A real contradiction here: a prepared/unavailable row must not carry
        # a remote id, because a remote id is what reads as proof downstream.
        # A MOCK row is exempt -- the mock publisher legitimately mints a
        # synthetic id, which is noted rather than treated as a finding.
        checks.append(CheckResult(
            "remote_id_without_live_mode", False,
            f"a remote id is present but the mode is {mode}; a remote id is "
            f"only evidence of a LIVE publication"))
    checks.append(CheckResult(
        "mode_declared", True,
        f"{mode}" + (" (live)" if mode.is_live else
                     " (not evidence of a live publication)"),
        critical=False))
    if contradicted:
        checks.append(CheckResult(
            "mode_contradiction", False,
            "the row claimed LIVE while is_mock=True; resolved as MOCK "
            "(fail closed) rather than trusting the mode"))
    checks.append(CheckResult("receipt_present", has_remote,
                              f"remote_post_id={post.remote_post_id[:60] or '(none)'}"))

    # -- handoff-specific contract --------------------------------------
    if mode is PublicationMode.HANDOFF:
        payload = getattr(post, "handoff_payload", None) or {}
        checks.append(CheckResult(
            "handoff_recorded", bool(payload),
            f"handoff payload keys={sorted(payload)[:6] if payload else '(none)'}"))
        checks.append(CheckResult(
            "handoff_is_not_publication", not has_remote,
            "a user handoff carries no remote id: the human has not published yet"))
        checks.append(CheckResult(
            "handoff_requires_human", bool(payload.get("requires_human", True)),
            "handoff is recorded as requiring a human to publish", critical=False))
        if exp.get("live") is True:
            checks.append(CheckResult(
                "live_proof", False,
                "a USER_HANDOFF cannot verify a live publication: the media is "
                "prepared but nobody has published it"))
            return execution, "NOT_VERIFIED", [c.as_dict() for c in checks]
        return execution, "NOT_VERIFIED", [c.as_dict() for c in checks]

    if mode is PublicationMode.UNAVAILABLE:
        checks.append(CheckResult(
            "not_a_publication", False,
            "mode is UNAVAILABLE: the capability or its preconditions were not "
            "met, so this is not a publication"))
        return execution, "NOT_VERIFIED", [c.as_dict() for c in checks]

    if mode is PublicationMode.MOCK:
        checks.append(CheckResult("mock_classified", True,
                                  "MOCK-labeled: verifies as mock only, never live",
                                  critical=False))
        if exp.get("live") is True:
            checks.append(CheckResult("live_proof", False,
                                      "mock result cannot verify a live publication"))
            return execution, "NOT_VERIFIED", [c.as_dict() for c in checks]
        # A mock verifies as a mock on its own; it only FAILS when a live
        # publication was explicitly expected, which is handled above.
        if exp.get("mock") is False:
            checks.append(CheckResult(
                "mock_expected", False,
                "expected a live publication but the mode is MOCK"))
        # duplicate check + platform/account still apply
        checks.extend(_publication_shape_checks(session, post, exp))
        return execution, _verdict(checks), [c.as_dict() for c in checks]

    # -- LIVE ------------------------------------------------------------
    checks.append(CheckResult("remote_id_present", has_remote,
                              f"remote_url={(post.remote_url or '')[:120]}"))
    checks.extend(_publication_shape_checks(session, post, exp))
    checks.append(CheckResult("mode", True, "verified as live", critical=False))
    return execution, _verdict(checks), [c.as_dict() for c in checks]


def _publication_shape_checks(session, post, exp: dict) -> list[CheckResult]:
    """The checks that apply to any recorded publication, live or mock."""
    from app.models import PublishedPost

    checks: list[CheckResult] = []
    dupes = session.query(PublishedPost).filter(
        PublishedPost.video_id == post.video_id,
        PublishedPost.platform == post.platform).count()
    checks.append(CheckResult("idempotency_single_record", dupes == 1,
                              f"{dupes} row(s) for video+platform"))
    if "platform" in exp:
        checks.append(CheckResult("platform_match", post.platform == exp["platform"],
                                  f"expected {exp['platform']} got {post.platform}"))
    if exp.get("account_id"):
        checks.append(CheckResult("account_match",
                                  (post.account_id or "") == exp["account_id"],
                                  f"expected {exp['account_id']} got {post.account_id}"))
    return checks


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


def check_reply(action, receipt: dict, account_id: str,
                platform: str) -> tuple[str, str, list[dict]]:
    """Community-reply proof (kind ``community_reply``).

    Four independent proofs must hold for VERIFIED: a provider receipt, a
    remote reply id, a persisted ``CommunityAction`` row, and account/platform
    agreement (the action's own values, reconciled with anything the receipt
    declares). MOCK/fixture receipts NEVER verify — at most NOT_VERIFIED,
    never VERIFIED, regardless of how complete they look.
    """
    from sqlalchemy.orm import object_session

    from app.models.community import CommunityAction

    receipt = dict(receipt or {})
    remote_reply_id = ""
    for key in ("remote_reply_id", "reply_id", "new_reply_id", "id"):
        value = receipt.get(key)
        if value:
            remote_reply_id = str(value)
            break
    execution = "COMPLETED" if remote_reply_id else "FAILED"
    checks: list[CheckResult] = []

    checks.append(CheckResult(
        "provider_receipt", bool(receipt),
        f"{len(receipt)} receipt field(s): "
        + (", ".join(sorted(receipt)[:6])[:120] or "none")))
    checks.append(CheckResult(
        "remote_reply_id", bool(remote_reply_id),
        f"remote_reply_id={remote_reply_id[:80]}"))

    session = object_session(action)
    row = session.get(CommunityAction, getattr(action, "id", None)) \
        if session is not None and getattr(action, "id", None) else None
    checks.append(CheckResult(
        "action_row_persisted", row is not None,
        f"community_actions {getattr(action, 'id', None)} "
        f"{'found' if row is not None else 'not found'} in DB"))

    expected_account = str(account_id or "")
    expected_platform = str(platform or "")
    action_account = str(getattr(action, "account_id", "") or "")
    action_platform = str(getattr(action, "platform", "") or "")
    declared_account = str(receipt.get("account_id") or "")
    declared_platform = str(receipt.get("platform") or "")
    account_ok = bool(expected_account) and action_account == expected_account
    if declared_account:
        account_ok = account_ok and declared_account == expected_account
    platform_ok = bool(expected_platform) and action_platform == expected_platform
    if declared_platform:
        platform_ok = platform_ok and declared_platform == expected_platform
    checks.append(CheckResult(
        "account_platform_match", account_ok and platform_ok,
        f"action=({action_account},{action_platform}) "
        f"expected=({expected_account},{expected_platform}) "
        f"receipt=({declared_account},{declared_platform})"))

    is_mock = bool(receipt.get("mock") or receipt.get("is_mock")) or bool(
        getattr(action, "is_mock", False))
    if is_mock:
        checks.append(CheckResult(
            "live_proof", False,
            "MOCK/fixture receipt: verifies as mock only, never live"))
        return execution, "NOT_VERIFIED", [c.as_dict() for c in checks]
    return execution, _verdict(checks), [c.as_dict() for c in checks]


def _check_reply_contract(session, workspace_id: str,
                          contract: CompletionContract) -> tuple[str, str, list[dict]]:
    from app.models.community import CommunityAction

    action = session.get(CommunityAction, contract.subject_id)
    if action is None or action.workspace_id != workspace_id:
        return "UNKNOWN", *_blocked(BLOCKED_CROSS_WORKSPACE)
    return check_reply(action, action.provider_receipt_json or {},
                       action.account_id, action.platform)


# ---------------------------------------------------------------------------
# export (Work 11 Lane X) -- an export is COMPLETE only when its own
# independent verification proved it; we replay that evidence into the ledger
# rather than re-probing the bytes (the export center already recorded
# per-check {name, passed, detail} at run time).
# ---------------------------------------------------------------------------


def check_export(session, workspace_id: str,
                 contract: CompletionContract) -> tuple[str, str, list[dict]]:
    """Replay a finished export's own verdict as ledger evidence.

    A COMPLETE export is only VERIFIED when the export row says COMPLETE *and*
    every critical check passed. Anything else (FAILED, CANCELLED, still
    QUEUED/RUNNING, or a verdict with a failed critical check) is
    NOT_VERIFIED -- an unproven export never claims to be a verified one.
    """
    from app.engine.exporter.verify import check_export_contract

    outcome = check_export_contract(session, workspace_id, contract.subject_id)
    if not outcome.get("found"):
        # unknown id or another workspace's export -> isolation, never a
        # half-filled NOT_VERIFIED that looks like a real evaluation
        return "UNKNOWN", *_blocked(BLOCKED_CROSS_WORKSPACE)
    checks = [
        CheckResult(str(c.get("name") or "check"), bool(c.get("passed")),
                    str(c.get("detail") or ""),
                    critical=bool(c.get("critical", True)))
        for c in outcome["checks"]
    ]
    return str(outcome.get("execution") or "UNKNOWN"), _verdict(checks), [
        c.as_dict() for c in checks]


_CHECKERS = {
    "video": check_video,
    "publication": check_publication,
    "campaign": check_campaign,
    "research": check_research,
    "community_reply": _check_reply_contract,
    "export": check_export,
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
