/* The new application shell.
 *
 * Sidebar + top bar + workspace selector + breadcrumbs + notifications +
 * command palette + user menu, in ONE navigation system. §1 is explicit that
 * old and new navigation must not be mixed, so this shell owns the chrome
 * completely and old screens are reached as marked legacy bridges inside it,
 * never beside it.
 *
 * Design direction is the tokens file, not this component: nothing here
 * hardcodes a colour.
 */

import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { NavLink, useLocation, useNavigate } from "react-router-dom";
import {
  Badge,
  Button,
  cx,
  Modal,
  toneForStatus,
} from "../design-system/primitives";
import { Toasts } from "../components/ui";
import {
  GROUP_LABEL,
  NAV_ROUTES,
  ROUTE_GLYPH,
  ROUTE_GROUPS,
  breadcrumbsFor,
  routeInventory,
  searchRoutes,
  type RouteGroup,
} from "../routes/registry";
import { useWsQuery } from "../api/queries";
import { useSession } from "../state/session";

/* ==========================================================================
 * Command palette -- Ctrl/Cmd-K
 * ========================================================================== */

function CommandPalette({ open, onClose }: { open: boolean; onClose: () => void }) {
  const [query, setQuery] = useState("");
  const [cursor, setCursor] = useState(0);
  const navigate = useNavigate();
  const inputRef = useRef<HTMLInputElement>(null);

  const results = useMemo(() => searchRoutes(query), [query]);

  useEffect(() => {
    if (open) {
      setQuery("");
      setCursor(0);
      // Focus after paint so the dialog exists.
      window.setTimeout(() => inputRef.current?.focus(), 0);
    }
  }, [open]);

  const go = useCallback(
    (path: string) => {
      onClose();
      navigate(path);
    },
    [navigate, onClose],
  );

  const onKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === "Escape") {
      onClose();
      return;
    }
    if (e.key === "ArrowDown") {
      e.preventDefault();
      setCursor((c) => Math.min(c + 1, results.length - 1));
    }
    if (e.key === "ArrowUp") {
      e.preventDefault();
      setCursor((c) => Math.max(c - 1, 0));
    }
    if (e.key === "Enter" && results[cursor]) {
      e.preventDefault();
      go(results[cursor].path);
    }
  };

  if (!open) return null;

  return (
    <div className="ym-palette-backdrop" onClick={onClose} role="presentation">
      <div
        className="ym-palette"
        role="dialog"
        aria-modal="true"
        aria-label="Command palette"
        onClick={(e) => e.stopPropagation()}
        onKeyDown={onKeyDown}
      >
        <input
          ref={inputRef}
          className="ym-palette-input"
          placeholder="Search screens, workflows, settings"
          value={query}
          onChange={(e) => {
            setQuery(e.target.value);
            setCursor(0);
          }}
          aria-label="Search"
          aria-controls="ym-palette-results"
        />
        <div className="ym-palette-results" id="ym-palette-results" role="listbox">
          {results.length === 0 && <div className="ym-palette-empty">No screens match</div>}
          {results.map((r, i) => (
            <button
              key={r.path}
              role="option"
              aria-selected={i === cursor}
              className={cx("ym-palette-item", i === cursor && "ym-palette-item--active")}
              onMouseEnter={() => setCursor(i)}
              onClick={() => go(r.path)}
            >
              <span className="ym-palette-glyph" aria-hidden="true">{ROUTE_GLYPH[r.path] ?? "◇"}</span>
              <span className="ym-palette-label">
                {r.label}
                {r.description && <span className="ym-palette-desc">{r.description}</span>}
              </span>
              {r.state === "LEGACY_BRIDGE" && (
                <Badge tone="warning" title="Not rebuilt yet">legacy</Badge>
              )}
            </button>
          ))}
        </div>
        <div className="ym-palette-foot">
          <kbd>↑↓</kbd> navigate · <kbd>↵</kbd> open · <kbd>esc</kbd> close
        </div>
      </div>
    </div>
  );
}

/* ==========================================================================
 * Notifications
 *
 * Backed by real signals: failing jobs, unknown paid exposure and provider
 * incidents. Nothing here invents an alert.
 * ========================================================================== */

