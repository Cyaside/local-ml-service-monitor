"""Domain-neutral temporal features shared by public training and live scoring."""
from __future__ import annotations

from collections import deque

import numpy as np
import pandas as pd


BASE = ["cpu_process_pct", "memory_rss_mib", "request_rate_rpm",
        "latency_p95_ms", "error_rate"]
FEATURES = [
    "abs_z_max", "abs_z_mean", "abs_z_q90", "extreme_ratio_gt3",
    "positive_change_1_max", "negative_change_1_max",
    "positive_change_6_max", "negative_change_6_max",
    "positive_change_30_max", "negative_change_30_max",
    "fast_slow_abs_max", "fast_slow_abs_mean",
    "fast_slow_positive_max", "fast_slow_negative_max",
    "current_z_max", "current_z_min",
]
SCHEMA = "universal-temporal-v1"
WINDOW = 31


def fit_reference(values):
    values = np.asarray(values, dtype=np.float64)
    if values.ndim != 2 or len(values) < WINDOW or not np.isfinite(values).all():
        raise ValueError("Reference data must be a finite 2D matrix with at least 31 rows")
    median = np.median(values, axis=0)
    low, high = np.quantile(values, [.25, .75], axis=0)
    scale = np.maximum(high - low, np.maximum(np.abs(median) * 1e-6, 1e-6))
    return {"median": median.tolist(), "scale": scale.tolist()}


def _window_features(normalized_window):
    window = np.asarray(normalized_window, dtype=np.float64)
    if window.ndim != 2 or window.shape[0] != WINDOW:
        raise ValueError(f"Expected a ({WINDOW}, dimensions) normalized window")
    current = window[-1]
    absolute = np.abs(current)

    def changes(lag):
        delta = current - window[-1-lag]
        return max(0.0, float(delta.max())), max(0.0, float((-delta).max()))

    positive_1, negative_1 = changes(1)
    positive_6, negative_6 = changes(6)
    positive_30, negative_30 = changes(30)
    drift = window[-6:].mean(axis=0) - window[-30:].mean(axis=0)
    return np.asarray([
        absolute.max(), absolute.mean(), np.quantile(absolute, .9),
        np.mean(absolute > 3),
        positive_1, negative_1, positive_6, negative_6,
        positive_30, negative_30,
        np.abs(drift).max(), np.abs(drift).mean(),
        max(0.0, float(drift.max())), max(0.0, float((-drift).max())),
        current.max(), current.min(),
    ], dtype=np.float32)


def transform_sequence(values, reference):
    """Create causal fixed-width features from any multivariate time series."""
    values = np.asarray(values, dtype=np.float64)
    median = np.asarray(reference["median"], dtype=np.float64)
    scale = np.asarray(reference["scale"], dtype=np.float64)
    if values.ndim != 2 or values.shape[1] != len(median):
        raise ValueError("Sequence dimensions do not match the reference profile")
    normalized = (values - median) / scale
    return np.asarray([_window_features(normalized[end-WINDOW:end])
                       for end in range(WINDOW, len(values) + 1)], dtype=np.float32)


def _valid_row(row):
    values = np.asarray([row[key] for key in BASE], dtype=float)
    count, errors = float(row["completed_requests"]), float(row["server_errors"])
    duration = float(row["interval_seconds"])
    return (
        row["sample_status"] == "valid" and np.isfinite(values).all()
        and np.isfinite([count, errors, duration]).all()
        and count >= 5 and count.is_integer() and errors.is_integer()
        and 0 <= errors <= count and 8 <= duration <= 12
        and (values >= 0).all() and values[4] <= 1
        and np.isclose(values[4], errors / count, atol=1e-6)
        and np.isclose(values[2], count / duration * 60, rtol=1e-5)
    )


def reference_from_frame(frame):
    rows = [np.asarray([row[key] for key in BASE], dtype=float)
            for row in frame.to_dict("records") if _valid_row(row)]
    return fit_reference(np.asarray(rows))


class FeatureBuilder:
    def __init__(self, reference):
        self.reference = reference
        self.median = np.asarray(reference["median"], dtype=float)
        self.scale = np.asarray(reference["scale"], dtype=float)
        if len(self.median) != len(BASE) or len(self.scale) != len(BASE):
            raise ValueError("Local reference profile must match the five runtime metrics")
        self.history = deque(maxlen=WINDOW)
        self.previous = None
        self.status = "WARMING_UP"

    def push(self, row):
        timestamp = pd.Timestamp(row["timestamp"])
        if timestamp.tzinfo is None:
            raise ValueError("timestamp must include timezone")
        identity = (str(row["service_id"]), str(row["run_id"]))
        seq = int(row["seq"])
        if self.previous is not None:
            old_id, old_seq, old_time = self.previous
            if identity == old_id and (seq <= old_seq or timestamp <= old_time):
                raise ValueError("duplicate or out-of-order sample")
            if identity != old_id or seq != old_seq + 1 or not 8 <= (timestamp-old_time).total_seconds() <= 12:
                self.history.clear()
        self.previous = (identity, seq, timestamp)
        if not _valid_row(row):
            self.history.clear()
            self.status = "DATA_INVALID"
            return None
        values = np.asarray([row[key] for key in BASE], dtype=float)
        self.history.append((timestamp, (values-self.median)/self.scale))
        if len(self.history) < WINDOW:
            self.status = "WARMING_UP"
            return None
        if not 270 <= (timestamp - self.history[0][0]).total_seconds() <= 330:
            self.history.clear()
            self.status = "WARMING_UP"
            return None
        self.status = "READY"
        return _window_features(np.asarray([value for _, value in self.history]))


def prepare(frame, reference):
    required = set(BASE + ["timestamp", "service_id", "run_id", "seq",
                          "interval_seconds", "completed_requests",
                          "server_errors", "sample_status"])
    missing = required - set(frame.columns)
    if missing:
        raise ValueError(f"Missing columns: {sorted(missing)}")
    builder, vectors, indices = FeatureBuilder(reference), [], []
    for index, row in enumerate(frame.to_dict("records")):
        vector = builder.push(row)
        if vector is not None:
            vectors.append(vector)
            indices.append(index)
    return np.asarray(vectors, dtype=np.float32).reshape(-1, len(FEATURES)), indices
