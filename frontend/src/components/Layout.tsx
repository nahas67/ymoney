import { useEffect, useRef, useState } from "react";
import { NavLink, Outlet, useNavigate } from "react-router-dom";
import { api, getWorkspace, setAuth, setWorkspace, activityStreamUrl } from "../lib/api";
import { useTheme } from "../hooks/hooks";
import { Avatar, Badge, Toasts, toast } from "./ui";
import { brandLogoSrc } from "../pages/Brand";

const NAV: { group: string; items: { to: string; label: string; icon: string; keys?: string; desc: string }[] }[] = [
  { group: "Operate", items: [
    { to: "/", label: "Command Center", icon: "◉", keys: "c", desc: "START/STOP, pipeline state, decisions" },
    { to: "/live", label: "Live Monitor", icon: "⌁", keys: "l", desc: "Real-time agent activity + work graph" },
    { to: "/autopilot", label: "Autopilot", icon: "⏻", keys: "a", desc: "Jobs, cycles, dead letters" },
    { to: "/calendar", label: "Calendar", icon: "▤", keys: "k", desc: "Schedule + auto-fill" },
    { to: "/approvals", label: "Approvals", icon: "☑", keys: "r", desc: "Human gate: QC queue" },
  ]},
  { group: "Discover", items: [
    { to: "/trends", label: "Trend Center", icon: "▲", keys: "t", desc: "Opportunities by lifecycle + scores" },
    { to: "/ideas", label: "Ideas", icon: "✦", keys: "i", desc: "Idea queue" },
  ]},
  { group: "Create", items: [
    { to: "/studio", label: "Content Studio", icon: "▦", keys: "s", desc: "Library, QC, covers, approvals" },
    { to: "/longform", label: "Long-Form Studio", icon: "🎥", desc: "Multi-chapter video projects" },
    { to: "/composer", label: "Composer", icon: "✎", desc: "Manual post composer" },
    { to: "/assets", label: "Assets", icon: "◈", desc: "Videos, images, voice lab" },
    { to: "/brand", label: "Brand", icon: "◐", desc: "Voice, niche, identity" },
  ]},
  { group: "Distribute", items: [
    { to: "/publishing", label: "Publishing", icon: "↥", keys: "p", desc: "Accounts, jobs, delivery" },
    { to: "/inbox", label: "Inbox", icon: "✉", desc: "Capability surface" },
  ]},
  { group: "Understand", items: [
    { to: "/analytics", label: "Analytics", icon: "◫", keys: "n", desc: "Performance + cost efficiency" },
    { to: "/intelligence", label: "Intelligence", icon: "☰", keys: "g", desc: "Patterns, decisions, strategy" },
    { to: "/memory", label: "Memory", icon: "◍", desc: "Semantic + style memory" },
  ]},
  { group: "Developers", items: [
    { to: "/developers/keys", label: "API Keys", icon: "⚷", keys: "d", desc: "Third-party keys, hash-only" },
    { to: "/developers/webhooks", label: "Webhooks", icon: "⇄", desc: "Signed event delivery" },
    { to: "/developers/templates", label: "Templates", icon: "❐", desc: "Prompt library + customs" },
  ]},
  { group: "Control", items: [
    { to: "/agents", label: "Agents", icon: "⬡", desc: "22-agent crew, runs, toggles" },
    { to: "/campaigns", label: "Campaigns", icon: "◎", desc: "Goals + budgets" },
    { to: "/integrations", label: "Telegram", icon: "✈", desc: "Phone remote control" },
    { to: "/health", label: "System Health", icon: "♥", keys: "h", desc: "Doctor, probes, spend" },
    { to: "/settings", label: "Settings", icon: "⚙", desc: "Safety, connections, keys" },
  ]},
];

type FeedItem = { level: string; kind: string; message: string; created_at?: string };

