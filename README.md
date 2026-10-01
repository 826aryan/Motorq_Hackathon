# 🚗 Asset Recovery Platform

> A real-time telemetry intelligence platform for vehicle lenders — built for the **MotorQ Hackathon**.

[![CI](https://github.com/826aryan/Motorq_Hackathon/actions/workflows/ci.yml/badge.svg)](https://github.com/826aryan/Motorq_Hackathon/actions/workflows/ci.yml)
![Python](https://img.shields.io/badge/Python-3.12-blue?logo=python)
![TypeScript](https://img.shields.io/badge/TypeScript-React-3178C6?logo=typescript)
![License](https://img.shields.io/badge/license-MIT-green)

---

## 📽️ Demo Video

▶️ **[Watch the full demo on Google Drive](https://drive.google.com/file/d/1I5dioUDOJ0yaEWzD-m3xExyjHIlHT4pV/view?usp=sharing)**

---

## 🧩 What It Does

Banks and NBFCs that finance vehicles face a critical problem: when borrowers default, locating and recovering the asset is expensive, slow, and often too late. This platform solves that by turning raw vehicle telemetry into real-time, actionable recovery intelligence.

**Core flow:** `simulate → ingest → clean → detect → store → serve`

| Problem Statement | Coverage |
|---|---|
| 🎯 Asset Recovery | Primary — locate vehicles, detect abnormal movement |
| 🔌 Multi-OEM Normalization | Ingestion layer normalizes 3 OEM telemetry formats |
| 🔒 Privacy-safe Data Sharing | Partner API with pseudonymized borrower fields |

---

## ✨ Features

### 🛰️ Real-time Detection Engine
- **Tow Detection** — ignition-off movement beyond 500m in a sliding 10-min window
- **Geofence Exit** — geohash-6 based boundary monitoring (~1.2 × 0.6 km cells)
- **GPS Tamper Detection** — voltage drop + GPS jump signature recognition
- **Route Deviation** — A* shortest-path comparison with 2 km / 5 min tolerance
- **Convoy Detection** — Connected-components clustering over 30-minute rolling windows
- **Night Movement** — Suspicious activity during off-hours (00:00–05:00)

### 📊 Risk Scoring & Leaderboard
Weighted multi-signal risk scorer with 24-hour exponential decay:

| Signal | Weight |
|---|---|
| TOW_SUSPECTED | 30 |
| GEOFENCE_EXIT | 25 |
| TAMPER_SUSPECTED | 25 |
| CONVOY | 20 |
| ROUTE_DEVIATION | 15 |
| NIGHT_MOVEMENT | 10 |

Scores above 70 automatically create recovery cases and fire alerts.

### 🗺️ Live Map Dashboard
- MapLibre GL map with 100k vehicle dots, color-coded by risk score
- WebSocket-pushed positions, alerts, and Top-K leaderboard (no polling)
- Switch between 1k / 10k / 100k active vehicles live from the UI
- Full vehicle drill-down: trajectory replay, alert timeline, route overlay
- Light / dark theme toggle

### 🏭 Simulated Fleet (Bengaluru road network)
- **100,000 vehicles** on a real OSM road graph (8 km radius, central Bengaluru)
- 3 OEM telemetry formats (A 40%, B 35%, C 25%)
- Realistic personas: towed, absconding, GPS-tampered, convoy groups
- Chaos injection: duplicates (5%), out-of-order (5%), bursts (10×), malformed (1%), GPS jitter

### 📦 Batch Analytics
- DuckDB over Parquet files for historical analysis
- Daily reports, fleet-level statistics, and Parquet history export

---

## 🏗️ Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│                        React Dashboard (web)                        │
│          Live Map · Alerts · Leaderboard · Cases · Reports          │
└──────────────────────────┬──────────────────────────────────────────┘
                           │ REST + WebSocket
┌──────────────────────────▼──────────────────────────────────────────┐
│                        FastAPI  (api :8000)                         │
│      JWT auth · /vehicles · /alerts · /cases · /ws · /reports       │
└────┬──────────────────┬───────────────────────┬─────────────────────┘
     │                  │                       │
┌────▼────┐   ┌─────────▼──────────┐   ┌───────▼───────┐
│Postgres │   │  Redis Stack       │   │ TimescaleDB   │
│PostGIS  │   │  GEO · Bloom ·     │   │  raw telemetry│
│3NF core │   │  Sorted Sets ·     │   │  hypertable   │
│loans,   │   │  Pub/Sub           │   └───────────────┘
│geofences│   │  pos:live, risk:top│
└─────────┘   └──────┬─────────────┘
                     │
        ┌────────────┼────────────┐
        │            │            │
┌───────▼──┐  ┌──────▼──────┐  ┌─▼──────────┐
│ pipeline │  │  detectors  │  │  storage   │
│ (×3)     │  │  (×3)       │  │            │
│ parse·   │  │ geofence·   │  │ normalized │
│ dedup·   │  │ tow·convoy· │  │ → Timescale│
│ reorder· │  │ route·risk  │  └────────────┘
│ noise    │  └─────────────┘
└──────────┘
        ▲
        │  Kafka topics (Redpanda)
        │  raw.telemetry · normalized.events
        │  detector.signals · dead.letter · late.events
        │
┌───────┴──────┐         ┌──────────────┐
│  gateway     │◄────────│  simulator   │
│  :8001       │  HTTP   │  100k vehs   │
│  ingest API  │  batch  │  6 workers   │
└──────────────┘         └──────────────┘
```

---

## 🔧 Tech Stack

| Layer | Technology |
|---|---|
| **Backend** | Python 3.12, FastAPI, uvicorn |
| **Frontend** | TypeScript, React, MapLibre GL |
| **Message Broker** | Redpanda (Kafka-compatible), 12 partitions |
| **Hot Store** | Redis Stack (GEO, RedisBloom, sorted sets, Pub/Sub) |
| **Relational DB** | PostgreSQL 16 + PostGIS (3NF schema) |
| **Time-series DB** | TimescaleDB 2.17 (telemetry hypertable) |
| **Batch / Analytics** | DuckDB over Parquet |
| **Road Graph** | osmnx + networkx (A*, Dijkstra) |
| **Containerization** | Docker Compose |
| **CI/CD** | GitHub Actions |
| **Testing** | pytest, Testcontainers |

---

## 🚀 Quick Start

### Prerequisites
- Docker & Docker Compose
- 8 GB RAM recommended (100k vehicles mode)

### 1. Clone & configure

```bash
git clone https://github.com/826aryan/Motorq_Hackathon.git
cd Motorq_Hackathon
cp .env.example .env
# Edit .env — set your own passwords / secrets
```

### 2. Build the road graph (one-time)

```bash
docker compose -f infra/docker-compose.yml --profile tools run --rm graph-builder
```

> Downloads the Bengaluru OSM road graph and saves it to `data/graph/road_graph.npz`.

### 3. Start the full stack

```bash
docker compose -f infra/docker-compose.yml --env-file .env up -d --build
```

Services start in dependency order. The seed container populates 100k synthetic loans, geofences, and lender accounts, then exits.

### 4. Open the dashboard

```
http://localhost:8080
```

Sign in with any lender (Alpha / Beta / Gamma) and the `DEMO_PASSWORD` from your `.env`.

### Service ports

| Service | Host port |
|---|---|
| Web dashboard | `8080` |
| API (REST + WS) | `8000` |
| Gateway (ingest) | `8001` |
| Simulator health | `8002` |
| Batch | `8005` |
| Storage | `8006` |
| Pipeline replicas | `8030–8039` |
| Detector replicas | `8040–8049` |
| Redpanda (Kafka) | `19092` |
| PostgreSQL | `15432` |
| TimescaleDB | `15433` |
| Redis | `16379` |

---

## 📁 Repository Layout

```
simulator/    # 100k-vehicle fleet on Bengaluru road graph; chaos injector
gateway/      # HTTP ingest API → raw.telemetry Kafka topic
pipeline/     # parse → Bloom dedup → reorder buffer → noise filter (×3 replicas)
detectors/    # geofence · tow · tamper · route · convoy · risk scorer (×3 replicas)
storage/      # normalized events → TimescaleDB
batch/        # DuckDB batch jobs over Parquet history
api/          # FastAPI REST + WebSocket served to the dashboard
web/          # React + MapLibre dashboard (TypeScript)
shared/       # canonical schema, geo utils, road graph helpers (imported by all services)
db/
  migrations/ # 3NF PostgreSQL schema (3 migrations, auto-applied on first boot)
  timescale/  # TimescaleDB hypertable DDL
  seed/       # synthetic lenders, loans, geofences
tests/
  unit/       # algorithm unit tests (Bloom, geo, parser, detectors …)
  chaos/      # chaos pipeline tests
  integration/# end-to-end towed-vehicle scenario
  load/       # load test runner + results
infra/        # docker-compose.yml, Dockerfiles, cloud deploy script
.github/
  workflows/  # ci.yml (test), deploy.yml (cloud)
```

---

## 🧪 Running Tests

```bash
# Install dev dependencies
pip install -r requirements-dev.txt

# Unit tests only (no Docker needed)
pytest tests/unit/ -v

# All tests (requires Docker for Testcontainers)
pytest tests/ -v
```

---

## 🔑 Algorithms Used

| Algorithm | Where |
|---|---|
| **Bloom Filter** | Pipeline dedup — rejects seen event IDs in O(1) with zero false negatives |
| **Sliding Window** | Anomaly detector — tow detection, tamper silence, speed spike |
| **Geohash** | Geofence boundary encoding and lookup |
| **A\* / Dijkstra** | Route deviation — expected path vs actual GPS trace |
| **Top-K (sorted set)** | Redis leaderboard of highest-risk vehicles |
| **Connected Components** | Convoy detection — clusters vehicles moving together |
| **Regex parsing** | OEM format normalization (3 distinct schemas) |

---

## 🔒 Security Notes

- Secrets live in `.env` (git-ignored); `.env.example` contains only placeholder values
- All data is fully synthetic — no real PII; borrower fields are hashes only
- JWT-authenticated API; per-lender data isolation enforced server-side
- Partner API pseudonymizes all borrower identifiers before sharing

---

## 👥 Team

Built at the **MotorQ Hackathon** — [826aryan](https://github.com/826aryan)
