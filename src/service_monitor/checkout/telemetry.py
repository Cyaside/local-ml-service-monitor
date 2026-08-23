"""Measure the application process and completed checkout requests."""
import math
import time
from collections import deque
from uuid import uuid4
import psutil
from .scenarios import utcnow


def percentile95(values):
    if not values:
        return None
    ordered = sorted(values)
    position = .95 * (len(ordered)-1)
    low = math.floor(position)
    high = math.ceil(position)
    return ordered[low] + (ordered[high]-ordered[low])*(position-low)


class Telemetry:
    def __init__(self, service_id, scenarios, process=None, clock=time.monotonic, wall=utcnow):
        self.service_id, self.run_id = service_id, str(uuid4())
        self.scenarios = scenarios
        self.process = process or psutil.Process()
        self.clock, self.wall = clock, wall
        self.last_tick, self.last_wall = clock(), wall()
        self.process.cpu_percent(None)  # Prime: do not export the meaningless first value.
        self.samples = deque(maxlen=60)
        self.durations = []
        self.count = self.errors = self.seq = 0
        self.overflow = False

    def record(self, duration_ms, status):
        self.count += 1
        self.errors += int(status >= 500)
        if len(self.durations) < 10000:
            self.durations.append(duration_ms)
        else:
            self.overflow = True  # bounded memory; do not claim an exact p95 on overflow

    def sample(self):
        now, end = self.clock(), self.wall()
        seconds = now-self.last_tick
        phase, fault_id = self.scenarios.annotation(self.last_wall, end)
        status = ("no_traffic" if not self.count else
                  "insufficient_traffic" if self.count < 5 else "valid")
        if not 8 <= seconds <= 12:
            status = "invalid_interval"
        if self.overflow:
            status = "overflow"
        if not self.count and phase == "normal":
            phase = "idle"
        row = {
            "schema_version": "telemetry-v1", "service_id": self.service_id,
            "run_id": self.run_id, "seq": self.seq, "timestamp": end.isoformat(),
            "interval_seconds": seconds, "completed_requests": self.count,
            "server_errors": self.errors, "cpu_process_pct": self.process.cpu_percent(None),
            "memory_rss_mib": self.process.memory_info().rss/(1024*1024),
            "request_rate_rpm": self.count/seconds*60 if seconds > 0 else 0,
            "latency_p95_ms": percentile95(self.durations),
            "error_rate": self.errors/self.count if self.count else None,
            "sample_status": status, "source": "measured",
            "phase": phase, "fault_id": fault_id,
        }
        self.samples.append(row)
        self.seq += 1
        self.last_tick, self.last_wall = now, end
        self.count = self.errors = 0
        self.overflow = False
        self.durations.clear()
        return row

    def since(self, run_id=None, since_seq=-1):
        same_run = run_id == self.run_id
        cursor = since_seq if same_run else -1
        earliest = self.samples[0]["seq"] if self.samples else 0
        return {
            "schema_version": "telemetry-v1", "service_id": self.service_id,
            "run_id": self.run_id, "latest_seq": self.seq-1,
            "history_truncated": bool(self.samples and cursor < earliest-1),
            "samples": [r for r in self.samples if r["seq"] > cursor],
        }
