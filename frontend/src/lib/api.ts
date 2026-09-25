/* YMONEY API client — every backend capability in one place.
   Auth: Bearer access token + single-use refresh rotation.
   SSE: EventSource cannot send headers, so the token rides ?token=. */

const BASE = "/api/v1";

let accessToken: string | null = localStorage.getItem("ym_token");
let refreshToken: string | null = localStorage.getItem("ym_rt");
let workspaceId: string | null = localStorage.getItem("ym_ws");

export function setAuth(t: string | null) {
  accessToken = t;
  if (t) localStorage.setItem("ym_token", t);
  else localStorage.removeItem("ym_token");
}
export function setRefreshToken(t: string | null) {
  refreshToken = t;
  if (t) localStorage.setItem("ym_rt", t);
  else localStorage.removeItem("ym_rt");
}
export function setWorkspace(id: string | null) {
  workspaceId = id;
  if (id) localStorage.setItem("ym_ws", id);
  else localStorage.removeItem("ym_ws");
}
export function getWorkspace() {
  return workspaceId;
}
export function getToken() {
  return accessToken;
}

async function tryRefreshSession(): Promise<boolean> {
  if (!refreshToken) return false;
  try {
    const res = await fetch(`${BASE}/auth/refresh`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ refresh_token: refreshToken }),
    });
    if (!res.ok) {
      setRefreshToken(null);
      setAuth(null);
      return false;
    }
    const data = await res.json();
    if (typeof data.access_token !== "string" || !data.access_token) return false;
    setAuth(data.access_token);
    if (data.refresh_token) setRefreshToken(data.refresh_token);
    return true;
  } catch {
    return false;
  }
}

const NO_AUTO_REFRESH = ["/auth/login", "/auth/register", "/auth/refresh"];

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

async function request<T = any>(method: string, path: string, body?: unknown, allowRefresh = true): Promise<T> {
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
    if (res.status === 401 && allowRefresh && !NO_AUTO_REFRESH.includes(path) && (await tryRefreshSession())) {
      return request<T>(method, path, body, false);
    }
    throw err;
  }
  if (res.status === 204) return undefined as T;
  const text = await res.text();
  return (text ? JSON.parse(text) : undefined) as T;
}

export async function api<T = any>(method: string, path: string, body?: unknown): Promise<T> {
  return request<T>(method, path, body);
}

async function uploadFile<T = any>(path: string, file: File, extra?: Record<string, string>): Promise<T> {
  const form = new FormData();
  form.append("file", file);
  if (extra) for (const [k, v] of Object.entries(extra)) form.append(k, v);
  const headers: Record<string, string> = {};
  if (accessToken) headers["Authorization"] = `Bearer ${accessToken}`;
  const res = await fetch(`${BASE}${path}`, { method: "POST", headers, body: form });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      detail = (await res.json()).detail ?? detail;
    } catch {}
    throw new ApiError(res.status, String(detail));
  }
  return (await res.json()) as T;
}

export const wsApi = {
  get: (p: string) => api("GET", `/workspaces/${workspaceId}${p}`),
  post: (p: string, body?: unknown) => api("POST", `/workspaces/${workspaceId}${p}`, body ?? {}),
  put: (p: string, body?: unknown) => api("PUT", `/workspaces/${workspaceId}${p}`, body ?? {}),
  patch: (p: string, body?: unknown) => api("PATCH", `/workspaces/${workspaceId}${p}`, body ?? {}),
  del: (p: string) => api("DELETE", `/workspaces/${workspaceId}${p}`),
  upload: (p: string, file: File) => uploadFile(`/workspaces/${workspaceId}${p}`, file),
};

export function activityStreamUrl(): string {
  const t = accessToken ? `?token=${encodeURIComponent(accessToken)}` : "";
  return `${BASE}/workspaces/${workspaceId}/activity/stream${t}`;
}

export function videoFileUrl(videoId: string): string {
  const t = accessToken ? `?token=${encodeURIComponent(accessToken)}` : "";
  return `${BASE}/workspaces/${workspaceId}/videos/${videoId}/file${t}`;
}
export function videoThumbUrl(videoId: string): string {
  const t = accessToken ? `?token=${encodeURIComponent(accessToken)}` : "";
  return `${BASE}/workspaces/${workspaceId}/videos/${videoId}/thumbnail${t}`;
}
export function coverFileUrl(videoId: string, index: number): string {
  const t = accessToken ? `?token=${encodeURIComponent(accessToken)}` : "";
  return `${BASE}/workspaces/${workspaceId}/videos/${videoId}/covers/${index}/file${t}`;
}
export function aiCoverFileUrl(videoId: string, index: number): string {
  const t = accessToken ? `?token=${encodeURIComponent(accessToken)}` : "";
  return `${BASE}/workspaces/${workspaceId}/videos/${videoId}/ai-covers/${index}/file${t}`;
}
export function mediaFileUrl(assetId: string): string {
  const t = accessToken ? `?token=${encodeURIComponent(accessToken)}` : "";
  return `${BASE}/workspaces/${workspaceId}/assets/media/${assetId}/file${t}`;
}

export async function downloadAudit(contentId: string): Promise<void> {
  const headers: Record<string, string> = {};
  if (accessToken) headers["Authorization"] = `Bearer ${accessToken}`;
  const res = await fetch(`${BASE}/workspaces/${workspaceId}/content/${contentId}/audit`, { headers });
  if (!res.ok) throw new ApiError(res.status, res.statusText);
  const blob = await res.blob();
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = `audit-${contentId.slice(0, 8)}.json`;
  a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 5000);
}
