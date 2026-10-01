// 3D view of the ingestion pipeline: three OEM sources fire events (particles) through gateway -> Kafka ->
// parse -> dedup -> reorder -> noise -> detectors. Particles keep their OEM colour until the parser, then all
// turn the canonical colour: three formats in, one out. Rates and exit ratios come from live counters.
import * as THREE from "three";
import { OrbitControls } from "three/examples/jsm/controls/OrbitControls.js";
import { CSS2DObject, CSS2DRenderer } from "three/examples/jsm/renderers/CSS2DRenderer.js";

export type Oem = "A" | "B" | "C";
export type StageId = "sources" | "gateway" | "kafka" | "parse" | "dedup" | "reorder" | "noise" | "detectors";
/** Per-second rates the scene animates, derived from the pipeline counters. */
export type Rates = {
  in: Record<Oem, number>;
  dead: Record<Oem, number>;
  dup: Record<Oem, number>;
  late: number;
  flagged: number;
  normalized: number;
};
export type StageInfo = { id: StageId; title: string; value: string };

const OEM_COLOR: Record<Oem, number> = { A: 0x22d3ee, B: 0xa78bfa, C: 0xfbbf24 };
const CANON = 0x60a5fa;
const FLAG = 0xfb923c;
const DROP = 0xf87171;
const MAX_PARTICLES = 1400;
const MAX_SPAWN_PER_S = 70;          // visual cap: the real stream is thousands per second
const SPEED = 7;                     // scene units per second

// Main line along x; exit bins hang below their stage.
const P = {
  A: new THREE.Vector3(-17, 0, -4.5), B: new THREE.Vector3(-17, 0, 0), C: new THREE.Vector3(-17, 0, 4.5),
  gateway: new THREE.Vector3(-11, 0, 0), kafkaIn: new THREE.Vector3(-7.5, 0, 0), kafkaOut: new THREE.Vector3(-2.5, 0, 0),
  parse: new THREE.Vector3(1, 0, 0), dedup: new THREE.Vector3(5, 0, 0), reorder: new THREE.Vector3(9, 0, 0),
  noise: new THREE.Vector3(13, 0, 0), detectors: new THREE.Vector3(17.5, 0, 0),
};
const BIN_Y = -4.2;
const binOf = (v: THREE.Vector3) => new THREE.Vector3(v.x, BIN_Y, v.z + 0.01);

type Outcome = "dead" | "dup" | "late" | "clean" | "flagged";
type Particle = { alive: boolean; path: THREE.Vector3[]; seg: number; t: number; oem: Oem; outcome: Outcome; colorAt: number };

export class PipelineScene {
  private renderer: THREE.WebGLRenderer;
  private labels: CSS2DRenderer;
  private scene = new THREE.Scene();
  private camera: THREE.PerspectiveCamera;
  private controls: OrbitControls;
  private mesh: THREE.InstancedMesh;
  private particles: Particle[] = [];
  private rates: Rates | null = null;
  private spawnDebt: Record<Oem, number> = { A: 0, B: 0, C: 0 };
  private raf = 0;
  private last = performance.now();
  private valueEls = new Map<StageId, HTMLElement>();
  private pickables: THREE.Object3D[] = [];
  private raycaster = new THREE.Raycaster();
  private tmp = new THREE.Object3D();
  private color = new THREE.Color();
  private ro: ResizeObserver;

