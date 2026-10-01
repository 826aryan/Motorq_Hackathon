-- Fixed reference rows every environment needs (SPEC §1 OEM formats, §4 signal weights).
-- SPEED_SPIKE weight is not in the spec; set to 5 as a placeholder (flagged for review).

INSERT INTO oem (name, payload_format) VALUES
    ('A', 'flat_json'),
    ('B', 'pipe_text'),
    ('C', 'nested_json_hex');

INSERT INTO signal_type (code, weight) VALUES
    ('TOW_SUSPECTED', 30),
    ('GEOFENCE_EXIT', 25),
    ('TAMPER_SUSPECTED', 25),
    ('CONVOY', 20),
    ('ROUTE_DEVIATION', 15),
    ('NIGHT_MOVEMENT', 10),
    ('SPEED_SPIKE', 5);
