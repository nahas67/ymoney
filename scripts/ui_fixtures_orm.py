"""Independent ORM fixture seeds for the last uncontracted endpoints (16.5.5 §1).

Work 16.5.4's seeding inserted everything into ONE staged transaction, which had
two failure modes that cost most of the remaining 32 endpoints:

1. One bad insert rolled back every seed after it, and because ids were published
   from a `pending` dict, the harness handed out ids for rows that no longer
   existed. Coverage went DOWN and nothing said why.
2. Discovering what a table needed was a trial-and-error loop of IntegrityErrors.

So each seed here is an INDEPENDENT unit: its own commit, its own error capture,
and a name that maps to the path parameters it exists to satisfy. A seed that
cannot be satisfied is REPORTED, never silently skipped -- and it can never take
its neighbours down with it.

Row CONSTRUCTION is inside the try as well as the commit. SQLAlchemy raises
`TypeError` for an unknown column at construction time, which is a different
failure from a constraint violation at flush time; 16.5.5 lost a whole generation
run to `Video(title=...)` when the model has no `title`.

Dependencies are explicit and ordered: a parent is always committed before its
child.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from sqlalchemy import select

from app.db import SessionLocal
from app.models import (
    AvatarProfileRow,
    BrandAsset,
    CommunityAction,
    ContentItem,
    ContentTimeline,
    Conversation,
    EditorialPlan,
    EditorialPlanItem,
    Experiment,
    ExportJob,
    ExportProfile,
    LipSyncJob,
    LocalizedContent,
    LocalizationQCReport,
    LongFormProject,
    MediaAsset,
    Notification,
    Opportunity,
    Project,
    PublishedPost,
    ScheduleEntry,
    SocialAccount,
    SocialInteraction,
    SourceConnector,
    TelegramLink,
    TrendSource,
    UgcProjectRow,
    Video,
    VideoVariant,
    WebhookSubscription,
    WorkspaceMember,
)


def _id() -> str:
    return str(uuid.uuid4())


def _add_row(
    db: Any,
    name: str,
    build: Callable[[], Any],
    created: dict[str, str],
) -> str | None:
    """Build, insert and commit ONE row, capturing every failure mode."""
    try:
        row = build()
    except Exception as exc:
        created[f"!{name}"] = f"build {type(exc).__name__}: {str(exc)[:90]}"
        return None
    try:
        db.add(row)
        db.commit()
        return str(getattr(row, "id", "") or "")
    except Exception as exc:
        db.rollback()
        created[f"!{name}"] = f"{type(exc).__name__}: {str(exc)[:90]}"
        return None


def _write_avatar_inputs(
    workspace_id: str, created: dict[str, str]
) -> tuple[str, str]:
    """Write a portrait image and a driving audio file into workspace storage.

    `_workspace_file` requires the resolved path to EXIST, so the fixture has to
    put real bytes on disk rather than register assets pointing at nothing. Both
    files are generated with ffmpeg (a colour card and a tone), which keeps them
    valid inputs for the renderer instead of unparseable placeholders.
    """
    import subprocess

    from app.services.storage import STORAGE_ROOT

    root = STORAGE_ROOT / workspace_id
    root.mkdir(parents=True, exist_ok=True)
    portrait = (root / "observed-portrait.png").resolve()
    audio = (root / "observed-audio.wav").resolve()

    def run(args: list[str]) -> None:
        try:
            proc = subprocess.run(args, capture_output=True, timeout=120)
        except Exception as exc:  # pragma: no cover - environment dependent
            created["!avatar_inputs"] = f"{type(exc).__name__}: {str(exc)[:80]}"
            return
        if proc.returncode != 0:
            created["!avatar_inputs"] = (
                f"ffmpeg rc={proc.returncode}: {proc.stderr[-160:]!r}"
            )

    run([
        "ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=blue:s=512x512:d=1",
        "-frames:v", "1", str(portrait),
    ])
    run([
        "ffmpeg", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
        str(audio),
    ])
    # ABSOLUTE paths, resolved. `managed_path` treats a relative stored path as
    # `cwd`-relative and then requires the result to sit INSIDE the workspace
    # root -- so a bare filename resolves against the repo directory, fails the
    # containment check, returns None, and the route reports "presenter image
    # must be a workspace asset" for a file that exists.
    return str(portrait), str(audio)


def _set_source_asset(
    db: Any,
    avatar_id: str,
    media_asset_id: str,
    created: dict[str, str],
) -> Any:
    """Point an avatar profile at a real portrait asset.

    `source_asset_ref` must reference an asset that EXISTS: the render route
    resolves it and reports a missing asset by name. Returning the row (rather
    than a new object) keeps this an UPDATE on the row `_add_row` already knows
    how to commit.
    """
    from app.models import AvatarProfileRow as _AP

    row = db.query(_AP).filter(_AP.id == avatar_id).one()
    row.source_asset_ref = media_asset_id
    return row


def _link_interaction(
    db: Any,
    interaction_id: str,
    conversation_id: str,
    created: dict[str, str],
) -> None:
    """Point an interaction at its conversation, in its own transaction.

    This is a two-sided relationship and the reply route reads it from ONE side
    only, so the link is the fixture, not a detail. Committed separately so a
    failure here is reported on its own instead of masking the inserts above.
    """
    from app.models import SocialInteraction as _SI

    try:
        row = db.query(_SI).filter(_SI.id == interaction_id).one()
        row.conversation_id = conversation_id
        db.commit()
    except Exception as exc:
        db.rollback()
        created["!interaction_link"] = f"{type(exc).__name__}: {str(exc)[:90]}"


def run_seeds(session: dict[str, Any]) -> dict[str, str]:
    """Insert every seed independently. Returns name -> id, or an error marker."""
    try:
        return _run_seeds(session)
    except Exception as exc:  # pragma: no cover - defensive
        return {"!seed_harness": f"{type(exc).__name__}: {str(exc)[:140]}"}


def _run_seeds(session: dict[str, Any]) -> dict[str, str]:
    created: dict[str, str] = {}
    now = datetime.now(timezone.utc)
    wid = session["workspace_id"]
    # The brand is created over HTTP by `ui_contract_observer.seed`, which runs
    # first and passes its ids in. ORM seeding is not self-sufficient: a
    # BrandAsset without a real brand is a row nothing can resolve.
    brand_id = session.get("seeded", {}).get("brand") or _id()
    # The brand that CARRIES the asset link. It must not be the brand that
    # `POST /brands/{id}/assets` targets: that route refuses with "asset already
    # linked for this role" when a link exists, and `DELETE` on the same path
    # refuses when one does not. The two brands the HTTP seed creates make that
    # split available without inventing anything.
    link_brand_id = session.get("seeded", {}).get("brand_secondary") or brand_id
    db = SessionLocal()

    def put(name: str, build: Callable[[], Any]) -> None:
        _add_row(db, name, build, created)

    # -- content + calendar -------------------------------------------------
    content_id = _id()
    put(
        "content",
        lambda: ContentItem(
            id=content_id,
            workspace_id=wid,
            topic="An observed content item",
            status="IDEA",
            strategy_json={"angle": "observed"},
            research_json={},
            tags_json=["observed"],
        ),
    )
    created["content"] = content_id

    entry_id = _id()
    put(
        "schedule_entry",
        lambda: ScheduleEntry(
            id=entry_id,
            workspace_id=wid,
            content_item_id=content_id,
            platform="tiktok",
            status="DRAFT",
            run_at=now + timedelta(days=1),  # NOT NULL, no default
        ),
    )
    created["entry"] = entry_id
    created["schedule_entry"] = entry_id

    # -- media asset: brand asset links need one ----------------------------
    media_id = _id()
    put(
        "media_asset",
        lambda: MediaAsset(
            id=media_id,
            workspace_id=wid,
            type="video",
            origin="generated",
            storage_key="e2e/observed.mp4",
            mime_type="video/mp4",
            checksum="observed",
            meta_json={},
            derivation_json={},
        ),
    )
    created["media_asset"] = media_id

    # An AUTHORIZED avatar profile.
    #
    # The HTTP seed creates a profile through `POST /avatars`, which leaves
    # `consent_state` at its default. `POST /avatars/render` then refuses with a
    # 403 BEFORE any provider work -- correctly: a custom avatar may not be
    # rendered on a pending consent. That refusal is the behaviour under test, so
    # it is not bypassed; it is satisfied by seeding a profile that HAS consent.
    authorized_avatar_id = _id()
    put(
        "authorized_avatar",
        lambda: AvatarProfileRow(
            id=authorized_avatar_id,
            workspace_id=wid,
            name="An observed authorized avatar",
            profile_json={},
            consent_state="authorized",
            consent_json={"granted_by": "observed", "source": "observed-source"},
            # "mock", not "stub". The render route resolves the backend from this
            # column and 503s on anything outside
            # (server|sadtalker|wavlip|mock). "mock" is the lane that exercises
            # the real request path with no live provider -- which is what the
            # no-live-provider rule requires here, not a bypass of it.
            provider="mock",
            status="READY",
        ),
    )
    created["authorized_avatar"] = authorized_avatar_id

    # The two files the renderer actually opens.
    #
    # `_workspace_file` resolves `image_ref`/`audio_ref` through `managed_path`
    # and requires the resolved file to EXIST, so a registered MediaAsset is not
    # enough -- `audio_ref` also reaches past the asset row to the file on disk.
    # Both are written into the workspace storage root that `managed_path`
    # resolves against, so the render reads real files.
    #
    # `mock` is the avatar backend: it exercises the whole request path (asset
    # resolution, ffmpeg, QC, storage) and produces a labelled placeholder rather
    # than a synthetic talking head. That satisfies the no-live-provider rule
    # without skipping the code under test.
    portrait_key, audio_key = _write_avatar_inputs(wid, created)
    created["portrait_ref"] = portrait_key
    created["avatar_audio_ref"] = audio_key

    # The render route resolves `source_asset_ref` to a FILE, so the asset row
    # must point at the portrait actually written to disk.
    put(
        "authorized_avatar_portrait",
        lambda: _set_source_asset(
            db, authorized_avatar_id, portrait_key, created
        ),
    )

    # -- publishing account, video, published post --------------------------
    account_id = _id()
    put(
        "social_account",
        lambda: SocialAccount(
            id=account_id,
            workspace_id=wid,
            platform="tiktok",
            external_id="observed-account",
            display_name="Observed Account",
            status="connected",
            meta_json={},
        ),
    )
    created["social_account"] = account_id

    variant_id = _id()
    put(
        "video_variant",
        lambda: VideoVariant(
            id=variant_id,
            content_item_id=content_id,  # NOT NULL
            label="observed",
            hook="observed",
            script="observed",
            visual_plan_json={},
            metadata_json={},
        ),
    )
    created["video_variant"] = variant_id
    created["variant"] = variant_id

    video_id = _id()
    put(
        "video",
        lambda: Video(
            id=video_id,
            workspace_id=wid,
            # NOT NULL and a real FK: a Video is a render OUTPUT of a variant.
            variant_id=variant_id,
            engine="observed",
            status="READY",
        ),
    )
    created["video"] = video_id

    post_id = _id()
    put(
        "published_post",
        # `is_mock` true: retention/analytics must never imply a live publication.
        lambda: PublishedPost(
            id=post_id,
            workspace_id=wid,
            video_id=video_id,
            platform="tiktok",
            remote_post_id="observed-remote-post",
            remote_url="https://example.invalid/observed",
            title="An observed post",
            is_mock=True,
        ),
    )
    created["post"] = post_id
    created["published_post"] = post_id
    created["retention_post"] = post_id

    # A brand/asset LINK. `DELETE /brands/{brand_id}/assets` deletes the link
    # row, not the asset, so it 404s with "brand asset link not found" unless a
    # link exists. The link is what makes both the add and the delete endpoint
    # answerable.
    link_id = _id()
    put(
        "brand_asset_link",
        lambda: BrandAsset(
            id=link_id,
            workspace_id=wid,
            # `brand_id` has no FK constraint, but the route resolves the link
            # scoped to a brand, so a real brand id is required for the link to
            # be findable.
            brand_id=link_brand_id,
            media_asset_id=media_id,
            asset_role="logo",
        ),
    )
    created["brand_asset"] = link_id
    # The DELETE target is this brand; the POST target must be a DIFFERENT brand,
    # because POST refuses with "asset already linked for this role" when the
    # link exists. One brand cannot satisfy both routes.
    created["brand_with_asset"] = link_brand_id

    # -- inbox: interaction -> conversation -> action ------------------------
    interaction_id = _id()
    put(
        "interaction",
        lambda: SocialInteraction(
            id=interaction_id,
            workspace_id=wid,
            account_id=account_id,
            platform="tiktok",
            remote_id="observed-remote",
            kind="comment",
            text="An observed comment asking a question",
            author_remote_id="author-1",
            author_name="Observed Author",
            status="NEW",
            is_question=True,
            thread_id="observed-thread",
        ),
    )
    created["interaction"] = interaction_id

    conversation_id = _id()
    put(
        "conversation",
        lambda: Conversation(
            id=conversation_id,
            workspace_id=wid,
            platform="tiktok",
            account_id=account_id,
            thread_key="observed-thread",
            title="Observed thread",
            participant_remote_id="author-1",
            participant_name="Observed Author",
            status="OPEN",
        ),
    )
    created["conversation"] = conversation_id

    # The interaction must point BACK at the conversation.
    #
    # `POST /inbox/conversations/{id}/reply` finds its anchor interaction with
    # `SocialInteraction.conversation_id == conv.id`, and refuses with
    # "conversation has no interaction to reply to" when there is none. A
    # conversation carrying `thread_key` matching the interaction is not enough:
    # the link lives on the interaction row, and inserting it after the
    # interaction means a second UPDATE rather than a field on the first insert.
    _link_interaction(db, interaction_id, conversation_id, created)

    action_id = _id()
    put(
        "action",
        lambda: CommunityAction(
            id=action_id,
            workspace_id=wid,
            interaction_id=interaction_id,
            platform="tiktok",
            action_type="reply",
            mode="draft",
            # LOWERCASE, and one of ("draft", "pending_approval").
            # `approve_action` accepts exactly those two. An UPPERCASE "PENDING"
            # -- which is what the create route writes -- is refused as
            # "PENDING and cannot be approved", and the route answered 500
            # instead of the 409 a state mismatch deserves.
            state="pending_approval",
            draft_text="An observed drafted reply",
        ),
    )
    created["action"] = action_id

    # `POST /inbox/actions/{id}/send` needs an APPROVED action -- a third state.
    # One row cannot be approvable and sendable in the same observation pass, so
    # there are two, each published under its own name.
    sendable_id = _id()
    put(
        "sendable_action",
        lambda: CommunityAction(
            id=sendable_id,
            workspace_id=wid,
            interaction_id=interaction_id,
            platform="tiktok",
            action_type="reply",
            mode="draft",
            state="approved",
            draft_text="An observed approved reply",
        ),
    )
    created["send_action"] = sendable_id

    # A THIRD action, for `reject`.
    #
    # approve / reject / send all read and WRITE `state`, and their preconditions
    # conflict, so one row cannot satisfy all three in a single observation pass:
    # whichever ran first left a state the next one refuses. Concretely, reject
    # sets `rejected`, and send then answers 409 "action is rejected" -- which
    # looked exactly like a missing provider while being a fixture collision.
    # Each verb gets a row already in the state it documents.
    rejectable_id = _id()
    put(
        "rejectable_action",
        lambda: CommunityAction(
            id=rejectable_id,
            workspace_id=wid,
            interaction_id=interaction_id,
            platform="tiktok",
            action_type="reply",
            mode="draft",
            state="pending_approval",
            draft_text="An observed draft awaiting a decision",
        ),
    )
    created["reject_action"] = rejectable_id

    # -- experiment: `analyze` refuses anything but a STARTED one -----------
    experiment_id = _id()
    put(
        "experiment",
        lambda: Experiment(
            id=experiment_id,
            workspace_id=wid,
            kind="hook",
            hypothesis="An observed hypothesis",
            platform="tiktok",
            primary_metric="views",
            status="RUNNING",
        ),
    )
    created["experiment"] = experiment_id

    # -- planner: opportunity -> plan -> plan item --------------------------
    opportunity_id = _id()
    put(
        "opportunity",
        lambda: Opportunity(
            id=opportunity_id,
            workspace_id=wid,
            topic="An observed opportunity",
            source="research",
            external_ref="observed-ref",
            score=0.5,
            components_json={},
            # TEXT column holding serialised JSON; a dict fails to bind.
            recommendation="{}",
            confidence=0.5,
        ),
    )
    created["opportunity"] = opportunity_id

    plan_id = _id()
    put(
        "plan",
        # No `name` column: EditorialPlan is described by horizon/goals/
        # platforms/budget. Passing `name` raised a TypeError at construction,
        # which silently took the PLAN ITEM down with it through the FK and left
        # all five `/planner/items/{id}/...` routes 404ing.
        lambda: EditorialPlan(
            # The id MUST be stated. Left to the default it gets a fresh uuid,
            # so `created["plan"]` published an id that was never inserted, the
            # plan item's `plan_id` FK pointed at nothing, and all five
            # `/planner/items/{id}/...` routes kept 404ing on "plan item not
            # found" while the fixture list looked complete.
            id=plan_id,
            workspace_id=wid,
            horizon_days=30,
            goals_json={},
            platforms_json=["tiktok"],
            status="ACTIVE",
            # SCHEDULE is gated: `_REQUIRED[SCHEDULE] is AutonomyMode.AUTONOMOUS`
            # and `assert_may_advance` refuses anything below it. A plan left on
            # the APPROVAL default answers "SCHEDULE needs AUTONOMOUS autonomy",
            # which is the policy working correctly -- the fixture was simply not
            # in the state the endpoint documents.
            autonomy="AUTONOMOUS",
            # There is no `allowed_actions` column -- the allowlist is part of the
            # resolved policy, not the stored row. Passing one raised a TypeError
            # at construction, which is the failure mode that silently took the
            # plan item down through the FK on the previous run.
            constraints_json={},
        ),
    )
    created["plan"] = plan_id

    plan_item_id = _id()
    put(
        # `/planner/items/{item_id}` reads EditorialPlanItem, NOT Opportunity.
        # Seeding an opportunity left every planner action route 404ing on
        # "plan item not found" while the fixture looked populated.
        "plan_item",
        lambda: EditorialPlanItem(
            id=plan_item_id,
            workspace_id=wid,
            plan_id=plan_id,
            opportunity_id=opportunity_id,
            angle="An observed angle",
            content_format="short",
            platforms_json=["tiktok"],
            status="PROPOSED",
            why_json={},
        ),
    )
    created["item"] = plan_item_id
    created["plan_item"] = plan_item_id

    # -- schedule entries in the states the calendar routes require ---------
    #
    # One entry cannot satisfy both routes, and the two sets of acceptable states
    # only PARTIALLY overlap:
    #
    #   PATCH /calendar/{id}  -- refuses a past `run_at`; entry.status must be
    #                            PENDING or FAILED
    #   DELETE /calendar/{id} -- entry.status must be PENDING or FAILED
    #
    # PENDING satisfies BOTH, so `entry` is PENDING. An earlier version used
    # SCHEDULED, which reads like the natural "scheduled" state and produced
    # 409 "cannot reschedule a SCHEDULED entry".
    for name, status in (("entry", "PENDING"), ("cancellable_entry", "PENDING")):
        eid = _id()
        put(
            name,
            lambda eid=eid, status=status: ScheduleEntry(
                id=eid,
                workspace_id=wid,
                content_item_id=content_id,
                platform="tiktok",
                status=status,
                run_at=now + timedelta(days=2),
            ),
        )
        created[name] = eid
    created["schedule_entry"] = created["entry"]

    # -- localization, ugc, lip-sync ----------------------------------------
    localized_id = _id()
    put(
        "localized",
        lambda: LocalizedContent(
            id=localized_id,
            workspace_id=wid,
            source_content_id=content_id,
            language="es",
            locale="es-ES",
            status="completed",  # `qc` needs a COMPLETED run
        ),
    )
    created["localized"] = localized_id

    ugc_id = _id()
    put(
        "ugc_project",
        lambda: UgcProjectRow(
            id=ugc_id,
            workspace_id=wid,
            preset="PRODUCT_DEMO",
            brief_json={},
            status="DRAFT",
        ),
    )
    created["ugc_project"] = ugc_id

    # A project WITH a timeline, because `render()` refuses one without:
    # "project has no timeline - run the pipeline first". Running the pipeline is
    # not an option here -- it is real generation work, and the brief for this
    # harness is no live/paid provider operation -- so the timeline is attached
    # directly, which is the state the endpoint documents as its precondition.
    ugc_with_timeline_id = _id()
    ugc_timeline_id = _id()
    put(
        "ugc_timeline",
        lambda: ContentTimeline(
            id=ugc_timeline_id,
            workspace_id=wid,
            content_item_id=content_id,
            name="An observed UGC timeline",
            fps=30,
            duration_seconds=3.0,
            version=1,
            # QC runs on the rendered OUTPUT and fails a project whose timeline
            # has no hook clip in the opening window and no CTA anywhere -- an
            # empty `tracks` list renders and then fails the gate, so the route
            # answered "rendered output failed QC (FAIL) ... the project stays
            # BLOCKED".
            #
            # This is a fixture for the RESPONSE, not a way to skip QC: the check
            # still runs and still sees a real timeline. It is given a real hook
            # and a real CTA because that is what the endpoint expects to render.
            tracks_json={
                # `duration_seconds` must be INSIDE the doc as well as on the
                # column: the QC rollup reads `doc.get("duration_seconds")` and
                # the renderer defaults it to 0.0, reporting "zero duration".
                "duration_seconds": 3.0,
                "fps": 30,
                # The canonical track shape is `{"kind": ..., "clips": [...]}`,
                # NOT one dict per clip inside `tracks`. QC's `_track_clips`
                # returns `tr.get("clips") or []` for a matching track, so a flat
                # list of clip dicts yields EMPTY clip lists for every kind: the
                # hook check failed, `timeline_complete` reported "no voice
                # clips; no caption clips; no visual clips", and the render was
                # refused with the project left BLOCKED.
                #
                # The label vocabulary matters too: `_label` reads `id`/`name`,
                # so a clip called `hook` satisfies the opening-window check.
                "tracks": [
                    {
                        "kind": "voice",
                        "clips": [
                            {
                                "id": "observed-voice-1",
                                "name": "voiceover",
                                "start": 0.0,
                                "duration": 3.0,
                                "source": {"ref": "observed-voice.wav"},
                            }
                        ],
                    },
                    {
                        "kind": "caption",
                        "clips": [
                            {
                                "id": "observed-hook",
                                "name": "hook",
                                "start": 0.0,
                                "duration": 1.5,
                                "text": "Stop scrolling - here is the thing",
                            },
                            {
                                "id": "observed-cta",
                                "name": "cta",
                                "start": 2.0,
                                "duration": 1.0,
                                "text": "Follow for more",
                            },
                        ],
                    },
                    {
                        "kind": "video",
                        "clips": [
                            {
                                "id": "observed-visual-1",
                                "name": "broll",
                                "start": 0.0,
                                "duration": 3.0,
                                "source": {"ref": "observed-visual.mp4"},
                            }
                        ],
                    },
                ],
            },
        ),
    )
    put(
        "ugc_project_with_timeline",
        lambda: UgcProjectRow(
            id=ugc_with_timeline_id,
            workspace_id=wid,
            preset="PRODUCT_DEMO",
            brief_json={
                "topic": "an observed product",
                "cta": "Follow for more",
            },
            status="READY",
            # A timeline with a real duration. The renderer reads
            # `duration_seconds` and refuses `total <= 0` with "timeline has no
            # duration", so an empty `tracks_json` timeline is not enough -- the
            # project must point at a timeline the renderer will accept.
            timeline_id=ugc_timeline_id,
            qc_json={"status": "PASS", "checks": {}},
        ),
    )
    created["ugc_render_project"] = ugc_with_timeline_id

    # A localization QC report. `GET /localization/{localized_id}/qc` reads this
    # table, so a COMPLETED `LocalizedContent` alone is not enough -- the report
    # is a separate row and its absence is what the route reports as "qc report
    # not found".
    qc_id = _id()
    put(
        "localization_qc",
        lambda: LocalizationQCReport(
            id=qc_id,
            workspace_id=wid,
            localized_content_id=localized_id,
            status="passed",
            checks_json=[],
        ),
    )
    created["qc_report"] = qc_id

    lipsync_id = _id()
    put(
        "lipsync_job",
        lambda: LipSyncJob(
            id=lipsync_id,
            workspace_id=wid,
            provider="stub",
            status="QUEUED",
            video_ref="observed-video",
            audio_ref="observed-audio",
        ),
    )
    created["lipsync_job"] = lipsync_id

    # -- contract-observation fixtures --------------------------------------
    # These rows exist so the response-contract observer can watch endpoints
    # whose preconditions would otherwise 404. Each is DEDICATED to one
    # endpoint (see ROUTE_FIXTURE_OVERRIDES): sharing one row between two
    # endpoints with conflicting preconditions manufactures a failure that
    # looks like a missing provider.

    # `POST /webhooks/{sub_id}/test` enqueues a dispatch job for an ACTIVE
    # subscription. The enqueue is request-time only -- no outbound HTTP --
    # so this is safe to observe. `secret_enc` is never decrypted here; the
    # value is a placeholder and the observation workspace is throwaway.
    webhook_test_id = _id()
    put(
        "webhook_test",
        lambda: WebhookSubscription(
            id=webhook_test_id,
            workspace_id=wid,
            url="https://example.invalid/hook",
            secret_enc="observed",
            events_json=["webhook.test"],
            active=True,
        ),
    )
    created["webhook_test"] = webhook_test_id

    # `POST /knowledge/sources/{id}/sync` enqueues a SOURCE_SYNC job; nothing
    # is fetched at request time. `rss` needs no OAuth at enqueue time.
    # `disconnect` on the SAME row would disable it and break sync
    # order-independently, so each gets its own row.
    connector_sync_id = _id()
    put(
        "connector_sync",
        lambda: SourceConnector(
            id=connector_sync_id,
            workspace_id=wid,
            kind="rss",
            name="An observed sync connector",
            status="READY",
            enabled=True,
        ),
    )
    created["connector_sync"] = connector_sync_id

    connector_disable_id = _id()
    put(
        "connector_disable",
        lambda: SourceConnector(
            id=connector_disable_id,
            workspace_id=wid,
            kind="rss",
            name="An observed disconnect connector",
            status="READY",
            enabled=True,
        ),
    )
    created["connector_disable"] = connector_disable_id

    # `DELETE /trend-sources/{id}` removes the row. One row, observed once.
    trend_source_id = _id()
    put(
        "trend_source",
        lambda: TrendSource(
            id=trend_source_id,
            workspace_id=wid,
            kind="mock",
            name="An observed trend source",
            enabled=True,
        ),
    )
    created["trend_source"] = trend_source_id

    # -- second-round observation fixtures -----------------------------------
    # Same dedicated-row rule as above. Each of these endpoints 404s/409s/422s
    # without a precondition row that no other endpoint can share.

    # `POST /notifications/{id}/read` filters by (workspace, user): a foreign
    # id is "not mine" and 404s. The row must belong to the observing user,
    # who is the workspace's registering owner. The id is resolved once here
    # because the export-cancel row below needs an owner too (`created_by` is
    # non-nullable with no default).
    owner_link = db.scalar(
        select(WorkspaceMember).where(WorkspaceMember.workspace_id == wid)
    )
    owner_id = str(owner_link.user_id) if owner_link is not None else ""
    if owner_link is not None:
        notif_id = _id()
        put(
            "notification",
            lambda: Notification(
                id=notif_id,
                workspace_id=wid,
                user_id=owner_id,
                kind="info",
                payload_json={"observed": True},
            ),
        )
        created["notification"] = notif_id

    # `POST /exports` validates profile → format probe → compatibility, in
    # that order, and writes nothing until all three pass. The profile is a
    # workspace row cloning a builtin preset's own validated config, so the
    # compatibility gate cannot fail on fixture grounds. MP4 probes available
    # in this environment (see `GET /exports/formats`).
    from app.engine.exporter.profiles import preset_config

    export_profile_id = _id()
    put(
        "export_profile",
        lambda: ExportProfile(
            id=export_profile_id,
            workspace_id=wid,
            name="An observed export profile",
            preset="SHORTS_1080x1920",
            config_json=dict(preset_config("SHORTS_1080x1920")),
            is_builtin=False,
        ),
    )
    created["export_profile"] = export_profile_id

    # `POST /exports/{id}/cancel` needs a QUEUED export. Seeded directly: the
    # queue path itself is observed separately (see the body override), and
    # cancelling a self-seeded row exercises the same state machine.
    # `created_by` is non-nullable with no default, so the owner resolved
    # above is reused -- omitting it fails the insert and the harness hands
    # the cancel observation a random id.
    export_queued_id = _id()
    put(
        "export_queued",
        lambda: ExportJob(
            id=export_queued_id,
            workspace_id=wid,
            profile_id=export_profile_id,
            format="MP4",
            target_type="timeline",
            target_id=session.get("seeded", {}).get("timeline") or _id(),
            state="QUEUED",
            created_by=(owner_id or _id()),
        ),
    )
    created["export_queued"] = export_queued_id

    # Telegram links: `toggle` flips, `DELETE` removes. One shared row breaks
    # whichever runs second, so each gets its own.
    link_toggle_id = _id()
    put(
        "link_toggle",
        lambda: TelegramLink(
            id=link_toggle_id,
            workspace_id=wid,
            chat_id="observed-toggle",
            chat_title="An observed toggle link",
            active=True,
        ),
    )
    created["link_toggle"] = link_toggle_id

    link_delete_id = _id()
    put(
        "link_delete",
        lambda: TelegramLink(
            id=link_delete_id,
            workspace_id=wid,
            chat_id="observed-delete",
            chat_title="An observed delete link",
            active=True,
        ),
    )
    created["link_delete"] = link_delete_id

    # `POST /long-form/projects/{id}/advance` only needs the project to
    # exist (`_get` → 404 otherwise); it enqueues a stage job. The same row
    # backs the `{id}`-scoped READS (detail / estimate / progress).
    longform_id = _id()
    put(
        "longform_project",
        lambda: LongFormProject(
            id=longform_id,
            workspace_id=wid,
            topic="An observed long-form project",
            stage="IDEA",
            status="DRAFT",
        ),
    )
    created["longform_project"] = longform_id

    # `GET /projects/{id}`. `Project` has no non-pipeline create route usable
    # without queuing work, so the row is written directly; `created_by` is
    # non-nullable, so the workspace owner resolved above is reused.
    project_id = _id()
    put(
        "project",
        lambda: Project(
            id=project_id,
            workspace_id=wid,
            name="An observed project",
            description="An observed project for contract observation.",
            status="ACTIVE",
            created_by=(owner_id or _id()),
        ),
    )
    created["project"] = project_id

    db.close()
    return created
