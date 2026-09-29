import { useState } from "react";
import type { ReactNode } from "react";
import { ApiError, api, wsApi } from "../lib/api";
import { useFetch } from "../hooks/hooks";
import {
  Accordion,
  Badge,
  Card,
  ConfirmButton,
  Empty,
  ErrorBox,
  Field,
  Loading,
  Modal,
  PageHeader,
  SearchInput,
  Tabs,
  toast,
} from "../components/ui";
import { fmtAgo, fmtDate } from "../lib/format";

/* ---- Wire shapes (contracts §5 + §7, models/collab.py) ------------------ */

type ReviewState =
  | "DRAFT"
  | "IN_REVIEW"
  | "CHANGES_REQUESTED"
  | "APPROVED"
  | "REJECTED"
  | "CANCELLED";

/** What the backend computed for THIS user on THIS review — exactly the keys
 *  api/v1/reviews.py::_capabilities emits (can_approve, can_request_changes,
 *  can_cancel, can_assign; tests/test_reviews_api.py:382 locks them). There is
 *  NO can_submit/can_comment badge: submit rides the workspace-role floor, so
 *  the page gates it on the member role instead of inventing a capability.
 *  Missing keys mean "backend did not tell us" — we then stay quiet rather
 *  than guess; the server stays the boundary either way. */
type Capabilities = {
  can_approve?: boolean;
  can_request_changes?: boolean;
  can_cancel?: boolean;
  can_assign?: boolean;
};

type Review = {
  id: string;
  project_id: string | null;
  target_type: string;
  target_id: string;
  title: string;
  state: ReviewState | string;
  bound_version: string | null;
  bound_manifest_hash: string | null;
  stale: boolean;
  stale_detected_at: string | null;
  created_by: string;
  created_at: string;
  updated_at?: string;
  closed_at: string | null;
  approval_valid?: boolean;
  capabilities?: Capabilities;
};

type Decision = {
  id: string;
  user_id: string;
  decision: "APPROVE" | "REJECT" | "REQUEST_CHANGES" | string;
  bound_version: string | null;
  bound_manifest_hash: string | null;
  body: string;
  created_at: string;
};

type Assignment = { user_id: string; assigned_by: string; created_at: string };

type ReviewDetail = Review & { decisions?: Decision[]; assignments?: Assignment[] };

type RevisionItem = { kind: string; description: string; anchor?: Record<string, unknown> };

type Revision = {
  id: string;
  review_id: string | null;
  target_type: string;
  target_id: string;
  state: "OPEN" | "ADDRESSED" | "DISMISSED" | string;
  items?: RevisionItem[];
  created_by: string;
  created_at: string;
  resolved_at: string | null;
  resolved_by: string | null;
};

/* ---- constants ---------------------------------------------------------- */

type Filter = "ALL" | ReviewState;

const FILTERS: { key: Filter; label: string }[] = [
  { key: "ALL", label: "All" },
  { key: "IN_REVIEW", label: "In review" },
  { key: "CHANGES_REQUESTED", label: "Changes requested" },
  { key: "APPROVED", label: "Approved" },
  { key: "REJECTED", label: "Rejected" },
  { key: "DRAFT", label: "Draft" },
  { key: "CANCELLED", label: "Cancelled" },
];

function reviewTone(state: string): "success" | "warning" | "error" | "info" | "muted" {
  switch (state) {
    case "APPROVED":
      return "success";
    case "IN_REVIEW":
      return "info";
    case "CHANGES_REQUESTED":
      return "warning";
    case "REJECTED":
      return "error";
    default:
      return "muted";
  }
}

function decisionTone(d: string): "success" | "warning" | "error" {
  if (d === "APPROVE") return "success";
  if (d === "REQUEST_CHANGES") return "warning";
  return "error";
}

function short(s?: string | null, n = 8): string {
  if (!s) return "—";
  return s.length > n ? `${s.slice(0, n)}…` : s;
}

/** A 409 on a stale approve rides a DICT detail ({error, stale, tip_version},
 *  reviews.py::_guard) which lib/api.ts stringifies into the message
 *  (api.ts:81). Parse it back — same idea as Editor.tsx::parseConflict — so the
 *  user reads "target changed" instead of raw JSON. Illegal-transition 409s are
 *  plain strings and fall through untouched. */
function conflictToast(message: string): { text: string; title: string } | null {
  try {
    const parsed = JSON.parse(message);
    if (parsed && typeof parsed === "object" && typeof parsed.error === "string") {
      const tip = parsed.tip_version ? ` — current version is ${parsed.tip_version}` : "";
      return {
        text: `${parsed.error}${tip}`,
        title: parsed.stale ? "Target changed" : "Conflict",
      };
    }
  } catch {
    /* plain-string detail: handled by the caller */
  }
  return null;
}

