import { useEffect, useRef, useState } from "react";
import { NavLink, Outlet, useNavigate } from "react-router-dom";
import { api, getWorkspace, setAuth, setWorkspace, activityStreamUrl } from "../lib/api";
import { useTheme } from "../hooks/hooks";
import { Badge } from "./ui";
import { brandLogoSrc } from "../pages/Brand";

const NAV: { group: string; items: { to: string; label: string; icon: string; keys?: string }[] }[] = [
  { group: "Operate", items: [
    { to: "/", label: "Command Center", icon: "◉", keys: "c" },
    { to: "/live", label: "Live Monitor", icon: "⌁", keys: "l" },
    { to: "/autopilot", label: "Autopilot", icon: "⏻", keys: "a" },
    { to: "/calendar", label: "Calendar", icon: "▤", keys: "k" },
  ]},
  { group: "Discover", items: [
    { to: "/trends", label: "Trend Center", icon: "▲", keys: "t" },
    { to: "/ideas", label: "Ideas", icon: "✦", keys: "i" },
  ]},
  { group: "Create", items: [
    { to: "/studio", label: "Content Studio", icon: "▦", keys: "s" },
    { to: "/composer", label: "Composer", icon: "✎" },
    { to: "/assets", label: "Assets", icon: "◈" },
    { to: "/brand", label: "Brand", icon: "◐" },
  ]},
  { group: "Distribute", items: [
    { to: "/publishing", label: "Publishing", icon: "↥", keys: "p" },
    { to: "/inbox", label: "Inbox", icon: "✉" },
  ]},
  { group: "Understand", items: [
    { to: "/analytics", label: "Analytics", icon: "◫", keys: "n" },
    { to: "/intelligence", label: "Intelligence", icon: "☰", keys: "g" },
    { to: "/memory", label: "Memory", icon: "◍" },
  ]},
  { group: "Control", items: [
    { to: "/agents", label: "Agents", icon: "⬡" },
    { to: "/campaigns", label: "Campaigns", icon: "◎" },
    { to: "/integrations", label: "Telegram", icon: "✈" },
    { to: "/health", label: "System Health", icon: "♥", keys: "h" },
    { to: "/settings", label: "Settings", icon: "⚙" },
  ]},
];

type FeedItem = { level: string; kind: string; message: string; created_at?: string };

