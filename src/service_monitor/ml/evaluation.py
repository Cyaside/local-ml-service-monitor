"""Incident-level evaluation from explicit per-row evaluation annotations."""
import numpy as np
import pandas as pd
from datetime import timedelta
from ..incidents import replay


def evaluate(frame, indices, flags):
    for column in ["phase", "fault_id"]:
        if column not in frame:
            raise ValueError(f"Evaluation requires {column} annotation")
    if not set(frame.phase).issubset({"normal", "fault", "recovery", "idle"}):
        raise ValueError("Unknown evaluation phase")
    fault_ids = frame.fault_id.fillna("").astype(str)
    if ((frame.phase == "fault") & (fault_ids == "")).any():
        raise ValueError("Fault rows require fault_id")
    events = replay(frame, indices, flags)
    used, delays, per_fault = set(), [], []
    for fault_id in fault_ids[frame.phase == "fault"].unique():
        group = frame[(fault_ids == fault_id) & (frame.phase == "fault")]
        start = pd.Timestamp(group.timestamp.iloc[0]).to_pydatetime() - timedelta(seconds=float(group.interval_seconds.iloc[0]))
        end = pd.Timestamp(group.timestamp.iloc[-1]).to_pydatetime()
        match = next((j for j, event in enumerate(events)
                      if j not in used and start <= pd.Timestamp(event["opened_at"]).to_pydatetime() <= end + timedelta(seconds=30)), None)
        delay = None
        if match is not None:
            used.add(match)
            delay = (pd.Timestamp(events[match]["opened_at"]).to_pydatetime() - start).total_seconds()
            delays.append(delay)
        per_fault.append({"fault_id": fault_id, "detected": match is not None, "delay_seconds": delay})
    observable_normal = [i for i in indices if frame.iloc[i].phase == "normal"]
    hours = float(frame.iloc[observable_normal].interval_seconds.sum()) / 3600
    false = sum(j not in used and frame.iloc[e["opened_index"]].phase == "normal"
                for j, e in enumerate(events))
    expected = 0
    for _, group in frame.groupby(["service_id", "run_id"], sort=False):
        expected += round((pd.Timestamp(group.timestamp.iloc[-1]) -
                           pd.Timestamp(group.timestamp.iloc[0])).total_seconds() / 10) + 1
    return {
        "faults_detected": len(used), "faults_total": len(per_fault),
        "incident_recall": len(used) / len(per_fault) if per_fault else None,
        "false_incidents": false, "observable_normal_hours": hours,
        "false_incidents_per_hour": false / hours if hours else None,
        "coverage": len(indices) / expected if expected else 0,
        "median_detection_delay_seconds": float(np.median(delays)) if delays else None,
        "unmatched_non_normal_incidents": sum(j not in used and frame.iloc[e["opened_index"]].phase != "normal" for j, e in enumerate(events)),
        "per_fault": per_fault, "incidents": events,
    }
