export async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, { ...init, headers: { ...(init?.body instanceof FormData ? {} : {"Content-Type":"application/json"}), ...(init?.headers || {}) }, cache: "no-store" });
  if (!response.ok) {
    let message = `${response.status} ${response.statusText}`;
    try { const body = await response.json(); message = body.detail || body.message || message; } catch { /* ignore */ }
    throw new Error(message);
  }
  return response.json() as Promise<T>;
}
export function timecode(seconds: number): string {
  const m = Math.floor(seconds / 60), s = seconds % 60;
  return `${String(m).padStart(2,"0")}:${s.toFixed(2).padStart(5,"0")}`;
}
