/* Session + workspace context.
 *
 * The old app read `localStorage` directly from three different places
 * (`lib/api.ts`, `Brand.tsx`, and App.tsx) and each kept its own copy of
 * "who am I". That is how a workspace switch leaves stale rows on screen.
 * One context, read from one place, is the fix -- and the API client already
 * reads the same three keys, so the values cannot drift.
 */

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";
import { api, getWorkspace, setWorkspace as persistWorkspace } from "../lib/api";

export type WorkspaceSummary = {
  id: string;
  name: string;
  slug?: string;
  niche?: string;
  /** Raw server-side role. The UI branches on `capabilities`, never on this. */
  role?: string;
  /** Backend-derived capability list for THIS workspace (Work 16.5.2 §2). */
  capabilities?: string[];
};

export type Session = {
  authenticated: boolean;
  user: { id?: string; email?: string; name?: string; role?: string } | null;
  workspaces: WorkspaceSummary[];
  workspaceId: string | null;
  workspace: WorkspaceSummary | null;
  /** Capabilities for the ACTIVE workspace. Empty means "none", not "unknown". */
  capabilities: string[];
  /**
   * Whether the server actually sent a capability list.
   *
   * This distinction is the whole safety story. An empty array means the server
   * said "you have none" and controls must be hidden. `false` means the field was
   * absent -- an older backend -- and hiding everything would make the product
   * unusable, so the UI fails OPEN and lets the server answer with a 403.
   */
  capabilitiesKnown: boolean;
  switchWorkspace: (id: string) => void;
  reload: () => void;
};

const SessionContext = createContext<Session | null>(null);

const TOKEN_KEY = "ym_token";

export function SessionProvider({ children }: { children: ReactNode }) {
  const [authenticated, setAuthenticated] = useState<boolean | null>(null);
  const [user, setUser] = useState<Session["user"]>(null);
  const [workspaces, setWorkspaces] = useState<WorkspaceSummary[]>([]);
  const [workspaceId, setWorkspaceId] = useState<string | null>(getWorkspace());

  const load = useCallback(async () => {
    if (!localStorage.getItem(TOKEN_KEY)) {
      setAuthenticated(false);
      return;
    }

    let meWorkspaces: WorkspaceSummary[] | null = null;
    try {
      const me = (await api<{
        id?: string;
        email?: string;
        display_name?: string | null;
        workspaces?: WorkspaceSummary[];
      }>("GET", "/auth/me")) as {
        id?: string;
        email?: string;
        display_name?: string | null;
        workspaces?: WorkspaceSummary[];
      };
      setUser({
        id: me.id,
        email: me.email,
        name: me.display_name ?? undefined,
      });
      setAuthenticated(true);
      meWorkspaces = me.workspaces ?? null;
    } catch {
      setAuthenticated(false);
      setUser(null);
      return;
    }

    try {
      // Prefer `/auth/me`: it is the only source that carries each workspace's
      // role and backend-derived capabilities. `/workspaces` is a bare listing,
      // so preferring it would silently strip the permission contract.
      let rows = meWorkspaces;
      if (!rows) {
        const list = await api<{ workspaces?: WorkspaceSummary[] } | WorkspaceSummary[]>(
          "GET",
          "/workspaces",
        );
        rows = Array.isArray(list) ? list : (list.workspaces ?? []);
      }
      setWorkspaces(rows);
      setWorkspaceId((current) => {
        if (current && rows.some((w) => w.id === current)) return current;
        const first = rows[0]?.id ?? null;
        persistWorkspace(first);
        return first;
      });
    } catch {
      // A missing workspace list is a real failure, not an empty workspace.
      setWorkspaces([]);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const switchWorkspace = useCallback((id: string) => {
    persistWorkspace(id);
    setWorkspaceId(id);
    // A workspace change invalidates every cached read; a reload is honest and
    // cheaper than getting invalidation subtly wrong.
    window.location.reload();
  }, []);

  const active = workspaces.find((w) => w.id === workspaceId) ?? null;
  // An empty array is a real answer ("this role has nothing"); `undefined`
  // means the field was never sent. Only the latter may fall open.
  const caps = active?.capabilities;
  const capabilitiesKnown = Array.isArray(caps);

  const value = useMemo<Session>(
    () => ({
      authenticated: authenticated === true,
      user,
      workspaces,
      workspaceId,
      workspace: active,
      capabilities: capabilitiesKnown ? caps : [],
      capabilitiesKnown,
      switchWorkspace,
      reload: load,
    }),
    [authenticated, user, workspaces, workspaceId, active, capabilitiesKnown, caps, switchWorkspace, load],
  );

  return <SessionContext.Provider value={value}>{children}</SessionContext.Provider>;
}

export function useSession(): Session {
  const ctx = useContext(SessionContext);
  if (!ctx) throw new Error("useSession must be used inside <SessionProvider>");
  return ctx;
}

/**
 * Affordance only -- NEVER authorization.
 *
 * The backend remains authoritative and answers a 403 regardless of what this
 * returns. Two rules, and the difference between them matters:
 *
 *   - server REPORTED capabilities -> enforce them. A `viewer` really does get
 *     `[]`, and hiding a control they cannot use is a courtesy, not security.
 *   - server did NOT report the field (older backend) -> allow. Hiding
 *     everything would render the product unusable, and the request would have
 *     been rejected anyway.
 */
export function can(permission: string | null): boolean {
  if (!permission) return true;
  const { capabilities, capabilitiesKnown } = useSession();
  if (!capabilitiesKnown) return true;
  return capabilities.includes(permission);
}

/**
 * Why an action is unavailable, for rendering next to a disabled control.
 * Returns `null` when the action IS available, so a caller can use it directly.
 */
export function blockedReason(permission: string | null): string | null {
  if (!permission) return null;
  const { capabilities, capabilitiesKnown } = useSession();
  if (!capabilitiesKnown || capabilities.includes(permission)) return null;
  return `Your workspace role does not include "${permission}". The server enforces this.`;
}