"""What every detector emits. The risk scorer turns signals into one score per vehicle (SPEC §4)."""
from dataclasses import dataclass, field

TOW_SUSPECTED = "TOW_SUSPECTED"
GEOFENCE_EXIT = "GEOFENCE_EXIT"
TAMPER_SUSPECTED = "TAMPER_SUSPECTED"
CONVOY = "CONVOY"
ROUTE_DEVIATION = "ROUTE_DEVIATION"
NIGHT_MOVEMENT = "NIGHT_MOVEMENT"
SPEED_SPIKE = "SPEED_SPIKE"


@dataclass
class Signal:
    vehicle_id: str
    code: str
    ts: float                 # event time the signal refers to
    lat: float
    lon: float
    strength: float = 1.0     # 0..1
    detail: dict = field(default_factory=dict)
