"""Batch jobs (SPEC §6) on DuckDB over Parquet, with TimescaleDB as the source of recent days.

    export        a day of telemetry: TimescaleDB -> Parquet  telemetry/date=YYYY-MM-DD/cell=<gh2>/*.parquet
    baseline      last 14 days -> vehicle_baseline (home cell, work cell, active hours, speed mean/std)
    rescore       replay a day in event-time order (late events included) through the SAME DetectorEngine
                  as the live stream; add the alerts the stream missed
    convoy        24 h co-location over 5-minute snapshots, with a looser rule, for slow convoys
    report        per-lender daily JSON (at-risk vehicles, cases opened/closed, recovery rate)

    python -m batch.jobs export --date 2026-09-29      (dates are local, IST; default = yesterday)
"""
import argparse
import json
import logging
import shutil
import tempfile
import time
from datetime import UTC, date, datetime, timedelta, timezone
from pathlib import Path

import duckdb
import psycopg
import redis
import yaml

from detectors.engine import DetectorEngine
from detectors.runner import load_state
from shared.config import settings
from shared.detect.convoy import ConvoyDetector, LiveState
from shared.roadgraph import RoadGraph
from shared.schema import CanonicalEvent

log = logging.getLogger("batch")
CFG = yaml.safe_load(Path("batch/config.yaml").read_text())
DET = yaml.safe_load(Path("detectors/config.yaml").read_text())
TZ = timezone(timedelta(hours=CFG["tz_offset_h"]))
ROOT = Path(CFG["parquet_root"]) / "telemetry"
COLUMNS = ("vehicle_id, device_ts, event_id, oem, seq, ingest_ts, lat, lon, geohash, speed_kmh, heading_deg, "
           "ignition, battery_v, odometer_km, flags")


def day_bounds(d: date) -> tuple[datetime, datetime]:
    start = datetime(d.year, d.month, d.day, tzinfo=TZ)
    return start, start + timedelta(days=1)


def yesterday() -> date:
    return (datetime.now(TZ) - timedelta(days=1)).date()


def parquet_glob(days: list[date]) -> list[str]:
    return [str(p) for d in days for p in (ROOT / f"date={d.isoformat()}").glob("cell=*/*.parquet")]


# ---------------------------------------------------------------- export
def export(d: date) -> dict:
    """Stream one local day out of TimescaleDB (COPY ... CSV) and rewrite its Parquet partition."""
    start, end = day_bounds(d)
    with tempfile.NamedTemporaryFile("wb", suffix=".csv", delete=False) as tmp:
        with psycopg.connect(settings.timescale_dsn) as conn, conn.cursor() as cur:
            query = (f"COPY (SELECT {COLUMNS} FROM telemetry WHERE device_ts >= %s AND device_ts < %s) "
                     "TO STDOUT WITH (FORMAT csv, HEADER true)")
            with cur.copy(query, (start, end)) as cp:
                for chunk in cp:
                    tmp.write(chunk)
        csv_path = tmp.name
    target = ROOT / f"date={d.isoformat()}"
    shutil.rmtree(target, ignore_errors=True)                # idempotent: re-running replaces the day
    ROOT.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    rows = con.execute(f"""
        SELECT count(*) FROM read_csv('{csv_path}', header = true, types = {{'flags': 'VARCHAR'}})""").fetchone()[0]
    if rows:
        con.execute(f"""
            COPY (SELECT *, '{d.isoformat()}' AS date, substr(geohash, 1, 2) AS cell,
                         string_split(trim(flags, '{{}}'), ',') AS flag_list
                  FROM read_csv('{csv_path}', header = true, types = {{'flags': 'VARCHAR'}}))
            TO '{ROOT}' (FORMAT parquet, PARTITION_BY (date, cell), OVERWRITE_OR_IGNORE true)""")
    Path(csv_path).unlink(missing_ok=True)
    return {"date": d.isoformat(), "rows": rows}


def ensure_exported(days: list[date]) -> None:
    for d in days:
        if not parquet_glob([d]) or d >= datetime.now(TZ).date():   # today is always refreshed
            export(d)


