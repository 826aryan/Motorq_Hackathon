"""Remove alerts that the simulated fleet cannot have produced honestly (dev / demo clean-up).

Personas are fixed by the simulator seed, so the ground truth is known: only `towed` vehicles are really towed
and only `tampered` ones are really tampered with. TOW_SUSPECTED / TAMPER_SUSPECTED alerts on any other vehicle
are false positives (e.g. from a demo-clock jump or an old load-control bug). They are deleted together with
their case links; recovery cases left without any alert are deleted too, and the vehicles leave Redis risk:top.

    python -m db.tools.purge_false_alerts            # dry run: print what would be removed
    python -m db.tools.purge_false_alerts --apply    # delete (restart the detectors afterwards)
"""
import argparse
from pathlib import Path

import psycopg
import redis
import yaml

from shared.config import settings
from shared.fleet import build_fleet, vehicle_id
from shared.roadgraph import RoadGraph

TRUE_PERSONA = {"TOW_SUSPECTED": "towed", "TAMPER_SUSPECTED": "tampered"}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()

    cfg = yaml.safe_load(Path("simulator/config.yaml").read_text())
    fleet = build_fleet(cfg, RoadGraph.load(cfg["graph"]["path"], cfg["graph"]["traffic_speed_factor"]))
    honest = {code: [vehicle_id(i) for i, p in enumerate(fleet.persona) if p == persona]
              for code, persona in TRUE_PERSONA.items()}

    with psycopg.connect(settings.postgres_dsn) as pg, pg.cursor() as cur:
        false_ids, vehicles = [], set()
        for code, ok in honest.items():
            rows = cur.execute("""
                SELECT a.alert_id, a.vehicle_id FROM alert a JOIN signal_type s USING (signal_type_id)
                WHERE s.code = %s AND NOT (a.vehicle_id = ANY(%s))""", (code, ok)).fetchall()
            print(f"{code}: {len(rows)} false alerts on {len({v for _, v in rows})} vehicles")
            false_ids += [aid for aid, _ in rows]
            vehicles |= {v for _, v in rows}
        if not a.apply:
            print("dry run: nothing deleted (use --apply)")
            return
        cur.execute("DELETE FROM case_alert WHERE alert_id = ANY(%s)", (false_ids,))
        cur.execute("DELETE FROM alert WHERE alert_id = ANY(%s)", (false_ids,))
        cur.execute("""DELETE FROM recovery_case c
                       WHERE NOT EXISTS (SELECT 1 FROM case_alert ca WHERE ca.case_id = c.case_id)""")
        print(f"deleted {len(false_ids)} alerts and {cur.rowcount} empty cases")
        pg.commit()

    if vehicles:
        r = redis.Redis.from_url(settings.redis_url)
        ids = sorted(vehicles)
        for i in range(0, len(ids), 5000):
            r.zrem("risk:top", *ids[i:i + 5000])
        print(f"removed {len(ids)} vehicles from risk:top; restart the detectors to clear their in-memory scores")


if __name__ == "__main__":
    main()
