import type { Map as MLMap } from "maplibre-gl";
import * as maplibregl from "maplibre-gl";
import { useEffect, useMemo, useRef, useState } from "react";
import type { Alert, Ctx } from "./App";
import { SIGNAL_LABEL, api, fmtEta, fmtTime, riskColor } from "./api";
import { COLORS, type Filter, FleetLayer } from "./fleetLayer";
import { Icon, SIGNAL_INFO } from "./icons";
import { MapView, RISK_COLOR_EXPR, addGeofences, emptyFC, lineFC, pointsFC, setData } from "./MapView";

const Pill = ({ code }: { code: string }) => (
  <span className="pill" style={{ background: code === "TOW_SUSPECTED" || code === "GEOFENCE_EXIT" ? "#c0262d" : "#5b6b85" }}>
    {SIGNAL_LABEL[code] || code}
  </span>
);
const Score = ({ v }: { v: number }) => <b style={{ color: riskColor(v) }}>{v.toFixed(1)}</b>;

// ------------------------------------------------------------------ Live map
const RISK_VAR = (v: number) => (v >= 70 ? "var(--high)" : v >= 40 ? "var(--mid)" : "var(--low)");

/** "2 min ago". Sim time can run ahead of the wall clock (demo mode), so "now" is whichever is later. */
function ago(ts: number, now: number) {
  const s = Math.max(0, now - ts);
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
  return `${Math.floor(s / 86400)} d ago`;
}

type AlertGroup = { a: Alert; count: number };

/** Newest alert per vehicle + signal, with how many times it repeated (one card instead of a wall of repeats). */
function groupAlerts(alerts: Alert[]): AlertGroup[] {
  const groups = new Map<string, AlertGroup>();
  for (const a of alerts) {
    const key = `${a.vehicle_id}|${a.code}`;
    const g = groups.get(key);
    if (g) g.count += 1;
    else groups.set(key, { a, count: 1 });
  }
  return [...groups.values()];
}

export function LiveMap({ ctx }: { ctx: Ctx }) {
  const fleet = useRef<FleetLayer | null>(null);
  const [filter, setFilter] = useState<Filter>("all");
  const [counts, setCounts] = useState({ total: 0, moving: 0, risk: 0 });

  const onReady = (map: MLMap) => {
    api<{ geofences: { area: GeoJSON.Polygon }[] }>("/geofences").then((r) => addGeofences(map, r.geofences));
    fleet.current = new FleetLayer(map);
    for (const layer of FleetLayer.clickableLayers) {
      map.on("click", layer, (e) => ctx.openVehicle(String(e.features![0].properties!.id)));
      map.on("mouseenter", layer, () => (map.getCanvas().style.cursor = "pointer"));
      map.on("mouseleave", layer, () => (map.getCanvas().style.cursor = ""));
    }
    const sendView = () => {
      const b = map.getBounds();
      ctx.setBbox([b.getWest(), b.getSouth(), b.getEast(), b.getNorth()]);
    };
    map.on("moveend", sendView);
    sendView();
    return () => fleet.current?.destroy();
  };

  useEffect(() => {
    if (!fleet.current) return;
    fleet.current.update(ctx.positions);
    setCounts(fleet.current.counts());
  }, [ctx.positions]);
  useEffect(() => fleet.current?.setFilter(filter), [filter]);

  const now = Math.max(Date.now() / 1000, ctx.alerts[0]?.ts ?? 0);
  const groups = useMemo(() => groupAlerts(ctx.alerts), [ctx.alerts]);
  const openCases = new Set(ctx.alerts.filter((a) => a.case_id).map((a) => a.case_id)).size;
  const lastHour = ctx.alerts.filter((a) => now - a.ts <= 3600).length;
  const reasons = (vid: string) =>
    [...new Set(ctx.alerts.filter((a) => a.vehicle_id === vid).map((a) => SIGNAL_LABEL[a.code] || a.code))].join(" · ");

  const kpi = (f: Filter, label: string, n: number) => (
    <button className={`kpi ${filter === f ? "on" : ""}`} onClick={() => setFilter(f)} title={`Show: ${label}`}>
      <span className="v">{n.toLocaleString()}</span><span className="l">{label}</span>
    </button>
  );
  const stat = (label: string, n: number) => (
    <div className="kpi static"><span className="v">{n.toLocaleString()}</span><span className="l">{label}</span></div>
  );
  return (
    <main className="full">
      <div className="map-wrap">
        <MapView onReady={onReady} zoom={12} />
        <div className="map-overlay kpis">
          {stat("Fleet live", ctx.fleetTotal)}
          <div className="kpi-sep" />
          {kpi("all", "On map", counts.total)}
          {kpi("moving", "Moving", counts.moving)}
          {kpi("risk", "At risk", counts.risk)}
          <div className="kpi-sep" />
          {stat("Open cases", openCases)}
          {stat("Alerts, last hour", lastHour)}
        </div>
        <div className="map-overlay legend">
          <span><i style={{ background: COLORS.normal }} />Moving</span>
          <span><i style={{ background: COLORS.parked, opacity: 0.6 }} />Parked</span>
          <span><i style={{ background: COLORS.low }} />Low risk</span>
          <span><i style={{ background: COLORS.mid }} />Elevated (40+)</span>
          <span><i style={{ background: COLORS.high }} />Case opened (70+)</span>
        </div>
      </div>
      <aside className="side">
        <section>
          <h3>Riskiest now <span>score</span></h3>
          {ctx.top.length === 0 && <div className="muted">No vehicles at risk.</div>}
          {ctx.top.slice(0, 6).map((t) => (
            <div key={t.vehicle_id} className="risk-row" onClick={() => ctx.openVehicle(t.vehicle_id)}>
              <span className="id">{t.vehicle_id}</span>
              <span className="score" style={{ color: RISK_VAR(t.score) }}>{t.score.toFixed(0)}</span>
              <div className="track"><div style={{ width: `${Math.min(100, t.score)}%`, background: RISK_VAR(t.score) }} /></div>
              <span className="why">{reasons(t.vehicle_id) || "Earlier signals"}</span>
            </div>
          ))}
        </section>
        <section>
          <h3>Alert feed <span>{groups.length} active</span></h3>
          {groups.length === 0 && <div className="muted">No alerts yet.</div>}
          {groups.slice(0, 25).map((g) => <AlertCard key={g.a.alert_id} g={g} now={now} ctx={ctx} />)}
        </section>
      </aside>
    </main>
  );
}

