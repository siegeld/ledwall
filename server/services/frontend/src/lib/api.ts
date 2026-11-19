export const BASE = ((window as any).__APP_BASE__ ?? "").replace(/\/$/, "");

export async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${BASE}/api/v1${path}`, {
    credentials: "include",
    headers: { "Content-Type": "application/json", ...(init?.headers || {}) },
    ...init,
  });
  if (res.status === 401) { location.href = `${BASE}/login`; throw new Error("unauthenticated"); }
  if (!res.ok) {
    let detail = res.statusText;
    try { detail = (await res.json()).detail ?? detail; } catch {}
    throw new Error(detail);
  }
  return res.status === 204 ? (undefined as T) : res.json();
}

export async function authPost<T>(path: string, body?: unknown): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    method: "POST", credentials: "include",
    headers: { "Content-Type": "application/json" },
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!res.ok) { let d = res.statusText; try { d = (await res.json()).detail ?? d; } catch {} throw new Error(d); }
  return res.json();
}

/** The universal no-value glyph — never print null/NaN. */
export const fmt = (v: unknown, digits = 1) =>
  v === null || v === undefined || (typeof v === "number" && Number.isNaN(v))
    ? "—" : typeof v === "number" ? (v as number).toFixed(digits) : String(v);
