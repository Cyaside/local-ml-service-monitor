"""Synthetic development fixtures, NOT measured application telemetry."""
from pathlib import Path
from datetime import datetime, timedelta, timezone
import numpy as np
import pandas as pd


def generate(directory, rows=1200, seed=42):
    if rows < 900:
        raise ValueError("Use at least 900 rows to separate fault and recovery windows")
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    if any((directory / f"{name}.csv").exists() for name in ("train", "calibration", "validation", "test")):
        raise FileExistsError("Dataset already exists; select another directory")
    for part, name in enumerate(("train", "calibration", "validation", "test")):
        rng = np.random.default_rng(seed + part)
        start = datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(days=part)
        records = []
        for i in range(rows):
            target = (1, 3, 6)[(i // 30) % 3]
            count = max(5, int(rng.poisson(target * 10)))
            errors = int(rng.binomial(count, .003))
            memory = 74 + .02 * count + rng.normal(0, .7)
            latency = 115 + .4 * count + rng.normal(0, 7)
            cpu = max(.01, .8 + .05 * count + rng.normal(0, .2))
            phase, fault_id = "normal", ""
            if part >= 2:
                for j, kind in enumerate(("slow", "error", "memory")):
                    begin = 150 + j * 240
                    duration = 24 if kind != "memory" else 36
                    if begin <= i < begin + duration:
                        phase, fault_id = "fault", f"{name}-{kind}"
                        if kind == "slow":
                            latency += 500 if part == 2 else 400
                        elif kind == "error":
                            errors = int(rng.binomial(count, .20 if part == 2 else .15))
                        else:
                            memory += min(64, (i-begin+1) * (2 if part == 2 else 1.5))
                    elif begin + duration <= i < begin + duration + 60:
                        phase = "recovery"
            records.append({
                "timestamp": (start + timedelta(seconds=(i+1)*10)).isoformat(),
                "service_id": "synthetic-checkout", "run_id": name, "seq": i,
                "interval_seconds": 10, "completed_requests": count, "server_errors": errors,
                "cpu_process_pct": cpu, "memory_rss_mib": memory,
                "request_rate_rpm": count * 6, "latency_p95_ms": latency,
                "error_rate": errors/count, "sample_status": "valid",
                "source": "synthetic", "phase": phase, "fault_id": fault_id,
            })
        pd.DataFrame(records).to_csv(directory / f"{name}.csv", index=False)
