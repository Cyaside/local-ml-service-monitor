"""Calibrate one public foundation model for one monitored service."""
from __future__ import annotations

import hashlib
import json
import platform
import time
from pathlib import Path

import numpy as np
import pandas as pd
import sklearn
from threadpoolctl import threadpool_limits
import xgboost
from xgboost import XGBClassifier

from .evaluation import evaluate
from .features import BASE, FEATURES, SCHEMA, FeatureBuilder, prepare, reference_from_frame


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, allow_nan=False), encoding="utf-8")
    temporary.replace(path)


def read_data(path):
    frame = pd.read_csv(path, keep_default_na=False)
    for column in BASE + ["completed_requests", "server_errors", "interval_seconds"]:
        if column in frame:
            frame[column] = pd.to_numeric(frame[column], errors="coerce")
    if frame.empty:
        raise ValueError("Dataset is empty")
    if "source" not in frame or not set(frame.source).issubset({"synthetic", "measured"}):
        raise ValueError("source must explicitly be synthetic or measured")
    return frame


def _load_foundation(path):
    path = Path(path)
    if not (path / "READY").exists():
        raise ValueError("Incomplete foundation model directory")
    metadata = json.loads((path / "metadata.json").read_text(encoding="utf-8"))
    model_path = path / "foundation.ubj"
    if metadata.get("artifact_type") != "public-foundation":
        raise ValueError("Expected a public foundation artifact")
    if metadata.get("feature_schema_version") != SCHEMA or metadata.get("feature_names") != FEATURES:
        raise ValueError("Foundation feature schema mismatch")
    if metadata.get("xgboost_version") != xgboost.__version__:
        raise ValueError("Foundation XGBoost version mismatch")
    if hashlib.sha256(model_path.read_bytes()).hexdigest() != metadata.get("model_sha256"):
        raise ValueError("Foundation checksum mismatch")
    model = XGBClassifier(device="cpu", n_jobs=1)
    model.load_model(model_path)
    model.set_params(device="cpu", n_jobs=1)
    return model, metadata


def _adapt(model, baseline_x, validation_path, reference, service_id):
    """Add a small supervised local layer to the public booster."""
    frame = read_data(validation_path)
    if set(frame.service_id) != {service_id}:
        raise ValueError("Validation service does not match baseline")
    if not {"phase", "fault_id"}.issubset(frame.columns):
        raise ValueError("Validation requires phase and fault_id annotations")
    if not set(frame.phase).issubset({"normal", "fault", "recovery", "idle"}):
        raise ValueError("Unknown validation phase")
    validation_x, indices = prepare(frame, reference)
    rows = frame.iloc[indices]
    keep = rows.phase.isin({"normal", "fault"}).to_numpy()
    labels = rows.phase.eq("fault").to_numpy(dtype=np.int8)[keep]
    if labels.sum() < 20 or (labels == 0).sum() < 20:
        raise ValueError("Validation needs at least 20 scored fault and normal rows")
    fault_count = rows.loc[rows.phase.eq("fault"), "fault_id"].nunique()
    if fault_count < 2:
        raise ValueError("Validation needs at least two distinct faults")
    x = np.r_[baseline_x, validation_x[keep]]
    y = np.r_[np.zeros(len(baseline_x), dtype=np.int8), labels]
    positive_weight = float((y == 0).sum() / (y == 1).sum())
    adapted = XGBClassifier(
        n_estimators=80,
        learning_rate=.03,
        max_depth=4,
        min_child_weight=2,
        subsample=.85,
        colsample_bytree=.9,
        reg_alpha=.05,
        reg_lambda=2.0,
        max_bin=256,
        objective="binary:logistic",
        eval_metric="aucpr",
        tree_method="hist",
        device="cpu",
        n_jobs=1,
        scale_pos_weight=positive_weight,
        random_state=42,
    )
    adapted.fit(x, y, xgb_model=model.get_booster(), verbose=False)
    return adapted, {
        "file": str(Path(validation_path).resolve()),
        "sha256": hashlib.sha256(Path(validation_path).read_bytes()).hexdigest(),
        "rows": len(x),
        "positive_rows": int(y.sum()),
        "faults": int(fault_count),
        "added_rounds": 80,
    }


