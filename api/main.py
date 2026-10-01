"""API service (SPEC §7): REST + WebSocket in one FastAPI app. Every lender-facing query is filtered to the
lender in the caller's JWT. Partners use /share/insights with an API key and only get privacy-safe data."""
import asyncio
import contextlib
import glob
import json
import math
import os
import re
import time
import zlib
from datetime import UTC, datetime, timedelta
from pathlib import Path

import duckdb
import httpx
import redis
import redis.asyncio as aioredis
import yaml
from confluent_kafka import Consumer, TopicPartition
from fastapi import Depends, FastAPI, Header, HTTPException, Query, WebSocket, WebSocketDisconnect
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool
from pydantic import BaseModel

from api.auth import check_password, current_lender, decode_token, issue_token
from api.routing import Router
from shared import privacy
from shared.config import settings
from shared.health import run_checks
from shared.topk import top_k

CFG = yaml.safe_load(Path("api/config.yaml").read_text())
DET = yaml.safe_load(Path("detectors/config.yaml").read_text())
WEIGHTS: dict[str, float] = DET["risk"]["weights"]

pg = ConnectionPool(settings.postgres_dsn, min_size=1, max_size=8, kwargs={"row_factory": dict_row}, open=True)
ts_db = ConnectionPool(settings.timescale_dsn, min_size=1, max_size=4, kwargs={"row_factory": dict_row}, open=True)
r = redis.Redis.from_url(settings.redis_url, decode_responses=True)
router = Router(CFG["graph_path"], CFG["traffic_speed_factor"])
app = FastAPI(title="Asset Recovery API")


def _load_ownership() -> tuple[dict[str, int], dict[int, str]]:
    with pg.connection() as c:
        owner = {row["vehicle_id"]: row["lender_id"] for row in c.execute(
            "SELECT vehicle_id, lender_id FROM loan WHERE status <> 'closed'")}
        lenders = {row["lender_id"]: row["name"] for row in c.execute("SELECT lender_id, name FROM lender")}
    return owner, lenders


OWNER, LENDERS = _load_ownership()
SIM_URL = CFG["service_stats"]["simulator"].removesuffix("/stats")
VEHICLE_ID = re.compile(r"VH-\d{6}")         # masked in dead-letter samples on the Pipeline screen


def _place_agents() -> None:
    """Simulated field agents get a starting spot on the road graph (once)."""
    if r.exists("agents:live"):
        return
    with pg.connection() as c:
        ids = [row["agent_id"] for row in c.execute("SELECT agent_id FROM agent ORDER BY agent_id")]
    spots = router.random_positions(len(ids), seed=7, center=tuple(CFG["center"]), radius_m=CFG["agent_radius_m"])
    for aid, (lat, lon) in zip(ids, spots, strict=True):
        r.geoadd("agents:live", (lon, lat, str(aid)))


_place_agents()


def own(vehicle_id: str, lender_id: int) -> None:
    if OWNER.get(vehicle_id) != lender_id:
        raise HTTPException(404, "vehicle not found")        # same answer as "doesn't exist": no probing


def log_access(lender_id: int, vehicle_id: str, endpoint: str) -> None:
    with pg.connection() as c:
        c.execute("INSERT INTO location_access_log (lender_id, vehicle_id, endpoint) VALUES (%s, %s, %s)",
                  (lender_id, vehicle_id, endpoint))


def live_position(vehicle_id: str) -> tuple[float, float] | None:
    pos = r.geopos("pos:live", vehicle_id)[0]
    return (pos[1], pos[0]) if pos else None


# ---------------------------------------------------------------- auth + health
class Login(BaseModel):
    username: str
    password: str


@app.post("/auth/login")
def login(body: Login):
    match = [(lid, name) for lid, name in LENDERS.items() if name.split()[-1].lower() == body.username.lower()]
    if not match or not check_password(body.password):
        raise HTTPException(401, "wrong username or password")
    lid, name = match[0]
    return {"token": issue_token(lid, name), "lender_id": lid, "lender": name}


@app.get("/health")
def health():
    return run_checks(["postgres", "timescale", "redis"])


