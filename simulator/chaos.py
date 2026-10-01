"""Chaos injector (SPEC §2): damages the stream on purpose so the pipeline has real work to do.

Before encoding: GPS jitter and occasional big jumps.
After encoding: malformed payloads, duplicates (1-3 extra copies), out-of-order delays (0-120 s).
Bursts: the vehicle scheduler asks send_rate_multiplier() and emits faster during a burst.
Rates live in the `chaos` block of config.yaml and are re-read when the file changes.
"""
import dataclasses
import json
import os
import time

import numpy as np
import yaml

from shared.geo import offset_m
from simulator.encoders import Reading


class Chaos:
    def __init__(self, config_path: str, rng: np.random.Generator, check_every_s: float = 2.0):
        self.path = config_path
        self.rng = rng
        self.check_every_s = check_every_s
        self._mtime = 0.0
        self._next_check = 0.0
        self.started = time.monotonic()
        self.cfg: dict = {}
        self.reload(force=True)

    def reload(self, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now < self._next_check:
            return
        self._next_check = now + self.check_every_s
        try:
            mtime = os.path.getmtime(self.path)
            if force or mtime != self._mtime:
                with open(self.path, encoding="utf-8") as fh:
                    self.cfg = yaml.safe_load(fh)["chaos"]
                self._mtime = mtime
        except (OSError, KeyError, yaml.YAMLError):
            pass   # keep the last good config if the file is mid-edit

    # ---- bursts ----
    def send_rate_multiplier(self) -> float:
        c = self.cfg
        elapsed = time.monotonic() - self.started
        in_burst = c["burst_every_s"] > 0 and (elapsed % c["burst_every_s"]) < c["burst_duration_s"]
        return float(c["burst_multiplier"]) if in_burst else 1.0

    # ---- before encoding ----
    def add_gps_noise(self, r: Reading) -> Reading:
        c, rng = self.cfg, self.rng
        north, east = rng.normal(0, c["gps_jitter_m"], size=2)
        if rng.random() < c["gps_jump_rate"]:
            dist = rng.uniform(*c["gps_jump_m"])
            angle = rng.uniform(0, 2 * np.pi)
            north, east = dist * np.cos(angle), dist * np.sin(angle)
        lat, lon = offset_m(r.lat, r.lon, float(north), float(east))
        return dataclasses.replace(r, lat=lat, lon=lon)

    # ---- after encoding ----
    def damage(self, payload: str, oem: str, now_real: float) -> list[tuple[float, str]]:
        """Return (release_time, payload) copies to send. Most events come back unchanged, once."""
        c, rng = self.cfg, self.rng
        if rng.random() < c["malformed_rate"]:
            payload = corrupt(payload, oem, rng)
        copies = 1 + (int(rng.integers(1, 4)) if rng.random() < c["duplicate_rate"] else 0)
        out = []
        for _ in range(copies):
            delay = rng.uniform(0, c["max_delay_s"]) if rng.random() < c["out_of_order_rate"] else 0.0
            out.append((now_real + delay, payload))
        return out


def corrupt(payload: str, oem: str, rng: np.random.Generator) -> str:
    """Make a payload the parser must reject: missing field, broken delimiter, bad hex, or truncation."""
    kind = int(rng.integers(0, 3))
    if oem == "B":
        parts = payload.split("|")
        if kind == 0:
            parts.pop(int(rng.integers(2, len(parts))))            # missing field
            return "|".join(parts)
        if kind == 1:
            return payload.replace("|", ";", 3)                     # broken delimiter
        return payload[: int(rng.integers(5, len(payload) - 1))]    # truncated
    doc = json.loads(payload)
    if kind == 0:
        doc.pop(next(iter(k for k in doc if k not in ("vehicle_id", "dev"))))   # missing field
        return json.dumps(doc, separators=(",", ":"))
    if kind == 1 and oem == "C":
        doc["status"] = "0xZZ"                                      # bad hex
        return json.dumps(doc, separators=(",", ":"))
    return payload[: int(rng.integers(5, len(payload) - 1))]        # truncated JSON