type Notice = { id: string; tone: "danger" | "unknown" | "warning" | "info"; title: string; detail?: string; to?: string };

function useNotices(): { notices: Notice[]; loading: boolean } {
  const { workspaceId } = useSession();
  const jobs = useWsQuery<{ jobs?: { id: string; type: string; status: string; last_error?: string }[] }>(
    "/jobs",
    { enabled: Boolean(workspaceId), ignoreStatuses: [404] },
  );
  const chains = useWsQuery<{ chains?: Record<string, { state?: string; outcome?: string; operation?: string }[]> }>(
    "/intelligence/routing/chains",
    { enabled: Boolean(workspaceId), ignoreStatuses: [404] },
  );

  const notices = useMemo<Notice[]>(() => {
    const out: Notice[] = [];

    // Paid ambiguity is the one thing that must never be missed, and it is
    // categorically different from a failed job.
    const unknownCount = Object.values(chains.data?.chains ?? {}).reduce(
      (n, entries) =>
        n + (entries ?? []).filter((e) =>
          String(e.state ?? "").toUpperCase().includes("UNKNOWN") ||
          String(e.outcome ?? "").toUpperCase().includes("UNKNOWN"),
        ).length,
      0,
    );
    if (unknownCount > 0) {
      out.push({
        id: "unknown-submission",
        tone: "unknown",
        title: `${unknownCount} ambiguous paid submission${unknownCount === 1 ? "" : "s"}`,
        detail: "Money may have moved with no confirmed outcome. Reconcile by remote id.",
        to: "/operations",
      });
    }

    const failed = (jobs.data?.jobs ?? []).filter((j) =>
      ["FAILED", "DEAD"].includes(String(j.status).toUpperCase()),
    );
    if (failed.length > 0) {
      out.push({
        id: "failed-jobs",
        tone: "danger",
        title: `${failed.length} failed job${failed.length === 1 ? "" : "s"}`,
        detail: failed[0]?.last_error || undefined,
        to: "/operations",
      });
    }
    return out;
  }, [chains.data, jobs.data]);

  return { notices, loading: jobs.loading || chains.loading };
}

/* ==========================================================================
 * Shell
 * ========================================================================== */

