import { useMemo, useState } from "react";
import { wsApi } from "../../lib/api";
import { useFetch } from "../../hooks/hooks";
import { Badge, Card, Empty, ErrorBox, Loading, toast } from "../ui";
import { fmtAgo } from "../../lib/format";

/* Wire shape: backend comment_to_dict (engine/collab/comments.py). */
export type CollabComment = {
  id: string;
  parent_id: string | null;
  target_type: string;
  target_id: string;
  anchor: Record<string, unknown>;
  body: string;
  author_id: string;
  mentions: string[];
  version_ref: string | null;
  resolved_at: string | null;
  resolved_by: string | null;
  created_at: string;
  updated_at: string;
};

type Scene = { id: string; index: number; title: string; start_seconds: number; end_seconds: number };
type Member = { user_id: string; role: string; email: string };
type AnchorMode = "timeline" | "timestamp" | "time_range" | "scene" | "timeline_item";

const ANCHOR_MODES: { key: AnchorMode; label: string }[] = [
  { key: "timeline", label: "On the timeline" },
  { key: "timestamp", label: "At playhead" },
  { key: "time_range", label: "Time range" },
  { key: "scene", label: "Scene" },
  { key: "timeline_item", label: "Selected clip" },
];

function sec(t: number): string {
  const m = Math.floor(t / 60);
  const s = (t % 60).toFixed(1).padStart(4, "0");
  return `${String(m).padStart(2, "0")}:${s}`;
}

function shortId(s?: string | null, n = 8): string {
  if (!s) return "—";
  return s.length > n ? `${s.slice(0, n)}…` : s;
}

/** The editor panel's view of one thread list. */
type Props = {
  timelineId: string;
  time: number;
  scenes: Scene[];
  selectedClipId?: string | null;
  selectedClip?: { id: string; start: number; duration: number } | null;
  onSeek: (t: number) => void;
};

/**
 * Comments on the current timeline.
 *
 * Anchor vocabulary follows the backend exactly (api/v1/comments.py): the
 * (target_type, target_id) pair selects the comment's bucket and the anchor
 * dict carries the position. The panel therefore READS every bucket that can
 * point at this timeline — timeline/timestamp/time_range/timeline_item under
 * the timeline id, scene comments under each scene id — and renders them as
 * one thread list. Replies reuse their root's target (backend 422 otherwise),
 * resolve/reopen is offered on ROOT comments only, and every 4xx comes back
 * as a toast (never an unhandled rejection).
 */
