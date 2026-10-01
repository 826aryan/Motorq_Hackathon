// Live fleet rendering for the map: smooth motion, heading-rotated car icons, parked dots, density heatmap.
//
// The server pushes positions every 2 s. Instead of jumping, each car glides from where it is drawn now to
// its new position over the next update interval, and points in its direction of travel.
import type { Map as MLMap } from "maplibre-gl";
import type * as maplibregl from "maplibre-gl";

export type Filter = "all" | "moving" | "risk";
type Live = { id: string; lat: number; lon: number; risk: number };
type Track = {
  fromLat: number; fromLon: number; toLat: number; toLon: number; t0: number;
  heading: number; moving: boolean; risk: number;
};

const GLIDE_MS = 2000;              // matches the server push interval
const MOVE_EPS_M = 4;               // less than this between updates = parked (GPS noise)
const CARS_FROM_ZOOM = 10;          // below this the heatmap shows density instead of individual cars
const FRAME_MS = 1000 / 30;

export const COLORS = { normal: "#2563eb", low: "#d8a31c", mid: "#e0701f", high: "#c0262d", parked: "#8a94a6" };

function colorName(risk: number): keyof typeof COLORS {
  if (risk >= 70) return "high";
  if (risk >= 40) return "mid";
  if (risk > 0) return "low";
  return "normal";
}

/** A small top-down car, pointing north, drawn once per colour (no emoji, no font server). */
function carImage(fill: string): ImageData {
  const w = 22, h = 38, pad = 3;
  const ctx = document.createElement("canvas").getContext("2d", { willReadFrequently: true })!;
  ctx.canvas.width = w + pad * 2;
  ctx.canvas.height = h + pad * 2;
  const x = pad, y = pad, r = 7;
  ctx.shadowColor = "rgba(0,0,0,0.35)";
  ctx.shadowBlur = 3;
  ctx.beginPath();
  ctx.roundRect(x, y, w, h, r);
  ctx.fillStyle = fill;
  ctx.fill();
  ctx.shadowBlur = 0;
  ctx.lineWidth = 2;
  ctx.strokeStyle = "#ffffff";
  ctx.stroke();
  ctx.fillStyle = "rgba(255,255,255,0.85)";                      // windscreen (front = top)
  ctx.beginPath();
  ctx.roundRect(x + 4, y + 7, w - 8, 8, 3);
  ctx.fill();
  ctx.fillStyle = "rgba(255,255,255,0.55)";                      // rear window
  ctx.beginPath();
  ctx.roundRect(x + 5, y + h - 11, w - 10, 5, 2);
  ctx.fill();
  return ctx.getImageData(0, 0, ctx.canvas.width, ctx.canvas.height);
}

function metres(aLat: number, aLon: number, bLat: number, bLon: number) {
  const dy = (bLat - aLat) * 110_540;
  const dx = (bLon - aLon) * 111_320 * Math.cos((aLat * Math.PI) / 180);
  return Math.hypot(dx, dy);
}

function bearing(aLat: number, aLon: number, bLat: number, bLon: number) {
  const dy = bLat - aLat;
  const dx = (bLon - aLon) * Math.cos((aLat * Math.PI) / 180);
  return ((Math.atan2(dx, dy) * 180) / Math.PI + 360) % 360;
}

export class FleetLayer {
  private tracks = new Map<string, Track>();
  private filter: Filter = "all";
  private raf = 0;
  private lastFrame = 0;