  constructor(private host: HTMLElement, private onPick: (s: StageId) => void, dark: boolean) {
    const w = host.clientWidth, h = host.clientHeight;
    this.renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true });
    this.renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
    this.renderer.setSize(w, h);
    host.appendChild(this.renderer.domElement);
    this.labels = new CSS2DRenderer();
    this.labels.setSize(w, h);
    this.labels.domElement.className = "p3d-labels";
    host.appendChild(this.labels.domElement);

    this.camera = new THREE.PerspectiveCamera(42, w / h, 0.1, 500);
    this.camera.position.set(-4, 17, 30);
    this.controls = new OrbitControls(this.camera, this.labels.domElement);
    this.controls.target.set(0, -1, 0);
    this.controls.enableDamping = true;
    this.controls.maxPolarAngle = Math.PI * 0.49;
    this.controls.minDistance = 12;
    this.controls.maxDistance = 70;

    this.scene.add(new THREE.AmbientLight(0xffffff, dark ? 0.55 : 0.9));
    const sun = new THREE.DirectionalLight(0xffffff, dark ? 1.1 : 1.4);
    sun.position.set(-10, 20, 12);
    this.scene.add(sun);
    const grid = new THREE.GridHelper(64, 32, dark ? 0x243049 : 0xd6dbe4, dark ? 0x172033 : 0xe8ebf0);
    grid.position.y = -5.5;
    this.scene.add(grid);

    this.buildStages(dark);
    this.mesh = new THREE.InstancedMesh(new THREE.SphereGeometry(0.16, 10, 8),
      new THREE.MeshBasicMaterial({ toneMapped: false }), MAX_PARTICLES);
    this.mesh.instanceMatrix.setUsage(THREE.DynamicDrawUsage);
    this.mesh.count = 0;
    this.scene.add(this.mesh);
    for (let i = 0; i < MAX_PARTICLES; i++) {
      this.particles.push({ alive: false, path: [], seg: 0, t: 0, oem: "A", outcome: "clean", colorAt: 0 });
    }

    this.labels.domElement.addEventListener("click", this.pick);
    this.ro = new ResizeObserver(() => this.resize());
    this.ro.observe(host);
    this.loop = this.loop.bind(this);
    this.raf = requestAnimationFrame(this.loop);
  }

  setRates(r: Rates) { this.rates = r; }

  setStageValues(values: StageInfo[]) {
    for (const v of values) {
      const el = this.valueEls.get(v.id);
      if (el) el.textContent = v.value;
    }
  }

  destroy() {
    cancelAnimationFrame(this.raf);
    this.ro.disconnect();
    this.controls.dispose();
    this.renderer.dispose();
    this.host.innerHTML = "";
  }

  // ---------------------------------------------------------------- scene objects
  private buildStages(dark: boolean) {
    const body = dark ? 0x1b2436 : 0xffffff;
    const edge = dark ? 0x3b4a66 : 0xc9d1dd;
    const box = (pos: THREE.Vector3, size: [number, number, number], color: number, id: StageId, emissive = 0) => {
      const m = new THREE.Mesh(new THREE.BoxGeometry(...size),
        new THREE.MeshStandardMaterial({ color, emissive, emissiveIntensity: emissive ? 0.35 : 0, roughness: 0.5, metalness: 0.1 }));
      m.position.copy(pos);
      m.userData.stage = id;
      const lines = new THREE.LineSegments(new THREE.EdgesGeometry(m.geometry), new THREE.LineBasicMaterial({ color: edge }));
      m.add(lines);
      this.scene.add(m);
      this.pickables.push(m);
      return m;
    };
    for (const o of ["A", "B", "C"] as Oem[]) {
      box(P[o], [2.2, 1.6, 2.2], OEM_COLOR[o], "sources", OEM_COLOR[o]);
      this.label(P[o], `OEM ${o}`, o === "A" ? "JSON · km/h · ISO UTC" : o === "B" ? "pipe text · mph · epoch ms" : "nested JSON · m/s · hex status", "sources", 1.4);
    }
    box(P.gateway, [2, 2.2, 3], body, "gateway");
    this.label(P.gateway, "Gateway", "POST /ingest", "gateway", 1.8);

    // Kafka: a translucent tube with 12 partition rings.
    const tube = new THREE.Mesh(new THREE.CylinderGeometry(1.3, 1.3, P.kafkaOut.x - P.kafkaIn.x, 32, 1, true),
      new THREE.MeshStandardMaterial({ color: 0x34d399, transparent: true, opacity: dark ? 0.22 : 0.28, side: THREE.DoubleSide }));
    tube.rotation.z = Math.PI / 2;
    tube.position.set((P.kafkaIn.x + P.kafkaOut.x) / 2, 0, 0);
    tube.userData.stage = "kafka";
    this.scene.add(tube);
    this.pickables.push(tube);
    for (let i = 0; i < 12; i++) {
      const ring = new THREE.Mesh(new THREE.TorusGeometry(1.32, 0.04, 6, 32), new THREE.MeshBasicMaterial({ color: 0x34d399 }));
      ring.rotation.y = Math.PI / 2;
      ring.position.set(P.kafkaIn.x + ((i + 0.5) * (P.kafkaOut.x - P.kafkaIn.x)) / 12, 0, 0);
      this.scene.add(ring);
    }
    this.label(tube.position, "Redpanda", "raw.telemetry · 12 partitions", "kafka", 1.9);

    const stages: ["parse" | "dedup" | "reorder" | "noise", string, string][] = [
      ["parse", "Parse", "regex → canonical"], ["dedup", "Dedup", "RedisBloom"],
      ["reorder", "Reorder", "2-min watermark"], ["noise", "Noise filter", "GPS jump check"],
    ];
    for (const [id, t, s] of stages) {
      box(P[id], [2, 2, 2], body, id);
      this.label(P[id], t, s, id, 1.7);
    }
    box(P.detectors, [2.4, 2.4, 2.4], 0x2563eb, "detectors", 0x2563eb);
    this.label(P.detectors, "Detectors", "tow · geofence · convoy", "detectors", 1.9);

    const bins: [THREE.Vector3, string, number][] = [
      [binOf(P.parse), "dead.letter", DROP], [binOf(P.dedup), "duplicates dropped", 0x94a3b8], [binOf(P.reorder), "late.events → batch", FLAG],
    ];
    for (const [pos, text, color] of bins) {
      const bin = new THREE.Mesh(new THREE.CylinderGeometry(0.9, 0.7, 0.8, 24),
        new THREE.MeshStandardMaterial({ color, transparent: true, opacity: 0.55 }));
      bin.position.copy(pos);
      this.scene.add(bin);
      const el = document.createElement("div");
      el.className = "p3d-bin";
      el.textContent = text;
      const o = new CSS2DObject(el);
      o.position.set(pos.x, pos.y - 1, pos.z);
      this.scene.add(o);
    }
    // faint rails showing the path
    const rail = (a: THREE.Vector3, b: THREE.Vector3) => {
      const g = new THREE.BufferGeometry().setFromPoints([a, b]);
      this.scene.add(new THREE.Line(g, new THREE.LineBasicMaterial({ color: edge, transparent: true, opacity: 0.6 })));
    };
    for (const o of ["A", "B", "C"] as Oem[]) rail(P[o], P.gateway);
    rail(P.gateway, P.detectors);
    for (const s of [P.parse, P.dedup, P.reorder]) rail(s, binOf(s));
  }

  private label(pos: THREE.Vector3, title: string, sub: string, id: StageId, lift: number) {
    const el = document.createElement("div");
    el.className = "p3d-label";
    el.dataset.stage = id;
    el.innerHTML = `<b></b><span></span><em></em>`;
    (el.children[0] as HTMLElement).textContent = title;
    (el.children[1] as HTMLElement).textContent = sub;
    const value = el.children[2] as HTMLElement;
    if (!this.valueEls.has(id)) this.valueEls.set(id, value);
    const o = new CSS2DObject(el);
    o.position.set(pos.x, pos.y + lift, pos.z);
    this.scene.add(o);
  }

  // ---------------------------------------------------------------- particles
  private spawn(oem: Oem) {
    const p = this.particles.find((x) => !x.alive);
    if (!p || !this.rates) return;
    const r = this.rates;
    const inRate = Math.max(r.in[oem], 1e-6);
    const roll = Math.random();
    const deadP = r.dead[oem] / inRate;
    const dupP = r.dup[oem] / inRate;
    const clean = Math.max(r.normalized, 1e-6);
    const lateP = (r.late / clean) * (1 - deadP - dupP);
    let outcome: Outcome = "clean";
    if (roll < deadP) outcome = "dead";
    else if (roll < deadP + dupP) outcome = "dup";
    else if (roll < deadP + dupP + lateP) outcome = "late";
    else if (Math.random() < r.flagged / clean) outcome = "flagged";

    const jitter = () => new THREE.Vector3(0, (Math.random() - 0.5) * 0.9, (Math.random() - 0.5) * 0.9);
    const main = [P[oem].clone(), P.gateway.clone().add(jitter()), P.kafkaIn.clone().add(jitter()), P.kafkaOut.clone().add(jitter()), P.parse.clone()];
    const path =
      outcome === "dead" ? [...main, binOf(P.parse)] :
      outcome === "dup" ? [...main, P.dedup.clone(), binOf(P.dedup)] :
      outcome === "late" ? [...main, P.dedup.clone(), P.reorder.clone(), binOf(P.reorder)] :
      [...main, P.dedup.clone(), P.reorder.clone(), P.noise.clone(), P.detectors.clone()];
    Object.assign(p, { alive: true, path, seg: 0, t: 0, oem, outcome, colorAt: 4 });
  }

  private loop(now: number) {
    this.raf = requestAnimationFrame(this.loop);
    const dt = Math.min(0.05, (now - this.last) / 1000);
    this.last = now;

    if (this.rates) {
      const total = this.rates.in.A + this.rates.in.B + this.rates.in.C;
      const scale = total > 0 ? Math.min(1, MAX_SPAWN_PER_S / total) : 0;
      for (const o of ["A", "B", "C"] as Oem[]) {
        this.spawnDebt[o] += this.rates.in[o] * scale * dt;
        while (this.spawnDebt[o] >= 1) { this.spawn(o); this.spawnDebt[o] -= 1; }
      }
    }

    let n = 0;
    for (const p of this.particles) {
      if (!p.alive) continue;
      const a = p.path[p.seg], b = p.path[p.seg + 1];
      const len = a.distanceTo(b) || 0.001;
      p.t += (SPEED * dt) / len;
      while (p.t >= 1 && p.alive) {
        p.t -= 1;
        p.seg += 1;
        if (p.seg >= p.path.length - 1) p.alive = false;
      }
      if (!p.alive) continue;
      const pa = p.path[p.seg], pb = p.path[p.seg + 1];
      this.tmp.position.lerpVectors(pa, pb, p.t);
      this.tmp.updateMatrix();
      this.mesh.setMatrixAt(n, this.tmp.matrix);
      // OEM colour until the parser (segment 4), then canonical; exits take their bin colour.
      let c: number = p.seg < p.colorAt ? OEM_COLOR[p.oem] : CANON;
      if (p.outcome === "dead" && p.seg >= 4) c = DROP;
      if (p.outcome === "dup" && p.seg >= 5) c = 0x94a3b8;
      if (p.outcome === "late" && p.seg >= 6) c = FLAG;
      if (p.outcome === "flagged" && p.seg >= 7) c = FLAG;
      this.mesh.setColorAt(n, this.color.setHex(c));
      n++;
    }
    this.mesh.count = n;
    this.mesh.instanceMatrix.needsUpdate = true;
    if (this.mesh.instanceColor) this.mesh.instanceColor.needsUpdate = true;

    this.controls.update();
    this.renderer.render(this.scene, this.camera);
    this.labels.render(this.scene, this.camera);
  }

  private pick = (ev: MouseEvent) => {
    const label = (ev.target as HTMLElement).closest?.(".p3d-label") as HTMLElement | null;
    if (label?.dataset.stage) { this.onPick(label.dataset.stage as StageId); return; }
    const rect = this.renderer.domElement.getBoundingClientRect();
    const ndc = new THREE.Vector2(((ev.clientX - rect.left) / rect.width) * 2 - 1, -((ev.clientY - rect.top) / rect.height) * 2 + 1);
    this.raycaster.setFromCamera(ndc, this.camera);
    const hit = this.raycaster.intersectObjects(this.pickables, false)[0];
    if (hit) this.onPick(hit.object.userData.stage as StageId);
  };

  private resize() {
    const w = this.host.clientWidth, h = this.host.clientHeight;
    if (!w || !h) return;
    this.camera.aspect = w / h;
    this.camera.updateProjectionMatrix();
    this.renderer.setSize(w, h);
    this.labels.setSize(w, h);
  }
}
