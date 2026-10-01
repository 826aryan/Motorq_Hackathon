"""Kafka loop around the Pipeline: raw.telemetry -> normalized.events / late.events / dead.letter.

Consumer group `pipeline`; topics are keyed by vehicle_id, so each vehicle's events (and its reorder
and noise state) stay on one worker. Scale by running more pipeline containers.
Counters are mirrored to the Redis hash `stats:pipeline` for the System health screen.
"""
import json
import logging
import time
from pathlib import Path

import redis
import yaml
from confluent_kafka import Consumer, Producer

from pipeline.core import Output, Pipeline
from pipeline.dedup import RedisBloom
from shared.config import settings

log = logging.getLogger("pipeline")
RAW, NORMALIZED, LATE, DEAD = "raw.telemetry", "normalized.events", "late.events", "dead.letter"


class Runner:
    def __init__(self, config_path: str = "pipeline/config.yaml"):
        self.cfg = yaml.safe_load(Path(config_path).read_text())
        self.redis = redis.Redis.from_url(settings.redis_url)
        self.pipeline = Pipeline.from_config(self.cfg, RedisBloom(self.redis))
        k = self.cfg["kafka"]
        self.consumer = Consumer({
            "bootstrap.servers": settings.kafka_bootstrap,
            "group.id": k["group_id"],
            "auto.offset.reset": "earliest",
            "enable.auto.commit": True,
        })
        self.producer = Producer({"bootstrap.servers": settings.kafka_bootstrap, "linger.ms": 20,
                                  "compression.type": "lz4", "queue.buffering.max.messages": 500_000})
        self.running = True
        self._flushed_counts: dict = {}

    def _on_revoke(self, _consumer, _partitions) -> None:
        # Another worker takes over these vehicles: hand over everything we are holding, in order.
        self._publish(self.pipeline.flush())
        self.producer.flush(10)

    def _publish(self, out: Output) -> None:
        for e in out.normalized:
            self._produce(NORMALIZED, e.vehicle_id, e.to_json())
        for e in out.late:
            self._produce(LATE, e.vehicle_id, e.to_json())
        for d in out.dead:
            self._produce(DEAD, None, d)
        self.producer.poll(0)

    def _produce(self, topic: str, key: str | None, value: bytes) -> None:
        while True:
            try:
                self.producer.produce(topic, value=value, key=key)
                return
            except BufferError:
                self.producer.poll(0.05)

    def _push_stats(self) -> None:
        delta = {k: v - self._flushed_counts.get(k, 0) for k, v in self.pipeline.counts.items()}
        delta = {k: v for k, v in delta.items() if v}
        if delta:
            pipe = self.redis.pipeline()
            for k, v in delta.items():
                pipe.hincrby("stats:pipeline", k, v)
            pipe.execute()
            self._flushed_counts = dict(self.pipeline.counts)
        if self.pipeline.samples:                      # newest first, capped (the Pipeline screen reads it)
            pipe = self.redis.pipeline()
            pipe.lpush("pipeline:samples", *[json.dumps(x) for x in self.pipeline.samples])
            pipe.ltrim("pipeline:samples", 0, self.cfg.get("samples_kept", 300) - 1)
            pipe.execute()
            self.pipeline.samples = []

    def run(self) -> None:
        k = self.cfg["kafka"]
        self.consumer.subscribe([RAW], on_revoke=self._on_revoke)
        last_stats = time.monotonic()
        log.info("pipeline consuming %s", RAW)
        while self.running:
            msgs = self.consumer.consume(k["batch_size"], timeout=k["poll_timeout_s"])
            batch = []
            for m in msgs:
                if m.error():
                    log.warning("kafka error: %s", m.error())
                    continue
                _, ts_ms = m.timestamp()
                batch.append((m.value(), ts_ms / 1000 if ts_ms > 0 else time.time()))
            self._publish(self.pipeline.process(batch, time.time()))
            if time.monotonic() - last_stats > 2:
                self._push_stats()
                last_stats = time.monotonic()
        self._publish(self.pipeline.flush())
        self.producer.flush(10)
        self.consumer.close()
