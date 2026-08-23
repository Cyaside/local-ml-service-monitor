import asyncio
import json
from datetime import datetime, timezone, timedelta
from types import SimpleNamespace
import numpy as np
import pytest
import httpx
from service_monitor.checkout.app import create_app
from service_monitor.checkout.scenarios import Scenarios, ScenarioConfig
from service_monitor.checkout.telemetry import Telemetry, percentile95
from service_monitor.schemas import MetricsBatch
from service_monitor.storage import Store, export_csv, history
from service_monitor.collector import Processor, format_monitor_output
from service_monitor.ml.features import prepare, reference_from_frame


class Clock:
    def __init__(self):
        self.seconds = 0
    def mono(self):
        return self.seconds
    def wall(self):
        return datetime(2026, 1, 1, tzinfo=timezone.utc)+timedelta(seconds=self.seconds)
    def advance(self, amount=10):
        self.seconds += amount


class Process:
    def cpu_percent(self, interval):
        return 2.5
    def memory_info(self):
        return SimpleNamespace(rss=80*1024*1024)


def setup_telemetry():
    clock = Clock()
    scenarios = Scenarios(clock.mono, clock.wall)
    telemetry = Telemetry("checkout-local", scenarios, Process(), clock.mono, clock.wall)
    return clock, scenarios, telemetry


def populate(telemetry, clock, count=40):
    for _ in range(count):
        for status in [200]*9+[500]:
            telemetry.record(100, status)
        clock.advance()
        telemetry.sample()


def test_measured_statistics_and_idle():
    clock, _, t = setup_telemetry()
    for duration in (10, 20, 30, 40, 50):
        t.record(duration, 200)
    clock.advance()
    row = t.sample()
    assert row["latency_p95_ms"] == pytest.approx(48)
    assert row["request_rate_rpm"] == 30 and row["memory_rss_mib"] == 80
    assert row["source"] == "measured" and row["sample_status"] == "valid"
    clock.advance()
    idle = t.sample()
    assert idle["sample_status"] == "no_traffic"
    assert idle["phase"] == "idle" and idle["error_rate"] is None
    MetricsBatch.model_validate(t.since())


def test_memory_cap_expiry_conflict_and_annotations():
    clock = Clock()
    s = Scenarios(clock.mono, clock.wall)
    event = s.start(ScenarioConfig(name="memory-growth", duration_seconds=50, step_mib=2, cap_mib=3))
    with pytest.raises(ValueError, match="already active"):
        s.start(ScenarioConfig(name="error-burst"))
    for _ in range(4):
        clock.advance()
        s.tick()
    assert s.allocated_mib == 3 and sum(map(len, s.blocks)) == 3*1024*1024
    assert s.annotation(clock.wall()-timedelta(seconds=10), clock.wall()) == ("fault", event["fault_id"])
    clock.advance(11)
    s.tick()
    assert s.active is None and not s.blocks and s.events[-1]["reason"] == "expired"
    clock.advance()
    assert s.annotation(clock.wall()-timedelta(seconds=10), clock.wall())[0] == "recovery"


def test_gradual_and_combined_effects_are_bounded():
    clock = Clock()
    scenarios = Scenarios(clock.mono, clock.wall)
    scenarios.start(ScenarioConfig(name="gradual-latency", duration_seconds=100, delay_ms=800))
    assert scenarios.effects(.002) == pytest.approx((0, .002))
    clock.advance(50)
    assert scenarios.effects(.002) == pytest.approx((.4, .002))
    clock.advance(50)
    assert scenarios.effects(.002) == pytest.approx((0, .002))
    scenarios.start(ScenarioConfig(name="combined-degradation", duration_seconds=30,
                                   delay_ms=350, error_probability=.12))
    assert scenarios.effects(.002) == pytest.approx((.35, .12))


def test_buffer_truncation_and_restart_cursor():
    clock, _, t = setup_telemetry()
    populate(t, clock, 65)
    payload = t.since(t.run_id, 0)
    assert len(payload["samples"]) == 60 and payload["history_truncated"]
    assert len(t.since(t.run_id, 64)["samples"]) == 0
    assert len(t.since("old-run", 999)["samples"]) == 60


def test_http_exclusions_fault_validation_and_dev_gate():
    async def check():
        private_app = create_app(enable_dev=False, baseline_error_probability=0)
        async with private_app.router.lifespan_context(private_app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=private_app), base_url="http://test") as client:
                assert (await client.post("/dev/reset")).status_code == 404
        app = create_app(enable_dev=True, baseline_error_probability=0)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                for _ in range(3):
                    assert (await client.get("/health")).status_code == 200
                    assert (await client.get("/metrics")).status_code == 200
                t = app.state.telemetry
                assert t.count == 0
                assert (await client.post("/checkout", json={"sku": "a", "quantity": 1})).status_code == 200
                assert (await client.post("/dev/scenario", json={"name": "error-burst", "error_probability": 1})).status_code == 200
                assert (await client.post("/checkout", json={"sku": "a"})).status_code == 500
                assert t.count == 2 and t.errors == 1
                assert (await client.post("/dev/scenario", json={"name": "memory-growth", "cap_mib": 999})).status_code == 422
                assert (await client.post("/dev/reset")).status_code == 200
                assert (await client.post("/checkout", json={"sku": "a"})).status_code == 200
                assert (await client.post("/dev/scenario", json={"name": "slow-response", "delay_ms": 300})).status_code == 200
                import time
                began = time.perf_counter()
                assert (await client.post("/checkout", json={"sku": "a"})).status_code == 200
                assert time.perf_counter()-began >= .38
        assert app.state.sampler.cancelled()
    asyncio.run(check())


