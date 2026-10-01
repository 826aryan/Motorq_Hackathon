-- Asset Recovery Platform: 3NF relational core (SPEC §5).
-- Runs automatically on first start of the postgres container.
-- 3NF notes: make/model/year live in vehicle_model (they depend on the model, not the vehicle);
-- recovery_case has no vehicle_id (derivable via loan); many-to-many relations use link tables;
-- borrower PII is stored only as hashes.

CREATE EXTENSION IF NOT EXISTS postgis;

CREATE TABLE oem (
    oem_id          SMALLSERIAL PRIMARY KEY,
    name            TEXT NOT NULL UNIQUE,              -- 'A', 'B', 'C'
    payload_format  TEXT NOT NULL                      -- 'flat_json', 'pipe_text', 'nested_json_hex'
);

CREATE TABLE device_model (
    model_id    SERIAL PRIMARY KEY,
    model_name  TEXT NOT NULL,
    firmware    TEXT NOT NULL,
    oem_id      SMALLINT NOT NULL REFERENCES oem(oem_id),
    UNIQUE (oem_id, model_name, firmware)
);

CREATE TABLE vehicle_model (
    vehicle_model_id  SERIAL PRIMARY KEY,
    make              TEXT NOT NULL,
    model             TEXT NOT NULL,
    year              SMALLINT NOT NULL CHECK (year BETWEEN 1990 AND 2100),
    UNIQUE (make, model, year)
);

CREATE TABLE vehicle (
    vehicle_id        TEXT PRIMARY KEY,                -- e.g. 'VH-004213'
    vin               CHAR(17) NOT NULL UNIQUE,
    registered_on     DATE NOT NULL,
    vehicle_model_id  INT NOT NULL REFERENCES vehicle_model(vehicle_model_id)
);

CREATE TABLE device (
    device_id     SERIAL PRIMARY KEY,
    serial_no     TEXT NOT NULL UNIQUE,
    installed_at  TIMESTAMPTZ NOT NULL,
    model_id      INT NOT NULL REFERENCES device_model(model_id),
    vehicle_id    TEXT NOT NULL REFERENCES vehicle(vehicle_id)
);
CREATE INDEX ON device (vehicle_id);

CREATE TABLE lender (
    lender_id  SERIAL PRIMARY KEY,
    name       TEXT NOT NULL UNIQUE
);

CREATE TABLE borrower (
    borrower_id  SERIAL PRIMARY KEY,
    name_hash    CHAR(64) NOT NULL,                    -- SHA-256 hex, never the raw name
    phone_hash   CHAR(64) NOT NULL
);

CREATE TABLE loan (
    loan_id        SERIAL PRIMARY KEY,
    amount         NUMERIC(12, 2) NOT NULL CHECK (amount > 0),
    start_date     DATE NOT NULL,
    status         TEXT NOT NULL CHECK (status IN ('current', 'overdue', 'default', 'closed')),
    days_past_due  INT NOT NULL DEFAULT 0 CHECK (days_past_due >= 0),
    lender_id      INT NOT NULL REFERENCES lender(lender_id),
    borrower_id    INT NOT NULL REFERENCES borrower(borrower_id),
    vehicle_id     TEXT NOT NULL REFERENCES vehicle(vehicle_id)
);
CREATE INDEX ON loan (lender_id);
CREATE INDEX ON loan (vehicle_id);

CREATE TABLE geofence (
    geofence_id  SERIAL PRIMARY KEY,
    name         TEXT NOT NULL,
    area         GEOGRAPHY(POLYGON, 4326) NOT NULL,
    lender_id    INT NOT NULL REFERENCES lender(lender_id)
);
CREATE INDEX ON geofence USING GIST (area);

CREATE TABLE vehicle_geofence (
    vehicle_id   TEXT NOT NULL REFERENCES vehicle(vehicle_id),
    geofence_id  INT NOT NULL REFERENCES geofence(geofence_id),
    PRIMARY KEY (vehicle_id, geofence_id)
);

CREATE TABLE signal_type (
    signal_type_id  SMALLSERIAL PRIMARY KEY,
    code            TEXT NOT NULL UNIQUE,
    weight          SMALLINT NOT NULL CHECK (weight >= 0)
);

CREATE TABLE alert (
    alert_id        BIGSERIAL PRIMARY KEY,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    lat             DOUBLE PRECISION NOT NULL,
    lon             DOUBLE PRECISION NOT NULL,
    risk_score      NUMERIC(5, 2) NOT NULL CHECK (risk_score BETWEEN 0 AND 100),
    vehicle_id      TEXT NOT NULL REFERENCES vehicle(vehicle_id),
    signal_type_id  SMALLINT NOT NULL REFERENCES signal_type(signal_type_id)
);
CREATE INDEX ON alert (vehicle_id, created_at DESC);

CREATE TABLE agent (
    agent_id      SERIAL PRIMARY KEY,
    display_name  TEXT NOT NULL,
    lender_id     INT NOT NULL REFERENCES lender(lender_id)
);

CREATE TABLE recovery_case (
    case_id    SERIAL PRIMARY KEY,
    opened_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    status     TEXT NOT NULL CHECK (status IN ('open', 'in_progress', 'recovered', 'closed')),
    loan_id    INT NOT NULL REFERENCES loan(loan_id),
    agent_id   INT REFERENCES agent(agent_id)           -- null until an agent is assigned
);

CREATE TABLE case_alert (
    case_id   INT NOT NULL REFERENCES recovery_case(case_id),
    alert_id  BIGINT NOT NULL REFERENCES alert(alert_id),
    PRIMARY KEY (case_id, alert_id)
);

CREATE TABLE vehicle_baseline (
    vehicle_id    TEXT PRIMARY KEY REFERENCES vehicle(vehicle_id),
    home_cell     VARCHAR(7),
    work_cell     VARCHAR(7),
    active_hours  INT4RANGE,                             -- usual driving hours, e.g. [8,20)
    avg_speed     REAL,
    std_speed     REAL
);

CREATE TABLE convoy (
    convoy_id    SERIAL PRIMARY KEY,
    detected_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE convoy_member (
    convoy_id   INT NOT NULL REFERENCES convoy(convoy_id),
    vehicle_id  TEXT NOT NULL REFERENCES vehicle(vehicle_id),
    PRIMARY KEY (convoy_id, vehicle_id)
);
