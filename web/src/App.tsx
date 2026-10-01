import { Fragment, useEffect, useRef, useState } from "react";
import { api, getToken, setToken, wsUrl } from "./api";
import {
  AlertsPage, CaseDetail, CasesPage, ConvoysPage, HealthPage, Leaderboard, LiveMap, ReportsPage, VehicleDetail,
} from "./pages";
import { Icon, type IconName } from "./icons";
import { PipelinePage } from "./pipeline3d";

export type Alert = {
  alert_id: number; vehicle_id: string; code: string; ts: number; lat: number; lon: number;
  risk_score: number; case_id: number | null;
};
export type Top = { vehicle_id: string; score: number }[];
export type LivePoint = { id: string; lat: number; lon: number; risk: number };

/** Everything the screens share: the live feed (one WebSocket) and navigation. */
export type Ctx = {
  alerts: Alert[];
  top: Top;
  positions: LivePoint[];
  fleetTotal: number;           // all live vehicles of this lender (the map may draw a sample)
  setBbox: (b: number[]) => void;
  openVehicle: (id: string) => void;
  openCase: (id: number) => void;
  theme: Theme;
};

export type Theme = "light" | "dark";

const TABS = ["Live map", "Pipeline", "Alerts", "Leaderboard", "Convoys", "Cases", "Reports", "System health"] as const;
type Tab = (typeof TABS)[number];
const TAB_ICON: Record<Tab, IconName> = {
  "Live map": "map", Pipeline: "layers", Alerts: "bell", Leaderboard: "trophy", Convoys: "convoy", Cases: "folder",
  Reports: "chart", "System health": "pulse",
};
const TAB_SHORT: Record<Tab, string> = {
  "Live map": "Map", Pipeline: "Pipeline", Alerts: "Alerts", Leaderboard: "Top risk", Convoys: "Convoys", Cases: "Cases",
  Reports: "Reports", "System health": "Health",
};

function useTheme(): [Theme, () => void] {
  const [theme, setTheme] = useState<Theme>(document.documentElement.dataset.theme === "dark" ? "dark" : "light");
  const toggle = () => {
    const next = theme === "dark" ? "light" : "dark";
    document.documentElement.dataset.theme = next;
    try { localStorage.setItem("theme", next); } catch { /* private mode: theme just isn't remembered */ }
    setTheme(next);
  };
  return [theme, toggle];
}

export default function App() {
  const [token, setTok] = useState(getToken());
  const [lender, setLender] = useState(sessionStorage.getItem("lender") || "");
  if (!token) {
    return <Login onLogin={(t, l) => { setToken(t); sessionStorage.setItem("lender", l); setTok(t); setLender(l); }} />;
  }
  return <Shell lender={lender} />;
}

