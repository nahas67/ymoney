/* Campaigns — the list, and the one place a campaign's identity is shown.
 *
 *   GET  /campaigns                   the list
 *   GET  /publishing/posts            publications + their measured outcome
 *   GET  /distribution/capabilities   per-platform publish_mode from the registry
 *   POST /campaigns                   create
 *   POST /campaigns/from-master       create the master→shorts campaign
 *
 * HIERARCHY (Campaign → Master → Shorts → Platform Variants → Publications) is
 * rendered on the DETAIL screen from `GET /campaigns/{id}/aggregate`; the list
 * stays a list, because a hierarchy per row makes both unreadable.
 *
 * PUBLICATION MODE IS NOT A STATUS.
 *
 * `engine/distribution/modes.py` is explicit that four outcomes must never be
 * collapsed: LIVE (an official API returned a remote id), MOCK (nothing was
 * sent), HANDOFF (media prepared, a human publishes it) and UNAVAILABLE. The
 * backend keeps `PublishedPost.publication_mode` authoritative and states that
 * `is_mock` "can no longer express a handoff".
 *
 * `GET /publishing/posts` does NOT return `publication_mode`. So the mode shown
 * here is resolved from the two facts it does return, in the precedence
 * `classify_publication` itself uses, and refuses to guess the rest:
 *
 *   is_mock === true                 → MOCK      (the flow writes
 *                                                is_mock = (mode is MOCK), so
 *                                                this one is exact)
 *   platform publish_mode is         → HANDOFF   (USER_HANDOFF comes from the
 *   USER_HANDOFF                                 publisher registry, not a name
 *                                                check; classify_publication
 *                                                returns HANDOFF for it)
 *   otherwise                        → UNAVAILABLE, NOT LIVE
 *
 * That last line is the whole point. `is_mock === false` cannot distinguish LIVE
 * from HANDOFF, and calling it LIVE is precisely the failure Work 14 §5 calls
 * out as FAILED. `toneForMode` gives LIVE / MOCK / HANDOFF three distinct
 * colours; the undecidable case gets an `unknown` badge that is deliberately
 * NOT one of the three.
 */

import { useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import {
  Badge,
  Button,
  DataTable,
  EmptyState,
  Field,
  Grid,
  ModeBadge,
  Money,
  Modal,
  PageHeader,
  Panel,
  QueryBoundary,
  Select,
  StatTile,
  StatusBadge,
  humanize,
  type Column,
} from "../../design-system/primitives";
import { useMutation, useWsQuery } from "../../api/queries";
import { wsApi } from "../../lib/api";
import { useSession } from "../../state/session";

/* ==========================================================================
 * Response shapes — api/v1/content.py
 * ======================================================================= */

/* `campaigns_router.list_campaigns` */
export type CampaignRow = {
  id: string;
  name: string;
  goal: string;
  /** DRAFT | RUNNING | READY | CANCELLED | … (whatever the flow wrote) */
  status: string;
  target_videos: number;
  videos_per_day: number;
  platforms: string[];
  /** DISABLED | MANUAL | SEMI_AUTONOMOUS | AUTONOMOUS */
  automation_level: string;
  budget_daily_usd: number | null;
  starts_at: string | null;
  ends_at: string | null;
};

export type CampaignList = { items: CampaignRow[] };

/* `campaigns_router.create_campaign` */
export type CreateCampaignResult = { id: string };

/* `CampaignBody` — the create request, so the form cannot invent a field. */
const CREATE_CAMPAIGN_PLATFORMS = ["youtube", "tiktok", "facebook", "instagram"] as const;
const AUTOMATION_LEVELS = ["DISABLED", "MANUAL", "SEMI_AUTONOMOUS", "AUTONOMOUS"] as const;

/* `campaign_flows_router.vc_create_from_master` → CreateFromMasterBody */
export type FromMasterResult = { id: string; plan: Record<string, unknown> };

/* engine/campaign/platforms.py CAMPAIGN_PLATFORMS */
export const CAMPAIGN_PLATFORMS = [
  "youtube_shorts",
  "tiktok",
  "instagram_reels",
  "facebook_reels",
  "linkedin",
  "x",
  "threads",
  "bluesky",
  "pinterest",
  "snapchat",
] as const;

/* engine/campaign/metadata.py CTA_KINDS */
export const CTA_KINDS = [
  "FOLLOW",
  "SUBSCRIBE",
  "COMMENT",
  "WATCH_FULL_VIDEO",
  "VISIT_PROFILE",
  "LEARN_MORE",
  "NONE",
] as const;

/* `content_router.list_content` rows (see `_serialize_content`). */
export type CampaignContentRow = {
  id: string;
  topic: string;
  status: string;
  campaign_id: string | null;
  created_at: string;
};

/* ==========================================================================
 * Publication mode
 * ======================================================================= */

/* `publishing_router.list_published_posts` (api/v1/misc.py).
   NOTE: no `campaign_id` and no `publication_mode` on this payload. */
export type PublishedPostRow = {
  id: string;
  platform: string;
  title: string;
  remote_url: string;
  published_at: string | null;
  /** Cannot express HANDOFF — see the header comment. */
  is_mock: boolean;
  metrics: {
    views: number;
    likes: number;
    comments: number;
    /** null when the provider did not report one. */
    completion_rate: number | null;
  };
};

export type PublishedPostList = { items: PublishedPostRow[] };

/* `distribution_router.capabilities` — publish_mode from HANDOFF_PLATFORMS. */
export type DistributionCapability = {
  platform: string;
  capabilities: string[];
  /** USER_HANDOFF | DIRECT_PUBLISH */
  publish_mode: string;
  direct_publish: boolean;
  user_handoff: boolean;
};

export type DistributionCapabilities = { items: DistributionCapability[] };

export type ResolvedMode = "LIVE" | "MOCK" | "HANDOFF" | "UNAVAILABLE";

/**
 * Resolve a publication's mode from what the API actually reports.
 *
 * `handoffOnly` comes from the publisher registry, never from a platform-name
 * check, so adding a platform cannot make this drift.
 */
export function resolvePublicationMode(
  post: Pick<PublishedPostRow, "is_mock">,
  handoffOnly: boolean,
): ResolvedMode {
  // Precedence mirrors classify_publication: on a handoff platform a mock is
  // still reported as the mock it is.
  if (post.is_mock) return "MOCK";
  if (handoffOnly) return "HANDOFF";
  // is_mock === false cannot separate LIVE from HANDOFF. Refusing to guess is
  // the honest answer; claiming LIVE here is the bug this guards.
  return "UNAVAILABLE";
}

/**
 * LIVE / MOCK / HANDOFF get three DISTINCT design-system colours through
 * `ModeBadge`. The undecidable case gets its own `unknown` badge so it is never
 * mistaken for one of the three.
 */
export function PublicationModeBadge({
  mode,
  title,
}: {
  mode: ResolvedMode;
  title?: string;
}) {
  if (mode === "UNAVAILABLE") {
    return (
      <Badge tone="unknown" title={title ?? "The API does not report publication_mode; is_mock=false cannot distinguish LIVE from HANDOFF."}>
        MODE UNAVAILABLE
      </Badge>
    );
  }
  return <ModeBadge mode={mode} />;
}

function useHandoffPlatforms(): { ids: Set<string>; loaded: boolean; error: string | null } {
  const caps = useWsQuery<DistributionCapabilities>("/distribution/capabilities");
  const ids = useMemo(() => {
    const set = new Set<string>();
    for (const c of caps.data?.items ?? []) if (c.user_handoff) set.add(c.platform);
    return set;
  }, [caps.data]);
  return { ids, loaded: caps.data !== null, error: caps.error };
}

function isoDay(iso: string | null | undefined): string {
  if (!iso) return "—";
  return iso.replace("Z", "").slice(0, 10);
}

/* ==========================================================================
 * List
 * ======================================================================= */

function CreateCampaignModal({ onClose, onDone }: { onClose: () => void; onDone: () => void }) {
  const [name, setName] = useState("");
  const [goal, setGoal] = useState("");
  const [platforms, setPlatforms] = useState<string[]>([]);
  const [automation, setAutomation] = useState<string>("SEMI_AUTONOMOUS");
  const [targetVideos, setTargetVideos] = useState("0");
  const [videosPerDay, setVideosPerDay] = useState("0");
  const [budgetDaily, setBudgetDaily] = useState("");

  const create = useMutation<void, CreateCampaignResult>(
    () =>
      wsApi.post("/campaigns", {
        name: name.trim(),
        goal,
        target_videos: Number(targetVideos) || 0,
        videos_per_day: Number(videosPerDay) || 0,
        platforms,
        automation_level: automation,
        budget_daily_usd: budgetDaily.trim() === "" ? null : Number(budgetDaily),
      }) as Promise<CreateCampaignResult>,
    { onSuccess: onDone },
  );

  const toggle = (platform: string) =>
    setPlatforms((current) =>
      current.includes(platform) ? current.filter((p) => p !== platform) : [...current, platform],
    );

  return (
    <Modal
      open
      onClose={onClose}
      title="Create a campaign"
      width={640}
      footer={
        <>
          <Button variant="ghost" onClick={onClose}>
            Cancel
          </Button>
          <Button
            variant="primary"
            loading={create.pending}
            disabled={name.trim().length === 0}
            onClick={() => void create.run()}
          >
            POST /campaigns
          </Button>
        </>
      }
    >
      <Field label="Name" value={name} onChange={(e) => setName(e.target.value)} />
      <Field label="Goal" value={goal} onChange={(e) => setGoal(e.target.value)} />
      <div>
        <span className="ym-label">Platforms</span>
        <div>
          {CREATE_CAMPAIGN_PLATFORMS.map((p) => (
            <Button
              key={p}
              size="sm"
              variant={platforms.includes(p) ? "primary" : "secondary"}
              onClick={() => toggle(p)}
            >
              {humanize(p)}
            </Button>
          ))}
        </div>
        <p className="ym-hint">
          The backend accepts only {CREATE_CAMPAIGN_PLATFORMS.join(", ")} here
          and returns 400 for anything else. A campaign built from a master uses
          the wider short-form platform set instead.
        </p>
      </div>
      <Grid min={150} gap="sm">
        <Select label="Automation level" value={automation} onChange={(e) => setAutomation(e.target.value)}>
          {AUTOMATION_LEVELS.map((a) => (
            <option key={a} value={a}>
              {humanize(a)}
            </option>
          ))}
        </Select>
        <Field label="Target videos" type="number" min={0} value={targetVideos} onChange={(e) => setTargetVideos(e.target.value)} />
        <Field label="Videos per day" type="number" min={0} step="0.1" value={videosPerDay} onChange={(e) => setVideosPerDay(e.target.value)} />
        <Field
          label="Budget per day (USD)"
          type="number"
          min={0}
          step="0.01"
          value={budgetDaily}
          onChange={(e) => setBudgetDaily(e.target.value)}
          hint="Leave blank to send null, which is different from 0."
        />
      </Grid>
      {create.error ? <p className="ym-error">{create.error}</p> : null}
    </Modal>
  );
}

function FromMasterModal({ onClose, onDone }: { onClose: () => void; onDone: (id: string) => void }) {
  const content = useWsQuery<{ total: number; items: CampaignContentRow[] }>(
    "/content?limit=200",
  );
  const [masterId, setMasterId] = useState("");
  const [name, setName] = useState("");
  const [goal, setGoal] = useState("");
  const [platforms, setPlatforms] = useState<string[]>([]);
  const [desiredShorts, setDesiredShorts] = useState("3");
  const [cta, setCta] = useState<string>("FOLLOW");

  const create = useMutation<void, FromMasterResult>(
    () =>
      wsApi.post("/campaigns/from-master", {
        master_content_id: masterId,
        name,
        goal,
        target_platforms: platforms,
        desired_shorts: Number(desiredShorts) || 1,
        cta_kind: cta,
      }) as Promise<FromMasterResult>,
    { onSuccess: (r) => onDone(r.id) },
  );

  const toggle = (platform: string) =>
    setPlatforms((current) =>
      current.includes(platform) ? current.filter((p) => p !== platform) : [...current, platform],
    );

  /* `_serialize_content` does not expose `derivation_type`, so there is no
     field here that distinguishes a master from a short. Rather than guess, the
     whole library is offered and the backend's own 404 is the arbiter: a master
     id that is not in this workspace is refused with "content not found". */
  const candidates = useMemo(() => content.data?.items ?? [], [content.data]);

  return (
    <Modal
      open
      onClose={onClose}
      title="Build a campaign from a master"
      width={680}
      footer={
        <>
          <Button variant="ghost" onClick={onClose}>
            Cancel
          </Button>
          <Button
            variant="primary"
            loading={create.pending}
            disabled={!masterId}
            onClick={() => void create.run()}
          >
            POST /campaigns/from-master
          </Button>
        </>
      }
    >
      <p className="ym-hint">
        Creates a <strong>DRAFT</strong> campaign whose shorts are derived from
        one master. Creating a draft is not approval, and it publishes nothing.
      </p>
      <Select label="Master content item" value={masterId} onChange={(e) => setMasterId(e.target.value)}>
        <option value="">Select a master…</option>
        {candidates.map((c) => (
          <option key={c.id} value={c.id}>
            {c.topic} — {humanize(c.status)}
          </option>
        ))}
      </Select>
      {content.error ? <p className="ym-error">Could not load content items: {content.error}</p> : null}
      <Field label="Campaign name" value={name} onChange={(e) => setName(e.target.value)} hint="Blank uses “Campaign from &lt;master topic&gt;”." />
      <Field label="Goal" value={goal} onChange={(e) => setGoal(e.target.value)} />
      <div>
        <span className="ym-label">Target platforms</span>
        <div>
          {CAMPAIGN_PLATFORMS.map((p) => (
            <Button
              key={p}
              size="sm"
              variant={platforms.includes(p) ? "primary" : "secondary"}
              onClick={() => toggle(p)}
            >
              {humanize(p)}
            </Button>
          ))}
        </div>
        <p className="ym-hint">
          Blank uses every campaign platform. The backend returns 400 for a
          platform outside this list.
        </p>
      </div>
      <Grid min={150} gap="sm">
        <Field label="Desired shorts" type="number" min={1} max={20} value={desiredShorts} onChange={(e) => setDesiredShorts(e.target.value)} hint="1–20. Target videos = shorts × platforms." />
        <Select label="CTA kind" value={cta} onChange={(e) => setCta(e.target.value)}>
          {CTA_KINDS.map((c) => (
            <option key={c} value={c}>
              {humanize(c)}
            </option>
          ))}
        </Select>
      </Grid>
      {create.error ? <p className="ym-error">{create.error}</p> : null}
    </Modal>
  );
}

export function Campaigns() {
  const { workspaceId, workspace } = useSession();
  const navigate = useNavigate();
  const [creating, setCreating] = useState(false);
  const [fromMaster, setFromMaster] = useState(false);

  const campaigns = useWsQuery<CampaignList>("/campaigns");
  const posts = useWsQuery<PublishedPostList>("/publishing/posts?limit=100");
  const handoff = useHandoffPlatforms();

  const rows = campaigns.data?.items ?? null;

  const modeCounts = useMemo(() => {
    if (posts.data === null) return null;
    const counts = { LIVE: 0, MOCK: 0, HANDOFF: 0, UNAVAILABLE: 0 } as Record<ResolvedMode, number>;
    for (const post of posts.data.items) {
      counts[resolvePublicationMode(post, handoff.ids.has(post.platform))] += 1;
    }
    return counts;
  }, [posts.data, handoff.ids]);

  const columns: Column<CampaignRow>[] = [
    {
      key: "name",
      header: "Campaign",
      cell: (c) => (
        <span>
          <strong>{c.name || c.id.slice(0, 8)}</strong>
          {c.goal ? <span className="ym-notif-detail"> — {c.goal.slice(0, 90)}</span> : null}
        </span>
      ),
    },
    { key: "status", header: "Status", cell: (c) => <StatusBadge status={c.status} /> },
    {
      key: "automation",
      header: "Automation",
      cell: (c) => <Badge tone={c.automation_level === "AUTONOMOUS" ? "warning" : "neutral"}>{humanize(c.automation_level)}</Badge>,
      hideBelow: "md",
    },
    {
      key: "platforms",
      header: "Platforms",
      cell: (c) =>
        (c.platforms ?? []).length ? (
          <span>
            {(c.platforms ?? []).map((p) => (
              <Badge key={p} tone="info">
                {humanize(p)}
              </Badge>
            ))}
          </span>
        ) : (
          <span className="ym-muted">none declared</span>
        ),
      hideBelow: "lg",
    },
    {
      key: "target",
      header: "Target videos",
      align: "right",
      cell: (c) => c.target_videos,
    },
    {
      key: "rate",
      header: "Per day",
      align: "right",
      cell: (c) => (c.videos_per_day > 0 ? c.videos_per_day : <span className="ym-muted">UNAVAILABLE</span>),
      hideBelow: "lg",
    },
    {
      key: "budget",
      header: "Daily budget",
      align: "right",
      cell: (c) =>
        // null means the column was never set — a real "undeclared", not 0.
        c.budget_daily_usd === null || c.budget_daily_usd === undefined ? (
          <span className="ym-muted" title="No daily budget was declared">
            UNDECLARED
          </span>
        ) : (
          <Money usd={c.budget_daily_usd} />
        ),
      hideBelow: "md",
    },
    {
      key: "window",
      header: "Window",
      cell: (c) =>
        c.starts_at || c.ends_at ? (
          <span className="ym-notif-detail">
            {isoDay(c.starts_at)} → {isoDay(c.ends_at)}
          </span>
        ) : (
          <span className="ym-muted">open</span>
        ),
      hideBelow: "lg",
    },
  ];

  if (!workspaceId) {
    return (
      <>
        <PageHeader title="Campaigns" description="Campaign → master → shorts → variants → publications." />
        <Panel title="No workspace selected">
          <EmptyState
            title="Nothing to list"
            description="Campaigns are workspace-scoped. Select a workspace to load its campaigns."
          />
        </Panel>
      </>
    );
  }

  return (
    <>
      <PageHeader
        title="Campaigns"
        description={workspace?.name ? `${workspace.name} — the master-to-shorts pipeline.` : "The master-to-shorts pipeline."}
        actions={
          <>
            <Button onClick={() => setFromMaster(true)}>From master</Button>
            <Button variant="primary" onClick={() => setCreating(true)}>
              New campaign
            </Button>
          </>
        }
      />

      <Panel title="At a glance" dense>
        <Grid min={170} gap="sm">
          <StatTile
            label="Campaigns"
            value={rows === null ? undefined : rows.length}
            unavailable={rows === null}
            source="GET /campaigns"
          />
          <StatTile
            label="Running or ready"
            value={rows === null ? undefined : rows.filter((c) => ["RUNNING", "READY"].includes((c.status ?? "").toUpperCase())).length}
            unavailable={rows === null}
            tone="info"
            source="GET /campaigns"
          />
          <StatTile
            label="Cancelled"
            value={rows === null ? undefined : rows.filter((c) => (c.status ?? "").toUpperCase() === "CANCELLED").length}
            unavailable={rows === null}
            tone={rows && rows.some((c) => (c.status ?? "").toUpperCase() === "CANCELLED") ? "warning" : "neutral"}
            source="GET /campaigns"
          />
          <StatTile
            label="Live publications"
            value={modeCounts === null ? undefined : modeCounts.LIVE}
            unavailable={modeCounts === null}
            tone="live"
            hint="Only a real remote id counts as live."
            source="GET /publishing/posts"
          />
          <StatTile
            label="Mock publications"
            value={modeCounts === null ? undefined : modeCounts.MOCK}
            unavailable={modeCounts === null}
            tone="mock"
            hint="Nothing was sent; a local stand-in recorded the intent."
            source="GET /publishing/posts"
          />
          <StatTile
            label="Handoff publications"
            value={modeCounts === null ? undefined : modeCounts.HANDOFF}
            unavailable={modeCounts === null}
            tone="handoff"
            hint="Media prepared; a human still has to publish."
            source="GET /publishing/posts + /distribution/capabilities"
          />
          <StatTile
            label="Mode not reported"
            value={modeCounts === null ? undefined : modeCounts.UNAVAILABLE}
            unavailable={modeCounts === null}
            tone="unknown"
            hint="is_mock=false cannot separate LIVE from HANDOFF, so no mode is claimed."
            source="GET /publishing/posts"
          />
          <StatTile
            label="Handoff platforms known"
            value={handoff.loaded ? handoff.ids.size : undefined}
            unavailable={!handoff.loaded}
            hint="From the publisher registry, not a platform-name check."
            source="GET /distribution/capabilities"
          />
        </Grid>
        {handoff.error ? (
          <p className="ym-error">
            The publisher registry could not be read ({handoff.error}). Every
            publication therefore renders MODE UNAVAILABLE rather than being
            labelled live on a guess.
          </p>
        ) : null}
      </Panel>

      <Panel title="Campaigns" dense>
        <QueryBoundary query={campaigns} skeletonRows={6}>
          {(d) => (
            <DataTable
              rows={d.items ?? []}
              columns={columns}
              rowKey={(c) => c.id}
              caption="Campaigns"
              maxHeight={620}
              empty="No campaign yet"
              emptyHint="A campaign appears here once an operator creates one, or the planner's trend → campaign flow creates a DRAFT from an approved plan item."
              onRowClick={(c) => navigate(`/campaigns/${c.id}`)}
            />
          )}
        </QueryBoundary>
      </Panel>

      <Panel
        title="Recent publications in this workspace"
        subtitle="Workspace-scoped: GET /publishing/posts carries no campaign_id, so none of these rows is attributed to a campaign."
        dense
      >
        <QueryBoundary query={posts} skeletonRows={4}>
          {(d) => (
            <DataTable
              rows={d.items ?? []}
              rowKey={(p) => p.id}
              caption="Published posts"
              maxHeight={420}
              empty="No publication recorded"
              emptyHint="Nothing has been published for this workspace. A publication row is only written after a provider accepted the content or a handoff was prepared."
              columns={[
                { key: "published", header: "Published at", cell: (p) => (p.published_at ? p.published_at.replace("Z", " UTC").replace("T", " ") : <span className="ym-muted">—</span>) },
                { key: "platform", header: "Platform", cell: (p) => humanize(p.platform) },
                {
                  key: "mode",
                  header: "Publication mode",
                  cell: (p) => <PublicationModeBadge mode={resolvePublicationMode(p, handoff.ids.has(p.platform))} />,
                },
                { key: "title", header: "Title", cell: (p) => p.title || <span className="ym-muted">—</span>, hideBelow: "md" },
                {
                  key: "remote",
                  header: "Remote URL",
                  cell: (p) =>
                    p.remote_url ? (
                      <a className="ym-notif-detail" href={p.remote_url} target="_blank" rel="noreferrer">
                        {p.remote_url.slice(0, 48)}
                      </a>
                    ) : (
                      <span className="ym-muted" title="No remote id — a mock or a handoff never carries one">
                        none
                      </span>
                    ),
                  hideBelow: "lg",
                },
                { key: "views", header: "Views", align: "right", cell: (p) => p.metrics?.views },
                { key: "likes", header: "Likes", align: "right", cell: (p) => p.metrics?.likes, hideBelow: "md" },
                {
                  key: "completion",
                  header: "Completion",
                  align: "right",
                  cell: (p) =>
                    p.metrics?.completion_rate === null || p.metrics?.completion_rate === undefined ? (
                      <span className="ym-muted" title="The provider did not report a completion rate">
                        UNAVAILABLE
                      </span>
                    ) : (
                      `${((p.metrics?.completion_rate ?? 0) * 100).toFixed(1)}%`
                    ),
                  hideBelow: "lg",
                },
              ]}
            />
          )}
        </QueryBoundary>
      </Panel>

      {creating ? <CreateCampaignModal onClose={() => setCreating(false)} onDone={() => { setCreating(false); campaigns.reload(); }} /> : null}
      {fromMaster ? (
        <FromMasterModal
          onClose={() => setFromMaster(false)}
          onDone={(id) => {
            setFromMaster(false);
            navigate(`/campaigns/${id}`);
          }}
        />
      ) : null}
    </>
  );
}

export default Campaigns;