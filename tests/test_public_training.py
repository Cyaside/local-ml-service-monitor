"""Checks for the public-data training path without fetching large datasets."""

import io
import json

import numpy as np

from service_monitor.ml import public_training
from service_monitor.ml.training import _load_foundation


def test_download_retries_and_keeps_complete_cache(tmp_path, monkeypatch):
    calls = []

    def open_url(url, timeout):
        calls.append((url, timeout))
        if len(calls) == 1:
            raise TimeoutError("transient read timeout")
        return io.BytesIO(b"complete")

    monkeypatch.setattr(public_training.urllib.request, "urlopen", open_url)
    monkeypatch.setattr(public_training.time, "sleep", lambda seconds: None)
    destination = tmp_path / "source" / "data.txt"
    assert public_training._download("https://example.org/data.txt", destination) == destination
    assert destination.read_bytes() == b"complete"
    assert not destination.with_suffix(".txt.part").exists()
    public_training._download("https://example.org/data.txt", destination)
    assert len(calls) == 2


def test_public_model_artifact_roundtrip(tmp_path, monkeypatch):
    rng = np.random.default_rng(42)

    def dataset(name):
        train = rng.normal(size=(100, 5))
        test = rng.normal(size=(100, 5))
        labels = np.zeros(100, dtype=np.int8)
        labels[50:70] = 1
        test[50:70, :2] += 5
        return name, train, test, labels

    monkeypatch.setattr(public_training, "load_psm", lambda cache: dataset("PSM"))
    monkeypatch.setattr(public_training, "load_smd", lambda cache, machine: dataset(machine))
    output = tmp_path / "foundation"
    metadata = public_training.train_public_foundation(tmp_path / "cache", output,
                                                       profile="quick", device="cpu")
    assert metadata["model_family"] == "XGBClassifier"
    assert metadata["boosting_rounds"] >= 1
    assert metadata["validation"]["held_out_dataset"] == "machine-3-1"
    assert "threshold_from_development" in metadata["validation"]
    assert len(metadata["datasets"]) == 4
    assert (output / "READY").is_file()
    model, loaded_metadata = _load_foundation(output)
    assert model.n_features_in_ == len(public_training.FEATURES)
    assert loaded_metadata == json.loads((output / "metadata.json").read_text())
