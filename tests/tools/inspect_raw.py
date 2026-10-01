"""Dev helper: summarise what is currently in raw.telemetry (moving vehicles, ign-off movers, junk).

    python -m tests.tools.inspect_raw        (needs the stack running; reads via localhost:19092)
"""
import collections
import json
import re

from confluent_kafka import Consumer, TopicPartition

TOPIC = "raw.telemetry"


def main() -> None:
    c = Consumer({"bootstrap.servers": "localhost:19092", "group.id": "inspect-raw",
                  "enable.auto.commit": False})
    parts = c.list_topics(TOPIC, timeout=10).topics[TOPIC].partitions
    ends = {p: c.get_watermark_offsets(TopicPartition(TOPIC, p), timeout=10)[1] for p in parts}
    c.assign([TopicPartition(TOPIC, p, 0) for p in parts])
    remaining = {p for p, end in ends.items() if end > 0}
    counts, moving, ign_off_movers = collections.Counter(), set(), set()
    while remaining:
        for m in c.consume(5000, timeout=5):
            if m.error() or m.partition() not in remaining:
                continue
            if m.offset() >= ends[m.partition()] - 1:
                remaining.discard(m.partition())
            v = m.value().decode(errors="replace")
            counts["total"] += 1
            vid = (re.search(r"VH-\d{6}", v) or [None])[0]
            try:
                if v.startswith("B|"):
                    p = v.split("|")
                    if len(p) != 11:
                        raise ValueError("malformed B")
                    ign, spd, oem = p[6] == "1", float(p[5]), "B"
                else:
                    d = json.loads(v)
                    if "dev" in d:
                        ign, spd, oem = bool(int(d["status"], 16) & 1), d["motion"]["spd_ms"], "C"
                    else:
                        ign, spd, oem = d["ignition"], d["speed_kmh"], "A"
            except (ValueError, KeyError, IndexError):
                counts["unparseable"] += 1
                continue
            counts[f"oem_{oem}"] += 1
            if spd > 0:
                (moving if ign else ign_off_movers).add(vid)
    c.close()
    print(dict(counts))
    print(f"vehicles seen moving: {len(moving)}   moving with ignition OFF: {sorted(ign_off_movers)}")


if __name__ == "__main__":
    main()