# ---------------------------------------------------------------- baseline
def baseline(until: date, days: int | None = None) -> dict:
    days = days or CFG["baseline_days"]
    span = [until - timedelta(days=i) for i in range(days)]
    ensure_exported(span)
    files = parquet_glob(span)
    if not files:
        return {"vehicles": 0}
    tz_h, moving = CFG["tz_offset_h"], CFG["moving_kmh"]
    con = duckdb.connect()
    con.execute(f"CREATE VIEW t AS SELECT *, (hour(device_ts + INTERVAL {int(tz_h * 60)} MINUTE)) AS h "
                f"FROM read_parquet({files!r})")
    rows = con.execute(f"""
        WITH parked AS (SELECT vehicle_id, geohash, h FROM t WHERE speed_kmh < {moving}),
        home AS (SELECT vehicle_id, mode(geohash) AS cell FROM parked WHERE h < 6 OR h >= 22 GROUP BY 1),
        work AS (SELECT vehicle_id, mode(geohash) AS cell FROM parked WHERE h BETWEEN 10 AND 16 GROUP BY 1),
        drive AS (SELECT vehicle_id, quantile_disc(h, 0.05) AS h0, quantile_disc(h, 0.95) AS h1,
                         avg(speed_kmh) AS avg_speed, coalesce(stddev_samp(speed_kmh), 0) AS std_speed
                  FROM t WHERE speed_kmh >= {moving} AND NOT list_contains(flag_list, 'suspect_gps') GROUP BY 1)
        SELECT v.vehicle_id, home.cell, work.cell, drive.h0, drive.h1, drive.avg_speed, drive.std_speed
        FROM (SELECT DISTINCT vehicle_id FROM t) v
        LEFT JOIN home USING (vehicle_id) LEFT JOIN work USING (vehicle_id) LEFT JOIN drive USING (vehicle_id)
    """).fetchall()
    with psycopg.connect(settings.postgres_dsn) as conn, conn.cursor() as cur:
        cur.executemany("""
            INSERT INTO vehicle_baseline (vehicle_id, home_cell, work_cell, active_hours, avg_speed, std_speed)
            VALUES (%s, %s, %s, CASE WHEN %s::int IS NULL THEN NULL ELSE int4range(%s::int, %s::int + 1) END, %s, %s)
            ON CONFLICT (vehicle_id) DO UPDATE SET home_cell = EXCLUDED.home_cell, work_cell = EXCLUDED.work_cell,
                active_hours = EXCLUDED.active_hours, avg_speed = EXCLUDED.avg_speed, std_speed = EXCLUDED.std_speed""",
            [(vid, home, work, h0, h0, h1, avg, std) for vid, home, work, h0, h1, avg, std in rows])
    return {"vehicles": len(rows), "days": [d.isoformat() for d in span]}