export default function Reviews() {
  const [filter, setFilter] = useState<Filter>("ALL");
  const [q, setQ] = useState("");
  const [openId, setOpenId] = useState<string | null>(null);
  const [busy, setBusy] = useState("");
  const [draft, setDraft] = useState<{ id: string; kind: string; label: string } | null>(null);
  const [body, setBody] = useState("");

  const list = useFetch<{ items?: Review[] }>(
    () => wsApi.get(filter === "ALL" ? "/reviews" : `/reviews?state=${filter}`) as Promise<{ items?: Review[] }>,
    [filter]
  );
  const detail = useFetch<ReviewDetail | null>(
    () => (openId ? (wsApi.get(`/reviews/${openId}`) as Promise<ReviewDetail>) : Promise.resolve(null)),
    [openId]
  );
  /* Revisions ride the REVIEW route in the real backend
   * (GET /reviews/{id}/revisions -> {"items": [...]}); contracts §7 sketched a
   * flat /revisions collection that was not implemented. Backend wins. */
  const revisions = useFetch<{ items?: Revision[] }>(
    () => (openId ? (wsApi.get(`/reviews/${openId}/revisions`) as Promise<{ items?: Revision[] }>) : Promise.resolve({ items: [] })),
    [openId]
  );
  /* Workspace role for RBAC gating: /auth/me gives MY id (+ superuser flag),
   * /members gives the per-workspace roles (api/v1/workspaces.py:112). A
   * confirmed viewer never sees mutating buttons; if either call fails we stay
   * permissive and let the server's 403 (surfaced as a toast) tell the truth. */
  const me = useFetch<{ id?: string; is_superuser?: boolean }>(
    () => api("GET", "/auth/me") as Promise<{ id?: string; is_superuser?: boolean }>,
    []
  );
  const members = useFetch<{ items?: { user_id: string; role: string }[] }>(
    () => wsApi.get("/members") as Promise<{ items?: { user_id: string; role: string }[] }>,
    []
  );
  const myRole = members.data?.items?.find((m) => m.user_id === me.data?.id)?.role;
  const isViewer = myRole === "viewer" && !me.data?.is_superuser;

  const review = detail.data ?? null;
  const caps: Capabilities = review?.capabilities ?? {};
  const items: Review[] = (list.data?.items ?? []).filter(
    (r) => !q || (r.title ?? "").toLowerCase().includes(q.toLowerCase())
  );

  function open(id: string) {
    setOpenId(id);
    setDraft(null);
    setBody("");
  }

  function close() {
    setOpenId(null);
    setDraft(null);
    setBody("");
  }

  async function act(fn: () => Promise<unknown>, success: string, failure: string) {
    setBusy(failure);
    try {
      await fn();
      toast(success, "success");
      detail.reload();
      list.reload();
      setDraft(null);
      setBody("");
    } catch (e: any) {
      // A stale approval is a 409 with a dict detail, not a crash — parse it
      // into a human sentence (see conflictToast above).
      if (e instanceof ApiError && e.status === 409) {
        const conflict = conflictToast(e.message);
        if (conflict) toast(conflict.text, "warning", conflict.title);
        else toast(e.message, "warning", "Conflict");
      } else {
        toast(e.message ?? "request failed", "error", failure);
      }
      detail.reload();
      list.reload();
    } finally {
      setBusy("");
    }
  }

  function decide(kind: "APPROVE" | "REJECT" | "REQUEST_CHANGES") {
    if (!review) return;
    setDraft({ id: review.id, kind, label: kind === "APPROVE" ? "Approve" : kind === "REJECT" ? "Reject" : "Request changes" });
    setBody("");
  }

  function submitDecision() {
    if (!draft) return;
    const label = draft.label;
    act(
      () => wsApi.post(`/reviews/${draft.id}/decisions`, { decision: draft.kind, body }),
      `${label} recorded`,
      `${label} failed`
    );
  }

  const revs: Revision[] = revisions.data?.items ?? [];

  return (
    <div className="space-y-4">
      <PageHeader
        title="Reviews"
        subtitle="Approvals bind to an exact version — when the target moves, the approval goes stale instead of silently passing."
      />

      <div className="flex flex-wrap gap-3 items-center">
        <Tabs tabs={FILTERS} active={filter} onChange={setFilter} />
        <div className="w-[240px] ml-auto">
          <SearchInput value={q} onChange={setQ} placeholder="Filter by title…" />
        </div>
      </div>

      {list.loading && !list.data ? (
        <Loading rows={3} />
      ) : list.error && !list.data ? (
        <ErrorBox error={list.error} onRetry={list.reload} />
      ) : !items.length ? (
        <Card>
          <Empty
            title={filter === "ALL" ? "No reviews yet" : `Nothing ${filter.toLowerCase().replace(/_/g, " ")}`}
            hint="Reviews are opened from the editor (Request review) or by a workspace member. Approval requires the approve capability on the linked project."
            action={
              <button className="btn-outline !text-xs" onClick={list.reload}>
                Refresh
              </button>
            }
          />
        </Card>
      ) : (
        <div className="grid md:grid-cols-2 gap-3">
          {items.map((r) => (
            <Card key={r.id} className="card-hover">
              <div className="flex items-start gap-2.5">
                <div className="flex-1 min-w-0">
                  <div className="font-semibold text-[14px] leading-snug">{r.title || `${r.target_type} review`}</div>
                  <div className="flex items-center gap-2 mt-1.5 flex-wrap">
                    <Badge tone={reviewTone(r.state)}>{r.state}</Badge>
                    {r.stale && <Badge tone="warning">stale</Badge>}
                    {r.state === "APPROVED" && r.approval_valid === false && <Badge tone="error">approval invalid</Badge>}
                    <span className="font-mono text-[11px]" style={{ color: "var(--text-faint)" }}>
                      {r.target_type}:{short(r.target_id)}
                    </span>
                  </div>
                  <div className="font-mono text-[11px] mt-1" style={{ color: "var(--text-faint)" }}>
                    bound {r.bound_version ?? "no version"} · {fmtAgo(r.created_at)}
                  </div>
                </div>
                <button className="btn-outline !text-xs" onClick={() => open(r.id)}>
                  Open
                </button>
              </div>
            </Card>
          ))}
        </div>
      )}

      {/* ---- detail ------------------------------------------------------ */}
      <Modal open={!!openId} onClose={close} title={review?.title || "Review"} wide>
        {detail.loading && !review ? (
          <Loading rows={3} />
        ) : detail.error && !review ? (
          <ErrorBox error={detail.error} onRetry={detail.reload} />
        ) : !review ? (
          <Empty title="Review not loaded" hint="Close and reopen this review." />
        ) : (
          <div className="space-y-4">
            {review.stale && (
              <div
                className="rounded-xl px-4 py-3 text-[13px]"
                style={{ background: "var(--warn-dim)", border: "1px solid var(--warn)" }}
              >
                <b style={{ color: "var(--warn)" }}>Target changed since review — approval stale.</b>{" "}
                <span style={{ color: "var(--text-muted)" }}>
                  This review is bound to version{" "}
                  <span className="font-mono">{review.bound_version ?? "—"}</span>. Open a new review on the current
                  version; any earlier approval no longer counts.
                  {review.stale_detected_at ? ` Detected ${fmtAgo(review.stale_detected_at)}.` : ""}
                </span>
              </div>
            )}

            <div className="grid sm:grid-cols-2 gap-x-6 gap-y-1.5 text-[12.5px]">
              <Meta label="State" value={<Badge tone={reviewTone(review.state)}>{review.state}</Badge>} />
              <Meta label="Target" value={`${review.target_type}:${review.target_id}`} mono />
              <Meta
                label="Bound version"
                value={review.bound_version ?? "not version-bound (staleness tracked by row timestamp)"}
                mono
              />
              <Meta label="Manifest hash" value={short(review.bound_manifest_hash, 12)} mono />
              <Meta label="Requested by" value={review.created_by} mono />
              <Meta label="Opened" value={fmtDate(review.created_at)} />
              {review.closed_at && <Meta label="Closed" value={fmtDate(review.closed_at)} />}
              {review.approval_valid !== undefined && (
                <Meta
                  label="Approval valid"
                  value={
                    <Badge tone={review.approval_valid ? "success" : "error"}>
                      {review.approval_valid ? "yes" : "no — target moved"}
                    </Badge>
                  }
                />
              )}
            </div>

            {/* decisions — RBAC-aware: the server told us what this user may do */}
            <div>
              <div className="panel-label mb-2">Decision</div>
              <div className="flex gap-2 flex-wrap items-center">
                {review.state === "APPROVED" || review.state === "REJECTED" || review.state === "CANCELLED" ? (
                  <span className="text-[12.5px]" style={{ color: "var(--text-faint)" }}>
                    This review is closed — {review.state.toLowerCase()} is terminal, history stays readable.
                  </span>
                ) : isViewer ? (
                  <span className="text-[12.5px]" style={{ color: "var(--text-faint)" }}>
                    Your workspace role is <b>viewer</b> — you can read this review, but decisions are recorded by
                    members. Hidden buttons are a courtesy; the server rejects a denied call with 403 anyway.
                  </span>
                ) : (
                  <>
                    {caps.can_approve === false && (
                      <span className="text-[12px]" style={{ color: "var(--text-faint)" }}>
                        You don't hold the approve capability on this target.
                      </span>
                    )}
                    {caps.can_approve !== false && (
                      <>
                        <button
                          className="btn-primary !text-xs"
                          disabled={busy !== "" || review.state === "DRAFT"}
                          title={review.state === "DRAFT" ? "Submit the review first" : undefined}
                          onClick={() => decide("APPROVE")}
                        >
                          Approve
                        </button>
                        <button
                          className="btn-outline !text-xs"
                          disabled={busy !== "" || review.state === "DRAFT"}
                          title={review.state === "DRAFT" ? "Submit the review first" : undefined}
                          onClick={() => decide("REJECT")}
                        >
                          Reject
                        </button>
                      </>
                    )}
                    {caps.can_request_changes !== false && (
                      <button
                        className="btn-outline !text-xs"
                        disabled={busy !== "" || review.state === "DRAFT"}
                        title={review.state === "DRAFT" ? "Submit the review first" : undefined}
                        onClick={() => decide("REQUEST_CHANGES")}
                      >
                        Request changes
                      </button>
                    )}
                  </>
                )}
                {!isViewer && review.state === "DRAFT" && (
                  <button
                    className="btn-accent !text-xs"
                    disabled={busy !== ""}
                    onClick={() => act(() => wsApi.post(`/reviews/${review.id}/submit`), "Review submitted", "Submit failed")}
                  >
                    {busy === "Submit failed" ? "…" : "Submit for review"}
                  </button>
                )}
                {!isViewer && review.state === "CHANGES_REQUESTED" && (
                  <button
                    className="btn-accent !text-xs"
                    disabled={busy !== ""}
                    title="Re-opens the review and rebinds it to the current version"
                    onClick={() => act(() => wsApi.post(`/reviews/${review.id}/submit`), "Re-requested on the current version", "Re-request failed")}
                  >
                    {busy === "Re-request failed" ? "…" : "Re-request for review"}
                  </button>
                )}
                {!isViewer &&
                  review.state !== "APPROVED" &&
                  review.state !== "REJECTED" &&
                  review.state !== "CANCELLED" &&
                  caps.can_cancel !== false && (
                    <span className="ml-auto">
                      <ConfirmButton
                        onConfirm={() => act(() => wsApi.post(`/reviews/${review.id}/cancel`), "Review cancelled", "Cancel failed")}
                        confirmText="Cancel review?"
                      >
                        Cancel
                      </ConfirmButton>
                    </span>
                  )}
              </div>
              <div className="mt-2 text-[11.5px]" style={{ color: "var(--text-faint)" }}>
                The backend is the boundary: hidden buttons are a courtesy, and a denied call still returns 403.
              </div>
            </div>

            {/* assignments */}
            <Accordion
              title="Assignments"
              badge={<Badge tone="muted">{(review.assignments ?? []).length}</Badge>}
              defaultOpen
            >
              {(review.assignments ?? []).length ? (
                <div className="space-y-1.5">
                  {(review.assignments ?? []).map((a, i) => (
                    <div key={`${a.user_id}-${i}`} className="flex items-center gap-2 text-[12.5px] flex-wrap">
                      <span className="font-mono">{a.user_id}</span>
                      <span style={{ color: "var(--text-faint)" }}>
                        by {a.assigned_by} · {fmtAgo(a.created_at)}
                      </span>
                    </div>
                  ))}
                </div>
              ) : (
                <div className="text-[12.5px]" style={{ color: "var(--text-faint)" }}>
                  Nobody is assigned — the review creator still owns it.
                </div>
              )}
            </Accordion>

            {/* decisions timeline — append-only */}
            <Accordion
              title="Decisions"
              badge={<Badge tone="muted">{(review.decisions ?? []).length}</Badge>}
              defaultOpen
            >
              {(review.decisions ?? []).length ? (
                <div className="space-y-2.5">
                  {(review.decisions ?? []).map((d) => (
                    <div key={d.id} style={{ borderLeft: "2px solid var(--border)", paddingLeft: 12 }}>
                      <div className="flex items-center gap-2 flex-wrap">
                        <Badge tone={decisionTone(d.decision)}>{d.decision.replace("_", " ")}</Badge>
                        <span className="font-mono text-[11.5px]">{d.user_id}</span>
                        <span className="font-mono text-[11.5px]" style={{ color: "var(--text-faint)" }}>
                          {fmtDate(d.created_at)}
                        </span>
                      </div>
                      <div className="font-mono text-[11px] mt-1" style={{ color: "var(--text-faint)" }}>
                        decided on {d.bound_version ?? "no version"} · hash {short(d.bound_manifest_hash, 12)}
                      </div>
                      {d.body && <div className="text-[12.5px] mt-1 whitespace-pre-wrap">{d.body}</div>}
                    </div>
                  ))}
                </div>
              ) : (
                <div className="text-[12.5px]" style={{ color: "var(--text-faint)" }}>
                  No decisions yet — history appends here and is never rewritten.
                </div>
              )}
            </Accordion>

            {/* revisions */}
            <Accordion
              title="Revisions"
              badge={<Badge tone={revs.some((r) => r.state === "OPEN") ? "warning" : "muted"}>{revs.length}</Badge>}
            >
              {revisions.loading && !revisions.data ? (
                <Loading rows={1} />
              ) : !revs.length ? (
                <div className="text-[12.5px]" style={{ color: "var(--text-faint)" }}>
                  No change requests on this target. Requesting changes files one automatically.
                </div>
              ) : (
                <div className="space-y-3">
                  {revs.map((r) => (
                    <RevisionRow key={r.id} rev={r} />
                  ))}
                  <div className="text-[11.5px]" style={{ color: "var(--text-faint)" }}>
                    Editing the timeline never marks a revision fixed — states move only when someone says so.
                  </div>
                </div>
              )}
            </Accordion>
          </div>
        )}
      </Modal>

      {/* ---- decision composer ------------------------------------------- */}
      <Modal open={!!draft} onClose={() => setDraft(null)} title={draft ? `${draft.label} this review` : ""}>
        <div className="text-[12.5px] mb-3" style={{ color: "var(--text-muted)" }}>
          {draft?.kind === "APPROVE"
            ? "Approving re-verifies the binding — if the target moved since the review opened, the server answers 409 and nothing is written."
            : "A revision request is filed automatically and stays OPEN until someone addresses it."}
        </div>
        <Field label="Note (optional)" hint="Stored on the append-only decision record.">
          <textarea
            className="textarea"
            rows={4}
            value={body}
            onChange={(e) => setBody(e.target.value)}
            placeholder="What did you check? What should change?"
          />
        </Field>
        <div className="flex justify-end gap-2">
          <button className="btn-ghost !text-xs" onClick={() => setDraft(null)}>
            Cancel
          </button>
          <button
            className={draft?.kind === "REJECT" ? "btn-danger !text-xs" : "btn-primary !text-xs"}
            disabled={busy !== ""}
            onClick={submitDecision}
          >
            {busy ? "…" : `${draft?.label ?? "Confirm"}`}
          </button>
        </div>
      </Modal>
    </div>
  );
}