function AlertCard({ g, now, ctx }: { g: AlertGroup; now: number; ctx: Ctx }) {
  const { a, count } = g;
  const info = SIGNAL_INFO[a.code] ?? { icon: "tamper" as const, text: a.code };
  const color = RISK_VAR(Math.max(a.risk_score, 1));
  return (
    <div className="alert-card" onClick={() => ctx.openVehicle(a.vehicle_id)}>
      <div className="icon" style={{ color, background: `color-mix(in srgb, ${color} 14%, transparent)` }}>
        <Icon name={info.icon} />
      </div>
      <div>
        <div className="title">{SIGNAL_LABEL[a.code] || a.code} · {a.vehicle_id}</div>
        <div className="sub">{info.text}{a.case_id ? ` · case #${a.case_id}` : ""}</div>
      </div>
      <div className="meta">
        <span>{ago(a.ts, now)}</span>
        {count > 1 && <span className="badge">×{count}</span>}
      </div>
    </div>
  );
}

// ------------------------------------------------------------------ Alerts
export function AlertsPage({ ctx }: { ctx: Ctx }) {
  const [code, setCode] = useState("");
  const [rows, setRows] = useState<Alert[]>([]);
  useEffect(() => {
    api<{ alerts: Alert[] }>(`/alerts?limit=300${code ? `&code=${code}` : ""}`).then((r) => setRows(r.alerts));
  }, [code, ctx.alerts.length]);
  return (
    <main>
      <div className="card">
        <div className="row" style={{ marginBottom: 12 }}>
          <h2 style={{ margin: 0 }}>Open alerts</h2>
          <select value={code} onChange={(e) => setCode(e.target.value)} style={{ marginLeft: "auto" }}>
            <option value="">All signals</option>
            {Object.entries(SIGNAL_LABEL).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
          </select>
        </div>
        <div className="table-wrap"><table>
          <thead><tr><th>Time</th><th>Vehicle</th><th>Signal</th><th>Risk</th><th>Case</th><th /></tr></thead>
          <tbody>{rows.map((a) => (
            <tr key={a.alert_id} className="click" onClick={() => ctx.openVehicle(a.vehicle_id)}>
              <td>{fmtTime(a.ts)}</td><td>{a.vehicle_id}</td><td><Pill code={a.code} /></td>
              <td className="num"><Score v={a.risk_score} /></td>
              <td>{a.case_id ? <a href="#" onClick={(e) => { e.stopPropagation(); ctx.openCase(a.case_id!); }}>#{a.case_id}</a> : "—"}</td>
              <td>{!a.case_id && <OpenCaseButton vehicle={a.vehicle_id} ctx={ctx} />}</td>
            </tr>))}
          </tbody></table></div>
        {rows.length === 0 && <div className="muted">No open alerts.</div>}
      </div>
    </main>
  );
}

function OpenCaseButton({ vehicle, ctx }: { vehicle: string; ctx: Ctx }) {
  return (
    <button onClick={async (e) => {
      e.stopPropagation();
      const r = await api<{ case_id: number }>("/cases", { method: "POST", body: JSON.stringify({ vehicle_id: vehicle }) });
      ctx.openCase(r.case_id);
    }}>Open case</button>
  );
}

// ------------------------------------------------------------------ Leaderboard
export function Leaderboard({ ctx }: { ctx: Ctx }) {
  const [rows, setRows] = useState<{ vehicle_id: string; score: number }[]>([]);
  useEffect(() => {
    const load = () => api<{ top: typeof rows }>("/risk/top?k=50").then((r) => setRows(r.top));
    load();
    const t = setInterval(load, 5000);
    return () => clearInterval(t);
  }, []);
  const max = Math.max(1, ...rows.map((r) => r.score));
  return (
    <main><div className="card">
      <h2>Risk leaderboard · Top 50 (live, from Redis sorted set)</h2>
      <div className="table-wrap"><table>
        <thead><tr><th>#</th><th>Vehicle</th><th>Score</th><th style={{ width: "40%" }} /></tr></thead>
        <tbody>{rows.map((r, i) => (
          <tr key={r.vehicle_id} className="click" onClick={() => ctx.openVehicle(r.vehicle_id)}>
            <td>{i + 1}</td><td>{r.vehicle_id}</td><td className="num"><Score v={r.score} /></td>
            <td><div className="bar" style={{ width: `${(r.score / max) * 100}%`, background: riskColor(r.score) }} /></td>
          </tr>))}
        </tbody></table></div>
      {rows.length === 0 && <div className="muted">No vehicles at risk right now.</div>}
    </div></main>
  );
}

// ------------------------------------------------------------------ Vehicle detail
export function VehicleDetail({ id, ctx, onBack }: { id: string; ctx: Ctx; onBack: () => void }) {
  const [v, setV] = useState<any>(null);
  const [trail, setTrail] = useState<{ ts: number; lat: number; lon: number; speed_kmh: number; ignition: boolean }[]>([]);
  const [hours, setHours] = useState(2);
  const [at, setAt] = useState(0);
  const mapRef = useRef<MLMap | null>(null);
  const marker = useRef<maplibregl.Marker | null>(null);

  useEffect(() => { api(`/vehicles/${id}`).then(setV); }, [id]);
  useEffect(() => {
    const now = Date.now() / 1000;
    api<{ points: typeof trail }>(`/vehicles/${id}/trail?from=${now - hours * 3600}&to=${now}`).then((r) => {
      setTrail(r.points);
      setAt(Math.max(0, r.points.length - 1));
    });
  }, [id, hours]);
  useEffect(() => {
    const map = mapRef.current;
    if (!map?.getSource("trail")) return;
    setData(map, "trail", lineFC(trail.map((p) => [p.lon, p.lat])));
    if (trail.length) map.fitBounds(trail.reduce((b, p) => b.extend([p.lon, p.lat]),
      new maplibregl.LngLatBounds([trail[0].lon, trail[0].lat], [trail[0].lon, trail[0].lat])), { padding: 60, maxZoom: 15 });
  }, [trail]);
  useEffect(() => {
    const p = trail[at];
    if (p && mapRef.current) {
      marker.current ??= new maplibregl.Marker({ color: "#c0262d" }).setLngLat([p.lon, p.lat]).addTo(mapRef.current);
      marker.current.setLngLat([p.lon, p.lat]);
    }
  }, [at, trail]);

  const onReady = (map: MLMap) => {
    mapRef.current = map;
    map.addSource("trail", { type: "geojson", data: emptyFC() });
    map.addLayer({ id: "trail", type: "line", source: "trail", paint: { "line-color": "#1f5fbf", "line-width": 3 } });
    api<{ geofences: { area: GeoJSON.Polygon }[] }>("/geofences").then((r) => addGeofences(map, r.geofences));
    setTrail((t) => [...t]);            // redraw once the source exists
  };

  if (!v) return <main>Loading…</main>;
  const p = trail[at];
  const parts = Object.entries(v.score_breakdown.signals as Record<string, number>);
  return (
    <main>
      <div className="row" style={{ marginBottom: 12 }}>
        <button onClick={onBack}>← Back</button>
        <h2 style={{ margin: 0 }}>{v.vehicle_id} · {v.make} {v.model} {v.year}</h2>
        <span style={{ marginLeft: "auto" }}>Risk <Score v={v.risk_score} /></span>
        {v.open_case ? <button className="primary" onClick={() => ctx.openCase(v.open_case.case_id)}>Case #{v.open_case.case_id}</button>
          : <OpenCaseButton vehicle={v.vehicle_id} ctx={ctx} />}
      </div>
      <div className="grid2">
        <div className="card" style={{ padding: 0, height: 420, overflow: "hidden" }}><MapView onReady={onReady} zoom={13} /></div>
        <div className="card">
          <h2>Trail replay</h2>
          <div className="row">
            <select value={hours} onChange={(e) => setHours(Number(e.target.value))}>
              {[1, 2, 6, 24].map((h) => <option key={h} value={h}>Last {h} h</option>)}
            </select>
            <span className="muted">{trail.length} points</span>
          </div>
          <input type="range" min={0} max={Math.max(0, trail.length - 1)} value={at}
            onChange={(e) => setAt(Number(e.target.value))} style={{ width: "100%", marginTop: 12 }} />
          {p && <div className="muted">{fmtTime(p.ts)} · {p.speed_kmh.toFixed(0)} km/h · ignition {p.ignition ? "on" : "off"}</div>}
          <h2 style={{ marginTop: 20 }}>Score breakdown</h2>
          {parts.length === 0 && <div className="muted">No active signals.</div>}
          {parts.map(([code, pts]) => (
            <div key={code} style={{ marginBottom: 8 }}>
              <div className="row"><Pill code={code} /><span className="muted" style={{ marginLeft: "auto" }}>{pts} pts</span></div>
              <div className="bar" style={{ width: `${Math.min(100, pts * 3)}%` }} />
            </div>))}
          <div className="muted">× loan multiplier {v.score_breakdown.loan_multiplier} (days past due: {v.days_past_due})</div>
        </div>
      </div>
      <div className="grid2">
        <div className="card">
          <h2>Loan &amp; device</h2>
          <table><tbody>
            <tr><th>Loan</th><td>#{v.loan_id} · ₹{v.amount.toLocaleString()} · {v.loan_status}</td></tr>
            <tr><th>Days past due</th><td>{v.days_past_due}</td></tr>
            <tr><th>VIN</th><td>{v.vin}</td></tr>
            <tr><th>Tracker</th><td>OEM {v.oem} · {v.device_model}</td></tr>
            <tr><th>Baseline</th><td>{v.baseline ? `home ${v.baseline.home_cell}, work ${v.baseline.work_cell}, active ${v.baseline.active_from}-${v.baseline.active_to} h` : "not learned yet (nightly job)"}</td></tr>
          </tbody></table>
        </div>
        <div className="card">
          <h2>Signals</h2>
          {v.alerts.length === 0 && <div className="muted">None.</div>}
          {v.alerts.slice(0, 15).map((a: any) => (
            <div key={a.alert_id} className="row" style={{ padding: "4px 0" }}>
              <Pill code={a.code} /><span className="muted">{fmtTime(a.ts)}</span><span style={{ marginLeft: "auto" }}><Score v={a.risk_score} /></span>
            </div>))}
        </div>
      </div>
    </main>
  );
}

// ------------------------------------------------------------------ Convoys
export function ConvoysPage({ ctx }: { ctx: Ctx }) {
  const [convoys, setConvoys] = useState<any[]>([]);
  const mapRef = useRef<MLMap | null>(null);
  useEffect(() => { api<{ convoys: any[] }>("/convoys/active?hours=6").then((r) => setConvoys(r.convoys)); }, []);
  const features = useMemo(() => convoys.flatMap((c) => {
    const pts = c.members.filter((m: any) => m.position).map((m: any) => [m.position[1], m.position[0]]);
    return lineFC(pts).features;
  }), [convoys]);
  useEffect(() => {
    const map = mapRef.current;
    if (map?.getSource("convoys")) {
      setData(map, "convoys", { type: "FeatureCollection", features });
      const pts = convoys.flatMap((c) => c.members.filter((m: any) => m.position).map((m: any) => ({ lat: m.position[0], lon: m.position[1], risk: 70, id: m.vehicle_id })));
      setData(map, "members", pointsFC(pts));
    }
  }, [features, convoys]);
  const onReady = (map: MLMap) => {
    mapRef.current = map;
    map.addSource("convoys", { type: "geojson", data: emptyFC() });
    map.addSource("members", { type: "geojson", data: emptyFC() });
    map.addLayer({ id: "convoys", type: "line", source: "convoys", paint: { "line-color": "#c0262d", "line-width": 4 } });
    map.addLayer({ id: "members", type: "circle", source: "members", paint: { "circle-color": "#c0262d", "circle-radius": 6, "circle-stroke-color": "#fff", "circle-stroke-width": 1 } });
    map.on("click", "members", (e) => ctx.openVehicle(String(e.features![0].properties!.id)));
    setConvoys((c) => [...c]);
  };
  return (
    <main className="full">
      <MapView onReady={onReady} />
      <aside className="side">
        <h3 style={{ marginTop: 0 }}>Convoys (last 6 h)</h3>
        {convoys.length === 0 && <div className="muted">None detected. A group must travel together for 30 minutes.</div>}
        {convoys.map((c) => (
          <div key={c.convoy_id} className="card">
            <b>Convoy #{c.convoy_id}</b> <span className="muted">{fmtTime(c.detected_at)}</span>
            {c.members.map((m: any) => <div key={m.vehicle_id} className="feed-item" onClick={() => ctx.openVehicle(m.vehicle_id)}>{m.vehicle_id}</div>)}
            <OpenCaseButton vehicle={c.members[0].vehicle_id} ctx={ctx} />
          </div>))}
      </aside>
    </main>
  );
}

// ------------------------------------------------------------------ Cases
export function CasesPage({ ctx }: { ctx: Ctx }) {
  const [rows, setRows] = useState<any[]>([]);
  useEffect(() => { api<{ cases: any[] }>("/cases").then((r) => setRows(r.cases)); }, []);
  return (
    <main><div className="card">
      <h2>Recovery cases</h2>
      <div className="table-wrap"><table>
        <thead><tr><th>Case</th><th>Opened</th><th>Vehicle</th><th>Status</th><th>Agent</th><th>Days past due</th><th>Risk</th></tr></thead>
        <tbody>{rows.map((c) => (
          <tr key={c.case_id} className="click" onClick={() => ctx.openCase(c.case_id)}>
            <td>#{c.case_id}</td><td>{fmtTime(c.opened_at)}</td><td>{c.vehicle_id}</td><td>{c.status}</td>
            <td>{c.agent || "—"}</td><td className="num">{c.days_past_due}</td><td className="num"><Score v={c.risk_score} /></td>
          </tr>))}</tbody></table></div>
      {rows.length === 0 && <div className="muted">No cases yet. Cases open automatically above risk 70, or from an alert.</div>}
    </div></main>
  );
}

export function CaseDetail({ id, ctx, onBack }: { id: number; ctx: Ctx; onBack: () => void }) {
  const [c, setC] = useState<any>(null);
  const [agents, setAgents] = useState<any[]>([]);
  const [nearest, setNearest] = useState<any>(null);
  const mapRef = useRef<MLMap | null>(null);

  const load = () => api(`/cases/${id}`).then(setC);
  useEffect(() => {
    load();
    api<{ agents: any[] }>("/agents").then((r) => setAgents(r.agents));
    const t = setInterval(load, 30_000);           // vehicle keeps moving: refresh route + ETA every 30 s
    return () => clearInterval(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [id]);
  useEffect(() => {
    const map = mapRef.current;
    if (!map?.getSource("route") || !c) return;
    setData(map, "route", lineFC(c.route?.path || []));
    const pts = [];
    if (c.position) pts.push({ lat: c.position[0], lon: c.position[1], risk: 100, id: c.vehicle_id });
    const ag = agents.find((a) => a.agent_id === c.agent_id);
    if (ag?.position) pts.push({ lat: ag.position[0], lon: ag.position[1], risk: 0, id: ag.display_name });
    setData(map, "ends", pointsFC(pts));
    if (c.position) map.easeTo({ center: [c.position[1], c.position[0]] });
  }, [c, agents]);

  const onReady = (map: MLMap) => {
    mapRef.current = map;
    map.addSource("route", { type: "geojson", data: emptyFC() });
    map.addSource("ends", { type: "geojson", data: emptyFC() });
    map.addLayer({ id: "route", type: "line", source: "route", paint: { "line-color": "#1f5fbf", "line-width": 5 } });
    map.addLayer({ id: "ends", type: "circle", source: "ends", paint: { "circle-color": RISK_COLOR_EXPR, "circle-radius": 8, "circle-stroke-color": "#fff", "circle-stroke-width": 2 } });
    setC((x: any) => (x ? { ...x } : x));
  };
  const update = async (body: object) => setC(await api(`/cases/${id}`, { method: "PATCH", body: JSON.stringify(body) }));

  if (!c) return <main>Loading…</main>;
  return (
    <main>
      <div className="row" style={{ marginBottom: 12 }}>
        <button onClick={onBack}>← Back</button>
        <h2 style={{ margin: 0 }}>Case #{c.case_id} · {c.vehicle_id}</h2>
        <span className="pill" style={{ background: "#5b6b85" }}>{c.status}</span>
        <button style={{ marginLeft: "auto" }} onClick={() => ctx.openVehicle(c.vehicle_id)}>Vehicle details</button>
      </div>
      <div className="grid2">
        <div className="card" style={{ padding: 0, height: 460, overflow: "hidden" }}><MapView onReady={onReady} zoom={13} /></div>
        <div className="card">
          <h2>Agent &amp; route</h2>
          <div className="row">
            <select value={c.agent_id || ""} onChange={(e) => update({ agent_id: Number(e.target.value) })}>
              <option value="" disabled>Assign agent…</option>
              {agents.map((a) => <option key={a.agent_id} value={a.agent_id}>{a.display_name}</option>)}
            </select>
            <button onClick={async () => setNearest(await api(`/route/nearest-agent?vehicle=${c.vehicle_id}`))}>Find nearest</button>
          </div>
          {nearest && (
            <p>Nearest: <b>{agents.find((a) => String(a.agent_id) === nearest.agent_id)?.display_name}</b>, {fmtEta(nearest.eta_s)} away{" "}
              <button className="primary" onClick={() => { update({ agent_id: Number(nearest.agent_id) }); setNearest(null); }}>Assign</button></p>
          )}
          {c.route ? <p>ETA <b>{fmtEta(c.route.eta_s)}</b> · {(c.route.distance_m / 1000).toFixed(1)} km by road (A*, refreshed every 30 s)</p>
            : <p className="muted">{c.agent_id ? "No live route (vehicle position unknown)." : "Assign an agent to see the route."}</p>}
          <div className="row" style={{ marginTop: 12 }}>
            <button onClick={() => update({ status: "recovered" })}>Mark recovered</button>
            <button onClick={() => update({ status: "closed" })}>Close case</button>
          </div>
          <h2 style={{ marginTop: 20 }}>Linked alerts</h2>
          {c.alerts.map((a: any) => (
            <div key={a.alert_id} className="row" style={{ padding: "4px 0" }}>
              <Pill code={a.code} /><span className="muted">{fmtTime(a.ts)}</span><span style={{ marginLeft: "auto" }}><Score v={a.risk_score} /></span>
            </div>))}
        </div>
      </div>
    </main>
  );
}

// ------------------------------------------------------------------ Reports
export function ReportsPage() {
  const [r, setR] = useState<any>(null);
  const [err, setErr] = useState("");
  useEffect(() => { api("/reports/daily").then(setR).catch((e) => setErr(e.message)); }, []);
  if (err) return <main><div className="card"><h2>Daily lender report</h2><div className="muted">{err}</div></div></main>;
  if (!r) return <main>Loading…</main>;
  return (
    <main>
      <h2>Daily report · {r.lender} · {r.date}</h2>
      <div className="stats" style={{ marginBottom: 16 }}>
        {Object.entries(r.summary as Record<string, number>).map(([k, v]) => (
          <div key={k} className="stat"><div className="v">{typeof v === "number" && v < 1 && v > 0 ? `${(v * 100).toFixed(1)}%` : v}</div><div className="l">{k.replaceAll("_", " ")}</div></div>))}
      </div>
      <div className="grid2">
        <div className="card"><h2>Alerts by signal (24 h)</h2><table><tbody>
          {Object.entries(r.alerts_by_signal as Record<string, number>).map(([k, v]) => <tr key={k}><td><Pill code={k} /></td><td className="num">{v}</td></tr>)}
        </tbody></table></div>
        <div className="card"><h2>Vehicles at risk</h2><div className="table-wrap"><table>
          <thead><tr><th>Vehicle</th><th>Peak score</th><th>Days past due</th></tr></thead>
          <tbody>{r.at_risk.map((v: any) => <tr key={v.vehicle_id}><td>{v.vehicle_id}</td><td className="num"><Score v={v.peak_score} /></td><td className="num">{v.days_past_due}</td></tr>)}</tbody>
        </table></div></div>
      </div>
    </main>
  );
}

// ------------------------------------------------------------------ System health
export function HealthPage() {
  const [h, setH] = useState<any>(null);
  const prev = useRef<any>(null);
  const [rate, setRate] = useState<Record<string, number>>({});
  useEffect(() => {
    const load = async () => {
      const cur = await api("/system/health");
      if (prev.current) {
        const dt = cur.ts - prev.current.ts;
        const d = (f: (x: any) => number) => Math.max(0, (f(cur) - f(prev.current)) / dt);
        setRate({
          sent: d((x) => x.services.simulator?.sent || 0),
          pipeline: d((x) => Number(x.pipeline.in || 0)),
          normalized: d((x) => Number(x.pipeline.normalized || 0)),
          dups: d((x) => Number(x.pipeline.duplicates_dropped || 0)),
        });
      }
      prev.current = cur;
      setH(cur);
    };
    load();
    const t = setInterval(load, 3000);
    return () => clearInterval(t);
  }, []);
  if (!h) return <main>Loading…</main>;
  const p = h.pipeline, dl = Object.entries(p).filter(([k]) => k.startsWith("dead_letter."));
  const stat = (v: any, l: string) => <div className="stat"><div className="v">{typeof v === "number" ? Math.round(v).toLocaleString() : v ?? "—"}</div><div className="l">{l}</div></div>;
  return (
    <main>
      <h2>System health <span className="muted" style={{ fontSize: 13 }}>· refreshes every 3 s · turn chaos up in simulator/config.yaml and watch</span></h2>
      <div className="stats" style={{ marginBottom: 16 }}>
        {stat(rate.sent, "events/s sent by simulator")}
        {stat(rate.pipeline, "events/s into pipeline")}
        {stat(rate.normalized, "events/s cleaned")}
        {stat(rate.dups, "duplicates dropped /s")}
        {stat(h.vehicles_live, "vehicles on the map")}
      </div>
      <div className="grid2">
        <div className="card"><h2>Pipeline totals</h2><table><tbody>
          {["in", "normalized", "duplicates_dropped", "late", "dead_letter", "suspect_gps"].map((k) => <tr key={k}><td>{k.replaceAll("_", " ")}</td><td className="num">{Number(p[k] || 0).toLocaleString()}</td></tr>)}
        </tbody></table></div>
        <div className="card"><h2>Kafka consumer lag</h2><table><tbody>
          {Object.entries(h.kafka_lag as Record<string, number>).map(([k, v]) => <tr key={k}><td>{k}</td><td className="num">{v.toLocaleString()}</td></tr>)}
        </tbody></table>
          <h2 style={{ marginTop: 16 }}>Dead-letter reasons</h2><table><tbody>
            {dl.map(([k, v]) => <tr key={k}><td>{k.slice(12)}</td><td className="num">{Number(v).toLocaleString()}</td></tr>)}
          </tbody></table></div>
        <div className="card"><h2>Detectors</h2><table><tbody>
          {Object.entries(h.detectors as Record<string, string>).map(([k, v]) => <tr key={k}><td>{k.replace("signal.", "")}</td><td className="num">{Number(v).toLocaleString()}</td></tr>)}
        </tbody></table></div>
      </div>
    </main>
  );
}
