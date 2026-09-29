import { useState } from "react";
import { ApiError, wsApi } from "../../lib/api";
import { useFetch } from "../../hooks/hooks";
import { Badge, Card, ErrorBox, Field, Loading, Modal, toast } from "../ui";
import { fmtAgo } from "../../lib/format";

/* Wire shapes: engine/collab/reviews.py `_review_dto` + route `_capabilities`. */
type ReviewState = "DRAFT" | "IN_REVIEW" | "CHANGES_REQUESTED" | "APPROVED" | "REJECTED" | "CANCELLED" | string;

/** One read-only row from GET /revisions (engine/collab/revisions.py
 *  `revision_to_dict`): state + the validated ask list. */
type Revision = {
  id: string;
  review_id: string | null;
  state: string;
  items: { kind: string; description: string }[];
  created_at: string;
};

/** FastAPI rides the 409 body under `detail`; api.ts stringifies objects, so
 *  the {error, stale, tip_version} payload arrives as JSON in e.message. */
function parseReview409(message?: string): { error?: string; stale?: boolean; tip_version?: string | null } | null {
  try {
    const parsed = JSON.parse(message ?? "");
    return parsed && typeof parsed === "object" ? parsed : null;
  } catch {
    return null;
  }
}

type Review = {
  id: string;
  target_type: string;
  target_id: string;
  title: string;
  state: ReviewState;
  bound_version: string | null;
  stale: boolean;
  stale_detected_at: string | null;
  approval_valid?: boolean;
  created_by: string;
  created_at: string;
};

/** Exactly what the backend computes for THIS user (reviews.py::_capabilities).
 *  There is no `can_submit` key in the real payload — submit is floor-gated. */
type Capabilities = {
  can_approve?: boolean;
  can_request_changes?: boolean;
  can_cancel?: boolean;
  can_assign?: boolean;
};

type ReviewDetail = Review & {
  capabilities?: Capabilities;
  decisions?: { id: string; decision: string; body: string; created_at: string }[];
};

/** Revision item as DecisionBody.items[] takes it (api/v1/reviews.py
 *  RevisionItem): kind enum + description; the engine validates both. */
const ITEM_KINDS = ["trim", "timing", "asset_swap", "text", "voice", "caption", "brand", "other"];

/** One line -> one item. Optional `kind:` prefix sets the enum; anything
 *  else files under `other` (honest default, never a rejected call). */
function parseRevisionItems(text: string): { kind: string; description: string }[] {
  return text
    .split("\n")
    .map((l) => l.trim())
    .filter(Boolean)
    .slice(0, 100)
    .map((line) => {
      const m = line.match(/^([a-z_]+)\s*[:\-–—]\s+(.+)$/i);
      const kind = m && ITEM_KINDS.includes(m[1].toLowerCase()) ? m[1].toLowerCase() : "other";
      const description = (m && ITEM_KINDS.includes(m[1].toLowerCase()) ? m[2] : line).slice(0, 2000).trim();
      return { kind, description };
    })
    .filter((i) => i.description);
}

type Props = { timelineId: string; version: number };

const DECIDABLE = "IN_REVIEW";

function reviewTone(state: string): "success" | "warning" | "error" | "info" | "muted" {
  switch (state) {
    case "APPROVED": return "success";
    case "IN_REVIEW": return "info";
    case "CHANGES_REQUESTED": return "warning";
    case "REJECTED": return "error";
    default: return "muted";
  }
}

/**
 * Editor status bar for the timeline's active review.
 *
 * RBAC-aware UI, never RBAC enforcement: buttons are gated on the review
 * detail `capabilities` (hide on explicit `false`) and the state machine
 * mirrors ALLOWED_TRANSITIONS (decisions are legal only from IN_REVIEW, so a
 * fresh review is created AND submitted before the bar reports IN_REVIEW).
 * Every failure path lands in a toast; a stale approval is a 409, not a crash.
 */
