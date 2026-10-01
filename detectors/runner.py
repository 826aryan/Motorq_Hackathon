"""Kafka loop for the detectors: normalized.events -> signals -> Redis (live state) + PostgreSQL (alerts, cases).

Per batch:
  Redis   GEOADD pos:live (latest position), HSET live:state (position + speed + heading, for convoy),
          ZADD risk:top (scores), PUBLISH alerts (new alerts)
  Postgres alert rows (one per signal, cooldown-limited), recovery_case + case_alert when score > 70
Every 5 min one replica (holder of lock:convoy) runs the fleet-wide convoy detector on live:state and
publishes CONVOY signals to detector.signals, keyed by vehicle, so each lands on the replica that owns
that vehicle and is scored there. Counters go to the Redis hash stats:detectors.
"""
import json
import logging
import os
import socket
import time
from pathlib import Path

import psycopg
import redis
import yaml
from confluent_kafka import Consumer, Producer

from detectors.engine import DetectorEngine, EngineState, Scored
from shared.config import settings
from shared.detect.anomaly import Baseline
from shared.detect.convoy import ConvoyDetector, LiveState
from shared.detect.risk import Loan
from shared.detect.signals import CONVOY, Signal
from shared.roadgraph import RoadGraph
from shared.schema import CanonicalEvent

log = logging.getLogger("detectors")
NORMALIZED = "normalized.events"
SIGNALS = "detector.signals"


def load_state(conn) -> tuple[EngineState, dict[str, int]]:
    polygons = {}
    for fid, geojson in conn.execute("SELECT geofence_id, ST_AsGeoJSON(area) FROM geofence"):
        ring = json.loads(geojson)["coordinates"][0][:-1]          # GeoJSON is [lon, lat], closed ring
        polygons[fid] = [(lat, lon) for lon, lat in ring]
    fences: dict[str, list[int]] = {}
    for vid, fid in conn.execute("SELECT vehicle_id, geofence_id FROM vehicle_geofence"):
        fences.setdefault(vid, []).append(fid)
    loans = {vid: Loan(lid, lender, dpd) for vid, lid, lender, dpd in conn.execute(
        "SELECT vehicle_id, loan_id, lender_id, days_past_due FROM loan WHERE status <> 'closed'")}
    baselines, destinations = {}, {}
    for vid, hours, avg, std, home, work in conn.execute(
            "SELECT vehicle_id, active_hours, avg_speed, std_speed, home_cell, work_cell FROM vehicle_baseline"):
        baselines[vid] = Baseline((hours.lower, hours.upper) if hours else None, avg, std)
        destinations[vid] = [c for c in (home, work) if c]
    open_cases = {vid for (vid,) in conn.execute(
        "SELECT l.vehicle_id FROM recovery_case c JOIN loan l USING (loan_id) WHERE c.status IN ('open', 'in_progress')")}
    signal_ids = dict(conn.execute("SELECT code, signal_type_id FROM signal_type").fetchall())
    return EngineState(polygons, fences, loans, baselines, open_cases, destinations), signal_ids


