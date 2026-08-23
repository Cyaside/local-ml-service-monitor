"""Validate network telemetry before it enters storage or feature windows."""
from datetime import datetime
from typing import Literal
import math
from pydantic import BaseModel, ConfigDict, Field, model_validator


class Sample(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    schema_version: Literal["telemetry-v1"]
    service_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    seq: int = Field(ge=0)
    timestamp: datetime
    interval_seconds: float = Field(gt=0)
    completed_requests: int = Field(ge=0)
    server_errors: int = Field(ge=0)
    cpu_process_pct: float = Field(ge=0)
    memory_rss_mib: float = Field(ge=0)
    request_rate_rpm: float = Field(ge=0)
    latency_p95_ms: float | None = Field(default=None, ge=0)
    error_rate: float | None = Field(default=None, ge=0, le=1)
    sample_status: Literal["valid", "no_traffic", "insufficient_traffic", "invalid_interval", "overflow"]
    source: Literal["measured"]
    phase: Literal["normal", "fault", "recovery", "idle"]
    fault_id: str = ""

    @model_validator(mode="after")
    def consistent(self):
        if self.timestamp.tzinfo is None:
            raise ValueError("timestamp needs timezone")
        if self.server_errors > self.completed_requests:
            raise ValueError("errors exceed requests")
        if not math.isclose(self.request_rate_rpm, self.completed_requests/self.interval_seconds*60, rel_tol=1e-5, abs_tol=1e-6):
            raise ValueError("request rate disagrees with count")
        if self.completed_requests:
            if self.latency_p95_ms is None or self.error_rate is None:
                raise ValueError("completed requests require request statistics")
            if not math.isclose(self.error_rate, self.server_errors/self.completed_requests, abs_tol=1e-6):
                raise ValueError("error rate disagrees with count")
        elif self.error_rate is not None or self.latency_p95_ms is not None:
            raise ValueError("empty interval must have null request statistics")
        if self.sample_status == "valid" and (self.completed_requests < 5 or not 8 <= self.interval_seconds <= 12):
            raise ValueError("invalid valid-status sample")
        if self.phase == "fault" and not self.fault_id:
            raise ValueError("fault requires ID")
        return self


class MetricsBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal["telemetry-v1"]
    service_id: str
    run_id: str
    latest_seq: int = Field(ge=-1)
    history_truncated: bool
    samples: list[Sample] = Field(max_length=60)

    @model_validator(mode="after")
    def consistent(self):
        previous = None
        for sample in self.samples:
            if sample.service_id != self.service_id or sample.run_id != self.run_id:
                raise ValueError("batch identity mismatch")
            if sample.seq > self.latest_seq:
                raise ValueError("sample ahead of batch cursor")
            if previous and (sample.seq <= previous.seq or sample.timestamp <= previous.timestamp):
                raise ValueError("unordered batch")
            previous = sample
        return self
