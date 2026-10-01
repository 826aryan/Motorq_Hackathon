"""Dev helper: log in as each lender and call every endpoint once (reads DEMO_PASSWORD / PARTNER_KEYS from .env)."""
import time
from pathlib import Path

import httpx

ENV = dict(line.split("=", 1) for line in Path(".env").read_text().splitlines() if "=" in line and not line.startswith("#"))
API = "http://localhost:8000"


def main() -> None:
    tok = httpx.post(f"{API}/auth/login", json={"username": "alpha", "password": ENV["DEMO_PASSWORD"]}).json()["token"]
    h = {"Authorization": f"Bearer {tok}"}
    bad = httpx.post(f"{API}/auth/login", json={"username": "alpha", "password": "wrong"})
    print("login ok; wrong password ->", bad.status_code, "; no token ->", httpx.get(f"{API}/risk/top").status_code)
    live = httpx.get(f"{API}/vehicles/live", params={"bbox": "77.50,12.90,77.70,13.05"}, headers=h).json()["vehicles"]
    print("live vehicles in view:", len(live))
    top = httpx.get(f"{API}/risk/top", params={"k": 5}, headers=h).json()["top"]
    print("top 5:", top)
    vid = (top or live)[0]["vehicle_id" if top else "id"]
    d = httpx.get(f"{API}/vehicles/{vid}", headers=h).json()
    print("detail:", vid, d["make"], d["model"], "dpd", d["days_past_due"], "score", d["risk_score"], d["score_breakdown"])
    t = httpx.get(f"{API}/vehicles/{vid}/trail", params={"from": time.time() - 3600, "to": time.time()}, headers=h).json()
    print("trail points last hour:", len(t["points"]))
    other = next(v for v in ENV and [f"VH-{i:06d}" for i in range(1, 400)] if v != vid)
    print("other lender's vehicle probe ->", {httpx.get(f"{API}/vehicles/{o}", headers=h).status_code for o in [other]})
    print("alerts:", len(httpx.get(f"{API}/alerts", headers=h).json()["alerts"]),
          " convoys:", len(httpx.get(f"{API}/convoys/active", headers=h).json()["convoys"]))
    case = httpx.post(f"{API}/cases", json={"vehicle_id": vid}, headers=h).json()
    near = httpx.get(f"{API}/route/nearest-agent", params={"vehicle": vid}, headers=h).json()
    print("case", case, "nearest agent", near.get("agent_id"), "eta_s", near.get("eta_s"), "path pts", len(near.get("path", [])))
    upd = httpx.patch(f"{API}/cases/{case['case_id']}", json={"agent_id": int(near["agent_id"])}, headers=h).json()
    print("assigned:", upd["status"], upd["agent"], "route eta_s", (upd.get("route") or {}).get("eta_s"))
    hist = httpx.get(f"{API}/risk/history", params={"from": time.time() - 86400 * 2, "to": time.time()}, headers=h).json()
    print("history top:", hist["top"][:3])
    key = ENV["PARTNER_KEYS"].split(",")[0].split(":", 1)[1]
    share = httpx.get(f"{API}/share/insights", headers={"X-Partner-Key": key}).json()
    print("share: cells", len(share["cells"]), "events", len(share["events"]), "sample", share["cells"][:1], share["events"][:1])
    print("share with bad key ->", httpx.get(f"{API}/share/insights", headers={"X-Partner-Key": "nope"}).status_code)
    sh = httpx.get(f"{API}/system/health", headers=h, timeout=30).json()
    print("health: lag", sh["kafka_lag"], "live", sh["vehicles_live"])


if __name__ == "__main__":
    main()