function Meta({ label, value, mono }: { label: string; value: ReactNode; mono?: boolean }) {
  return (
    <div className="flex items-baseline gap-2 min-w-0">
      <span className="text-[11.5px] shrink-0" style={{ color: "var(--text-faint)" }}>
        {label}
      </span>
      <span className={`truncate ${mono ? "font-mono" : ""}`}>{value}</span>
    </div>
  );
}

function RevisionRow({ rev }: { rev: Revision }) {
  const items = rev.items ?? [];
  return (
    <div className="rounded-xl p-3" style={{ background: "var(--bg-inset)", border: "var(--seam)" }}>
      <div className="flex items-center gap-2 flex-wrap">
        <Badge tone={rev.state === "OPEN" ? "warning" : rev.state === "ADDRESSED" ? "success" : "muted"}>{rev.state}</Badge>
        <span className="font-mono text-[11px]" style={{ color: "var(--text-faint)" }}>
          {fmtAgo(rev.created_at)}
        </span>
        {rev.resolved_at && (
          <span className="font-mono text-[11px]" style={{ color: "var(--text-faint)" }}>
            resolved by {rev.resolved_by ?? "unknown"} · {fmtDate(rev.resolved_at)}
          </span>
        )}
      </div>
      {items.length ? (
        <ul className="mt-2 space-y-1 text-[12.5px]">
          {items.map((it, i) => (
            <li key={i} className="flex gap-2">
              <Badge tone="muted">{it.kind}</Badge>
              <span className="flex-1">{it.description}</span>
            </li>
          ))}
        </ul>
      ) : (
        <div className="text-[12.5px] mt-1" style={{ color: "var(--text-faint)" }}>
          No individual items — the request body is empty.
        </div>
      )}
    </div>
  );
}