  constructor(private map: MLMap) {
    for (const [name, color] of Object.entries(COLORS)) {
      if (name !== "parked") map.addImage(`car-${name}`, carImage(color), { pixelRatio: 2 });
    }
    map.addSource("fleet", { type: "geojson", data: { type: "FeatureCollection", features: [] } });

    // Zoomed out: where the fleet is, weighted up by risk.
    map.addLayer({
      id: "fleet-heat", type: "heatmap", source: "fleet", maxzoom: CARS_FROM_ZOOM + 1,
      paint: {
        // Density is blue; only real risk pushes it to orange/red (weight 0.2 per car, +1 at risk 100).
        "heatmap-weight": ["+", 0.2, ["/", ["get", "risk"], 100]],
        "heatmap-radius": ["interpolate", ["linear"], ["zoom"], 9, 5, 11, 10, 13, 18],
        "heatmap-intensity": ["interpolate", ["linear"], ["zoom"], 9, 0.5, 13, 1.5],
        "heatmap-opacity": ["interpolate", ["linear"], ["zoom"], CARS_FROM_ZOOM, 0.8, CARS_FROM_ZOOM + 1, 0],
        "heatmap-color": ["interpolate", ["linear"], ["heatmap-density"],
          0, "rgba(37,99,235,0)", 0.15, "rgba(96,165,250,0.35)", 0.45, "rgba(37,99,235,0.55)",
          0.7, "rgba(30,64,175,0.7)", 0.85, "rgba(224,112,31,0.8)", 1, "rgba(192,38,45,0.9)"],
      },
    });
    // Zoomed in: parked vehicles as quiet grey dots...
    map.addLayer({
      id: "fleet-parked", type: "circle", source: "fleet", minzoom: CARS_FROM_ZOOM,
      filter: ["all", ["!", ["get", "moving"]], ["==", ["get", "risk"], 0]],
      paint: { "circle-radius": ["interpolate", ["linear"], ["zoom"], 10, 1.5, 13, 2.5, 16, 4], "circle-color": COLORS.parked,
        // faint when zoomed out so moving and risky cars stand out; clearer as you zoom in
        "circle-opacity": ["interpolate", ["linear"], ["zoom"], 10, 0.3, 14, 0.75], "circle-stroke-width": 0 },
    });
    // ...a soft ring under clearly risky vehicles (40+), so they stand out even when parked...
    map.addLayer({
      id: "fleet-risk-ring", type: "circle", source: "fleet", minzoom: CARS_FROM_ZOOM, filter: [">=", ["get", "risk"], 40],
      paint: { "circle-radius": ["interpolate", ["linear"], ["zoom"], 10, 5, 12, 7, 16, 13],
        "circle-color": ["step", ["get", "risk"], COLORS.low, 40, COLORS.mid, 70, COLORS.high],
        "circle-opacity": 0.12, "circle-stroke-width": 1.5, "circle-stroke-opacity": 0.8,
        "circle-stroke-color": ["step", ["get", "risk"], COLORS.low, 40, COLORS.mid, 70, COLORS.high] },
    });
    // ...and cars (moving or at risk), rotated to their direction of travel.
    map.addLayer({
      id: "fleet-cars", type: "symbol", source: "fleet", minzoom: CARS_FROM_ZOOM,
      filter: ["any", ["get", "moving"], [">", ["get", "risk"], 0]],
      layout: {
        "icon-image": ["concat", "car-", ["get", "color"]],
        "icon-size": ["interpolate", ["linear"], ["zoom"], 10, 0.45, 12, 0.6, 15, 0.9, 18, 1.2],
        "icon-rotate": ["get", "heading"], "icon-rotation-alignment": "map",
        "icon-allow-overlap": true, "icon-ignore-placement": true,
        "symbol-sort-key": ["get", "risk"],                     // risky cars drawn on top
      },
    });
    this.loop = this.loop.bind(this);
    this.raf = requestAnimationFrame(this.loop);
  }

  static readonly clickableLayers = ["fleet-cars", "fleet-risk-ring", "fleet-parked"];

  setFilter(f: Filter) {
    this.filter = f;
    this.render(performance.now());
  }

  /** New positions from the server (everything currently in the map view). */
  update(points: Live[]) {
    const now = performance.now();
    const seen = new Set<string>();
    for (const p of points) {
      seen.add(p.id);
      const t = this.tracks.get(p.id);
      if (!t) {
        this.tracks.set(p.id, { fromLat: p.lat, fromLon: p.lon, toLat: p.lat, toLon: p.lon, t0: now,
          heading: 0, moving: false, risk: p.risk });
        continue;
      }
      const [curLat, curLon] = this.at(t, now);
      const moved = metres(t.toLat, t.toLon, p.lat, p.lon);
      t.moving = moved > MOVE_EPS_M;
      if (t.moving) t.heading = bearing(t.toLat, t.toLon, p.lat, p.lon);
      Object.assign(t, { fromLat: curLat, fromLon: curLon, toLat: p.lat, toLon: p.lon, t0: now, risk: p.risk });
    }
    for (const id of this.tracks.keys()) if (!seen.has(id)) this.tracks.delete(id);   // left the view
    this.render(now);
  }

  destroy() {
    cancelAnimationFrame(this.raf);
  }

  private at(t: Track, now: number): [number, number] {
    const k = Math.min(1, (now - t.t0) / GLIDE_MS);
    const ease = k * (2 - k);                                   // ease-out: arrives smoothly
    return [t.fromLat + (t.toLat - t.fromLat) * ease, t.fromLon + (t.toLon - t.fromLon) * ease];
  }

  private loop(now: number) {
    this.raf = requestAnimationFrame(this.loop);
    // Only animate when cars are visible; zoomed out, the heatmap needs no per-frame updates.
    if (this.map.getZoom() < CARS_FROM_ZOOM || now - this.lastFrame < FRAME_MS) return;
    this.lastFrame = now;
    this.render(now);
  }

  private render(now: number) {
    const features: GeoJSON.Feature[] = [];
    for (const [id, t] of this.tracks) {
      if (this.filter === "moving" && !t.moving) continue;
      if (this.filter === "risk" && t.risk <= 0) continue;
      const [lat, lon] = this.at(t, now);
      features.push({ type: "Feature", geometry: { type: "Point", coordinates: [lon, lat] },
        properties: { id, risk: t.risk, heading: t.heading, moving: t.moving, color: colorName(t.risk) } });
    }
    (this.map.getSource("fleet") as maplibregl.GeoJSONSource | undefined)?.setData({ type: "FeatureCollection", features });
  }

  counts() {
    let moving = 0, risk = 0;
    for (const t of this.tracks.values()) {
      if (t.moving) moving++;
      if (t.risk > 0) risk++;
    }
    return { total: this.tracks.size, moving, risk };
  }
}