export default function CommentsPanel({ timelineId, time, scenes, selectedClipId, selectedClip, onSeek }: Props) {
  const [includeResolved, setIncludeResolved] = useState(false);
  const sceneKey = scenes.map((s) => s.id).join(",");

  const list = useFetch<CollabComment[]>(async () => {
    const queries: [string, string][] = [
      ["timeline", timelineId],
      ["timestamp", timelineId],
      ["time_range", timelineId],
      ["timeline_item", timelineId],
      ...scenes.map((s): [string, string] => ["scene", s.id]),
    ];
    const pages = await Promise.all(
      queries.map(([tt, tid]) =>
        wsApi.get(
          `/comments?target_type=${tt}&target_id=${encodeURIComponent(tid)}&include_resolved=${includeResolved}`
        ) as Promise<{ items?: CollabComment[] }>
      )
    );
    return pages.flatMap((p) => p?.items ?? []);
  }, [timelineId, includeResolved, sceneKey]);

  const members = useFetch<{ items?: Member[] }>(
    () => wsApi.get("/members") as Promise<{ items?: Member[] }>,
    [timelineId]
  );

  const comments = useMemo(
    () =>
      [...(list.data ?? [])].sort((a, b) =>
        (a.created_at ?? "").localeCompare(b.created_at ?? "")
      ),
    [list.data]
  );
  const roots = useMemo(() => comments.filter((c) => !c.parent_id), [comments]);
  const repliesOf = (id: string) => comments.filter((c) => c.parent_id === id);

  /* ---- composer state ---- */
  const [mode, setMode] = useState<AnchorMode>("timeline");
  const [text, setText] = useState("");
  const [anchorStart, setAnchorStart] = useState(0);
  const [rangeStart, setRangeStart] = useState("");
  const [tEnd, setTEnd] = useState("");
  const [sceneId, setSceneId] = useState("");
  const [mentions, setMentions] = useState<string[]>([]);
  const [replyTo, setReplyTo] = useState<CollabComment | null>(null);
  const [busy, setBusy] = useState(false);

  function pickMode(m: AnchorMode) {
    setMode(m);
    setAnchorStart(time); // freeze the playhead at mode pick — honest anchor
    if (m === "time_range") {
      setRangeStart(String(Number(time.toFixed(2))));
      setTEnd(String(Number((time + 5).toFixed(2))));
    }
    if (m === "scene" && !sceneId && scenes[0]) setSceneId(scenes[0].id);
  }

  function fail(e: any, title: string) {
    toast(e?.message ?? "request failed", "error", title);
  }

  async function post(payload: Record<string, unknown>, ok: string) {
    setBusy(true);
    try {
      await wsApi.post("/comments", payload);
      setText("");
      setMentions([]);
      setReplyTo(null);
      toast(ok, "success");
      list.reload();
    } catch (e: any) {
      // 422 (bad anchor / mentions / empty body), 403, 404 -> toast, never crash.
      fail(e, "Comment not posted");
    } finally {
      setBusy(false);
    }
  }

  function submitRoot() {
    const body = text.trim();
    if (!body) {
      toast("Write a comment first", "warning");
      return;
    }
    const payload: Record<string, unknown> = { target_type: "timeline", target_id: timelineId, body, mentions };
    if (mode === "timestamp") {
      payload.target_type = "timestamp";
      payload.anchor = { t_start: Number(anchorStart.toFixed(3)) };
    } else if (mode === "time_range") {
      const start = Number(rangeStart);
      const end = Number(tEnd);
      if (!Number.isFinite(start) || start < 0) {
        toast("Range start must be a non-negative number of seconds", "error", "Invalid time range");
        return;
      }
      if (!Number.isFinite(end)) {
        toast("Range end must be a number of seconds", "error", "Invalid time range");
        return;
      }
      if (end <= start) {
        // same rule the backend enforces (t_end > t_start) — fail before the round trip
        toast("t_end must be greater than t_start", "error", "Invalid time range");
        return;
      }
      payload.target_type = "time_range";
      payload.anchor = { t_start: Number(start.toFixed(3)), t_end: end };
    } else if (mode === "scene") {
      if (!sceneId) {
        toast("Pick a scene", "warning");
        return;
      }
      payload.target_type = "scene";
      payload.target_id = sceneId;
      payload.anchor = { scene_id: sceneId };
    } else if (mode === "timeline_item") {
      if (!selectedClipId) {
        toast("Select a clip on the timeline first", "warning");
        return;
      }
      payload.target_type = "timeline_item";
      payload.anchor = { item_id: selectedClipId };
    }
    void post(payload, "Comment posted");
  }

  function submitReply() {
    if (!replyTo) return;
    const body = text.trim();
    if (!body) {
      toast("Write a reply first", "warning");
      return;
    }
    // replies must target the SAME target as their root (backend 422)
    void post(
      {
        target_type: replyTo.target_type,
        target_id: replyTo.target_id,
        parent_id: replyTo.id,
        anchor: replyTo.anchor,
        body,
        mentions,
      },
      "Reply posted"
    );
  }

  async function toggleResolve(c: CollabComment) {
    try {
      await wsApi.post(`/comments/${c.id}/${c.resolved_at ? "reopen" : "resolve"}`);
      toast(c.resolved_at ? "Comment reopened" : "Comment resolved", "success");
      list.reload();
    } catch (e: any) {
      fail(e, c.resolved_at ? "Reopen failed" : "Resolve failed");
    }
  }

  function startReply(c: CollabComment) {
    setReplyTo(c);
    setText("");
  }

  const memberList = members.data?.items ?? [];
  const sceneTitle = (id: string) => scenes.find((s) => s.id === id)?.title ?? shortId(id);

  /* Submit is disabled until the anchor satisfies the SAME rules the API
     enforces (api/v1/comments.py: timestamp -> t_start >= 0; time_range ->
     t_start + t_end with t_end > t_start; scene/clip anchors need a target). */
  function rangeValid(): boolean {
    const s = Number(rangeStart);
    const e = Number(tEnd);
    return Number.isFinite(s) && s >= 0 && Number.isFinite(e) && e > s;
  }
  const anchorOk = replyTo
    ? true
    : mode === "timestamp"
      ? Number.isFinite(anchorStart) && anchorStart >= 0
      : mode === "time_range"
        ? rangeValid()
        : mode === "scene"
          ? !!sceneId
          : mode === "timeline_item"
            ? !!selectedClipId
            : true;
  const canPost = !!text.trim() && anchorOk && !busy;

  return (
    <Card>
      <div className="flex items-center gap-2 flex-wrap">
        <b className="text-[13px]">Comments</b>
        <Badge tone="muted">{roots.length} threads</Badge>
        {list.loading && !list.data ? <Loading rows={1} /> : null}
        <button className="btn-ghost !text-xs ml-auto" onClick={() => setIncludeResolved((v) => !v)}>
          {includeResolved ? "Hide resolved" : "Show resolved"}
        </button>
        <button className="btn-ghost !text-xs" onClick={list.reload}>↻</button>
      </div>

      {/* ---- composer (root or reply) ---- */}
      <div className="mt-2 space-y-2">
        <div className="flex gap-2 flex-wrap items-center">
          <select
            className="select !w-auto !text-[12px]"
            value={replyTo ? "reply" : mode}
            disabled={!!replyTo}
            onChange={(e) => pickMode(e.target.value as AnchorMode)}
            aria-label="Comment anchor"
          >
            {replyTo ? <option value="reply">Reply</option> : null}
            {ANCHOR_MODES.map((m) => (
              <option key={m.key} value={m.key}>{m.label}</option>
            ))}
          </select>
          {mode === "timestamp" && !replyTo && (
            <span className="font-mono text-[11.5px]" style={{ color: "var(--text-faint)" }}>
              @ {sec(anchorStart)} <button className="btn-ghost !text-[11px]" onClick={() => setAnchorStart(time)}>use playhead</button>
            </span>
          )}
          {mode === "time_range" && !replyTo && (
            <>
              <input
                className="input !w-20 !text-[12px]"
                inputMode="decimal"
                value={rangeStart}
                onChange={(e) => setRangeStart(e.target.value)}
                aria-label="Range start (seconds)"
                title="Range start in seconds"
              />
              <span
                className="font-mono text-[11.5px]"
                style={{ color: "var(--text-faint)" }}
                aria-hidden="true"
              >
                →
              </span>
              <input
                className="input !w-24 !text-[12px]"
                inputMode="decimal"
                value={tEnd}
                onChange={(e) => setTEnd(e.target.value)}
                aria-label="Range end (seconds)"
                title="Range end in seconds (must be greater than the start)"
              />
              {selectedClip && (
                <button
                  className="btn-ghost !text-[11px]"
                  onClick={() => {
                    setRangeStart(String(Number(selectedClip.start.toFixed(2))));
                    setTEnd(String(Number((selectedClip.start + selectedClip.duration).toFixed(2))));
                  }}
                >
                  from clip
                </button>
              )}
            </>
          )}
          {mode === "scene" && !replyTo && (
            <select
              className="select !w-auto !text-[12px]"
              value={sceneId}
              onChange={(e) => setSceneId(e.target.value)}
              aria-label="Scene"
            >
              <option value="">Pick a scene…</option>
              {scenes.map((s) => (
                <option key={s.id} value={s.id}>#{s.index + 1} {s.title}</option>
              ))}
            </select>
          )}
          {mode === "timeline_item" && !replyTo && (
            selectedClipId ? (
              <Badge tone="info">clip {shortId(selectedClipId)}</Badge>
            ) : (
              <span className="text-[11.5px]" style={{ color: "var(--text-faint)" }}>Select a clip first</span>
            )
          )}
          {replyTo && (
            <span className="text-[11.5px]" style={{ color: "var(--text-faint)" }}>
              replying to {shortId(replyTo.id)}
              <button className="btn-ghost !text-[11px]" onClick={() => { setReplyTo(null); setText(""); }}>cancel</button>
            </span>
          )}
        </div>

        <textarea
          className="textarea"
          rows={2}
          value={text}
          onChange={(e) => setText(e.target.value)}
          placeholder={replyTo ? "Write a reply…" : "Write a comment…"}
        />

        <div className="flex items-center gap-2 flex-wrap">
          <select
            className="select !w-auto !text-[12px]"
            value=""
            disabled={members.loading}
            onChange={(e) => {
              const v = e.target.value;
              if (v && !mentions.includes(v)) setMentions((m) => [...m, v]);
            }}
            aria-label="Mention a workspace member"
          >
            <option value="">
              {members.error ? "Members unavailable" : "@ mention…"}
            </option>
            {memberList.map((m) => (
              <option key={m.user_id} value={m.user_id}>{m.email || m.user_id}</option>
            ))}
          </select>
          {mentions.map((id) => (
            <span
              key={id}
              className="inline-flex items-center gap-1 rounded px-1.5 py-0.5 text-[11px] font-mono"
              style={{ background: "var(--seam)" }}
            >
              {memberList.find((m) => m.user_id === id)?.email || id}
              <button
                aria-label="Remove mention"
                onClick={() => setMentions((m) => m.filter((x) => x !== id))}
              >
                ✕
              </button>
            </span>
          ))}
          <button
            className="btn-primary !text-xs ml-auto"
            disabled={!canPost}
            title={
              !text.trim()
                ? "Write a comment first"
                : !anchorOk
                  ? "Anchor is incomplete or invalid (range needs end > start)"
                  : "Post"
            }
            onClick={() => (replyTo ? submitReply() : submitRoot())}
          >
            {busy ? "Posting…" : replyTo ? "Post reply" : "Post comment"}
          </button>
        </div>
      </div>

      {/* ---- threads ---- */}
      <div className="mt-3 space-y-3">
        {/* 404 = the target is not visible in this workspace (engine
            `_ensure_target_visible` only 404s a foreign DB-backed row); a
            FRESH timeline answers 200 with an empty list, so "No comments
            yet" below is the true empty state — an error strip is always a
            real failure, never "no comments". */}
        {list.error ? (
          <ErrorBox error={list.error} onRetry={list.reload} />
        ) : !roots.length ? (
          list.loading ? null : (
            <Empty
              title="No comments yet"
              hint="Anchor one to the playhead, a time range, a scene, or the selected clip above. Comments never change the timeline itself."
            />
          )
        ) : (
          roots.map((c) => (
            <div key={c.id} className="rounded-xl p-2.5" style={{ background: "var(--bg-inset)", border: "1px solid var(--seam)" }}>
              <CommentRow
                c={c}
                resolved={!!c.resolved_at}
                onToggle={() => void toggleResolve(c)}
                onReply={() => startReply(c)}
                onSeek={onSeek}
                sceneTitle={sceneTitle}
              />
              {repliesOf(c.id).map((r) => (
                <div key={r.id} className="ml-4 mt-2 pl-2" style={{ borderLeft: "2px solid var(--border)" }}>
                  <CommentRow
                    c={r}
                    isReply
                    resolved={!!r.resolved_at}
                    onToggle={null}
                    onReply={null}
                    onSeek={onSeek}
                    sceneTitle={sceneTitle}
                  />
                </div>
              ))}
            </div>
          ))
        )}
        {roots.length > 0 && list.loading && !list.data ? <Loading rows={2} /> : null}
      </div>
    </Card>
  );
}