# ---------------------------------------------------------------- vehicles
@app.get("/geofences")
def geofences(lender: int = Depends(current_lender)):
    with pg.connection() as c:
        rows = c.execute("SELECT geofence_id, name, ST_AsGeoJSON(area) AS area FROM geofence WHERE lender_id = %s",
                         (lender,)).fetchall()
    return {"geofences": [{**row, "area": json.loads(row["area"])} for row in rows]}


@app.get("/vehicles/live")
def vehicles_live(bbox: str = Query(..., description="minLon,minLat,maxLon,maxLat"),
                  lender: int = Depends(current_lender)):
    return {"vehicles": positions_in_bbox([float(x) for x in bbox.split(",")], lender)}


def event_now() -> float:
    """"Now" in device time: the newest position any vehicle reported (fallback: wall clock).

    Device clocks can run ahead of the wall clock (simulator demo mode), so freshness and decay must be measured
    against event time, the same clock the detectors use.
    """
    newest = r.zrevrange("pos:seen", 0, 0, withscores=True)
    return float(newest[0][1]) if newest else time.time()


def fleet_live(lender: int) -> int:
    """This lender's vehicles that reported within the last reporting_window_s (event time)."""
    return r.zcount(f"pos:seen:{lender}", event_now() - CFG["reporting_window_s"], "+inf")


def positions_in_bbox(bbox: list[float], lender: int, limit: int | None = None) -> list[dict]:
    min_lon, min_lat, max_lon, max_lat = bbox
    c_lat, c_lon = (min_lat + max_lat) / 2, (min_lon + max_lon) / 2
    width = max(0.1, (max_lon - min_lon) * 111.32 * math.cos(math.radians(c_lat)))
    height = max(0.1, (max_lat - min_lat) * 110.54)
    cap = limit or CFG["max_live_points"]
    # Everything in view from this lender's own set. If that is more than the cap, send an even sample over the
    # whole view (not the nearest N, which draws a dense disc around the centre) plus every vehicle at risk.
    hits = r.geosearch(f"pos:live:{lender}", longitude=c_lon, latitude=c_lat, width=width, height=height,
                       unit="km", withcoord=True)
    if not hits:
        return []
    ids = [vid for vid, _ in hits]
    seen = r.zmscore("pos:seen", ids)
    scores = r.zmscore(DET["risk"]["top_key"], ids)
    fresh_after = event_now() - CFG["reporting_window_s"]
    live = [(vid, lon, lat, s or 0) for (vid, (lon, lat)), ts, s in zip(hits, seen, scores, strict=True)
            if ts is not None and ts >= fresh_after]
    if len(live) > cap:
        # Sampling by a hash of the id keeps the same vehicles on every push (no flicker as the map refreshes).
        step = math.ceil(len(live) / cap)
        live = [v for v in live if v[3] > 0 or zlib.crc32(v[0].encode()) % step == 0]
    return [{"id": vid, "lat": round(lat, 6), "lon": round(lon, 6), "risk": s} for vid, lon, lat, s in live]


@app.get("/vehicles/{vehicle_id}")
def vehicle_detail(vehicle_id: str, lender: int = Depends(current_lender)):
    own(vehicle_id, lender)
    with pg.connection() as c:
        v = c.execute("""
            SELECT v.vehicle_id, v.vin, v.registered_on, vm.make, vm.model, vm.year,
                   l.loan_id, l.amount, l.status AS loan_status, l.days_past_due, l.start_date,
                   o.name AS oem, dm.model_name AS device_model
            FROM vehicle v JOIN vehicle_model vm USING (vehicle_model_id)
            JOIN loan l ON l.vehicle_id = v.vehicle_id AND l.status <> 'closed'
            LEFT JOIN device d ON d.vehicle_id = v.vehicle_id
            LEFT JOIN device_model dm ON dm.model_id = d.model_id LEFT JOIN oem o ON o.oem_id = dm.oem_id
            WHERE v.vehicle_id = %s""", (vehicle_id,)).fetchone()
        baseline = c.execute("SELECT home_cell, work_cell, lower(active_hours) AS active_from, "
                             "upper(active_hours) AS active_to, avg_speed, std_speed FROM vehicle_baseline "
                             "WHERE vehicle_id = %s", (vehicle_id,)).fetchone()
        alerts = c.execute("""
            SELECT a.alert_id, s.code, extract(epoch FROM a.created_at) AS ts, a.lat, a.lon, a.risk_score
            FROM alert a JOIN signal_type s USING (signal_type_id)
            WHERE a.vehicle_id = %s ORDER BY a.created_at DESC LIMIT 50""", (vehicle_id,)).fetchall()
        case = c.execute("SELECT case_id, status, agent_id FROM recovery_case WHERE loan_id = %s "
                         "AND status IN ('open', 'in_progress') ORDER BY case_id DESC LIMIT 1", (v["loan_id"],)).fetchone()
    log_access(lender, vehicle_id, "vehicle_detail")
    score = r.zscore(DET["risk"]["top_key"], vehicle_id) or 0
    return {**v, "amount": float(v["amount"]), "position": live_position(vehicle_id), "risk_score": score,
            "score_breakdown": breakdown(alerts, v["days_past_due"]), "baseline": baseline,
            "alerts": [{**a, "ts": float(a["ts"]), "risk_score": float(a["risk_score"])} for a in alerts],
            "open_case": case}


