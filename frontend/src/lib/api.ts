const BASE = "/api/v1";

let accessToken: string | null = localStorage.getItem("ym_token");
let refreshToken: string | null = localStorage.getItem("ym_rt");
let workspaceId: string | null = localStorage.getItem("ym_ws");

export function setAuth(token: string | null) {
  accessToken = token;
  if (token) localStorage.setItem("ym_token", token);
  else localStorage.removeItem("ym_token");
}

export function setRefreshToken(token: string | null) {
  refreshToken = token;
  if (token) localStorage.setItem("ym_rt", token);
  else localStorage.removeItem("ym_rt");
}

// Rotate the refresh token once and retry the failed request. Refresh tokens
// are single-use, so /auth/refresh returns a new pair we store immediately.
async function tryRefreshSession(): Promise<boolean> {
  if (!refreshToken) return false;
  try {
    const res = await fetch(`${BASE}/auth/refresh`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ refresh_token: refreshToken }),
    });
    if (!res.ok) {
      // Dead refresh token: drop the session instead of looping.
      setRefreshToken(null);
      setAuth(null);
      return false;
    }
    const data = await res.json();
    const newAccess: string = data.access_token;
    if (typeof newAccess !== "string" || !newAccess) return false;
    accessToken = newAccess;
    localStorage.setItem("ym_token", newAccess);
    if (data.refresh_token) setRefreshToken(data.refresh_token);
    return true;
  } catch {
    return false;
  }
}

const NO_AUTO_REFRESH = ["/auth/login", "/auth/register", "/auth/refresh"];

export function setWorkspace(id: string | null) {
  workspaceId = id;
  if (id) localStorage.setItem("ym_ws", id);
  else localStorage.removeItem("ym_ws");
}

export function getWorkspace() {
  return workspaceId;
}

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

async function request<T = any>(
  method: string,
  path: string,
  body?: unknown,
  allowRefresh = true
): Promise<T> {
  const headers: Record<string, string> = {};
  if (body !== undefined) headers["Content-Type"] = "application/json";
  if (accessToken) headers["Authorization"] = `Bearer ${accessToken}`;
  const res = await fetch(`${BASE}${path}`, {
    method,
    headers,
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const data = await res.json();
      detail = data.detail ?? JSON.stringify(data);
      if (Array.isArray(detail)) detail = detail.map((d: any) => d.msg).join(", ");
    } catch {}
    const err = new ApiError(res.status, String(detail));
    if (
      res.status === 401 &&
      allowRefresh &&
      !NO_AUTO_REFRESH.includes(path) &&
      (await tryRefreshSession())
    ) {
      return request<T>(method, path, body, false);
    }
    throw err;
  }
  return res.json();
}

export async function api<T = any>(
  method: string,
  path: string,
  body?: unknown
): Promise<T> {
  return request<T>(method, path, body);
}

// Workspace-scoped helpers
export const wsApi = {
  get: (p: string) => api("GET", `/workspaces/${workspaceId}${p}`),
  post: (p: string, body?: unknown) => api("POST", `/workspaces/${workspaceId}${p}`, body ?? {}),
  put: (p: string, body?: unknown) => api("PUT", `/workspaces/${workspaceId}${p}`, body ?? {}),
  patch: (p: string, body?: unknown) => api("PATCH", `/workspaces/${workspaceId}${p}`, body ?? {}),
  del: (p: string) => api("DELETE", `/workspaces/${workspaceId}${p}`),
  /** POST returning raw bytes + response headers (audio/artifact endpoints). */
  postBlob: async (
    p: string,
    body?: unknown
  ): Promise<{ blob: Blob; headers: Headers }> => {
    const headers: Record<string, string> = {
      "Content-Type": "application/json",
    };
    if (accessToken) headers["Authorization"] = `Bearer ${accessToken}`;
    const res = await fetch(`/api/v1/workspaces/${workspaceId}${p}`, {
      method: "POST",
      headers,
      body: JSON.stringify(body ?? {}),
    });
    if (!res.ok) {
      let detail = res.statusText;
      try {
        const data = await res.json();
        detail = data.detail ?? JSON.stringify(data);
      } catch {}
      throw new ApiError(res.status, String(detail));
    }
    return { blob: await res.blob(), headers: res.headers };
  },
};

export function activityStreamUrl(): string {
  const t = accessToken ? `?token=${encodeURIComponent(accessToken)}` : "";
  return `${BASE}/workspaces/${workspaceId}/activity/stream${t}`;
}
