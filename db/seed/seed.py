"""Seed the 3NF core with synthetic business data for the simulated fleet. All data is fake.

Creates lenders, one geofence per lender (a circle around the city core), vehicle models, 100k
vehicles + devices, hashed borrowers, loans (mostly current, some overdue), and recovery agents.
Idempotent: does nothing if the vehicles already exist.

    python -m db.seed.seed         (the `seed` compose service runs this once before detectors)
"""
import hashlib
import io
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import psycopg
import yaml

from shared.config import settings
from shared.fleet import vehicle_id
from shared.geo import circle_polygon

SEED_VEHICLES = int(os.environ.get("SEED_VEHICLES", "100000"))   # covers any simulator fleet size up to this
LENDERS = ["Lender Alpha", "Lender Beta", "Lender Gamma"]
VEHICLE_MODELS = [("Maruti", "Swift", 2021), ("Hyundai", "Creta", 2022), ("Tata", "Nexon", 2023),
                  ("Mahindra", "XUV300", 2022), ("Honda", "City", 2020), ("Toyota", "Innova", 2021)]
DEVICE_MODELS = {"A": ("TrackA-100", "1.4.2"), "B": ("PipeLog-B2", "2.0.1"), "C": ("HexNav-C", "3.1.0")}
AGENTS_PER_LENDER = 10


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def copy_rows(cur, table: str, columns: list[str], rows) -> None:
    buf = io.StringIO()
    for r in rows:
        buf.write("\t".join("\\N" if v is None else str(v) for v in r) + "\n")
    buf.seek(0)
    with cur.copy(f"COPY {table} ({', '.join(columns)}) FROM STDIN") as cp:
        cp.write(buf.read())


def main() -> None:
    sim = yaml.safe_load(Path("simulator/config.yaml").read_text())
    geo_cfg = yaml.safe_load(Path("db/seed/seed.yaml").read_text())
    rng = np.random.default_rng(sim["seed"])
    center = sim["graph"]["center"]

    with psycopg.connect(settings.postgres_dsn) as conn, conn.cursor() as cur:
        # Later migrations are idempotent; initdb only runs the folder on a brand-new volume.
        for sql in sorted(Path("db/migrations").glob("*.sql"))[2:]:
            cur.execute(sql.read_text())
        conn.commit()
        cur.execute("SELECT count(*) FROM vehicle")
        if cur.fetchone()[0] >= SEED_VEHICLES:
            print("seed: already seeded, nothing to do")
            return
        n = SEED_VEHICLES
        print(f"seed: creating {n} vehicles with loans ...")

        lender_ids = [cur.execute("INSERT INTO lender (name) VALUES (%s) RETURNING lender_id", (name,)).fetchone()[0]
                      for name in LENDERS]
        ring = circle_polygon(center[0], center[1], geo_cfg["geofence_radius_m"])
        wkt = "POLYGON((" + ", ".join(f"{lon} {lat}" for lat, lon in [*ring, ring[0]]) + "))"
        fence_ids = [cur.execute("INSERT INTO geofence (name, area, lender_id) VALUES (%s, ST_GeogFromText(%s), %s) "
                                 "RETURNING geofence_id", (f"Bengaluru core ({name})", wkt, lid)).fetchone()[0]
                     for name, lid in zip(LENDERS, lender_ids, strict=True)]
        vm_ids = [cur.execute("INSERT INTO vehicle_model (make, model, year) VALUES (%s, %s, %s) "
                              "RETURNING vehicle_model_id", m).fetchone()[0] for m in VEHICLE_MODELS]
        oem_ids = dict(cur.execute("SELECT name, oem_id FROM oem").fetchall())
        dm_ids = {oem: cur.execute("INSERT INTO device_model (model_name, firmware, oem_id) VALUES (%s, %s, %s) "
                                   "RETURNING model_id", (*DEVICE_MODELS[oem], oem_ids[oem])).fetchone()[0]
                  for oem in DEVICE_MODELS}

        ids = [vehicle_id(i) for i in range(n)]
        lender_of = rng.integers(0, len(LENDERS), size=n)
        today = datetime.now(UTC).date()
        registered = [today - timedelta(days=int(d)) for d in rng.integers(100, 2500, size=n)]
        copy_rows(cur, "vehicle", ["vehicle_id", "vin", "registered_on", "vehicle_model_id"],
                  ((vid, f"SIMVIN{i:011d}", registered[i], vm_ids[i % len(vm_ids)]) for i, vid in enumerate(ids)))
        oems = rng.choice(list(DEVICE_MODELS), size=n)
        copy_rows(cur, "device", ["serial_no", "installed_at", "model_id", "vehicle_id"],
                  ((f"SN-{i:08d}", f"{registered[i]} 10:00:00+05:30", dm_ids[oems[i]], vid) for i, vid in enumerate(ids)))
        copy_rows(cur, "vehicle_geofence", ["vehicle_id", "geofence_id"],
                  ((vid, fence_ids[lender_of[i]]) for i, vid in enumerate(ids)))

        # Borrowers: only hashes of synthetic identifiers are stored, never names or numbers.
        copy_rows(cur, "borrower", ["name_hash", "phone_hash"],
                  ((sha256(f"synthetic-borrower-{i}"), sha256(f"synthetic-phone-{i}")) for i in range(n)))
        first_borrower = cur.execute("SELECT min(borrower_id) FROM borrower").fetchone()[0]

        dpd_bucket = rng.choice(4, size=n, p=geo_cfg["days_past_due_share"])
        dpd = np.select([dpd_bucket == 0, dpd_bucket == 1, dpd_bucket == 2],
                        [0, rng.integers(1, 31, size=n), rng.integers(31, 90, size=n)], rng.integers(90, 181, size=n))
        status = np.where(dpd == 0, "current", np.where(dpd < 90, "overdue", "default"))
        amount = rng.integers(300_000, 1_500_000, size=n)
        copy_rows(cur, "loan", ["amount", "start_date", "status", "days_past_due", "lender_id", "borrower_id", "vehicle_id"],
                  ((int(amount[i]), registered[i], status[i], int(dpd[i]), lender_ids[lender_of[i]],
                    first_borrower + i, vid) for i, vid in enumerate(ids)))

        copy_rows(cur, "agent", ["display_name", "lender_id"],
                  ((f"Agent {name.split()[1][0]}-{k + 1:02d}", lid)
                   for name, lid in zip(LENDERS, lender_ids, strict=True) for k in range(AGENTS_PER_LENDER)))
        conn.commit()
        print(f"seed: done ({n} vehicles, {len(LENDERS)} lenders, {len(LENDERS) * AGENTS_PER_LENDER} agents)")


if __name__ == "__main__":
    main()