export default function Layout() {
  const nav = useNavigate();
  const [theme, setTheme] = useTheme();
  const [collapsed, setCollapsed] = useState(false);
  const [mobileOpen, setMobileOpen] = useState(false);
  const [feed, setFeed] = useState<FeedItem[]>([]);
  const [feedOpen, setFeedOpen] = useState(false);
  const [palette, setPalette] = useState(false);
  const [paletteQ, setPaletteQ] = useState("");
  const [status, setStatus] = useState<any>(null);
  const [mode, setMode] = useState<any>(null);
  const [wsName, setWsName] = useState("…");
  const [workspaces, setWorkspaces] = useState<any[]>([]);
  const [brand, setBrand] = useState<any>(null);
  const [wsMenu, setWsMenu] = useState(false);
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
      try {
        const item = JSON.parse(ev.data);
        setFeed((f) => [...f.slice(-150), item]);
        if (item.level === "error") toast(item.message, "error", "Pipeline");
      } catch {}
    };
    const poll = setInterval(() => {
      api<any>("GET", `/workspaces/${getWorkspace()}/autopilot/status`).then(setStatus).catch(() => {});
    }, 8000);
    api<any>("GET", `/workspaces/${getWorkspace()}/autopilot/status`).then(setStatus).catch(() => {});
    return () => { es.close(); clearInterval(poll); };
  }, []);

  // searchable palette + g+key navigation + Ctrl/Cmd+K
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const tag = (e.target as HTMLElement)?.tagName;
      if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") return;
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "k") { e.preventDefault(); setPaletteQ(""); setPalette((v) => !v); return; }
      if (e.key === "/") { e.preventDefault(); setPaletteQ(""); setPalette(true); return; }
      const now = Date.now();
      if (lastKey.current?.key === "g" && now - lastKey.current.at < 900) {
        const hit = NAV.flatMap((g) => g.items).find((i) => i.keys === e.key);
        if (hit) nav(hit.to);
        lastKey.current = null;
      } else if (e.key === "g") lastKey.current = { key: "g", at: now };
      if (e.key === "Escape") { setPalette(false); setFeedOpen(false); setWsMenu(false); }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [nav]);

  const running = status?.state === "RUNNING" || status?.state === "STARTING";
  const mockMode = mode?.mocks && (mode.mocks.publishing || mode.mocks.analytics || mode.mocks.video_engine);
  const palItems = NAV.flatMap((g) => g.items.map((i) => ({ ...i, group: g.group }))).filter(
    (i) => (i.label + " " + i.desc).toLowerCase().includes(paletteQ.toLowerCase())
  );
  const cycleTheme = () => setTheme(theme === "dark" ? "light" : theme === "light" ? "system" : "dark");

  const sidebar = (
    <>
      <div className="flex items-center gap-2.5 px-2 mb-4">
        {brand?.logo_path ? (
          <img src={brandLogoSrc()} alt="studio logo" className="w-9 h-9 rounded-xl object-cover" style={{ boxShadow: "0 0 16px -4px var(--accent-glow)" }} />
        ) : (
          <span className="grid place-items-center w-9 h-9 rounded-xl font-bold text-white text-[16px]"
            style={{ background: "linear-gradient(135deg, var(--accent-bright), var(--accent-deep))", boxShadow: "0 0 16px -4px var(--accent-glow)" }}>¥</span>
        )}
        {!collapsed && (
          <div className="min-w-0">
            <div className="font-bold tracking-tight text-[15px] leading-tight truncate">{brand?.app_name || "YMONEY"}</div>
            <div className="text-[10.5px] font-medium" style={{ color: "var(--text-faint)" }}>autonomous studio</div>
          </div>
        )}
        <button className="ml-auto text-[12px] opacity-60 hover:opacity-100 hidden md:block" onClick={() => setCollapsed(!collapsed)} aria-label="Toggle sidebar">{collapsed ? "»" : "«"}</button>
      </div>
      {NAV.map((g) => (
        <div key={g.group} className="mb-2">
          {!collapsed && <div className="px-3 mb-1 text-[10px] font-semibold uppercase tracking-[0.1em]" style={{ color: "var(--text-faint)" }}>{g.group}</div>}
          {g.items.map((it) => (
            <NavLink key={it.to} to={it.to} title={`${it.label} — ${it.desc}`}
              onClick={() => setMobileOpen(false)}
              className="flex items-center gap-2.5 px-3 py-[8px] rounded-xl text-[13px] font-medium no-underline mb-[2px]"
              style={({ isActive }: any) => isActive
                ? { background: "var(--accent-dim)", color: "var(--accent-bright)", boxShadow: "inset 0 0 0 1px var(--accent)" }
                : { color: "var(--text-muted)" }}>
              <span className="w-5 text-center text-[14px]">{it.icon}</span>
              {!collapsed && it.label}
            </NavLink>
          ))}
        </div>
      ))}
    </>
  );

  return (
    <div className="min-h-screen flex" style={{ background: "transparent", color: "var(--text)" }}>
      {/* Desktop sidebar */}
      <aside className="hidden md:flex flex-col shrink-0 py-4 px-3 overflow-y-auto"
        style={{ width: collapsed ? 66 : 224, borderRight: "var(--seam)", background: "var(--bg-glass)", backdropFilter: "blur(14px)" }}>
        {sidebar}
        <div className="mt-auto pt-3 space-y-2" style={{ borderTop: "var(--seam)" }}>
          {!collapsed && (
            <div className="px-1 text-[11px] font-mono truncate" style={{ color: "var(--text-faint)" }}>
              {status ? `${status.state}${status.cycles_completed ? ` · ${status.cycles_completed} cycles` : ""}` : "status…"}
            </div>
          )}
          <div className="flex gap-1.5">
            <button className="btn-ghost !px-2 !py-1.5 text-[13px] flex-1" onClick={cycleTheme} title={`Theme: ${theme}`}>
              {theme === "dark" ? "☾" : theme === "light" ? "☀" : "◐"}
            </button>
            <button className="btn-ghost !px-2 !py-1.5 text-[13px] flex-1" onClick={() => { setAuth(null); nav("/"); window.location.reload(); }} title="Sign out">⎋</button>
          </div>
        </div>
      </aside>

      {/* Mobile drawer */}
      {mobileOpen && (
        <div className="fixed inset-0 z-40 md:hidden" style={{ background: "rgba(0,0,0,0.55)" }} onClick={() => setMobileOpen(false)}>
          <aside className="w-[248px] h-full overflow-y-auto p-4 drawer-in" style={{ background: "var(--bg-panel)" }} onClick={(e) => e.stopPropagation()}>
            {sidebar}
          </aside>
        </div>
      )}

      {/* Main */}
      <div className="flex-1 min-w-0 flex flex-col">
        <header className="flex items-center gap-3 px-4 md:px-5 py-3 sticky top-0 z-30"
          style={{ background: "var(--bg-glass)", backdropFilter: "blur(16px)", borderBottom: "var(--seam)" }}>
          <button className="md:hidden btn-ghost !px-2 !py-1" onClick={() => setMobileOpen(true)} aria-label="Open menu">☰</button>
          <div className="relative">
            <button className="flex items-center gap-2 rounded-xl px-2.5 py-1.5 hover:opacity-90" style={{ background: "var(--bg-subtle)" }}
              onClick={() => setWsMenu(!wsMenu)} title="Switch workspace">
              <Avatar name={wsName} size={22} />
              <span className="font-semibold text-[13.5px] max-w-[180px] truncate">{wsName}</span>
              <span style={{ color: "var(--text-faint)", fontSize: 11 }}>▾</span>
            </button>
            {wsMenu && (
              <div className="absolute left-0 top-full mt-2 w-[240px] card overflow-hidden z-50" style={{ padding: 6 }}>
                {workspaces.map((w: any) => (
                  <button key={w.id} className="w-full flex items-center gap-2.5 px-2.5 py-2 rounded-lg text-left text-[13px] hover:opacity-100"
                    style={w.id === getWorkspace() ? { background: "var(--accent-dim)", color: "var(--accent-bright)" } : { color: "var(--text)" }}
                    onClick={() => { setWorkspace(w.id); window.location.reload(); }}>
                    <Avatar name={w.name} size={24} />
                    <span className="truncate flex-1">{w.name}</span>
                    {w.id === getWorkspace() && <span>✓</span>}
                  </button>
                ))}
              </div>
            )}
          </div>
          <Badge tone={mockMode ? "warning" : "success"}>{mockMode ? "MOCK" : mode ? mode.mode.toUpperCase() : "…"}</Badge>
          {status && <Badge tone={running ? "success" : "muted"}>{status.state}</Badge>}
          <div className="ml-auto flex items-center gap-2">
            <button className="btn-outline !text-xs !py-1.5 hidden sm:block" onClick={() => { setPaletteQ(""); setPalette(true); }}>⌘K Commands</button>
            <button className="btn-ghost !text-xs !py-1.5 relative" onClick={() => setFeedOpen(!feedOpen)}>
              Activity
              <span className="live-dot inline-block w-2 h-2 rounded-full ml-1.5" style={{ background: "var(--accent)" }} />
            </button>
          </div>
        </header>
        <main className="flex-1 p-4 md:p-6 max-w-[1240px] w-full mx-auto"><Outlet /></main>
      </div>

      {/* Activity drawer */}
      {feedOpen && (
        <aside className="fixed right-0 top-0 bottom-0 w-[340px] max-w-[90vw] z-40 p-4 overflow-y-auto drawer-in"
          style={{ background: "var(--bg-glass)", backdropFilter: "blur(16px)", borderLeft: "var(--seam)" }}>
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

      {/* Searchable command palette */}
      {palette && (
        <div className="fixed inset-0 z-50 p-4 pt-[10vh] flex justify-center" style={{ background: "rgba(0,0,0,0.5)" }} onClick={() => setPalette(false)}>
          <div className="cmdk w-full max-w-[540px] h-fit" onClick={(e) => e.stopPropagation()}>
            <input autoFocus placeholder="Go to… (filters as you type)" value={paletteQ} onChange={(e) => setPaletteQ(e.target.value)} />
            <div className="max-h-[50vh] overflow-y-auto p-1.5">
              {palItems.map((it) => (
                <button key={it.to} className="w-full text-left px-3 py-2 rounded-lg text-[13.5px] flex gap-2.5 items-center hover:opacity-100"
                  style={{ color: "var(--text)" }} onClick={() => { nav(it.to); setPalette(false); }}
                  onMouseOver={(e) => (e.currentTarget.style.background = "var(--bg-subtle)")}
                  onMouseOut={(e) => (e.currentTarget.style.background = "transparent")}>
                  <span style={{ color: "var(--text-faint)" }}>{it.icon}</span>
                  <span>{it.label}</span>
                  <span className="truncate text-[11.5px]" style={{ color: "var(--text-faint)" }}>{it.desc}</span>
                  {it.keys && <kbd className="ml-auto">g {it.keys}</kbd>}
                </button>
              ))}
              {!palItems.length && <div className="px-3 py-4 text-[13px] text-center" style={{ color: "var(--text-faint)" }}>No matches.</div>}
            </div>
          </div>
        </div>
      )}
      <Toasts />
    </div>
  );
}
