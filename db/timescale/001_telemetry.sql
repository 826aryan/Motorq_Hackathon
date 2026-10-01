-- Recent telemetry hypertable (SPEC §5): 1-day chunks, compress after 2 days, drop after 30
-- (the batch job exports to Parquet before the drop).

CREATE EXTENSION IF NOT EXISTS timescaledb;

CREATE TABLE telemetry (
    vehicle_id   TEXT NOT NULL,
    device_ts    TIMESTAMPTZ NOT NULL,
    event_id     TEXT NOT NULL,
    oem          CHAR(1) NOT NULL,
    seq          BIGINT NOT NULL,
    ingest_ts    TIMESTAMPTZ NOT NULL,
    lat          DOUBLE PRECISION NOT NULL,
    lon          DOUBLE PRECISION NOT NULL,
    geohash      VARCHAR(7) NOT NULL,
    speed_kmh    REAL,
    heading_deg  REAL,
    ignition     BOOLEAN,
    battery_v    REAL,
    odometer_km  REAL,
    flags        TEXT[] NOT NULL DEFAULT '{}',
    PRIMARY KEY (vehicle_id, device_ts, event_id)
);

SELECT create_hypertable('telemetry', by_range('device_ts', INTERVAL '1 day'));

ALTER TABLE telemetry SET (
    timescaledb.compress,
    timescaledb.compress_segmentby = 'vehicle_id',
    timescaledb.compress_orderby = 'device_ts'
);
SELECT add_compression_policy('telemetry', INTERVAL '2 days');
SELECT add_retention_policy('telemetry', INTERVAL '30 days');