# ---------------------------------------------------------------- late re-scorer
def rescore(d: date) -> dict:
    """Replay the day in true event-time order through the live detector code; insert missed alerts."""
    ensure_exported([d])
    files = parquet_glob([d])
    if not files:
        return {"events": 0, "missed_alerts": 0}
    with psycopg.connect(settings.postgres_dsn) as conn:
        state, signal_ids = load_state(conn)
        start, end = day_bounds(d)
        existing = conn.execute("""SELECT a.vehicle_id, s.code, extract(epoch FROM a.created_at) FROM alert a
                                   JOIN signal_type s USING (signal_type_id) WHERE a.created_at >= %s AND a.created_at < %s""",
                                (start, end)).fetchall()
    graph = RoadGraph.load(DET["route"]["graph_path"], DET["route"]["traffic_speed_factor"])
    engine = DetectorEngine(DET, state, graph)
    cooldown = DET["risk"]["alert_cooldown_s"]
    seen: dict[tuple[str, str], list[float]] = {}
    for vid, code, ts in existing:
        seen.setdefault((vid, code), []).append(float(ts))

    con = duckdb.connect()
    cur = con.execute(f"""SELECT vehicle_id, epoch(device_ts), event_id, oem, seq, epoch(ingest_ts), lat, lon, geohash,
                                 speed_kmh, heading_deg, ignition, battery_v, odometer_km, flag_list
                          FROM read_parquet({files!r}) ORDER BY device_ts""")
    events, missed = 0, []
    last_tick = 0.0
    while batch := cur.fetchmany(20_000):
        for row in batch:
            e = CanonicalEvent(row[2], row[0], row[3], row[4], row[1], row[5], row[6], row[7], row[8], row[9],
                               row[10], row[11], row[12], row[13], [f for f in (row[14] or []) if f and f != "late"])
            events += 1
            scored = engine.on_event(e)
            if e.device_ts - last_tick >= 1:                  # event-time ticks, like the live 1 s timer
                scored += engine.tick()
                last_tick = e.device_ts
            for s in scored:
                times = seen.get((s.signal.vehicle_id, s.signal.code), [])
                if s.decision.write_alert and not any(abs(s.signal.ts - t) < cooldown for t in times):
                    missed.append(s)
                    times.append(s.signal.ts)
                    seen[(s.signal.vehicle_id, s.signal.code)] = times
    r = redis.Redis.from_url(settings.redis_url)
    with psycopg.connect(settings.postgres_dsn) as conn, conn.cursor() as c:
        for s in missed:
            c.execute("INSERT INTO alert (created_at, lat, lon, risk_score, vehicle_id, signal_type_id) "
                      "VALUES (to_timestamp(%s), %s, %s, %s, %s, %s)",
                      (s.signal.ts, s.signal.lat, s.signal.lon, s.decision.score, s.signal.vehicle_id,
                       signal_ids[s.signal.code]))
            r.zadd(DET["risk"]["top_key"], {s.signal.vehicle_id: s.decision.score}, gt=True)
    return {"events": events, "missed_alerts": len(missed),
            "by_signal": {code: sum(s.signal.code == code for s in missed) for code in {s.signal.code for s in missed}}}


# ---------------------------------------------------------------- slow convoys
def convoy_rebuild(d: date) -> dict:
    """5-minute snapshots over the whole day, looser gap tolerance than live -> slow convoys."""
    ensure_exported([d])
    files = parquet_glob([d])
    if not files:
        return {"snapshots": 0, "convoys": 0}
    con = duckdb.connect()
    rows = con.execute(f"""
        SELECT epoch(time_bucket(INTERVAL 5 MINUTE, device_ts)) AS slot, vehicle_id,
               arg_max(lat, device_ts), arg_max(lon, device_ts), arg_max(speed_kmh, device_ts),
               arg_max(heading_deg, device_ts), arg_max(geohash, device_ts)
        FROM read_parquet({files!r}) WHERE NOT list_contains(flag_list, 'suspect_gps')
        GROUP BY 1, 2 ORDER BY 1""").fetchall()
    det = ConvoyDetector({**DET["convoy"], **CFG["slow_convoy"]})
    snapshots: dict = {}
    for slot, vid, lat, lon, speed, heading, gh in rows:
        snapshots.setdefault(slot, []).append(LiveState(vid, lat, lon, speed, heading, gh))
    found: list[set[str]] = []
    for slot in sorted(snapshots):
        for g in det.run(snapshots[slot]):
            if not any(g <= f for f in found):
                found = [f for f in found if not f <= g] + [g]
    with psycopg.connect(settings.postgres_dsn) as conn, conn.cursor() as c:
        known = [set(m) for (m,) in c.execute(
            "SELECT array_agg(vehicle_id) FROM convoy_member cm JOIN convoy USING (convoy_id) "
            "WHERE detected_at >= %s GROUP BY convoy_id", (day_bounds(d)[0],))]
        new = [g for g in found if not any(g <= k for k in known)]
        for g in new:
            cid = c.execute("INSERT INTO convoy (detected_at) VALUES (%s) RETURNING convoy_id",
                            (day_bounds(d)[1] - timedelta(seconds=1),)).fetchone()[0]
            c.executemany("INSERT INTO convoy_member (convoy_id, vehicle_id) VALUES (%s, %s)", [(cid, v) for v in sorted(g)])
    return {"snapshots": len(snapshots), "convoys_found": len(found), "new_convoys": len(new)}


