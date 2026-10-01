// Thin client for the FastAPI service. nginx (and the Vite dev proxy) map /api -> api:8000.

let token: string | null = sessionStorage.getItem("token");

export function setToken(t: string | null) {
  token = t;
  if (t) sessionStorage.setItem("token", t);
  else sessionStorage.removeItem("token");
}

export function getToken() {
  return token;
}

export async function api<T = any>(path: string, init: RequestInit = {}): Promise<T> {
  const res = await fetch(`/api${path}`, {
    ...init,
    headers: {
      "Content-Type": "application/json",
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
      ...(init.headers || {}),
    },
  });
  if (res.status === 401) {
    setToken(null);
    location.reload();
  }
  if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || res.statusText);
  return res.json();
}

export function wsUrl() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  return `${proto}://${location.host}/ws/live?token=${encodeURIComponent(token || "")}`;
}

export const SIGNAL_LABEL: Record<string, string> = {
  TOW_SUSPECTED: "Tow suspected",
  GEOFENCE_EXIT: "Left geofence",
  TAMPER_SUSPECTED: "Tamper suspected",
  CONVOY: "Convoy",
  ROUTE_DEVIATION: "Route deviation",
  NIGHT_MOVEMENT: "Night movement",
  SPEED_SPIKE: "Speed spike",
};

export function riskColor(score: number) {
  if (score >= 70) return "#c0262d";
  if (score >= 40) return "#e0801f";
  if (score > 0) return "#d8b21c";
  return "#2f7d4f";
}

export function fmtTime(epoch: number) {
  return new Date(epoch * 1000).toLocaleString([], { dateStyle: "short", timeStyle: "medium" });
}

export function fmtEta(s: number) {
  const m = Math.round(s / 60);
  return m < 60 ? `${m} min` : `${Math.floor(m / 60)} h ${m % 60} min`;
}