class Runner:
    def __init__(self, config_path: str = "detectors/config.yaml"):
        self.cfg = yaml.safe_load(Path(config_path).read_text())
        self.pg = psycopg.connect(settings.postgres_dsn, autocommit=False)
        state, self.signal_ids = load_state(self.pg)
        r = self.cfg["route"]
        graph = None
        if Path(r["graph_path"]).exists():
            graph = RoadGraph.load(r["graph_path"], r["traffic_speed_factor"])
        else:
            log.warning("no road graph at %s: route deviation disabled", r["graph_path"])
        self.engine = DetectorEngine(self.cfg, state, graph)
        self.convoy = ConvoyDetector(self.cfg["convoy"])
        self.convoy_members: set[str] = set()      # vehicles already reported in a current convoy
        self.replica_id = f"{socket.gethostname()}:{os.getpid()}"   # identifies the convoy leader
        self.loans = state.loans
        self.redis = redis.Redis.from_url(settings.redis_url)
        k = self.cfg["kafka"]
        self.consumer = Consumer({"bootstrap.servers": settings.kafka_bootstrap, "group.id": k["group_id"],
                                  "auto.offset.reset": "earliest", "enable.auto.commit": True})
        self.producer = Producer({"bootstrap.servers": settings.kafka_bootstrap, "linger.ms": 20})
        self.counts: dict[str, int] = {}
        self.running = True
        log.info("loaded %d geofences, %d vehicles with loans, %d baselines",
                 len(state.polygons), len(state.loans), len(state.baselines))

    def _count(self, name: str, n: int = 1) -> None:
        self.counts[name] = self.counts.get(name, 0) + n

    def _handle(self, events: list[CanonicalEvent], scored: list[Scored]) -> None:
        r = self.redis.pipeline(transaction=False)
        for e in events:
            if "suspect_gps" not in e.flags:
                r.geoadd("pos:live", (e.lon, e.lat, e.vehicle_id))
                loan = self.loans.get(e.vehicle_id)
                if loan:                                   # per-lender set: the map queries only its own fleet
                    r.geoadd(f"pos:live:{loan.lender_id}", (e.lon, e.lat, e.vehicle_id))
                    r.zadd(f"pos:seen:{loan.lender_id}", {e.vehicle_id: e.device_ts})   # "Fleet live" per lender
                r.zadd("pos:seen", {e.vehicle_id: e.device_ts})
                r.hset("live:state", e.vehicle_id,
                       f"{e.lat:.6f},{e.lon:.6f},{e.speed_kmh:.1f},{e.heading_deg:.0f},{e.geohash},{e.device_ts:.0f}")
        top = self.cfg["risk"]["top_key"]
        for s in scored:
            r.zadd(top, {s.signal.vehicle_id: s.decision.score})
            self._count(f"signal.{s.signal.code}")

        to_write = [s for s in scored if s.decision.write_alert]
        with self.pg.cursor() as cur:
            for s in to_write:
                sig, d = s.signal, s.decision
                alert_id = cur.execute(
                    "INSERT INTO alert (created_at, lat, lon, risk_score, vehicle_id, signal_type_id) "
                    "VALUES (to_timestamp(%s), %s, %s, %s, %s, %s) RETURNING alert_id",
                    (sig.ts, sig.lat, sig.lon, d.score, sig.vehicle_id, self.signal_ids[sig.code])).fetchone()[0]
                case_id = None
                loan = self.loans.get(sig.vehicle_id)
                if d.open_case and loan:
                    case_id = cur.execute("INSERT INTO recovery_case (status, loan_id) VALUES ('open', %s) "
                                          "RETURNING case_id", (loan.loan_id,)).fetchone()[0]
                    cur.execute("INSERT INTO case_alert (case_id, alert_id) VALUES (%s, %s)", (case_id, alert_id))
                    self._count("cases_opened")
                self._count("alerts_written")
                r.publish("alerts", json.dumps({
                    "alert_id": alert_id, "vehicle_id": sig.vehicle_id, "code": sig.code, "ts": sig.ts,
                    "lat": sig.lat, "lon": sig.lon, "risk_score": d.score, "case_id": case_id,
                    "lender_id": loan.lender_id if loan else None, "detail": sig.detail}))
        self.pg.commit()
        r.execute()

    def _run_convoy(self) -> None:
        """Fleet-wide, so only one replica runs it (Redis leader lock).

        The lock is sticky: the leader renews it every run, so the same replica keeps the convoy streaks
        (they live in its memory). Others take over only if the leader stops renewing for 3 periods.
        """
        c = self.cfg["convoy"]
        ttl = c["every_s"] * 3
        if not self.redis.set("lock:convoy", self.replica_id, nx=True, ex=ttl):
            if self.redis.get("lock:convoy") != self.replica_id.encode():
                return                             # another replica is the convoy leader
            self.redis.expire("lock:convoy", ttl)
        states = []
        for vid, raw in self.redis.hgetall("live:state").items():
            lat, lon, speed, heading, gh, ts = raw.decode().split(",")
            states.append((float(ts), LiveState(vid.decode(), float(lat), float(lon),
                                                float(speed), float(heading), gh)))
        if not states:
            return
        newest = max(ts for ts, _ in states)
        groups = self.convoy.run([s for ts, s in states if newest - ts <= c["fresh_s"]])
        by_id = {s.vehicle_id: s for _, s in states}
        in_groups = set().union(*groups) if groups else set()
        self.convoy_members &= in_groups           # members of convoys that broke up can be reported again
        with self.pg.cursor() as cur:
            for g in groups:
                if g <= self.convoy_members:
                    continue                       # this convoy was already reported
                convoy_id = cur.execute("INSERT INTO convoy (detected_at) VALUES (to_timestamp(%s)) "
                                        "RETURNING convoy_id", (newest,)).fetchone()[0]
                for vid in sorted(g):
                    cur.execute("INSERT INTO convoy_member (convoy_id, vehicle_id) VALUES (%s, %s)", (convoy_id, vid))
                    s = by_id[vid]
                    sig = Signal(vid, CONVOY, newest, s.lat, s.lon, detail={"convoy_id": convoy_id, "size": len(g)})
                    self.producer.produce(SIGNALS, key=vid, value=json.dumps(sig.__dict__))
                self.convoy_members |= g
                self._count("convoys_detected")
                log.info("convoy %d: %s", convoy_id, sorted(g))
        self.pg.commit()
        self.producer.flush(10)
        log.info("convoy run: %d vehicles, %d pairs compared, %d edges tracked, %d convoys",
                 len(states), self.convoy.pairs_compared, len(self.convoy.edges), len(groups))

    def _rescore(self) -> None:
        scores = self.engine.risk.rescore_all(self.engine.anomaly.clock)
        top = self.cfg["risk"]["top_key"]
        pipe = self.redis.pipeline(transaction=False)
        for vid, score in scores.items():
            if score > 0:
                pipe.zadd(top, {vid: score})
            else:
                pipe.zrem(top, vid)
        pipe.execute()

    def _prune_map_sets(self) -> None:
        """Vehicles silent for longer than map_stale_s leave the per-lender map sets (so the map only shows
        vehicles that are really reporting). Their last known position stays in pos:live for recovery."""
        cutoff = self.engine.anomaly.clock - self.cfg["map_stale_s"]
        stale = self.redis.zrangebyscore("pos:seen", "-inf", cutoff)
        if not stale:
            return
        pipe = self.redis.pipeline(transaction=False)
        for lender in {loan.lender_id for loan in self.loans.values()}:
            for i in range(0, len(stale), 5000):
                pipe.zrem(f"pos:live:{lender}", *stale[i:i + 5000])
        pipe.execute()
        self._count("map_pruned", len(stale))

    def _push_stats(self) -> None:
        if self.counts:
            pipe = self.redis.pipeline(transaction=False)
            for k, v in self.counts.items():
                pipe.hincrby("stats:detectors", k, v)
            pipe.execute()
            self.counts = {}

    def run(self) -> None:
        k = self.cfg["kafka"]
        self.consumer.subscribe([NORMALIZED, SIGNALS])
        last_tick = last_rescore = last_convoy = time.monotonic()
        log.info("detectors consuming %s, %s", NORMALIZED, SIGNALS)
        while self.running:
            msgs = self.consumer.consume(k["batch_size"], timeout=k["poll_timeout_s"])
            events, scored = [], []
            for m in msgs:
                if m.error():
                    continue
                if m.topic() == SIGNALS:
                    scored += self.engine.score([Signal(**json.loads(m.value()))])
                    continue
                e = CanonicalEvent.from_json(m.value())
                events.append(e)
                scored += self.engine.on_event(e)
            now = time.monotonic()
            if now - last_tick >= 1:
                scored += self.engine.tick()
                last_tick = now
            self._count("events", len(events))
            if events or scored:
                self._handle(events, scored)
            if now - last_convoy >= self.cfg["convoy"]["every_s"]:
                self._run_convoy()
                last_convoy = now
            if now - last_rescore >= self.cfg["risk"]["rescore_every_s"]:
                self._rescore()
                self._prune_map_sets()
                self._push_stats()
                last_rescore = now
        self.consumer.close()
        self.pg.close()
