import hashlib
import json

import numpy as np
import pandas as pd
import pytest
import sklearn
import xgboost
from xgboost import XGBClassifier

from service_monitor.incidents import replay
from service_monitor.ml.features import FEATURES, SCHEMA, FeatureBuilder, prepare, reference_from_frame
from service_monitor.ml.synthetic import generate
from service_monitor.ml.training import calibrate, Predictor, evaluate_model, read_data


@pytest.fixture
def data(tmp_path):
    directory = tmp_path / "data"
    generate(directory, rows=900)
    return directory


@pytest.fixture
def foundation(data, tmp_path):
    train_frame = read_data(data / "train.csv")
    validation_frame = read_data(data / "validation.csv")
    reference = reference_from_frame(train_frame)
    normal_x, _ = prepare(train_frame, reference)
    validation_x, indices = prepare(validation_frame, reference)
    validation_y = validation_frame.iloc[indices].phase.eq("fault").to_numpy(dtype=np.int8)
    x = np.r_[normal_x, validation_x]
    y = np.r_[np.zeros(len(normal_x), dtype=np.int8), validation_y]
    model = XGBClassifier(n_estimators=25, max_depth=4, learning_rate=.1,
                          tree_method="hist", device="cpu", n_jobs=1,
                          random_state=42).fit(x, y)
    directory = tmp_path / "foundation"
    directory.mkdir()
    model.save_model(directory / "foundation.ubj")
    metadata = {
        "artifact_type": "public-foundation",
        "model_family": "XGBClassifier",
        "feature_schema_version": SCHEMA,
        "feature_names": FEATURES,
        "sklearn_version": sklearn.__version__,
        "xgboost_version": xgboost.__version__,
        "model_sha256": hashlib.sha256((directory / "foundation.ubj").read_bytes()).hexdigest(),
    }
    (directory / "metadata.json").write_text(json.dumps(metadata))
    (directory / "READY").write_text("complete")
    return directory


def test_features_are_causal_and_same_in_stream(data):
    frame = read_data(data / "train.csv").iloc[:80].copy()
    reference = reference_from_frame(frame)
    matrix, indices = prepare(frame, reference)
    stream = FeatureBuilder(reference)
    streamed = [value for row in frame.to_dict("records")
                if (value := stream.push(row)) is not None]
    np.testing.assert_array_equal(matrix, streamed)
    assert indices[0] == 30 and matrix.shape[1] == len(FEATURES)
    changed = frame.copy()
    changed.loc[70:, "memory_rss_mib"] += 999
    np.testing.assert_array_equal(matrix[:40], prepare(changed, reference)[0][:40])


def test_gap_invalid_and_duplicates(data):
    frame = read_data(data / "train.csv").iloc[:100].copy()
    reference = reference_from_frame(frame)
    gap = frame.drop(index=40).reset_index(drop=True)
    _, indices = prepare(gap, reference)
    assert 40 not in indices and 69 not in indices and 70 in indices
    frame.loc[40, "sample_status"] = "no_traffic"
    _, indices = prepare(frame, reference)
    assert 40 not in indices and 70 not in indices and 71 in indices
    with pytest.raises(ValueError, match="duplicate"):
        prepare(pd.concat([frame.iloc[:2], frame.iloc[1:3]], ignore_index=True), reference)


def test_incident_confirmation_recovery_and_missing(data):
    frame = read_data(data / "train.csv").iloc[:20]
    flags = [True, False, True, False, True] + [True]*3 + [False]*5
    events = replay(frame.iloc[:13], list(range(13)), flags)
    assert len(events) == 1 and events[0]["opened_index"] == 4
    assert events[0]["status"] == "RESOLVED"
    events = replay(frame, list(range(5)), [True]*5)
    assert events[0]["status"] == "OPEN"


def test_foundation_calibration_roundtrip_and_evaluation(data, foundation, tmp_path, monkeypatch):
    foundation_metadata_path = foundation / "metadata.json"
    foundation_metadata = json.loads(foundation_metadata_path.read_text())
    foundation_metadata["sklearn_version"] = "Colab-installed-version"
    foundation_metadata_path.write_text(json.dumps(foundation_metadata))
    model_dir = tmp_path / "model"
    metadata = calibrate(foundation, data / "calibration.csv", model_dir)
    assert metadata["smoke_only"] and metadata["model_family"] == "XGBClassifier"
    with pytest.raises(ValueError, match="Synthetic"):
        Predictor(model_dir)
    predictor = Predictor(model_dir, allow_synthetic=True)
    test_frame = read_data(data / "test.csv")
    x, _ = prepare(test_frame, predictor.reference)
    scores, flags = predictor.score(x)
    np.testing.assert_array_equal(flags, scores > metadata["threshold"])
    np.testing.assert_allclose(scores, Predictor(model_dir, True).score(x)[0])
    result = evaluate_model(model_dir, data / "test.csv", tmp_path / "report.json", True)
    assert result["ml"]["faults_total"] == 3
    from service_monitor.cli import main
    prediction_path = tmp_path / "prediction.csv"
    monkeypatch.setattr("sys.argv", ["service-monitor", "predict", "--model", str(model_dir),
                                    "--input", str(data / "test.csv"), "--out", str(prediction_path),
                                    "--allow-synthetic"])
    main()
    predictions = pd.read_csv(prediction_path)
    assert predictions.anomaly_score.iloc[:30].isna().all()
    assert predictions.prediction_status.iloc[30:].eq("SCORED").all()
    with pytest.raises(FileExistsError):
        calibrate(foundation, data / "calibration.csv", model_dir)
    metadata["feature_names"] = list(reversed(FEATURES))
    (model_dir / "metadata.json").write_text(json.dumps(metadata))
    with pytest.raises(ValueError, match="schema"):
        Predictor(model_dir, True)


def test_calibration_requires_normal_baseline(data, foundation, tmp_path):
    with pytest.raises(ValueError, match="normal"):
        calibrate(foundation, data / "validation.csv", tmp_path / "bad")
