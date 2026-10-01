// Pipeline screen: live 3D view of multi-OEM ingestion and cleaning, with real sampled payloads.
import { useEffect, useRef, useState } from "react";
import type { Ctx } from "./App";
import { api } from "./api";
import { type Oem, PipelineScene, type Rates, type StageId } from "./pipelineScene";

type Sample = { oem: string; raw: string; outcome: string; detail: string; canonical: Record<string, unknown> | null };
type Flow = { ts: number; counts: Record<string, string>; detectors: Record<string, string>; samples: Sample[] };

const OEMS: Oem[] = ["A", "B", "C"];
const n = (c: Record<string, string>, k: string) => Number(c[k] || 0);
const fmt = (v: number) => (v >= 1000 ? `${(v / 1000).toFixed(1)}k` : v.toFixed(0));

const STAGE_TEXT: Record<StageId, { title: string; body: string; show: (s: Sample) => boolean }> = {
  sources: { title: "Three OEM formats", body: "Each tracker vendor sends a different wire format, units and clock. These are real payloads as they arrived.", show: () => true },
  gateway: { title: "Ingest gateway", body: "HTTP POST /ingest → Kafka topic raw.telemetry, keyed by vehicle_id so each vehicle stays on one worker.", show: () => true },
  kafka: { title: "Redpanda (Kafka API)", body: "12 partitions buffer bursts; the pipeline consumer group splits partitions between 3 replicas.", show: () => true },
  parse: { title: "Parse & normalize", body: "Regex detects the format, then units, time zones, GPS scaling and hex status bits are converted to one canonical event. Unreadable rows go to dead.letter with a reason.", show: (s) => s.outcome !== "duplicate" },
  dedup: { title: "Deduplicate", body: "A RedisBloom filter drops events the tracker re-sent.", show: (s) => s.outcome === "duplicate" },
  reorder: { title: "Reorder", body: "A per-vehicle buffer releases events in order; anything more than 2 minutes behind goes to late.events for the batch re-scorer.", show: (s) => s.outcome === "late" },
  noise: { title: "Noise filter", body: "Impossible GPS jumps are flagged suspect_gps (kept, because tamper detection needs them).", show: (s) => s.outcome === "flagged" },
  detectors: { title: "Detectors", body: "Clean events feed geofence, sliding-window anomaly, route deviation, convoy and the risk scorer.", show: (s) => s.outcome === "clean" || s.outcome === "flagged" },
};

const OUTCOME_LABEL: Record<string, string> = {
  clean: "clean → detectors", flagged: "flagged suspect_gps", duplicate: "duplicate dropped", late: "late → batch", dead_letter: "dead.letter",
};

