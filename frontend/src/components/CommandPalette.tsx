import { useEffect, useMemo, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { api, wsApi, getWorkspace } from "../lib/api";

type Cmd = {
  id: string;
  label: string;
  section: string;
  icon?: string;
  keywords?: string;
  run: () => void;
};

/** Global command palette (Ctrl/Cmd+K): navigation, autopilot actions and
    categorized search across content/trends. Keyboard driven. */
export default function CommandPalette({ open, onClose }: { open: boolean; onClose: () => void }) {
  const nav = useNavigate();
  const [q, setQ] = useState("");
  const [idx, setIdx] = useState(0);
  const [results, setResults] = useState<{ content: any[]; opportunities: any[] }>({ content: [], opportunities: [] });
  const [busyAction, setBusyAction] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);
  const listRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (open) {
      setQ(""); setIdx(0); setResults({ content: [], opportunities: [] });
      setTimeout(() => inputRef.current?.focus(), 30);
    }
  }, [open]);

  const go = (path: string) => { onClose(); nav(path); };

  async function autopilot(action: string) {
    setBusyAction(true);
    try {
      if (action === "start") await wsApi.post("/autopilot/start", { mode: "CONTINUOUS" });
      else await wsApi.post(`/autopilot/${action}`);
      onClose();
      nav("/");
    } catch { /* toast handled by caller pages; palette stays open on failure */ }
    finally { setBusyAction(false); }
  }

  // global search (debounced) when query length >= 2
  useEffect(() => {
    if (!open || q.trim().length < 2) return;
    const t = setTimeout(async () => {
      try {
        const [c, o] = await Promise.all([
          wsApi.get(`/content?search=${encodeURIComponent(q)}&limit=5`).catch(() => ({ items: [] })),
          api("GET", `/workspaces/${getWorkspace()}/opportunities?limit=100`).then((r: any) => ({
            items: (r.items ?? []).filter((x: any) => x.topic.toLowerCase().includes(q.toLowerCase())).slice(0, 5),
          })).catch(() => ({ items: [] })),
        ]);
        setResults({ content: c.items ?? [], opportunities: o.items ?? [] });
      } catch {}
    }, 200);
    return () => clearTimeout(t);
  }, [q, open]);

  const commands: Cmd[] = useMemo(() => {
    const base: Cmd[] = [
      { id: "nav-dash", label: "Command Center", section: "Navigate", run: () => go("/") },
      { id: "nav-live", label: "Live Monitor — real-time graphs & agent work graph", section: "Navigate", run: () => go("/live"), keywords: "live monitor real time graph dag agents activity" },
      { id: "nav-auto", label: "Autopilot", section: "Navigate", run: () => go("/autopilot") },
      { id: "nav-trends", label: "Trend Center", section: "Navigate", run: () => go("/trends") },
      { id: "nav-ideas", label: "Ideas", section: "Navigate", run: () => go("/ideas") },
      { id: "nav-studio", label: "Content Studio", section: "Navigate", run: () => go("/studio") },
      { id: "nav-calendar", label: "Calendar", section: "Navigate", run: () => go("/calendar") },
      { id: "nav-publishing", label: "Publishing", section: "Navigate", run: () => go("/publishing") },
      { id: "nav-analytics", label: "Analytics", section: "Navigate", run: () => go("/analytics") },
      { id: "nav-intel", label: "Intelligence", section: "Navigate", run: () => go("/intelligence") },
      { id: "nav-agents", label: "Agents", section: "Navigate", run: () => go("/agents"), keywords: "agents crew status" },
      { id: "nav-campaigns", label: "Campaigns", section: "Navigate", run: () => go("/campaigns") },
      { id: "nav-health", label: "System Health", section: "Navigate", run: () => go("/health") },
      { id: "nav-integrations", label: "Integrations — Telegram remote control", section: "Navigate", run: () => go("/integrations"), keywords: "telegram phone mobile bot pairing remote" },
      { id: "nav-settings", label: "Settings", section: "Navigate", run: () => go("/settings"), keywords: "config keys api telegram token" },
      { id: "act-start", label: "Start Autopilot", section: "Actions", run: () => autopilot("start") },
      { id: "act-pause", label: "Pause Autopilot", section: "Actions", run: () => autopilot("pause") },
      { id: "act-resume", label: "Resume Autopilot", section: "Actions", run: () => autopilot("resume") },
      { id: "act-stop", label: "Stop Autopilot", section: "Actions", run: () => autopilot("stop") },
      { id: "act-one", label: "Run One Cycle", section: "Actions", run: () => autopilot("run-one-cycle") },
      { id: "act-compose", label: "New scheduled post (Composer)", section: "Actions", run: () => go("/composer") },
      { id: "act-connect", label: "Connect account…", section: "Actions", run: () => go("/publishing") },
    ];
    const ql = q.trim().toLowerCase();
    const filtered = ql
      ? base.filter((c) => c.label.toLowerCase().includes(ql) || (c.keywords ?? "").includes(ql))
      : base;
    const contentCmds: Cmd[] = results.content.map((c) => ({
      id: `content-${c.id}`, section: "Content",
      label: `[${c.status}] ${String(c.topic).slice(0, 60)}`,
      run: () => go(`/studio/${c.id}`),
    }));
    const trendCmds: Cmd[] = results.opportunities.map((o) => ({
      id: `trend-${o.id}`, section: "Trends",
      label: `${Number(o.score).toFixed(0)} · ${String(o.topic).slice(0, 55)}`,
      run: () => go("/trends"),
    }));
    return [...filtered, ...contentCmds, ...trendCmds];
  }, [q, results.content, results.opportunities]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => { setIdx(0); }, [q]);

  function onKeyDown(e: React.KeyboardEvent) {
    if (e.key === "ArrowDown") { e.preventDefault(); setIdx((i) => Math.min(i + 1, commands.length - 1)); }
    else if (e.key === "ArrowUp") { e.preventDefault(); setIdx((i) => Math.max(i - 1, 0)); }
    else if (e.key === "Enter") {
      e.preventDefault();
      commands[idx]?.run();
    } else if (e.key === "Escape") onClose();
  }

  if (!open) return null;

  let lastSection = "";
  return (
    <div className="fixed inset-0 z-[90] bg-black/50 flex items-start justify-center pt-[12vh] px-4"
         onClick={onClose} role="dialog" aria-modal="true" aria-label="Command palette">
      <div className="card w-full max-w-xl overflow-hidden shadow-2xl" style={{ background: "var(--bg-panel)" }} onClick={(e) => e.stopPropagation()}>
        <input
          ref={inputRef}
          value={q}
          onChange={(e) => setQ(e.target.value)}
          onKeyDown={onKeyDown}
          placeholder="Type a command or search content & trends…"
          className="w-full bg-transparent border-0 outline-none px-4 py-3.5 text-sm font-mono"
          style={{ borderBottom: "1px solid var(--border)", color: "var(--text)" }}
          aria-label="Search commands"
        />
        <div ref={listRef} className="max-h-[46vh] overflow-y-auto py-1.5">
          {commands.length === 0 && (
            <p className="px-4 py-6 text-center text-sm" style={{ color: "var(--text-muted)" }}>No matches.</p>
          )}
          {commands.map((c, i) => {
            const header = c.section !== lastSection ? c.section : null;
            lastSection = c.section;
            return (
              <div key={c.id}>
                {header && (
                  <p className="px-4 pt-2 pb-1 text-[9.5px] font-mono font-semibold uppercase tracking-[0.16em]"
                     style={{ color: "var(--text-faint)" }}>{header}</p>
                )}
                <button
                  className={`w-full text-left px-4 py-2 text-sm flex items-center justify-between border-l-2 ${
                    i === idx ? "" : ""
                  }`}
                  style={{
                    background: i === idx ? "var(--accent-dim)" : "transparent",
                    color: "var(--text)",
                    borderLeftColor: i === idx ? "var(--accent)" : "transparent",
                  }}
                  onMouseEnter={() => setIdx(i)}
                  onClick={() => c.run()}
                >
                  <span className="truncate">{c.label}</span>
                  {i === idx && <span className="text-[10px] font-mono" style={{ color: "var(--accent)" }}>↵</span>}
                </button>
              </div>
            );
          })}
        </div>
        <div className="px-4 py-2 text-[10px] flex gap-3"
             style={{ borderTop: "1px solid var(--border)", color: "var(--text-muted)" }}>
          <span>↑↓ navigate</span><span>↵ select</span><span>esc close</span>
        </div>
      </div>
    </div>
  );
}
