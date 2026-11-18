// Thin API client. The access token lives in memory only; the refresh token is an httpOnly cookie
// that JavaScript cannot read. On a 401 the client refreshes once and replays the request.

export const API_BASE = "/api/v1";

let accessToken: string | null = null;
let refreshInFlight: Promise<boolean> | null = null;
let onSessionExpired: (() => void) | null = null;

export function setAccessToken(token: string | null) {
  accessToken = token;
}

export function setSessionExpiredHandler(handler: (() => void) | null) {
  onSessionExpired = handler;
}

export class ApiError extends Error {
  status: number;
  detail: unknown;
  constructor(status: number, message: string, detail?: unknown) {
    super(message);
    this.status = status;
    this.detail = detail;
  }
}

function messageFrom(body: unknown, fallback: string): string {
  if (body && typeof body === "object" && "detail" in body) {
    const detail = (body as { detail: unknown }).detail;
    if (typeof detail === "string") return detail;
    if (Array.isArray(detail)) {
      return detail
        .map((d) => (d && typeof d === "object" && "msg" in d ? String((d as { msg: unknown }).msg) : String(d)))
        .join("; ");
    }
    if (detail && typeof detail === "object" && "error" in detail) return String((detail as { error: unknown }).error);
    if (detail && typeof detail === "object" && "message" in detail) return String((detail as { message: unknown }).message);
  }
  return fallback;
}

async function parseError(res: Response): Promise<ApiError> {
  let body: unknown = null;
  try {
    body = await res.json();
  } catch {
    /* not JSON */
  }
  const detail = body && typeof body === "object" && "detail" in body ? (body as { detail: unknown }).detail : undefined;
  return new ApiError(res.status, messageFrom(body, `Request failed (${res.status})`), detail);
}

export async function refreshSession(): Promise<boolean> {
  if (!refreshInFlight) {
    refreshInFlight = (async () => {
      try {
        const res = await fetch(`${API_BASE}/auth/refresh`, { method: "POST", credentials: "same-origin" });
        if (!res.ok) return false;
        const data = await res.json();
        accessToken = data.access_token;
        return true;
      } catch {
        return false;
      } finally {
        refreshInFlight = null;
      }
    })();
  }
  return refreshInFlight;
}

type Options = { method?: string; body?: unknown; form?: FormData; auth?: boolean };

async function send(path: string, opts: Options): Promise<Response> {
  const headers: Record<string, string> = {};
  if (accessToken && opts.auth !== false) headers.Authorization = `Bearer ${accessToken}`;
  let body: BodyInit | undefined;
  if (opts.form) body = opts.form;
  else if (opts.body !== undefined) {
    headers["Content-Type"] = "application/json";
    body = JSON.stringify(opts.body);
  }
  return fetch(`${API_BASE}${path}`, { method: opts.method ?? "GET", headers, body, credentials: "same-origin" });
}

async function request(path: string, opts: Options = {}): Promise<Response> {
  let res = await send(path, opts);
  if (res.status === 401 && opts.auth !== false && !path.startsWith("/auth/login")) {
    if (await refreshSession()) {
      res = await send(path, opts);
    } else {
      accessToken = null;
      onSessionExpired?.();
    }
  }
  if (!res.ok) throw await parseError(res);
  return res;
}

export async function api<T>(path: string, opts: Options = {}): Promise<T> {
  const res = await request(path, opts);
  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}

export async function apiBlob(path: string): Promise<Blob> {
  const res = await request(path);
  return res.blob();
}

export async function downloadFile(path: string, fallbackName: string) {
  const res = await request(path);
  const disposition = res.headers.get("Content-Disposition") ?? "";
  const utf = /filename\*=UTF-8''([^;]+)/i.exec(disposition);
  const plain = /filename="?([^";]+)"?/i.exec(disposition);
  const name = utf ? decodeURIComponent(utf[1]) : plain ? plain[1] : fallbackName;
  const blob = await res.blob();
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = name;
  document.body.appendChild(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 2000);
}
