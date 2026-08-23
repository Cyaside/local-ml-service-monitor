"""Shared streaming/replay incident state machine."""
from collections import deque
from uuid import uuid4


class IncidentMachine:
    def __init__(self):
        self.window = deque(maxlen=5)
        self.active = None
        self.normal_count = 0
        self.previous = None

    def missing(self):
        self.window.clear()
        self.normal_count = 0

    def interrupt(self, timestamp, reason):
        event = self.active
        if event:
            event.update(status="INTERRUPTED", ended_at=timestamp, reason=reason)
        self.active = None
        self.missing()
        return event

    def push(self, row, abnormal, index=None, score=None):
        updates = []
        identity = (row["service_id"], row["run_id"])
        if self.previous is not None and identity != self.previous:
            event = self.interrupt(row["timestamp"], "service_restarted")
            if event:
                updates.append(dict(event))
        self.previous = identity
        if abnormal is None:
            self.missing()
            return updates
        if self.active is None:
            self.window.append(bool(abnormal))
            if len(self.window) == 5 and sum(self.window) >= 3:
                self.active = {"id": str(uuid4()), "service_id": row["service_id"],
                               "run_id": row["run_id"], "opened_at": row["timestamp"],
                               "opened_index": index, "status": "OPEN", "ended_at": None,
                               "last_seen_at": row["timestamp"], "peak_score": score}
                updates.append(dict(self.active))
        else:
            self.active["last_seen_at"] = row["timestamp"]
            if score is not None:
                previous_peak = self.active.get("peak_score")
                self.active["peak_score"] = score if previous_peak is None else max(score, previous_peak)
            self.normal_count = 0 if abnormal else self.normal_count+1
            if self.normal_count >= 5:
                self.active.update(status="RESOLVED", ended_at=row["timestamp"])
                updates.append(dict(self.active))
                self.active = None
                self.missing()
            else:
                updates.append(dict(self.active))
        return updates


def replay(frame, indices, flags):
    decisions = dict(zip(indices, flags, strict=True))
    machine, events = IncidentMachine(), {}
    for index, row in enumerate(frame.to_dict("records")):
        for event in machine.push(row, decisions.get(index), index):
            events[event["id"]] = event
    return list(events.values())