def test_sqlite_dedupe_export_and_writer_lock(tmp_path):
    clock, _, t = setup_telemetry()
    populate(t, clock)
    db = tmp_path / "metrics.sqlite"
    with Store(db) as store:
        processor = Processor(store)
        assert len(processor.process(t.since())) == 40
        assert processor.process(t.since()) == []
        with pytest.raises(ValueError, match="writer"):
            Store(db)
        assert export_csv(db, tmp_path / "data.csv") == 40
        import pandas as pd
        frame = pd.read_csv(tmp_path / "data.csv")
        assert len(prepare(frame, reference_from_frame(frame))[0]) == 10
    with Store(db):
        pass


def test_processor_rejects_invalid_batches_atomically(tmp_path):
    clock, _, t = setup_telemetry()
    populate(t, clock, 2)
    payload = t.since()
    payload["samples"][1]["error_rate"] = .9
    with Store(tmp_path / "data.sqlite") as store:
        processor = Processor(store)
        with pytest.raises(ValueError):
            processor.process(payload)
        assert store.db.execute("SELECT count(*) FROM metrics").fetchone()[0] == 0


class PredictorStub:
    metadata = {"service_id": "checkout-local", "model_version": "test", "threshold": .5}
    def __init__(self):
        self.count = 0
    def make_builder(self):
        class Builder:
            status = "WARMING_UP"
            def __init__(inner):
                inner.history = []
            def push(inner, row):
                inner.history.append(row)
                if len(inner.history) <= 30:
                    return None
                inner.status = "READY"
                return np.zeros(16)
        return Builder()
    def score(self, vector):
        self.count += 1
        value = .9 if self.count <= 5 else .1
        return np.array([value]), np.array([value > .5])


def test_pretty_monitor_output_contains_metrics_and_incident():
    line = format_monitor_output({
        "timestamp": "2026-01-01T12:34:56Z", "status": "ABNORMAL", "score": .71,
        "threshold": .59, "incident_status": "OPEN", "historical": False,
        "metrics": {"cpu_process_pct": 12.3, "memory_rss_mib": 52.1,
                    "request_rate_rpm": 180, "latency_p95_ms": 450.2,
                    "error_rate": .08}})
    assert "12:34:56  ANOMALY" in line
    assert "P95  450.2 ms" in line and "ERR   8.0%" in line
    assert "SCORE 0.710/0.590" in line and "INCIDENT OPEN" in line
    unavailable = format_monitor_output({
        "timestamp": "2026-01-01T12:35:00Z", "status": "DATA_UNAVAILABLE",
        "detail": "Waiting for fresh telemetry"})
    assert unavailable == "12:35:00  NO DATA | Waiting for fresh telemetry"


def test_monitor_persists_prediction_and_incident_lifecycle(tmp_path):
    clock, _, t = setup_telemetry()
    populate(t, clock, 40)
    db = tmp_path / "monitor.sqlite"
    with Store(db) as store:
        processor = Processor(store, PredictorStub())
        processor.process(t.since())
        assert store.db.execute("SELECT count(*) FROM predictions").fetchone()[0] == 10
        events = history(db)
        assert len(events) == 1 and events[0]["status"] == "RESOLVED"
        assert events[0]["model_version"] == "test"


def test_restart_interrupts_open_incident(tmp_path):
    clock, _, t = setup_telemetry()
    populate(t, clock, 35)
    db = tmp_path / "monitor.sqlite"
    with Store(db) as store:
        processor = Processor(store, PredictorStub())
        processor.process(t.since())
        assert history(db)[0]["status"] == "OPEN"
        _, _, fresh = setup_telemetry()
        processor.process(fresh.since())
        assert history(db)[0]["status"] == "INTERRUPTED"


def test_traffic_configuration_validation():
    from service_monitor.traffic import traffic
    with pytest.raises(ValueError):
        asyncio.run(traffic("http://localhost", rate=100))


def test_validation_schedule_has_distinct_ordered_faults_and_recovery():
    from service_monitor.validation import SCHEDULE, TEST_SCHEDULE
    assert [item["name"] for item in SCHEDULE] == [
        "slow-response", "gradual-latency", "error-burst",
        "memory-growth", "combined-degradation"]
    assert [item["at_minute"] for item in SCHEDULE] == sorted(
        item["at_minute"] for item in SCHEDULE)
    for current, following in zip(SCHEDULE, SCHEDULE[1:]):
        current_end = current["at_minute"] + current["duration_seconds"]/60
        assert following["at_minute"]-current_end >= 8
    assert [item["name"] for item in TEST_SCHEDULE] == [item["name"] for item in SCHEDULE]
    assert TEST_SCHEDULE != SCHEDULE
    assert TEST_SCHEDULE[0]["delay_ms"] < SCHEDULE[0]["delay_ms"]
    assert TEST_SCHEDULE[2]["error_probability"] < SCHEDULE[2]["error_probability"]