def breakdown(alerts: list[dict], dpd: int) -> dict:
    """Same formula as the risk scorer, from the latest alert per signal (for the "why is it red" panel)."""
    now = event_now()
    latest: dict[str, float] = {}
    for a in alerts:
        latest.setdefault(a["code"], float(a["ts"]))
    mult = 1 + min(dpd, DET["risk"]["max_days_past_due"]) / DET["risk"]["max_days_past_due"]
    parts = {code: round(WEIGHTS.get(code, 0) * max(0.0, 1 - (now - ts) / DET["risk"]["decay_s"]), 1)
             for code, ts in latest.items()}
    return {"signals": parts, "loan_multiplier": round(mult, 2)}


@app.get("/vehicles/{vehicle_id}/trail")
def trail(vehicle_id: str, frm: float = Query(alias="from"), to: float = Query(...),
          lender: int = Depends(current_lender)):
    own(vehicle_id, lender)
    if to - frm > 7 * 86400:
        raise HTTPException(400, "range too long (max 7 days)")
    points = []
    cutoff = time.time() - CFG["hot_days"] * 86400
    if frm < cutoff:                                        # older than 30 days: Parquet via DuckDB
        points += parquet_trail(vehicle_id, frm, min(to, cutoff))
    if to >= cutoff:
        with ts_db.connection() as c:
            points += [dict(row) for row in c.execute(
                "SELECT extract(epoch FROM device_ts)::float AS ts, lat, lon, speed_kmh, ignition, flags "
                "FROM telemetry WHERE vehicle_id = %s AND device_ts BETWEEN to_timestamp(%s) AND to_timestamp(%s) "
                "ORDER BY device_ts LIMIT 20000", (vehicle_id, max(frm, cutoff), to))]
    log_access(lender, vehicle_id, "trail")
    return {"vehicle_id": vehicle_id, "points": points}