export default function ReviewStatusBar({ timelineId, version }: Props) {
  const [busy, setBusy] = useState("");
  const [draft, setDraft] = useState<{ kind: "APPROVE" | "REQUEST_CHANGES"; label: string } | null>(null);
  const [body, setBody] = useState("");
  const [itemsText, setItemsText] = useState("");

  const list = useFetch<{ items?: Review[] }>(
    () =>
      wsApi.get(`/reviews?target_type=timeline_version&target_id=${timelineId}`) as Promise<{
        items?: Review[];
      }>,
    [timelineId]
  );

  // Read-only revision requests for this timeline (list supports target
  // filters: api/v1/reviews.py list_revisions). Refreshed after every action.
  const revisions = useFetch<{ items?: Revision[] }>(
    () =>
      wsApi.get(`/revisions?target_type=timeline_version&target_id=${timelineId}`) as Promise<{
        items?: Revision[];
      }>,
    [timelineId]
  );

  // The one review this bar reports on: prefer the actionable state, but an
  // APPROVED review still shows (its stale / approval_valid warnings are the
  // whole point of reading it). Terminal REJECTED/CANCELLED are skipped so a
  // new round can be requested.
  const items = list.data?.items ?? [];
  const active =
    items.find((r) => r.state === DECIDABLE) ??
    items.find((r) => r.state === "DRAFT") ??
    items.find((r) => r.state === "CHANGES_REQUESTED") ??
    items.find((r) => r.state === "APPROVED") ??
    null;

  const detail = useFetch<ReviewDetail | null>(
    () =>
      active
        ? (wsApi.get(`/reviews/${active.id}`) as Promise<ReviewDetail>)
        : Promise.resolve(null),
    [active?.id ?? ""]
  );

  const review = active && detail.data?.id === active.id ? detail.data : active;
  const caps: Capabilities = detail.data?.capabilities ?? {};
  const capsLoaded = !!active && detail.data?.id === active.id && !!detail.data?.capabilities;
  const actionable =
    !!review?.state &&
    (review.state === "IN_REVIEW" || review.state === "DRAFT" || review.state === "CHANGES_REQUESTED");
  const stale = !!review?.stale;
  const invalidApproval = review?.state === "APPROVED" && review?.approval_valid === false;

  /** 409 from the reviews API: stale approval ({error, stale:true}) or an
   *  illegal transition ({error}) — a warning with the server's own words. */
  function warn409(e: any): boolean {
    if (!(e instanceof ApiError) || e.status !== 409) return false;
    const parsed = parseReview409(e.message);
    if (parsed?.stale) {
      toast(
        `${parsed.error ?? e.message}${parsed.tip_version ? ` (tip v${parsed.tip_version})` : ""}`,
        "warning",
        "Approval stale — target changed since review"
      );
    } else {
      toast(parsed?.error ?? e.message, "warning", "Review state conflict");
    }
    return true;
  }

  async function run(fn: () => Promise<unknown>, ok: string, fail: string) {
    setBusy(fail);
    try {
      await fn();
      toast(ok, "success");
    } catch (e: any) {
      if (!warn409(e)) toast(e?.message ?? "request failed", "error", fail);
    } finally {
      setBusy("");
      list.reload();
      detail.reload();
      revisions.reload();
    }
  }

  /** Request Review: create the DRAFT bound to the current tip, then submit
   *  (submit rebinds to the tip and is the only legal DRAFT -> IN_REVIEW edge). */
  async function requestReview() {
    setBusy("Requesting review…");
    try {
      const created = (await wsApi.post("/reviews", {
        target_type: "timeline_version",
        target_id: timelineId,
        title: `Timeline review — v${version}`,
      })) as { id?: string } | undefined;
      if (!created?.id) throw new Error("review was not created");
      await wsApi.post(`/reviews/${created.id}/submit`);
      toast(`Review requested on v${version}`, "success");
    } catch (e: any) {
      if (!warn409(e)) toast(e?.message ?? "request failed", "error", "Could not request review");
    } finally {
      setBusy("");
      list.reload();
      detail.reload();
    }
  }

  function submitDecision() {
    if (!draft || !review) return;
    const label = draft.label;
    const kind = draft.kind;
    const items = kind === "REQUEST_CHANGES" ? parseRevisionItems(itemsText) : [];
    setDraft(null);
    setBody("");
    setItemsText("");
    // With items: each line files its own revision item; without items the
    // engine opens one `other` item carrying the note (reviews.py::_auto_revision).
    const payload: Record<string, unknown> = { decision: kind, body };
    if (items.length) payload.items = items;
    void run(
      () => wsApi.post(`/reviews/${review.id}/decisions`, payload),
      `${label} recorded`,
      `${label} failed`
    );
  }

  return (
    <Card>
      <div className="flex items-center gap-2 flex-wrap">
        <b className="text-[13px]">Review</b>
        <Badge tone={review ? reviewTone(review.state) : "muted"}>
          {review ? review.state : "NO ACTIVE REVIEW"}
        </Badge>
        {review && (
          <span className="font-mono text-[11.5px]" style={{ color: "var(--text-faint)" }}>
            bound v{review.bound_version ?? "—"}
            {review.title ? ` · ${review.title}` : ""}
          </span>
        )}
        {invalidApproval && <Badge tone="error">approval invalid</Badge>}
        <Badge tone="info">tip v{version}</Badge>

        <span className="ml-auto flex items-center gap-2 flex-wrap">
          {list.loading && !list.data && !active ? (
            <Loading rows={1} />
          ) : list.error ? (
            <ErrorBox error={list.error} onRetry={list.reload} />
          ) : !actionable ? (
            /* No live review (none / APPROVED / REJECTED / CANCELLED): a new
               round starts by creating a fresh DRAFT on the current tip. */
            <button className="btn-primary !text-xs" disabled={!!busy} onClick={() => void requestReview()}>
              {busy === "Requesting review…" ? "…" : "Request Review"}
            </button>
          ) : review?.state === "DRAFT" ? (
            <button
              className="btn-accent !text-xs"
              disabled={!!busy}
              title="Submit binds the review to the current version"
              onClick={() =>
                void run(() => wsApi.post(`/reviews/${review.id}/submit`), "Review submitted", "Submit failed")
              }
            >
              {busy === "Submit failed" ? "…" : "Submit for review"}
            </button>
          ) : review?.state === "CHANGES_REQUESTED" ? (
            <button
              className="btn-accent !text-xs"
              disabled={!!busy}
              title="Re-opens the review and rebinds it to the current version"
              onClick={() =>
                void run(
                  () => wsApi.post(`/reviews/${review.id}/submit`),
                  "Re-requested on the current version",
                  "Re-request failed"
                )
              }
            >
              {busy === "Re-request failed" ? "…" : "Re-request for review"}
            </button>
          ) : review?.state === DECIDABLE ? (
            /* Decision buttons appear only once the detail's `capabilities`
               object has arrived, and only when the flag is exactly true —
               hide on false AND while unknown (the server stays the boundary). */
            !capsLoaded ? (
              detail.error ? (
                <span className="text-[12px]" style={{ color: "var(--danger)" }}>
                  Capabilities unavailable ({detail.error}) — actions hidden.
                </span>
              ) : (
                <Loading rows={1} />
              )
            ) : (
              <>
                {caps.can_approve && (
                  <button
                    className="btn-primary !text-xs"
                    disabled={!!busy}
                    onClick={() => setDraft({ kind: "APPROVE", label: "Approve" })}
                  >
                    Approve
                  </button>
                )}
                {caps.can_request_changes && (
                  <button
                    className="btn-outline !text-xs"
                    disabled={!!busy}
                    onClick={() => setDraft({ kind: "REQUEST_CHANGES", label: "Request changes" })}
                  >
                    Request Changes
                  </button>
                )}
                {caps.can_cancel && (
                  <button
                    className="btn-ghost !text-xs"
                    disabled={!!busy}
                    title="IN_REVIEW → CANCELLED (the only legal cancel edge)"
                    onClick={() =>
                      void run(() => wsApi.post(`/reviews/${review.id}/cancel`), "Review cancelled", "Cancel failed")
                    }
                  >
                    Cancel review
                  </button>
                )}
                {!caps.can_approve && !caps.can_request_changes && !caps.can_cancel && (
                  <span className="text-[12px]" style={{ color: "var(--text-faint)" }}>
                    You hold no decision capability on this target.
                  </span>
                )}
              </>
            )
          ) : null}
        </span>
      </div>

      {(stale || invalidApproval) && (
        <div
          className="rounded-xl px-3 py-2 text-[12.5px] mt-2"
          style={{ background: "var(--warn-dim)", border: "1px solid var(--warn)" }}
        >
          <b style={{ color: "var(--warn)" }}>Target changed since this review was requested — approval stale.</b>{" "}
          <span style={{ color: "var(--text-muted)" }}>
            Bound to v{review?.bound_version ?? "—"}, timeline is now v{version}. Approving is refused (409) until the
            review is re-requested on the current version.
            {invalidApproval ? " The recorded approval no longer covers the current target (approval_valid: false)." : ""}
            {review?.stale_detected_at ? " Detected on the latest read." : ""}
          </span>
        </div>
      )}

      {(revisions.data?.items ?? []).length > 0 && (
        <div className="mt-2 space-y-1">
          <span className="panel-label">Revision requests for this timeline</span>
          {(revisions.data?.items ?? []).slice(0, 5).map((r) => (
            <div key={r.id} className="flex items-start gap-2 flex-wrap text-[12px]">
              <Badge tone={r.state === "OPEN" ? "warning" : r.state === "ADDRESSED" ? "success" : "muted"}>
                {r.state}
              </Badge>
              <span className="flex-1 min-w-0" style={{ color: "var(--text-muted)" }}>
                {(r.items ?? []).map((i) => i.description).join(" · ") || "(no items)"}
              </span>
              <span className="font-mono text-[10.5px]" style={{ color: "var(--text-faint)" }}>
                {fmtAgo(r.created_at)}
              </span>
            </div>
          ))}
          {(revisions.data?.items ?? []).length > 5 && (
            <div className="text-[11px]" style={{ color: "var(--text-faint)" }}>
              +{(revisions.data?.items ?? []).length - 5} more (see Reviews)
            </div>
          )}
        </div>
      )}
      {revisions.error && (
        <div className="mt-1 text-[11.5px]" style={{ color: "var(--text-faint)" }}>
          Revision list unavailable: {revisions.error}
        </div>
      )}

      <div className="mt-1 text-[11.5px]" style={{ color: "var(--text-faint)" }}>
        Hidden actions reflect the server's <span className="font-mono">capabilities</span> — the backend stays the
        boundary; a denied call still fails server-side.
      </div>

      <Modal open={!!draft} onClose={() => setDraft(null)} title={draft ? `${draft.label} this review` : ""}>
        <div className="text-[12.5px] mb-3" style={{ color: "var(--text-muted)" }}>
          {draft?.kind === "APPROVE"
            ? "Approving re-verifies the version binding — if the timeline moved since the review was requested, the server answers 409 and nothing is written."
            : "Requesting changes files a revision request that stays OPEN until someone addresses it explicitly."}
        </div>
        <Field label="Note (optional)" hint="Stored on the append-only decision record.">
          <textarea
            className="textarea"
            rows={3}
            value={body}
            onChange={(e) => setBody(e.target.value)}
            placeholder="What did you check? What should change?"
          />
        </Field>
        {draft?.kind === "REQUEST_CHANGES" && (
          <Field
            label="Revision items (one per line, optional)"
            hint="Each line files one revision item (max 100). Prefix a kind — trim, timing, asset_swap, text, voice, caption, brand, other — e.g. 'trim: cut the intro'; unprefixed lines file as 'other'. Left empty, the note above becomes the single item."
          >
            <textarea
              className="textarea"
              rows={3}
              value={itemsText}
              onChange={(e) => setItemsText(e.target.value)}
              placeholder={"trim: shorten the intro to 2s\nvoice: re-record the last line"}
            />
          </Field>
        )}
        <div className="flex justify-end gap-2">
          <button className="btn-ghost !text-xs" onClick={() => setDraft(null)}>Close</button>
          <button
            className={draft?.kind === "REQUEST_CHANGES" ? "btn-outline !text-xs" : "btn-primary !text-xs"}
            disabled={!!busy}
            onClick={submitDecision}
          >
            {busy ? "…" : `${draft?.label ?? "Confirm"}`}
          </button>
        </div>
      </Modal>
    </Card>
  );
}