def calibrate(foundation_path, baseline_path, out, threshold_quantile=.995,
              validation_path=None):
    """Adapt one public booster, then attach a local profile and threshold."""
    began = time.perf_counter()
    out = Path(out)
    if out.exists():
        raise FileExistsError(f"Artifact path exists: {out}")
    if not .95 <= threshold_quantile < 1:
        raise ValueError("threshold_quantile must be in [0.95, 1)")
    frame = read_data(baseline_path)
    if "phase" not in frame or not (frame.phase == "normal").all():
        raise ValueError("Baseline calibration requires explicit normal annotations")
    if len(set(frame.service_id)) != 1:
        raise ValueError("A calibrated model supports one service")
    model, foundation = _load_foundation(foundation_path)
    reference = reference_from_frame(frame)
    x, indices = prepare(frame, reference)
    if len(x) < 256:
        raise ValueError("Baseline needs at least 286 consecutive valid rows")
    adaptation = None
    if validation_path:
        model, adaptation = _adapt(
            model, x, validation_path, reference, str(frame.service_id.iloc[0]),
        )
    with threadpool_limits(limits=1):
        scores = model.predict_proba(x)[:, 1]
    threshold = float(np.quantile(scores, threshold_quantile))
    base_values = frame.iloc[indices][BASE].to_numpy(dtype=float)
    baseline_limits = np.quantile(base_values[:, [3, 4, 1]], threshold_quantile, axis=0)
    out.mkdir(parents=True)
    model_path = out / "model.ubj"
    model.set_params(device="cpu", n_jobs=1)
    model.save_model(model_path)
    write_json(out / "profile.json", reference)
    metadata = {
        "artifact_type": "service-model",
        "model_version": out.name,
        "model_family": "XGBClassifier",
        "feature_schema_version": SCHEMA,
        "feature_names": FEATURES,
        "score_definition": "public-foundation anomaly probability",
        "threshold": threshold,
        "threshold_quantile": threshold_quantile,
        "source": str(frame.source.iloc[0]),
        "smoke_only": bool((frame.source == "synthetic").any()),
        "experimental": True,
        "service_id": str(frame.service_id.iloc[0]),
        "sample_interval_seconds": 10,
        "feature_window_intervals": 31,
        "incident_rules": {"open_window": 5, "open_min": 3, "close_normal": 5},
        "baseline_limits_latency_error_memory": baseline_limits.tolist(),
        "baseline": {
            "file": str(Path(baseline_path).resolve()),
            "sha256": hashlib.sha256(Path(baseline_path).read_bytes()).hexdigest(),
            "rows": len(frame),
            "feature_rows": len(x),
        },
        "local_adaptation": adaptation,
        "foundation": foundation,
        "sklearn_version": sklearn.__version__,
        "xgboost_version": xgboost.__version__,
        "python_version": platform.python_version(),
        "numpy_version": np.__version__,
        "calibration_seconds": time.perf_counter() - began,
    }
    metadata["model_sha256"] = hashlib.sha256(model_path.read_bytes()).hexdigest()
    write_json(out / "metadata.json", metadata)
    write_json(out / "calibration.json", {
        "threshold": threshold,
        "threshold_quantile": threshold_quantile,
        "score_summary": {
            "min": float(scores.min()), "median": float(np.median(scores)),
            "max": float(scores.max()),
        },
    })
    (out / "READY").write_text("Complete service artifact\n", encoding="utf-8")
    return metadata


class Predictor:
    def __init__(self, model_dir, allow_synthetic=False):
        model_dir = Path(model_dir)
        if not (model_dir / "READY").exists():
            raise ValueError("Incomplete model directory")
        self.metadata = json.loads((model_dir / "metadata.json").read_text(encoding="utf-8"))
        metadata = self.metadata
        if metadata.get("artifact_type") != "service-model":
            raise ValueError("Expected a service model artifact")
        if metadata["feature_names"] != FEATURES or metadata["feature_schema_version"] != SCHEMA:
            raise ValueError("Feature schema mismatch")
        if metadata["sklearn_version"] != sklearn.__version__:
            raise ValueError("scikit-learn version mismatch; use locked environment")
        if metadata["smoke_only"] and not allow_synthetic:
            raise ValueError("Synthetic model: pass explicit --allow-synthetic for development only")
        if not np.isfinite(metadata["threshold"]):
            raise ValueError("Invalid threshold")
        if metadata.get("xgboost_version") != xgboost.__version__:
            raise ValueError("XGBoost version mismatch; use locked environment")
        model_path = model_dir / "model.ubj"
        if hashlib.sha256(model_path.read_bytes()).hexdigest() != metadata["model_sha256"]:
            raise ValueError("Model checksum mismatch")
        self.reference = json.loads((model_dir / "profile.json").read_text(encoding="utf-8"))
        self.model = XGBClassifier(device="cpu", n_jobs=1)
        self.model.load_model(model_path)
        self.model.set_params(device="cpu", n_jobs=1)

    def make_builder(self):
        return FeatureBuilder(self.reference)

    def score(self, values):
        values = np.asarray(values, dtype=np.float32)
        if values.ndim != 2 or values.shape[1] != len(FEATURES) or not np.isfinite(values).all():
            raise ValueError(f"Expected finite (n, {len(FEATURES)}) feature matrix")
        if not len(values):
            return np.array([]), np.array([], dtype=bool)
        with threadpool_limits(limits=1):
            scores = self.model.predict_proba(values)[:, 1]
        return scores, scores > self.metadata["threshold"]


def evaluate_model(model_dir, input_path, report, allow_synthetic=False):
    predictor = Predictor(model_dir, allow_synthetic)
    frame = read_data(input_path)
    metadata = predictor.metadata
    if set(frame.service_id) != {metadata["service_id"]}:
        raise ValueError("Service does not match calibration")
    x, indices = prepare(frame, predictor.reference)
    if not len(x):
        raise ValueError("No valid test features")
    _, flags = predictor.score(x)
    raw = frame.iloc[indices][BASE].to_numpy(dtype=float)
    limits = metadata["baseline_limits_latency_error_memory"]
    result = {
        "model_version": metadata["model_version"],
        "model_family": metadata["model_family"],
        "test_sources": sorted(set(frame.source)),
        "input_sha256": hashlib.sha256(Path(input_path).read_bytes()).hexdigest(),
        "ml": evaluate(frame, indices, flags),
        "baseline": evaluate(frame, indices, (raw[:, [3, 4, 1]] > limits).any(axis=1)),
    }
    write_json(report, result)
    return result