def parquet_trail(vehicle_id: str, frm: float, to: float) -> list[dict]:
    files = glob.glob(f"{CFG['parquet_root']}/telemetry/date=*/cell=*/*.parquet")
    if not files:
        return []
    days = [(datetime.fromtimestamp(frm, UTC) + timedelta(days=i)).strftime("%Y-%m-%d")
            for i in range(int((to - frm) // 86400) + 2)]
    rows = duckdb.sql(f"""
        SELECT epoch(device_ts) AS ts, lat, lon, speed_kmh, ignition, flags
        FROM read_parquet('{CFG['parquet_root']}/telemetry/*/*/*.parquet', hive_partitioning = true)
        WHERE date IN ({','.join(repr(d) for d in days)}) AND vehicle_id = ?
          AND epoch(device_ts) BETWEEN ? AND ? ORDER BY device_ts""", params=[vehicle_id, frm, to]).fetchall()
    return [{"ts": t, "lat": la, "lon": lo, "speed_kmh": s, "ignition": i, "flags": f} for t, la, lo, s, i, f in rows]


# ---------------------------------------------------------------- risk, alerts, convoys
@app.get("/risk/top")
def risk_top(k: int = Query(20, ge=1, le=200), lender: int = Depends(current_lender)):
    return {"top": lender_top(lender, k)}


def lender_top(lender: int, k: int) -> list[dict]:
    out, start, page = [], 0, max(200, k * 5)
    while len(out) < k:                                   # risk:top holds every lender: page until we have k
        rows = r.zrevrange(DET["risk"]["top_key"], start, start + page - 1, withscores=True)
        if not rows:
            break
        out += [{"vehicle_id": v, "score": s} for v, s in rows if OWNER.get(v) == lender]
        start += page
    return out[:k]


@app.get("/alerts")
def alerts(status: str = "open", limit: int = Query(100, le=500), code: str | None = None,
           lender: int = Depends(current_lender)):
    closed = "AND NOT EXISTS (SELECT 1 FROM case_alert ca JOIN recovery_case rc USING (case_id) " \
             "WHERE ca.alert_id = a.alert_id AND rc.status IN ('recovered', 'closed'))" if status == "open" else ""
    with pg.connection() as c:
        rows = c.execute(f"""
            SELECT a.alert_id, a.vehicle_id, s.code, extract(epoch FROM a.created_at)::float AS ts,
                   a.lat, a.lon, a.risk_score::float AS risk_score,
                   (SELECT ca.case_id FROM case_alert ca WHERE ca.alert_id = a.alert_id LIMIT 1) AS case_id
            FROM alert a JOIN signal_type s USING (signal_type_id)
            JOIN loan l ON l.vehicle_id = a.vehicle_id AND l.status <> 'closed'
            WHERE l.lender_id = %s {closed} AND (%s::text IS NULL OR s.code = %s)
            ORDER BY a.created_at DESC LIMIT %s""", (lender, code, code, limit)).fetchall()
    return {"alerts": rows}


@app.get("/convoys/active")
def convoys_active(hours: int = 2, lender: int = Depends(current_lender)):
    with pg.connection() as c:
        rows = c.execute("""
            SELECT cv.convoy_id, extract(epoch FROM cv.detected_at)::float AS detected_at,
                   array_agg(cm.vehicle_id ORDER BY cm.vehicle_id) AS members
            FROM convoy cv JOIN convoy_member cm USING (convoy_id)
            WHERE cv.detected_at > now() - make_interval(hours => %s)
            GROUP BY cv.convoy_id ORDER BY cv.detected_at DESC""", (hours,)).fetchall()
    out = []
    for row in rows:
        members = [m for m in row["members"] if OWNER.get(m) == lender]
        if members:
            out.append({**row, "members": [{"vehicle_id": m, "position": live_position(m)} for m in members]})
    return {"convoys": out}


# ---------------------------------------------------------------- cases + routing
class NewCase(BaseModel):
    vehicle_id: str


class CaseUpdate(BaseModel):
    agent_id: int | None = None
    status: str | None = None


@app.get("/cases")
def list_cases(lender: int = Depends(current_lender)):
    with pg.connection() as c:
        rows = c.execute("""
            SELECT rc.case_id, extract(epoch FROM rc.opened_at)::float AS opened_at, rc.status, rc.agent_id,
                   ag.display_name AS agent, l.vehicle_id, l.days_past_due
            FROM recovery_case rc JOIN loan l USING (loan_id) LEFT JOIN agent ag USING (agent_id)
            WHERE l.lender_id = %s ORDER BY rc.opened_at DESC LIMIT 200""", (lender,)).fetchall()
    for row in rows:
        row["risk_score"] = r.zscore(DET["risk"]["top_key"], row["vehicle_id"]) or 0
    return {"cases": rows}


@app.post("/cases", status_code=201)
def open_case(body: NewCase, lender: int = Depends(current_lender)):
    own(body.vehicle_id, lender)
    with pg.connection() as c:
        loan_id = c.execute("SELECT loan_id FROM loan WHERE vehicle_id = %s AND status <> 'closed'",
                            (body.vehicle_id,)).fetchone()["loan_id"]
        existing = c.execute("SELECT case_id FROM recovery_case WHERE loan_id = %s AND status IN ('open', 'in_progress')",
                             (loan_id,)).fetchone()
        if existing:
            return {"case_id": existing["case_id"], "existing": True}
        case_id = c.execute("INSERT INTO recovery_case (status, loan_id) VALUES ('open', %s) RETURNING case_id",
                            (loan_id,)).fetchone()["case_id"]
        c.execute("""INSERT INTO case_alert (case_id, alert_id)
                     SELECT %s, alert_id FROM alert WHERE vehicle_id = %s AND created_at > now() - interval '24 hours'""",
                  (case_id, body.vehicle_id))
    return {"case_id": case_id, "existing": False}


def _case(case_id: int, lender: int) -> dict:
    with pg.connection() as c:
        row = c.execute("""
            SELECT rc.case_id, extract(epoch FROM rc.opened_at)::float AS opened_at, rc.status, rc.agent_id,
                   ag.display_name AS agent, l.vehicle_id, l.lender_id, l.days_past_due, l.amount::float AS amount
            FROM recovery_case rc JOIN loan l USING (loan_id) LEFT JOIN agent ag USING (agent_id)
            WHERE rc.case_id = %s""", (case_id,)).fetchone()
    if not row or row["lender_id"] != lender:
        raise HTTPException(404, "case not found")
    return row


@app.get("/cases/{case_id}")
def get_case(case_id: int, lender: int = Depends(current_lender)):
    case = _case(case_id, lender)
    with pg.connection() as c:
        case["alerts"] = c.execute("""
            SELECT a.alert_id, s.code, extract(epoch FROM a.created_at)::float AS ts, a.risk_score::float AS risk_score
            FROM case_alert ca JOIN alert a USING (alert_id) JOIN signal_type s USING (signal_type_id)
            WHERE ca.case_id = %s ORDER BY a.created_at""", (case_id,)).fetchall()
    case["position"] = live_position(case["vehicle_id"])
    if case["agent_id"] and case["status"] in ("open", "in_progress"):
        case["route"] = _agent_route(case["agent_id"], case["vehicle_id"])
    log_access(lender, case["vehicle_id"], "case")
    return case


@app.patch("/cases/{case_id}")
def update_case(case_id: int, body: CaseUpdate, lender: int = Depends(current_lender)):
    case = _case(case_id, lender)
    if body.status and body.status not in ("open", "in_progress", "recovered", "closed"):
        raise HTTPException(400, "bad status")
    with pg.connection() as c:
        if body.agent_id is not None:
            ok = c.execute("SELECT 1 FROM agent WHERE agent_id = %s AND lender_id = %s", (body.agent_id, lender)).fetchone()
            if not ok:
                raise HTTPException(400, "agent does not belong to this lender")
        c.execute("UPDATE recovery_case SET agent_id = COALESCE(%s, agent_id), status = COALESCE(%s, "
                  "CASE WHEN %s::int IS NOT NULL THEN 'in_progress' ELSE status END) WHERE case_id = %s",
                  (body.agent_id, body.status, body.agent_id, case_id))
    if body.status in ("recovered", "closed"):
        r.zrem(DET["risk"]["top_key"], case["vehicle_id"])
    return get_case(case_id, lender)


@app.get("/agents")
def agents(lender: int = Depends(current_lender)):
    with pg.connection() as c:
        rows = c.execute("SELECT agent_id, display_name FROM agent WHERE lender_id = %s ORDER BY agent_id",
                         (lender,)).fetchall()
    for row in rows:
        pos = r.geopos("agents:live", str(row["agent_id"]))[0]
        row["position"] = (pos[1], pos[0]) if pos else None
    return {"agents": rows}


def _agent_route(agent_id: int, vehicle_id: str) -> dict | None:
    a, v = r.geopos("agents:live", str(agent_id))[0], live_position(vehicle_id)
    if not a or not v:
        return None
    return router.route(a[1], a[0], v[0], v[1])


@app.get("/route")
def route(agent: int, vehicle: str, lender: int = Depends(current_lender)):
    own(vehicle, lender)
    result = _agent_route(agent, vehicle)
    if result is None:
        raise HTTPException(404, "no route")
    log_access(lender, vehicle, "route")
    return result


@app.get("/route/nearest-agent")
def nearest_agent(vehicle: str, lender: int = Depends(current_lender)):
    own(vehicle, lender)
    v = live_position(vehicle)
    if not v:
        raise HTTPException(404, "no live position")
    mine = agents(lender)["agents"]
    found = router.nearest_agent(v[0], v[1], {str(a["agent_id"]): a["position"] for a in mine if a["position"]})
    if found is None:
        raise HTTPException(404, "no agent reachable")
    return found


# ---------------------------------------------------------------- reports + history
@app.get("/reports/daily")
def reports_daily(date: str | None = None, lender: int = Depends(current_lender)):
    folder = Path(CFG["reports_root"]) / f"lender_{lender}"
    files = sorted(folder.glob("*.json"))
    if date:
        files = [f for f in files if f.stem == date]
    if not files:
        raise HTTPException(404, "no report yet (the batch job writes one daily at 06:00)")
    return json.loads(files[-1].read_text())


@app.get("/risk/history")
def risk_history(frm: float = Query(alias="from"), to: float = Query(...), k: int = Query(20, le=200),
                 lender: int = Depends(current_lender)):
    """Riskiest vehicles over any date range: peak alert score per vehicle, then a size-K min-heap."""
    with pg.connection() as c:
        rows = c.execute("""
            SELECT a.vehicle_id, max(a.risk_score)::float AS peak FROM alert a
            JOIN loan l ON l.vehicle_id = a.vehicle_id AND l.status <> 'closed'
            WHERE l.lender_id = %s AND a.created_at BETWEEN to_timestamp(%s) AND to_timestamp(%s)
            GROUP BY a.vehicle_id""", (lender, frm, to))
        best = top_k(((row["vehicle_id"], row["peak"]) for row in rows), k)
    return {"top": [{"vehicle_id": v, "peak_score": s} for v, s in best]}


# ---------------------------------------------------------------- privacy API
def _partner(x_partner_key: str | None) -> str:
    pairs = dict(p.split(":", 1) for p in os.environ.get("PARTNER_KEYS", "").split(",") if ":" in p)
    for name, key in pairs.items():
        if x_partner_key and privacy.hmac.compare_digest(key, x_partner_key):
            return name
    raise HTTPException(401, "unknown partner key")


@app.get("/share/insights")
def share_insights(x_partner_key: str | None = Header(default=None)):
    partner = _partner(x_partner_key)
    k, precision = CFG["privacy"]["k_anonymity"], CFG["privacy"]["cell_precision"]
    fleet = []
    for vid, raw in r.hscan_iter("live:state", count=5000):
        _lat, _lon, speed, _heading, gh, _ts = raw.split(",")
        fleet.append({"vehicle_id": vid, "geohash": gh, "speed_kmh": float(speed)})
    scores = dict(r.zrange(DET["risk"]["top_key"], 0, -1, withscores=True))
    for v in fleet:
        v["risk"] = scores.get(v["vehicle_id"], 0)
    cells = privacy.cell_insights(fleet, k, precision)
    counts = {c["cell"]: c["vehicles"] for c in cells}
    with pg.connection() as c:
        events = c.execute("""
            SELECT a.vehicle_id, s.code, extract(epoch FROM a.created_at)::float AS ts, a.lat, a.lon
            FROM alert a JOIN signal_type s USING (signal_type_id)
            WHERE a.created_at > now() - interval '24 hours' AND s.code IN ('GEOFENCE_EXIT', 'TOW_SUSPECTED')""").fetchall()
    from shared.geo import geohash_encode
    for e in events:
        e["geohash"] = geohash_encode(e["lat"], e["lon"], precision)
    key = privacy.partner_key(os.environ["PSEUDONYM_SECRET"], partner)
    return {"partner": partner, "k_anonymity": k, "cell_precision": precision,
            "generated_hour": privacy.round_to_hour(time.time()),
            "cells": cells, "events": privacy.anonymize_events(events, key, counts, k, precision)}


# ---------------------------------------------------------------- system health
_lag_consumers: dict[str, Consumer] = {}
LAG_GROUPS = {"pipeline": "raw.telemetry", "detectors": "normalized.events", "storage": "normalized.events"}


def kafka_lag() -> dict[str, int]:
    out = {}
    for group, topic in LAG_GROUPS.items():
        c = _lag_consumers.setdefault(group, Consumer({"bootstrap.servers": settings.kafka_bootstrap,
                                                       "group.id": group, "enable.auto.commit": False}))
        parts = [TopicPartition(topic, p) for p in c.list_topics(topic, timeout=5).topics[topic].partitions]
        lag = 0
        for tp in c.committed(parts, timeout=5):
            high = c.get_watermark_offsets(tp, timeout=5, cached=False)[1]
            lag += max(0, high - tp.offset) if tp.offset >= 0 else high
        out[group] = lag
    return out


@app.get("/system/health")
def system_health(_lender: int = Depends(current_lender)):
    services = {}
    for name, url in CFG["service_stats"].items():
        try:
            services[name] = httpx.get(url, timeout=2).json()
        except httpx.HTTPError:
            services[name] = None
    return {"ts": time.time(), "pipeline": r.hgetall("stats:pipeline"), "detectors": r.hgetall("stats:detectors"),
            "services": services, "kafka_lag": kafka_lag(), "vehicles_live": r.zcard("pos:live")}


@app.get("/system/load")
def get_load(_lender: int = Depends(current_lender)):
    """Simulator load: active vehicles, fleet size and presets for the dashboard's Load button."""
    try:
        return httpx.get(SIM_URL + "/stats", timeout=5).json()
    except httpx.HTTPError as exc:
        raise HTTPException(503, "simulator unavailable") from exc


@app.post("/system/load")
def set_load(body: dict, _lender: int = Depends(current_lender)):
    """Demo control: change how many simulated vehicles report (e.g. 1k for development, 100k to show scale).

    Any signed-in lender may change it: the simulator is shared demo infrastructure, not lender data.
    """
    n = body.get("active_vehicles")
    if not isinstance(n, int) or n < 0:
        raise HTTPException(422, "active_vehicles must be a non-negative integer")
    try:
        return httpx.post(SIM_URL + "/control", json={"active_vehicles": n}, timeout=5).json()
    except httpx.HTTPError as exc:
        raise HTTPException(503, "simulator unavailable") from exc


@app.get("/system/pipeline")
def pipeline_flow(limit: int = Query(40, le=200), lender: int = Depends(current_lender)):
    """Counters (overall + per OEM) and recent sampled payloads for the 3D Pipeline screen.

    Samples are limited to this lender's vehicles. Dead-letter payloads can't be attributed to a vehicle,
    so their vehicle ids are masked.
    """
    out = []
    for raw in r.lrange("pipeline:samples", 0, 399):
        smp = json.loads(raw)
        if smp["canonical"] is None:
            smp["raw"] = VEHICLE_ID.sub("VH-******", smp["raw"])
        elif OWNER.get(smp["canonical"]["vehicle_id"]) != lender:
            continue
        out.append(smp)
        if len(out) >= limit:
            break
    return {"ts": time.time(), "counts": r.hgetall("stats:pipeline"), "detectors": r.hgetall("stats:detectors"),
            "samples": out}


# ---------------------------------------------------------------- WebSocket
@app.websocket("/ws/live")
async def ws_live(ws: WebSocket, token: str):
    lender = int(decode_token(token)["lender_id"])
    await ws.accept()
    ar = aioredis.Redis.from_url(settings.redis_url, decode_responses=True)
    pubsub = ar.pubsub()
    await pubsub.subscribe("alerts")
    view = {"bbox": None}
    last_top: list = []

    async def receive() -> None:
        while True:
            msg = await ws.receive_json()
            if "bbox" in msg:
                view["bbox"] = msg["bbox"]

    async def alerts_feed() -> None:
        async for msg in pubsub.listen():
            if msg["type"] == "message":
                alert = json.loads(msg["data"])
                if alert.get("lender_id") == lender:
                    await ws.send_json({"type": "alert", "alert": alert})

    tasks = [asyncio.create_task(receive()), asyncio.create_task(alerts_feed())]
    try:
        while True:                        # positions batched every 2 s, only for the map view on screen
            if view["bbox"]:
                points = await asyncio.to_thread(positions_in_bbox, view["bbox"], lender, CFG["ws_max_points"])
                total = await asyncio.to_thread(fleet_live, lender)   # whole reporting fleet, not just drawn
                await ws.send_json({"type": "positions", "vehicles": points, "total": total})
            top = await asyncio.to_thread(lender_top, lender, 10)
            if top != last_top:
                await ws.send_json({"type": "topk", "top": top})
                last_top = top
            if any(t.done() for t in tasks):
                break
            await asyncio.sleep(CFG["ws_push_every_s"])
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        for t in tasks:
            t.cancel()
        with contextlib.suppress(Exception):
            await pubsub.aclose()
            await ar.aclose()
