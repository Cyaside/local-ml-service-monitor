"""Collect and optionally score measured telemetry; one bounded polling loop."""
import asyncio
import json
import time
from datetime import datetime, timezone, timedelta
from uuid import uuid4
import httpx
from .schemas import MetricsBatch
from .storage import Store


def utcnow():
    return datetime.now(timezone.utc)


def format_monitor_output(output):
    """Human-readable single-line output suitable for a terminal demo."""
    timestamp = output.get("timestamp", "")
    clock = timestamp[11:19] if len(timestamp) >= 19 else "--:--:--"
    status = output.get("status", "UNKNOWN")
    labels = {
        "WARMING_UP": "WARMUP ", "NORMAL": "NORMAL ", "ABNORMAL": "ANOMALY",
        "DATA_INVALID": "INVALID", "DATA_UNAVAILABLE": "NO DATA",
        "COLLECTING": "COLLECT",
    }
    parts = [f"{clock}  {labels.get(status, status):<7}"]
    if status == "WARMING_UP" and output.get("warmup_samples") is not None:
        parts.append(f"[{output['warmup_samples']:02d}/31]")
    metrics = output.get("metrics")
    if metrics:
        parts.extend([
            f"CPU {metrics['cpu_process_pct']:5.1f}%",
            f"MEM {metrics['memory_rss_mib']:6.1f} MiB",
            f"RPM {metrics['request_rate_rpm']:5.0f}",
            f"P95 {metrics['latency_p95_ms']:6.1f} ms",
            f"ERR {metrics['error_rate']*100:5.1f}%",
        ])
    if output.get("score") is not None:
        parts.append(f"SCORE {output['score']:.3f}/{output['threshold']:.3f}")
    if output.get("incident_status"):
        marker = "<<< INCIDENT " + output["incident_status"] + " >>>"
        parts.append(marker)
    if output.get("historical"):
        parts.append("(historical)")
    if output.get("detail"):
        parts.append(str(output["detail"]))
    return " | ".join(parts)


class Processor:
    def __init__(self, store, predictor=None):
        self.store, self.predictor = store, predictor
        self.run_id = None
        self.service_id = None
        self.cursor = -1
        self.last_sample_at = None
        self.builder = None
        self.machine = None
        if predictor:
            from .incidents import IncidentMachine
            self.builder = predictor.make_builder() if hasattr(predictor, "make_builder") else None
            self.machine = IncidentMachine()
        with store.db:
            store.interrupt_open(utcnow().isoformat(), "collector_or_monitor_restarted")

    def process(self, payload, not_before=None):
        batch = MetricsBatch.model_validate(payload)
        if self.service_id and self.service_id != batch.service_id:
            raise ValueError("Target service identity changed")
        if self.predictor and self.predictor.metadata["service_id"] != batch.service_id:
            raise ValueError("Artifact service_id does not match backend; train on this service first")
        rows = [s.model_dump(mode="json") for s in batch.samples
                if not_before is None or s.timestamp >= not_before]
        if any(datetime.fromisoformat(r["timestamp"]) > utcnow()+timedelta(seconds=30) for r in rows):
            raise ValueError("Telemetry timestamp is in the future")
        changed = self.run_id is not None and self.run_id != batch.run_id
        if changed or batch.history_truncated:
            if self.builder:
                self.builder = self.predictor.make_builder()
            if self.machine:
                if changed:
                    event = self.machine.interrupt(utcnow().isoformat(), "service_restarted")
                    if event:
                        with self.store.db:
                            self.store.save_incident(event)
                else:
                    self.machine.missing()
        if changed:
            self.cursor = -1
        self.run_id, self.service_id = batch.run_id, batch.service_id
        outputs = []
        with self.store.db:
            for row in rows:
                metric_id = self.store.insert_metric(row)
                self.cursor = max(self.cursor, row["seq"])
                self.last_sample_at = datetime.fromisoformat(row["timestamp"])
                if metric_id is None:
                    continue
                status = "COLLECTING"
                output = {"timestamp": row["timestamp"], "status": status, "seq": row["seq"],
                          "metrics": {key: row[key] for key in (
                              "cpu_process_pct", "memory_rss_mib", "request_rate_rpm",
                              "latency_p95_ms", "error_rate")}}
                if self.predictor:
                    vector = self.builder.push(row)
                    flag = None
                    if vector is not None:
                        scores, flags = self.predictor.score(vector.reshape(1, -1))
                        flag = bool(flags[0])
                        output.update(status="ABNORMAL" if flag else "NORMAL", score=float(scores[0]),
                                      threshold=float(self.predictor.metadata["threshold"]))
                        self.store.db.execute("INSERT INTO predictions VALUES(?,?,?,?,?,?)",
                            (metric_id, self.predictor.metadata["model_version"], float(scores[0]),
                             self.predictor.metadata["threshold"], int(flag), utcnow().isoformat()))
                    else:
                        output["status"] = (row["sample_status"].upper()
                                            if row["sample_status"] != "valid" else self.builder.status)
                        if output["status"] == "WARMING_UP":
                            output["warmup_samples"] = len(self.builder.history)
                    updates = self.machine.push(row, flag, score=output.get("score"))
                    for event in updates:
                        event["model_version"] = self.predictor.metadata["model_version"]
                        self.store.save_incident(event)
                        output["incident_status"] = event["status"]
                        output["incident_id"] = event["id"]
                    output["historical"] = (utcnow()-datetime.fromisoformat(row["timestamp"])).total_seconds() > 30
                outputs.append(output)
        # Skip pre-session samples on first poll so separate datasets cannot overlap.
        self.cursor = max(self.cursor, batch.latest_seq)
        return outputs