function anchorSeconds(c: CollabComment): number | null {
  const a = c.anchor ?? {};
  return typeof a.t_start === "number" ? a.t_start : null;
}

function CommentRow({
  c,
  isReply = false,
  resolved,
  onToggle,
  onReply,
  onSeek,
  sceneTitle,
}: {
  c: CollabComment;
  isReply?: boolean;
  resolved: boolean;
  onToggle: (() => void) | null;
  onReply: (() => void) | null;
  onSeek: (t: number) => void;
  sceneTitle: (id: string) => string;
}) {
  const a = c.anchor ?? {};
  const tStart = anchorSeconds(c);
  const tEnd = typeof a.t_end === "number" ? a.t_end : null;

  let anchorLabel: string | null = null;
  if (c.target_type === "timestamp" && tStart != null) anchorLabel = `@ ${sec(tStart)}`;
  else if (c.target_type === "time_range" && tStart != null)
    anchorLabel = `${sec(tStart)}–${tEnd != null ? sec(tEnd) : "?"}`;
  else if (c.target_type === "scene") anchorLabel = `scene “${sceneTitle(c.target_id)}”`;
  else if (c.target_type === "timeline_item")
    anchorLabel = `clip ${shortId(typeof a.item_id === "string" ? a.item_id : c.target_id)}`;

  return (
    <div className="flex items-start gap-2">
      <div className="flex-1 min-w-0">
        <div className="flex items-center gap-2 flex-wrap">
          <span className="font-mono text-[11.5px]">{shortId(c.author_id, 10)}</span>
          <span className="text-[11px]" style={{ color: "var(--text-faint)" }}>{fmtAgo(c.created_at)}</span>
          {resolved && <Badge tone="success">resolved</Badge>}
          {anchorLabel && (
            tStart != null && !isReply ? (
              <button
                className="btn-ghost !text-[11px] font-mono"
                title="Seek to anchor"
                onClick={() => onSeek(tStart)}
              >
                ⏱ {anchorLabel}
              </button>
            ) : (
              <Badge tone="muted">{anchorLabel}</Badge>
            )
          )}
          {c.version_ref && (
            <span className="font-mono text-[10.5px]" style={{ color: "var(--text-faint)" }}>
              on v{c.version_ref}
            </span>
          )}
          {c.mentions?.length ? (
            <span className="text-[10.5px]" style={{ color: "var(--info)" }}>
              ↳ {c.mentions.length} mention{c.mentions.length > 1 ? "s" : ""}
            </span>
          ) : null}
        </div>
        <div className="text-[13px] mt-1 whitespace-pre-wrap break-words">{c.body}</div>
        <div className="flex items-center gap-2 mt-1">
          {!isReply && onToggle && (
            <button className="btn-ghost !text-[11px]" onClick={onToggle}>
              {resolved ? "Reopen" : "Resolve"}
            </button>
          )}
          {!isReply && onReply && (
            <button className="btn-ghost !text-[11px]" onClick={onReply}>Reply</button>
          )}
          {resolved && c.resolved_by && (
            <span className="font-mono text-[10.5px]" style={{ color: "var(--text-faint)" }}>
              by {shortId(c.resolved_by, 10)}
            </span>
          )}
        </div>
      </div>
    </div>
  );
}
