"""Runs every detector on one event and scores the signals. Kafka- and database-free, so the live
stream and the batch replay (step 6) drive the exact same code."""
from dataclasses import dataclass, field

from shared.detect.anomaly import AnomalyDetector, Baseline
from shared.detect.geofence import GeofenceDetector, GeofenceIndex
from shared.detect.risk import Decision, Loan, RiskScorer
from shared.detect.route import RouteDeviationDetector
from shared.detect.signals import Signal
from shared.roadgraph import RoadGraph
from shared.schema import CanonicalEvent


@dataclass
class Scored:
    signal: Signal
    decision: Decision


@dataclass
class EngineState:
    """Reference data loaded from PostgreSQL (or built by tests)."""
    polygons: dict[int, list[tuple[float, float]]]
    vehicle_fences: dict[str, list[int]]
    loans: dict[str, Loan]
    baselines: dict[str, Baseline] = field(default_factory=dict)
    open_case_vehicles: set[str] = field(default_factory=set)
    destinations: dict[str, list[str]] = field(default_factory=dict)   # usual home/work cells


class DetectorEngine:
    def __init__(self, cfg: dict, state: EngineState, graph: RoadGraph | None = None):
        g, a = cfg["geofence"], cfg["anomaly"]
        self.geofence = GeofenceDetector(GeofenceIndex(state.polygons, g["precision"]),
                                         state.vehicle_fences, g["exit_after"])
        self.anomaly = AnomalyDetector(a, state.baselines, a["tz_offset_h"])
        self.risk = RiskScorer(cfg["risk"], state.loans, state.open_case_vehicles)
        self.route = RouteDeviationDetector(graph, state.destinations, cfg["route"]) if graph else None

    def on_event(self, e: CanonicalEvent) -> list[Scored]:
        signals = self.geofence.on_event(e) + self.anomaly.on_event(e)
        if self.route:
            signals += self.route.on_event(e)
        return [Scored(s, self.risk.on_signal(s)) for s in signals]

    def score(self, signals: list[Signal]) -> list[Scored]:
        """Signals produced elsewhere (e.g. the fleet-wide convoy run)."""
        return [Scored(s, self.risk.on_signal(s)) for s in signals]

    def tick(self) -> list[Scored]:
        return [Scored(s, self.risk.on_signal(s)) for s in self.anomaly.tick()]