export default function Layout() {
  const nav = useNavigate();
  const [theme, setTheme] = useTheme();
  const [collapsed, setCollapsed] = useState(false);
  const [feed, setFeed] = useState<FeedItem[]>([]);
  const [feedOpen, setFeedOpen] = useState(false);
  const [palette, setPalette] = useState(false);
  const [status, setStatus] = useState<any>(null);
  const [mode, setMode] = useState<any>(null);
  const [wsName, setWsName] = useState("…");
  const [workspaces, setWorkspaces] = useState<any[]>([]);
  const [brand, setBrand] = useState<any>(null);
  const lastKey = useRef<{ key: string; at: number } | null>(null);

  function applyBrand(b: any) {
    setBrand(b);
    const root = document.documentElement;
    const accent = b?.accent;
    if (accent && /^#[0-9a-fA-F]{6}$/.test(accent)) {
      root.style.setProperty("--accent", accent);
      root.style.setProperty("--accent-hover", accent);
      root.style.setProperty("--accent-dim", accent + "1f");
      root.style.setProperty("--accent-glow", accent + "40");
      root.style.setProperty("--accent-bright", accent);
    }
  }

  useEffect(() => {
    const load = () => {
      if (!getWorkspace()) return;
      api<any>("GET", `/workspaces/${getWorkspace()}/brand`).then((r) => applyBrand(r.brand)).catch(() => {});
    };
    load();
    window.addEventListener("ym-brand", load);
    return () => window.removeEventListener("ym-brand", load);
  }, []);

  useEffect(() => {
    api<any>("GET", "/workspaces").then((r) => {
      setWorkspaces(r.items ?? []);
      const cur = (r.items ?? []).find((w: any) => w.id === getWorkspace());
      if (cur) setWsName(cur.name);
    }).catch(() => {});
    fetch("/api/v1/system/mode").then((r) => r.json()).then(setMode).catch(() => {});
    api<any>("GET", `/workspaces/${getWorkspace()}/activity/recent?limit=40`)
      .then((r) => setFeed(r.items ?? [])).catch(() => {});
    const es = new EventSource(activityStreamUrl());
    es.onmessage = (ev) => {
      try { setFeed((f) => [...f.slice(-150), JSON.parse(ev.data)]); } catch {}
    };
    const poll = setInterval(() => {
      api<any>("GET", `/workspaces/${getWorkspace()}/autopilot/status`).then(setStatus).catch(() => {});
    }, 8000);
    api<any>("GET", `/workspaces/${getWorkspace()}/autopilot/status`).then(setStatus).catch(() => {});
    return () => { es.close(); clearInterval(poll); };
  }, []);

  // g+key navigation + Ctrl/Cmd+K palette
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const tag = (e.target as HTMLElement)?.tagName;
      if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") return;
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "k") { e.preventDefault(); setPalette((v) => !v); return; }
      if (e.key === "/") { e.preventDefault(); setPalette(true); return; }
      const now = Date.now();
      if (lastKey.current?.key === "g" && now - lastKey.current.at < 900) {
        const hit = NAV.flatMap((g) => g.items).find((i) => i.keys === e.key);
        if (hit) nav(hit.to);
        lastKey.current = null;
      } else if (e.key === "g") lastKey.current = { key: "g", at: now };
      if (e.key === "Escape") setPalette(false);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [nav]);

  const running = status?.state === "RUNNING" || status?.state === "STARTING";
  const mockMode = mode?.mocks && (mode.mocks.publishing || mode.mocks.analytics || mode.mocks.video_engine);

  return (
    <div className="min-h-screen flex" style={{ background: "var(--bg)", color: "var(--text)" }}>
      {/* Sidebar */}
      <aside className="hidden md:flex flex-col shrink-0 py-4 px-3 gap-1 overflow-y-auto"
        style={{ width: collapsed ? 62 : 218, borderRight: "var(--seam)", background: "var(--bg-panel)" }}>
        <div className="flex items-center gap-2 px-2 mb-3">
          {brand?.logo_path ? (
            <img src={brandLogoSrc()} alt="studio logo" className="w-8 h-8 rounded-lg object-cover" />
          ) : (
            <span className="grid place-items-center w-8 h-8 rounded-lg font-bold text-white text-[15px]" style={{ background: "var(--accent)" }}>¥</span>
          )}
          {!collapsed && <span className="font-bold tracking-tight text-[15px]">{brand?.app_name || "YMONEY"}</span>}
          <button className="ml-auto text-[12px] opacity-60 hover:opacity-100" onClick={() => setCollapsed(!collapsed)} aria-label="Toggle sidebar">{collapsed ? "»" : "«"}</button>
        </div>
        {NAV.map((g) => (
          <div key={g.group} className="mb-1.5">
            {!collapsed && <div className="px-2.5 mb-1 text-[10px] font-semibold uppercase tracking-[0.09em]" style={{ color: "var(--text-faint)" }}>{g.group}</div>}
            {g.items.map((it) => (
              <NavLink key={it.to} to={it.to} title={it.label}
                className={({ isActive }) => `flex items-center gap-2.5 px-2.5 py-[7px] rounded-lg text-[13px] font-medium no-underline ${isActive ? "" : ""}`}
                style={({ isActive }: any) => isActive ? { background: "var(--accent-dim)", color: "var(--accent)" } : { color: "var(--text-muted)" }}>
                <span className="w-5 text-center">{it.icon}</span>
                {!collapsed && it.label}
              </NavLink>
            ))}
          </div>
        ))}
        <div className="mt-auto pt-3 space-y-2" style={{ borderTop: "var(--seam)" }}>
          {!collapsed && (
            <select className="select !text-xs" value={getWorkspace() ?? ""} aria-label="Workspace"
              onChange={(e) => { setWorkspace(e.target.value); window.location.reload(); }}>
              {workspaces.map((w: any) => <option key={w.id} value={w.id}>{w.name}</option>)}
            </select>
          )}
          <div className="flex gap-1.5">
            <button className="btn-ghost !px-2 !py-1 text-[12px] flex-1" onClick={() => setTheme(theme === "dark" ? "light" : "dark")} title="Toggle theme">{theme === "dark" ? "☀" : "☾"}</button>
            <button className="btn-ghost !px-2 !py-1 text-[12px] flex-1" onClick={() => { setAuth(null); nav("/"); window.location.reload(); }} title="Sign out">⎋</button>
          </div>
        </div>
      </aside>

      {/* Main */}
      <div className="flex-1 min-w-0 flex flex-col">
        <header className="flex items-center gap-3 px-5 py-3 sticky top-0 z-30" style={{ background: "var(--bg-panel)", borderBottom: "var(--seam)" }}>
          <button className="md:hidden btn-ghost !px-2 !py-1" onClick={() => setCollapsed(!collapsed)}>☰</button>
          <span className="font-semibold text-[14px] truncate">{wsName}</span>
          <Badge tone={mockMode ? "warning" : "success"}>{mockMode ? "MOCK" : mode ? mode.mode.toUpperCase() : "…"}</Badge>
          {status && <Badge tone={running ? "success" : "muted"}>{status.state}{status.cycles_completed ? ` · ${status.cycles_completed} cycles` : ""}</Badge>}
          <div className="ml-auto flex items-center gap-2">
            <button className="btn-outline !text-xs !py-1.5" onClick={() => setPalette(true)}>⌘K Commands</button>
            <button className="btn-ghost !text-xs !py-1.5 relative" onClick={() => setFeedOpen(!feedOpen)}>
              Activity
              <span className="live-dot inline-block w-2 h-2 rounded-full ml-1.5" style={{ background: "var(--accent)" }} />
            </button>
          </div>
        </header>
        <main className="flex-1 p-5 max-w-[1200px] w-full mx-auto"><Outlet /></main>
      </div>

      {/* Activity drawer */}
      {feedOpen && (
        <aside className="fixed right-0 top-0 bottom-0 w-[340px] z-40 p-4 overflow-y-auto" style={{ background: "var(--bg-panel)", borderLeft: "var(--seam)" }}>
          <div className="flex items-center justify-between mb-3">
            <b className="text-[14px]">Live activity</b>
            <button className="btn-ghost !px-2 !py-1 text-[12px]" onClick={() => setFeedOpen(false)}>✕</button>
          </div>
          <div className="space-y-0">
            {feed.slice(-40).reverse().map((e, i) => (
              <div key={i} className="py-2 text-[12.5px]" style={{ borderBottom: "var(--seam)" }}>
                <div className="break-words">{e.message}</div>
                <div className="font-mono text-[10.5px]" style={{ color: "var(--text-faint)" }}>{e.kind}</div>
              </div>
            ))}
            {!feed.length && <div className="text-[12.5px]" style={{ color: "var(--text-faint)" }}>No activity yet.</div>}
          </div>
        </aside>
      )}

      {/* Command palette */}
      {palette && (
        <div className="fixed inset-0 z-50 p-4 pt-[12vh] flex justify-center" style={{ background: "rgba(0,0,0,0.45)" }} onClick={() => setPalette(false)}>
          <div className="card w-full max-w-[520px] h-fit overflow-hidden" style={{ padding: 0 }} onClick={(e) => e.stopPropagation()}>
            <div className="px-4 py-3 text-[12px]" style={{ borderBottom: "var(--seam)", color: "var(--text-muted)" }}>Go to… (or press g then a key)</div>
            <div className="max-h-[50vh] overflow-y-auto p-1.5">
              {NAV.flatMap((g) => g.items).map((it) => (
                <button key={it.to} className="w-full text-left px-3 py-2 rounded-lg text-[13.5px] hover:opacity-100 flex gap-2.5 items-center"
                  style={{ color: "var(--text)" }} onClick={() => { nav(it.to); setPalette(false); }}
                  onMouseOver={(e) => (e.currentTarget.style.background = "var(--bg-subtle)")}
                  onMouseOut={(e) => (e.currentTarget.style.background = "transparent")}>
                  <span>{it.icon}</span> {it.label}
                  {it.keys && <kbd className="ml-auto font-mono text-[10.5px] px-1.5 py-0.5 rounded" style={{ background: "var(--bg-subtle)", color: "var(--text-faint)" }}>g {it.keys}</kbd>}
                </button>
              ))}
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
