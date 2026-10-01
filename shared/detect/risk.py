"""Risk scorer - Top-K (SPEC §4.5).

risk = min(100, (sum_i w_i * s_i) * m_loan)
  s_i     signal strength 0..1, fading linearly to 0 over 24 h
  m_loan  1.0 (loan current) rising to 2.0 at 90+ days past due
Scores go to the Redis sorted set risk:top (Top-K = one ZREVRANGE). A score crossing the threshold
opens a recovery case. Alert rows are written per signal, at most once per cooldown per vehicle+signal.
"""
from dataclasses import dataclass

from shared.detect.signals import Signal


@dataclass
class Loan:
    loan_id: int
    lender_id: int
    days_past_due: int


@dataclass
class Decision:
    score: float
    write_alert: bool     # new alert row (not a repeat inside the cooldown)
    open_case: bool       # score crossed the threshold and the loan has no open case yet


class RiskScorer:
    def __init__(self, cfg: dict, loans: dict[str, Loan], open_case_vehicles: set[str] | None = None):
        self.weights: dict[str, float] = cfg["weights"]
        self.decay_s = cfg["decay_s"]
        self.max_dpd = cfg["max_days_past_due"]
        self.threshold = cfg["case_threshold"]
        self.cooldown_s = cfg["alert_cooldown_s"]
        self.loans = loans
        self.open_cases = open_case_vehicles or set()
        self.active: dict[str, dict[str, tuple[float, float]]] = {}   # vehicle -> code -> (strength, ts)
        self.last_alert: dict[tuple[str, str], float] = {}

    def multiplier(self, vehicle_id: str) -> float:
        loan = self.loans.get(vehicle_id)
        dpd = loan.days_past_due if loan else 0
        return 1.0 + min(dpd, self.max_dpd) / self.max_dpd

    def score(self, vehicle_id: str, now: float) -> float:
        total = 0.0
        for code, (strength, ts) in self.active.get(vehicle_id, {}).items():
            fade = max(0.0, 1.0 - (now - ts) / self.decay_s)
            total += self.weights.get(code, 0) * strength * fade
        return round(min(100.0, total * self.multiplier(vehicle_id)), 2)

    def on_signal(self, sig: Signal) -> Decision:
        signals = self.active.setdefault(sig.vehicle_id, {})
        old = signals.get(sig.code)
        if old is None or sig.strength >= self._faded(old, sig.ts):
            signals[sig.code] = (sig.strength, sig.ts)
        score = self.score(sig.vehicle_id, sig.ts)

        key = (sig.vehicle_id, sig.code)
        write_alert = sig.ts - self.last_alert.get(key, float("-inf")) >= self.cooldown_s
        if write_alert:
            self.last_alert[key] = sig.ts
        open_case = score > self.threshold and sig.vehicle_id not in self.open_cases
        if open_case:
            self.open_cases.add(sig.vehicle_id)
        return Decision(score, write_alert, open_case)

    def rescore_all(self, now: float) -> dict[str, float]:
        """Scores fade with time even without new signals; returns current scores and forgets zeros."""
        out = {}
        for vid in list(self.active):
            s = self.score(vid, now)
            out[vid] = s
            if s == 0:
                del self.active[vid]
        return out

    def _faded(self, entry: tuple[float, float], now: float) -> float:
        strength, ts = entry
        return strength * max(0.0, 1.0 - (now - ts) / self.decay_s)