export function PipelinePage({ ctx }: { ctx: Ctx }) {
  const host = useRef<HTMLDivElement>(null);
  const scene = useRef<PipelineScene | null>(null);
  const prev = useRef<Flow | null>(null);
  const [flow, setFlow] = useState<Flow | null>(null);
  const [rates, setRates] = useState<Rates | null>(null);
  const [stage, setStage] = useState<StageId>("parse");
  const [open, setOpen] = useState<number | null>(0);

  useEffect(() => {
    scene.current = new PipelineScene(host.current!, (s) => { setStage(s); setOpen(0); }, ctx.theme === "dark");
    return () => scene.current?.destroy();
  }, [ctx.theme]);

  useEffect(() => {
    let stop = false;
    const tick = async () => {
      try {
        const f = await api<Flow>("/system/pipeline?limit=60");
        const p = prev.current;
        if (p && f.ts > p.ts) {
          const dt = f.ts - p.ts;
          const d = (k: string) => Math.max(0, (n(f.counts, k) - n(p.counts, k)) / dt);
          const r: Rates = {
            in: { A: d("in.A"), B: d("in.B"), C: d("in.C") },
            dead: { A: d("dead_letter.oem.A"), B: d("dead_letter.oem.B"), C: d("dead_letter.oem.C") },
            dup: { A: d("duplicates_dropped.A"), B: d("duplicates_dropped.B"), C: d("duplicates_dropped.C") },
            late: d("late"), flagged: d("suspect_gps"), normalized: d("normalized"),
          };
          setRates(r);
          scene.current?.setRates(r);
          const inAll = r.in.A + r.in.B + r.in.C;
          scene.current?.setStageValues([
            { id: "gateway", title: "", value: `${fmt(inAll)}/s` },
            { id: "kafka", title: "", value: `${fmt(inAll)}/s` },
            { id: "parse", title: "", value: `${fmt(r.dead.A + r.dead.B + r.dead.C)}/s rejected` },
            { id: "dedup", title: "", value: `${fmt(r.dup.A + r.dup.B + r.dup.C)}/s dropped` },
            { id: "reorder", title: "", value: `${fmt(r.late)}/s late` },
            { id: "noise", title: "", value: `${fmt(r.flagged)}/s flagged` },
            { id: "detectors", title: "", value: `${fmt(r.normalized)}/s clean` },
          ]);
        }
        prev.current = f;
        setFlow(f);
      } catch { /* keep the last frame; the next poll retries */ }
      if (!stop) setTimeout(tick, 2000);
    };
    tick();
    return () => { stop = true; };
  }, []);

  const info = STAGE_TEXT[stage];
  const samples = (flow?.samples ?? []).filter(info.show).slice(0, 12);
  const inAll = rates ? rates.in.A + rates.in.B + rates.in.C : 0;
  const deadAll = rates ? rates.dead.A + rates.dead.B + rates.dead.C : 0;
  const dupAll = rates ? rates.dup.A + rates.dup.B + rates.dup.C : 0;
  const funnel: [string, number, string][] = rates ? [
    ["Received", inAll, "var(--accent)"], ["Parsed", inAll - deadAll, "var(--accent)"],
    ["Unique", inAll - deadAll - dupAll, "var(--accent)"], ["Clean out", rates.normalized, "var(--ok)"],
  ] : [];

  return (
    <main className="full">
      <div className="map-wrap p3d">
        <div ref={host} className="p3d-host" />
        <div className="map-overlay kpis">
          {OEMS.map((o) => (
            <div key={o} className="kpi static">
              <span className="v"><i className={`oem-dot oem-${o}`} />{rates ? fmt(rates.in[o]) : "–"}</span>
              <span className="l">OEM {o} events/s</span>
            </div>
          ))}
          <div className="kpi-sep" />
          <div className="kpi static"><span className="v">{rates ? fmt(rates.normalized) : "–"}</span><span className="l">canonical events/s</span></div>
        </div>
        <div className="map-overlay legend">
          <span><i className="oem-dot oem-A" />OEM A · <i className="oem-dot oem-B" />B · <i className="oem-dot oem-C" />C (raw)</span>
          <span><i style={{ background: "#60a5fa" }} />Canonical (after parse)</span>
          <span><i style={{ background: "#fb923c" }} />Flagged / late</span>
          <span><i style={{ background: "#f87171" }} />Dead letter</span>
          <span className="muted">Drag to orbit · scroll to zoom · click a stage</span>
        </div>
      </div>
      <aside className="side">
        <section>
          <h3>Live funnel <span>events/s</span></h3>
          {funnel.map(([label, v, color]) => (
            <div key={label} className="funnel-row">
              <span>{label}</span><b>{fmt(v)}</b>
              <div className="track"><div style={{ width: `${inAll ? Math.max(2, (v / inAll) * 100) : 0}%`, background: color }} /></div>
            </div>
          ))}
          {!rates && <div className="muted">Waiting for two samples of the counters…</div>}
        </section>
        <section>
          <h3>{info.title}</h3>
          <p className="muted stage-body">{info.body}</p>
          {samples.length === 0 && <div className="muted">No sampled payloads for this stage yet (1 in 500 events is sampled).</div>}
          {samples.map((s, i) => (
            <div key={i} className={`sample ${open === i ? "open" : ""}`} onClick={() => setOpen(open === i ? null : i)}>
              <div className="sample-head">
                <i className={`oem-dot oem-${s.oem}`} /><b>OEM {s.oem}</b>
                <span className={`badge out-${s.outcome}`}>{OUTCOME_LABEL[s.outcome] || s.outcome}{s.detail ? ` · ${s.detail}` : ""}</span>
              </div>
              {open === i && (
                <div className="sample-body">
                  <div className="lbl">Raw, as received</div>
                  <pre>{s.raw}</pre>
                  {s.canonical && (<>
                    <div className="lbl">Canonical event</div>
                    <pre>{canonicalView(s.canonical)}</pre>
                  </>)}
                </div>
              )}
            </div>
          ))}
        </section>
      </aside>
    </main>
  );
}

/** The fields that show what normalization did (units, UTC time, decoded ignition), in reading order. */
function canonicalView(c: Record<string, unknown>) {
  const ts = typeof c.device_ts === "number" ? new Date(c.device_ts * 1000).toISOString() : String(c.device_ts);
  const rows: [string, unknown][] = [
    ["vehicle_id", c.vehicle_id], ["oem", c.oem], ["seq", c.seq], ["device_ts (UTC)", ts],
    ["lat, lon", `${c.lat}, ${c.lon}`], ["geohash", c.geohash], ["speed_kmh", c.speed_kmh], ["heading_deg", c.heading_deg],
    ["ignition", c.ignition], ["battery_v", c.battery_v], ["odometer_km", c.odometer_km],
    ["flags", Array.isArray(c.flags) && c.flags.length ? c.flags.join(",") : "—"],
  ];
  return rows.map(([k, v]) => `${k.padEnd(16)} ${v}`).join("\n");
}