async def collect(url, database, duration_minutes=30, model=None, allow_experimental=False,
                  pretty=False):
    if not 0 < duration_minutes <= 1440:
        raise ValueError("duration-minutes must be >0 and <=1440")
    predictor = None
    if model:
        from .ml.training import Predictor
        predictor = Predictor(model)  # Synthetic models are never accepted for live monitoring.
        if predictor.metadata["experimental"] and not allow_experimental:
            raise ValueError("Experimental model requires --allow-experimental")
        if predictor.metadata.get("sample_interval_seconds") != 10 or predictor.metadata.get("incident_rules") != {
            "open_window": 5, "open_min": 3, "close_normal": 5
        }:
            raise ValueError("Artifact runtime rules do not match collector")
    started, session = utcnow(), str(uuid4())
    deadline = time.monotonic()+duration_minutes*60
    with Store(database) as store:
        processor = Processor(store, predictor)
        with store.db:
            store.db.execute("INSERT INTO sessions VALUES(?,?,?,?,?)",
                (session, started.isoformat(), None, "monitor" if model else "collect", url))
        try:
            async with httpx.AsyncClient(base_url=url.rstrip("/"), timeout=3, trust_env=False) as client:
                while time.monotonic() < deadline:
                    tick = time.monotonic()
                    try:
                        response = await client.get("/metrics", params={
                            "run_id": processor.run_id or "", "since_seq": processor.cursor})
                        response.raise_for_status()
                        for output in processor.process(response.json(), not_before=started):
                            print(format_monitor_output(output) if pretty else json.dumps(output), flush=True)
                        if processor.last_sample_at is None or (utcnow()-processor.last_sample_at).total_seconds() > 30:
                            unavailable = {"timestamp": utcnow().isoformat(),
                                           "status": "DATA_UNAVAILABLE",
                                           "detail": "Waiting for fresh telemetry"}
                            print(format_monitor_output(unavailable) if pretty else json.dumps(unavailable),
                                  flush=True)
                            if processor.machine:
                                processor.machine.missing()
                    except httpx.HTTPError as exc:
                        if processor.machine:
                            processor.machine.missing()
                        unavailable = {"timestamp": utcnow().isoformat(),
                                       "status": "DATA_UNAVAILABLE", "detail": str(exc)}
                        print(format_monitor_output(unavailable) if pretty else json.dumps(unavailable),
                              flush=True)
                    # Malformed data/config or SQLite errors terminate visibly; no silent cursor advance.
                    remaining = min(10-(time.monotonic()-tick), deadline-time.monotonic())
                    if remaining > 0:
                        await asyncio.sleep(remaining)
        finally:
            with store.db:
                store.interrupt_open(utcnow().isoformat(), "monitor_stopped")
                store.db.execute("UPDATE sessions SET ended_at=? WHERE id=?", (utcnow().isoformat(), session))