export function AppShell({ children }: { children: ReactNode }) {
  const location = useLocation();
  const { workspace, workspaces, workspaceId, switchWorkspace, user } = useSession();
  const [paletteOpen, setPaletteOpen] = useState(false);
  const [collapsed, setCollapsed] = useState(false);
  const [navOpen, setNavOpen] = useState(false);
  
  /* A mobile navigation choice must not survive a route change: the drawer
   * covers the screen, so leaving it open would hide the page the user just
   * navigated to. Closing on navigation is the behaviour a user expects from
   * every mobile drawer, and it is why this lives next to the other shell
   * state rather than inside the nav component.
   */
  useEffect(() => {
    setNavOpen(false);
  }, [location.pathname]);
  const [notifOpen, setNotifOpen] = useState(false);
  const { notices, loading } = useNotices();
  const inventory = useMemo(() => routeInventory(), []);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        setPaletteOpen((v) => !v);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  const crumbs = useMemo(() => breadcrumbsFor(location.pathname), [location.pathname]);
  const groups = useMemo(() => {
    const byGroup = new Map<RouteGroup, typeof NAV_ROUTES>();
    for (const r of NAV_ROUTES) {
      if (!byGroup.has(r.group)) byGroup.set(r.group, []);
      byGroup.get(r.group)!.push(r);
    }
    return ROUTE_GROUPS.map((g) => ({ ...g, routes: byGroup.get(g.id) ?? [] })).filter(
      (g) => g.routes.length > 0,
    );
  }, []);

  const critical = notices.filter((n) => n.tone === "unknown" || n.tone === "danger");

  return (
    <div className={cx("ym-shell", collapsed && "ym-shell--collapsed", navOpen && "ym-shell--nav-open")}>
      <a className="ym-skip-link" href="#ym-main">Skip to content</a>


      {/* Mobile navigation toggle.
       *
       * ADDED IN WORK 16.5.4, after the browser suite found a real defect: the
       * stylesheet did `.ym-sidebar { display: none }` at <=768px with NO
       * replacement, so on a phone or a portrait tablet the application had no
       * navigation at all -- the only way to reach the other twenty-one routes
       * was to know the Ctrl+K command-palette shortcut. That is a lost-feature
       * failure, not a cosmetic one, and no source-reading test can see it
       * because every route still resolves.
       *
       * Rendered unconditionally and hidden by CSS above the breakpoint, rather
       * than switched by a media query in JS: a JS media query would make the
       * control appear only after hydration, and could not be asserted without
       * emulating matchMedia.
       */}
      <Button
        className="ym-nav-toggle"
        variant="secondary"
        size="sm"
        aria-expanded={navOpen}
        aria-controls="ym-sidebar"
        aria-label={navOpen ? "Close navigation" : "Open navigation"}
        onClick={() => setNavOpen((v) => !v)}
      >
        <span aria-hidden="true">{navOpen ? "✕" : "☰"}</span>
      </Button>

      <aside
        className="ym-sidebar"
        id="ym-sidebar"
        {...(navOpen ? { "data-mobile-open": "true" } : {})}
      >
        <div className="ym-brandmark">
          <span className="ym-brandmark-glyph" aria-hidden="true">◈</span>
          {!collapsed && <span className="ym-brandmark-text">YMONEY</span>}
        </div>

        <nav className="ym-nav" aria-label="Primary">
          {groups.map((group) => (
            <div key={group.id} className="ym-nav-group">
              {!collapsed && <div className="ym-nav-group-label">{GROUP_LABEL[group.id]}</div>}
              {group.routes.map((r) => {
                const active = location.pathname === r.path ||
                  (r.path !== "/" && location.pathname.startsWith(r.path));
                return (
                  <NavLink
                    key={r.path}
                    to={r.path}
                    className={cx("ym-nav-item", active && "ym-nav-item--active")}
                    title={collapsed ? r.label : undefined}
                    aria-current={active ? "page" : undefined}
                  >
                    <span className="ym-nav-glyph" aria-hidden="true">{ROUTE_GLYPH[r.path] ?? "◇"}</span>
                    {!collapsed && <span className="ym-nav-label">{r.label}</span>}
                    {!collapsed && r.state === "LEGACY_BRIDGE" && (
                      <span className="ym-nav-legacy" title="Not rebuilt yet — legacy screen" />
                    )}
                  </NavLink>
                );
              })}
            </div>
          ))}
        </nav>

        <div className="ym-sidebar-foot">
          {!collapsed && (
            <div className="ym-inventory" title="Route registry state">
              {inventory.rebuilt} rebuilt · {inventory.legacy} legacy
            </div>
          )}
          <Button
            variant="ghost"
            size="sm"
            onClick={() => setCollapsed((v) => !v)}
            aria-label={collapsed ? "Expand sidebar" : "Collapse sidebar"}
          >
            {collapsed ? "»" : "«"}
          </Button>
        </div>
      </aside>

      <div className="ym-shell-main">
        <header className="ym-topbar">
          <nav className="ym-breadcrumbs" aria-label="Breadcrumb">
            {crumbs.map((c, i) => (
              <span key={`${c.label}-${i}`} className="ym-crumb">
                {i > 0 && <span className="ym-crumb-sep" aria-hidden="true">/</span>}
                {c.path && i < crumbs.length - 1 ? (
                  <NavLink to={c.path}>{c.label}</NavLink>
                ) : (
                  <span aria-current={i === crumbs.length - 1 ? "page" : undefined}>{c.label}</span>
                )}
              </span>
            ))}
          </nav>

          <div className="ym-topbar-actions">
            <button
              className="ym-search-trigger"
              onClick={() => setPaletteOpen(true)}
              aria-label="Open command palette"
            >
              <span aria-hidden="true">⌕</span> Search
              <kbd>⌘K</kbd>
            </button>

            <div className="ym-notif-wrap">
              <button
                className={cx("ym-icon-btn", critical.length > 0 && "ym-icon-btn--critical")}
                onClick={() => setNotifOpen((v) => !v)}
                aria-label={`Notifications${critical.length ? `, ${critical.length} critical` : ""}`}
                aria-expanded={notifOpen}
              >
                <span aria-hidden="true">◔</span>
                {critical.length > 0 && (
                  <span className="ym-notif-count">{critical.length}</span>
                )}
              </button>
              {notifOpen && (
                <div className="ym-notif-panel" role="dialog" aria-label="Notifications">
                  <div className="ym-notif-head">
                    Notifications
                    <Button variant="ghost" size="sm" onClick={() => setNotifOpen(false)}>Close</Button>
                  </div>
                  {loading && <div className="ym-notif-empty">Checking…</div>}
                  {!loading && notices.length === 0 && (
                    <div className="ym-notif-empty">
                      Nothing needs attention. Paid ambiguity and failed jobs appear here.
                    </div>
                  )}
                  {notices.map((n) => (
                    <div key={n.id} className={cx("ym-notif-item", `ym-notif-${n.tone}`)}>
                      <Badge tone={toneForStatus(n.title)}>{n.tone}</Badge>
                      <div>
                        <div className="ym-notif-title">{n.title}</div>
                        {n.detail && <div className="ym-notif-detail">{n.detail}</div>}
                      </div>
                    </div>
                  ))}
                </div>
              )}
            </div>

            <label className="ym-ws-select">
              <span className="ym-sr-only">Workspace</span>
              <select
                value={workspaceId ?? ""}
                onChange={(e) => switchWorkspace(e.target.value)}
                aria-label="Active workspace"
              >
                {workspaces.length === 0 && <option value="">No workspace</option>}
                {workspaces.map((w) => (
                  <option key={w.id} value={w.id}>{w.name}</option>
                ))}
              </select>
            </label>

            <div className="ym-user" title={user?.email ?? undefined}>
              <span className="ym-user-avatar" aria-hidden="true">
                {(user?.email ?? user?.name ?? "?").slice(0, 1).toUpperCase()}
              </span>
              {!collapsed && <span className="ym-user-name">{user?.name ?? user?.email ?? "Account"}</span>}
            </div>
          </div>
        </header>

        <div className="ym-context-bar">
          <span className="ym-context-workspace">{workspace?.name ?? "No workspace selected"}</span>
          {workspace?.niche && <span className="ym-context-niche">{workspace.niche}</span>}
        </div>

        <main className="ym-main" id="ym-main" aria-label="Main content">
          {children}
        </main>
      </div>

      <CommandPalette open={paletteOpen} onClose={() => setPaletteOpen(false)} />

      {/* THE SINGLE TOAST MOUNT.
       *
       * `toast(...)` dispatches a `ym-toast` window event and `<Toasts/>` is the
       * only subscriber -- but it had NO call site anywhere in the app, so every
       * toast in the product was dispatched into the void. Messages such as
       * "Playhead is not inside a splittable clip", "Autosave failed - reloading
       * server state" and "Someone else saved this timeline - reload latest" were
       * invisible to every user.
       *
       * Mounted HERE, once, outside `<main>` and beside the command palette, for
       * two reasons:
       *  * the shell is the only component that survives every route, so one mount
       *    serves the whole app and a toast raised by one screen is still visible
       *    after navigating -- which is exactly when a user needs it;
       *  * it is NOT inside `<main>`, so `aria-live` announcements are not
       *    suppressed by a route change re-rendering the landmark's subtree.
       *
       * It is a sibling of the palette rather than a child of any feature on
       * purpose: a per-feature container would unmount with its route and would
       * duplicate the live region once per screen. */}
      <Toasts />
    </div>
  );
}

/**
 * The marker §14 requires: a legacy-bridged screen must be visibly labelled so
 * nobody mistakes an unrebuilt screen for a rebuilt one.
 */
export function LegacyBridge({ title, children }: { title: string; children: ReactNode }) {
  return (
    <div className="ym-legacy">
      <div className="ym-legacy-banner" role="note">
        <Badge tone="warning">Legacy bridge</Badge>
        <span>
          <strong>{title}</strong> has not been rebuilt yet. This is the previous screen,
          reached inside the new shell. Scheduled for Work 16.5.2.
        </span>
      </div>
      {children}
    </div>
  );
}

export { Modal };
