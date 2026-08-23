"""Bounded development faults with explicit event-time annotations."""
import time
from collections import deque
from datetime import datetime, timezone, timedelta
from uuid import uuid4
from pydantic import BaseModel, ConfigDict, Field
from typing import Literal


def utcnow():
    return datetime.now(timezone.utc)


class ScenarioConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: Literal["slow-response", "gradual-latency", "error-burst",
                  "memory-growth", "combined-degradation"]
    duration_seconds: float = Field(default=180, gt=0, le=1800)
    delay_ms: float = Field(default=600, ge=0, le=2000)
    error_probability: float = Field(default=.2, ge=0, le=1)
    step_mib: int = Field(default=2, ge=1, le=8)
    cap_mib: int = Field(default=64, ge=1, le=64)


class Scenarios:
    def __init__(self, clock=time.monotonic, wall=utcnow):
        self.clock, self.wall = clock, wall
        self.active = None
        self.events = deque(maxlen=100)
        self.blocks = []
        self.allocated_mib = 0

    def start(self, config):
        self.tick()
        if self.active:
            raise ValueError("A scenario is already active; reset it first")
        event = {"fault_id": str(uuid4()), "name": config.name,
                 "started_at": self.wall().isoformat(), "ended_at": None,
                 "reason": None, "parameters": config.model_dump()}
        self.events.append(event)
        self.active = {"event": event, "config": config,
                       "started_mono": self.clock(),
                       "deadline": self.clock()+config.duration_seconds,
                       "next_allocation": self.clock()+10}
        return dict(event)

    def reset(self, reason="reset"):
        if self.active:
            self.active["event"].update(ended_at=self.wall().isoformat(), reason=reason)
        self.active = None
        self.blocks.clear()
        self.allocated_mib = 0

    def tick(self):
        if not self.active:
            return
        now = self.clock()
        if now >= self.active["deadline"]:
            self.reset("expired")
            return
        config = self.active["config"]
        if config.name == "memory-growth" and now >= self.active["next_allocation"]:
            size = min(config.step_mib, config.cap_mib-self.allocated_mib)
            if size > 0:
                block = bytearray(size * 1024 * 1024)
                # Touch each page so the OS accounts for actual resident memory.
                for offset in range(0, len(block), 4096):
                    block[offset] = 1
                self.blocks.append(block)
                self.allocated_mib += size
            self.active["next_allocation"] = now+10

    def effects(self, baseline_error_probability):
        """Return bounded runtime effects without exposing labels to the model."""
        self.tick()
        delay_seconds = 0.0
        error_probability = baseline_error_probability
        if not self.active:
            return delay_seconds, error_probability
        config = self.active["config"]
        if config.name == "slow-response":
            delay_seconds = config.delay_ms/1000
        elif config.name == "gradual-latency":
            elapsed = max(0, self.clock()-self.active["started_mono"])
            fraction = min(1, elapsed/config.duration_seconds)
            delay_seconds = config.delay_ms/1000*fraction
        elif config.name == "error-burst":
            error_probability = config.error_probability
        elif config.name == "combined-degradation":
            delay_seconds = config.delay_ms/1000
            error_probability = config.error_probability
        return delay_seconds, error_probability

    def snapshot(self):
        self.tick()
        return {"active": dict(self.active["event"]) if self.active else None,
                "allocated_mib": self.allocated_mib,
                "events": [dict(event) for event in self.events]}

    def annotation(self, start, end):
        # Fault overlap wins over recovery; labels are evaluation-only.
        for event in reversed(self.events):
            began = datetime.fromisoformat(event["started_at"])
            finished = datetime.fromisoformat(event["ended_at"]) if event["ended_at"] else end
            if began < end and finished > start:
                return "fault", event["fault_id"]
        for event in reversed(self.events):
            if event["ended_at"]:
                finished = datetime.fromisoformat(event["ended_at"])
                if finished <= end and finished+timedelta(minutes=10) > start:
                    return "recovery", ""
        return "normal", ""
