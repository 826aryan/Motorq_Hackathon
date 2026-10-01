-- Privacy (SPEC §9): every read of an exact vehicle location is logged. Idempotent (applied by the seed service too).
CREATE TABLE IF NOT EXISTS location_access_log (
    access_id    BIGSERIAL PRIMARY KEY,
    accessed_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    lender_id    INT NOT NULL REFERENCES lender(lender_id),
    vehicle_id   TEXT NOT NULL REFERENCES vehicle(vehicle_id),
    endpoint     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS location_access_log_vehicle ON location_access_log (vehicle_id, accessed_at DESC);