# ---------------------------------------------------------------- lender report
def report(d: date) -> dict:
    start, end = day_bounds(d)
    out_root = Path(CFG["reports_root"])
    r = redis.Redis.from_url(settings.redis_url, decode_responses=True)
    scores = dict(r.zrange(DET["risk"]["top_key"], 0, -1, withscores=True))
    written = []
    with psycopg.connect(settings.postgres_dsn) as conn:
        for lender_id, name in conn.execute("SELECT lender_id, name FROM lender").fetchall():
            vehicles = conn.execute("SELECT count(*) FROM loan WHERE lender_id = %s AND status <> 'closed'",
                                    (lender_id,)).fetchone()[0]
            by_signal = dict(conn.execute("""
                SELECT s.code, count(*) FROM alert a JOIN signal_type s USING (signal_type_id)
                JOIN loan l ON l.vehicle_id = a.vehicle_id WHERE l.lender_id = %s AND a.created_at >= %s AND a.created_at < %s
                GROUP BY 1 ORDER BY 2 DESC""", (lender_id, start, end)).fetchall())
            opened = conn.execute("""SELECT count(*) FROM recovery_case rc JOIN loan l USING (loan_id)
                                     WHERE l.lender_id = %s AND rc.opened_at >= %s AND rc.opened_at < %s""",
                                  (lender_id, start, end)).fetchone()[0]
            outcomes = dict(conn.execute("""SELECT rc.status, count(*) FROM recovery_case rc JOIN loan l USING (loan_id)
                                            WHERE l.lender_id = %s AND rc.opened_at >= %s GROUP BY 1""",
                                         (lender_id, end - timedelta(days=30))).fetchall())
            at_risk = conn.execute("""
                SELECT a.vehicle_id, max(a.risk_score)::float AS peak, max(l.days_past_due) FROM alert a
                JOIN loan l ON l.vehicle_id = a.vehicle_id WHERE l.lender_id = %s AND a.created_at >= %s AND a.created_at < %s
                GROUP BY 1 ORDER BY 2 DESC LIMIT 25""", (lender_id, start, end)).fetchall()
            done = outcomes.get("recovered", 0) + outcomes.get("closed", 0)
            doc = {
                "lender": name, "date": d.isoformat(), "generated_at": datetime.now(UTC).isoformat(),
                "summary": {
                    "vehicles": vehicles,
                    "vehicles_scored_now": sum(1 for v in scores if v),
                    "vehicles_with_alerts": len(at_risk),
                    "alerts": sum(by_signal.values()),
                    "cases_opened": opened,
                    "cases_recovered_30d": outcomes.get("recovered", 0),
                    "cases_closed_30d": outcomes.get("closed", 0),
                    "recovery_rate_30d": round(outcomes.get("recovered", 0) / done, 3) if done else 0,
                },
                "alerts_by_signal": by_signal,
                "at_risk": [{"vehicle_id": v, "peak_score": p, "days_past_due": dpd} for v, p, dpd in at_risk],
            }
            folder = out_root / f"lender_{lender_id}"
            folder.mkdir(parents=True, exist_ok=True)
            (folder / f"{d.isoformat()}.json").write_text(json.dumps(doc, indent=2))
            written.append(str(folder / f"{d.isoformat()}.json"))
    return {"files": written}


JOBS = {"export": export, "baseline": baseline, "rescore": rescore, "convoy": convoy_rebuild, "report": report}


def run(name: str, d: date | None = None) -> dict:
    started = time.time()
    result = JOBS[name](d or yesterday())
    result["seconds"] = round(time.time() - started, 1)
    log.info("job %s: %s", name, result)
    return result


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    p = argparse.ArgumentParser()
    p.add_argument("job", choices=list(JOBS))
    p.add_argument("--date", type=date.fromisoformat, default=None, help="local date (default: yesterday)")
    a = p.parse_args()
    print(json.dumps(run(a.job, a.date), indent=2, default=str))