function Shell({ lender }: { lender: string }) {
  const [tab, setTab] = useState<Tab>("Live map");
  const [vehicle, setVehicle] = useState<string | null>(null);
  const [caseId, setCaseId] = useState<number | null>(null);
  const [alerts, setAlerts] = useState<Alert[]>([]);
  const [top, setTop] = useState<Top>([]);
  const [positions, setPositions] = useState<LivePoint[]>([]);
  const [fleetTotal, setFleetTotal] = useState(0);
  const [theme, toggleTheme] = useTheme();
  const ws = useRef<WebSocket | null>(null);
  const bbox = useRef<number[] | null>(null);

  useEffect(() => {
    api<{ alerts: Alert[] }>("/alerts?limit=50").then((r) => setAlerts(r.alerts)).catch(() => {});
    let closed = false;
    const connect = () => {
      const s = new WebSocket(wsUrl());
      ws.current = s;
      s.onopen = () => bbox.current && s.send(JSON.stringify({ bbox: bbox.current }));
      s.onmessage = (ev) => {
        const m = JSON.parse(ev.data);
        if (m.type === "positions") { setPositions(m.vehicles); setFleetTotal(m.total ?? m.vehicles.length); }
        else if (m.type === "topk") setTop(m.top);
        else if (m.type === "alert") setAlerts((a) => [{ ...m.alert, risk_score: m.alert.risk_score }, ...a].slice(0, 200));
      };
      s.onclose = () => { if (!closed) setTimeout(connect, 2000); };   // reconnect
    };
    connect();
    return () => { closed = true; ws.current?.close(); };
  }, []);

  const ctx: Ctx = {
    alerts, top, positions, fleetTotal,
    setBbox: (b) => {
      bbox.current = b;
      if (ws.current?.readyState === WebSocket.OPEN) ws.current.send(JSON.stringify({ bbox: b }));
    },
    openVehicle: (id) => { setCaseId(null); setVehicle(id); },
    openCase: (id) => { setVehicle(null); setCaseId(id); },
    theme,
  };
  const go = (t: Tab) => { setTab(t); setVehicle(null); setCaseId(null); };

  let page;
  if (vehicle) page = <VehicleDetail id={vehicle} ctx={ctx} onBack={() => setVehicle(null)} />;
  else if (caseId) page = <CaseDetail id={caseId} ctx={ctx} onBack={() => setCaseId(null)} />;
  else if (tab === "Live map") page = <LiveMap ctx={ctx} />;
  else if (tab === "Pipeline") page = <PipelinePage ctx={ctx} />;
  else if (tab === "Alerts") page = <AlertsPage ctx={ctx} />;
  else if (tab === "Leaderboard") page = <Leaderboard ctx={ctx} />;
  else if (tab === "Convoys") page = <ConvoysPage ctx={ctx} />;
  else if (tab === "Cases") page = <CasesPage ctx={ctx} />;
  else if (tab === "Reports") page = <ReportsPage />;
  else page = <HealthPage />;

  return (
    <div className="app">
      <nav className="nav">
        <div className="logo" title="Asset Recovery">AR</div>
        {TABS.map((t) => (
          <button key={t} title={t} className={t === tab && !vehicle && !caseId ? "active" : ""} onClick={() => go(t)}>
            <Icon name={TAB_ICON[t]} />{TAB_SHORT[t]}
          </button>
        ))}
        <div className="spacer" />
        <button title={theme === "dark" ? "Light theme" : "Dark theme"} onClick={toggleTheme}>
          <Icon name={theme === "dark" ? "sun" : "moon"} />Theme
        </button>
      </nav>
      <div className="page">
        <div className="topbar">
          <h1>{vehicle ? `Vehicle ${vehicle}` : caseId ? `Case #${caseId}` : tab}</h1>
          <div className="who">
            <LoadControl />
            <span><span className="live-dot" /> Live</span>
            <span>{lender}</span>
            <button className="icon-btn" title="Sign out" onClick={() => { setToken(null); location.reload(); }}>
              <Icon name="logout" />
            </button>
          </div>
        </div>
        <Fragment key={theme}>{page}</Fragment>{/* remount on theme change so maps pick the matching basemap */}
      </div>
    </div>
  );
}

/** Demo load switch: how many simulated vehicles report (the simulator keeps the whole fleet loaded). */
function LoadControl() {
  const [load, setLoad] = useState<{ active_vehicles: number; vehicles: number; presets: number[] } | null>(null);
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    const get = () => api<typeof load>("/system/load").then(setLoad).catch(() => setLoad(null));
    get();
    const t = setInterval(get, 15000);
    return () => clearInterval(t);
  }, []);
  if (!load) return null;
  const choose = async (n: number) => {
    setBusy(true);
    try {
      const r = await api<{ active_vehicles: number }>("/system/load", { method: "POST", body: JSON.stringify({ active_vehicles: n }) });
      setLoad({ ...load, active_vehicles: r.active_vehicles });
    } finally { setBusy(false); }
  };
  const short = (n: number) => (n >= 1000 ? `${n / 1000}k` : String(n));
  return (
    <div className="load-ctl" title="Simulated vehicles reporting. 100k shows full scale (heavy on a laptop); takes ~30 s to ramp.">
      <span className="lbl">Load</span>
      {(load.presets.length ? load.presets : [load.vehicles]).map((n) => (
        <button key={n} disabled={busy} className={load.active_vehicles === n ? "on" : ""} onClick={() => choose(n)}>{short(n)}</button>
      ))}
    </div>
  );
}

function Login({ onLogin }: { onLogin: (token: string, lender: string) => void }) {
  const [username, setUsername] = useState("alpha");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError("");
    try {
      const r = await api<{ token: string; lender: string }>("/auth/login", {
        method: "POST", body: JSON.stringify({ username, password }),
      });
      onLogin(r.token, r.lender);
    } catch (err) {
      setError(String((err as Error).message));
    }
  };
  return (
    <div className="login card">
      <h2>Asset Recovery · lender sign-in</h2>
      <form onSubmit={submit}>
        <label>Lender
          <select value={username} onChange={(e) => setUsername(e.target.value)} style={{ width: "100%" }}>
            <option value="alpha">Lender Alpha</option>
            <option value="beta">Lender Beta</option>
            <option value="gamma">Lender Gamma</option>
          </select>
        </label>
        <label>Password
          <input type="password" value={password} onChange={(e) => setPassword(e.target.value)} style={{ width: "100%" }} />
        </label>
        <button className="primary" type="submit">Sign in</button>
        {error && <div className="error">{error}</div>}
        <div className="muted">Demo password: <code>DEMO_PASSWORD</code> in your <code>.env</code>.</div>
      </form>
    </div>
  );
}
