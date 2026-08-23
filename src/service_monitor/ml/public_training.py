"""Train one CPU-light foundation model on public server telemetry."""
from __future__ import annotations

import hashlib
import json
import shutil
import time
import urllib.request
from pathlib import Path

import numpy as np
import pandas as pd
import sklearn
from sklearn.metrics import average_precision_score, f1_score, precision_recall_curve, roc_auc_score
from sklearn.model_selection import train_test_split
from threadpoolctl import threadpool_limits
import xgboost
from xgboost import XGBClassifier

from .features import FEATURES, SCHEMA, fit_reference, transform_sequence


PSM_REVISION = "948e61eb579815ff2a6bce721332d8220d59ca47"
SMD_REVISION = "7fb0e0acf89ea49908896bcc9f9e80fcfff6baf4"
QUICK_MACHINES = ("machine-1-1", "machine-2-1", "machine-3-1")


def _download(url, destination):
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if not destination.exists():
        partial = destination.with_suffix(destination.suffix + ".part")
        for attempt in range(4):
            try:
                with urllib.request.urlopen(url, timeout=90) as response, partial.open("wb") as output:
                    shutil.copyfileobj(response, output)
                partial.replace(destination)
                break
            except OSError:
                partial.unlink(missing_ok=True)
                if attempt == 3:
                    raise
                time.sleep(2 ** attempt)
    return destination


def _clean(train, test):
    train = np.asarray(train, dtype=np.float64)
    test = np.asarray(test, dtype=np.float64)
    train[~np.isfinite(train)] = np.nan
    test[~np.isfinite(test)] = np.nan
    medians = np.nanmedian(train, axis=0)
    medians[~np.isfinite(medians)] = 0
    train = np.where(np.isnan(train), medians, train)
    test = np.where(np.isnan(test), medians, test)
    keep = np.ptp(train, axis=0) > 1e-12
    return train[:, keep], test[:, keep]


def load_psm(cache_dir):
    root = f"https://raw.githubusercontent.com/eBay/RANSynCoders/{PSM_REVISION}/data"
    paths = {name: _download(f"{root}/{name}.csv", Path(cache_dir) / "psm" / f"{name}.csv")
             for name in ("train", "test", "test_label")}
    train_frame, test_frame = pd.read_csv(paths["train"]), pd.read_csv(paths["test"])
    labels = pd.read_csv(paths["test_label"]).iloc[:, -1].to_numpy(dtype=np.int8)
    train, test = _clean(train_frame.iloc[:, 1:].to_numpy(), test_frame.iloc[:, 1:].to_numpy())
    return "PSM", train, test, labels


def load_smd(cache_dir, machine):
    root = f"https://raw.githubusercontent.com/NetManAIOps/OmniAnomaly/{SMD_REVISION}/ServerMachineDataset"
    paths = {}
    for split in ("train", "test", "test_label"):
        paths[split] = _download(f"{root}/{split}/{machine}.txt",
                                 Path(cache_dir) / "smd" / split / f"{machine}.txt")
    train = np.loadtxt(paths["train"], delimiter=",")
    test = np.loadtxt(paths["test"], delimiter=",")
    labels = np.loadtxt(paths["test_label"], delimiter=",").astype(np.int8).reshape(-1)
    train, test = _clean(train, test)
    return f"SMD/{machine}", train, test, labels


def _machines(profile):
    if profile == "quick":
        return QUICK_MACHINES
    if profile == "full":
        return tuple(f"machine-{group}-{index}"
                     for group, count in ((1, 8), (2, 9), (3, 11))
                     for index in range(1, count + 1))
    raise ValueError("profile must be quick or full")


def _examples(dataset):
    name, train, test, labels = dataset
    reference = fit_reference(train)
    normal_x = transform_sequence(train, reference)
    labeled_x = transform_sequence(test, reference)
    labeled_y = labels[len(labels)-len(labeled_x):].astype(np.int8)
    if len(labeled_x) != len(labeled_y):
        raise ValueError(f"Samples and labels do not align for {name}")
    return name, normal_x, labeled_x, labeled_y


def _balanced(examples, seed=42, max_rows=400_000):
    rng = np.random.default_rng(seed)
    positives = [x[y == 1] for _, normal, x, y in examples]
    normals = [normal for _, normal, x, y in examples] + [x[y == 0] for _, normal, x, y in examples]
    positive_x = np.concatenate(positives)
    normal_x = np.concatenate(normals)
    normal_limit = min(len(normal_x), max(len(positive_x) * 6, 20_000), max_rows-len(positive_x))
    normal_x = normal_x[rng.choice(len(normal_x), normal_limit, replace=False)]
    x = np.concatenate([positive_x, normal_x])
    y = np.r_[np.ones(len(positive_x), dtype=np.int8), np.zeros(len(normal_x), dtype=np.int8)]
    order = rng.permutation(len(x))
    return x[order], y[order]


def _new_model(device, positive_weight, estimators=700, early_stopping_rounds=None):
    # GPU training, one compact tree model, and CPU-compatible inference.
    return XGBClassifier(
        n_estimators=estimators,
        learning_rate=.04,
        max_depth=6,
        min_child_weight=4,
        subsample=.85,
        colsample_bytree=.85,
        reg_alpha=.05,
        reg_lambda=2.0,
        max_bin=256,
        objective="binary:logistic",
        eval_metric="aucpr",
        tree_method="hist",
        device=device,
        n_jobs=1,
        scale_pos_weight=positive_weight,
        early_stopping_rounds=early_stopping_rounds,
        random_state=42,
    )


def _event_recall(labels, predictions):
    labels = np.asarray(labels, dtype=bool)
    starts = np.flatnonzero(labels & ~np.r_[False, labels[:-1]])
    ends = np.r_[np.flatnonzero(labels[:-1] & ~labels[1:]) + 1, len(labels)]
    return float(np.mean([predictions[start:end].any() for start, end in zip(starts, ends)]))


def _validation_metrics(model, example, threshold):
    name, _, x, y = example
    scores = model.predict_proba(x)[:, 1]
    predictions = scores >= threshold
    return {
        "held_out_dataset": name,
        "average_precision": float(average_precision_score(y, scores)),
        "roc_auc": float(roc_auc_score(y, scores)),
        "point_f1": float(f1_score(y, predictions)),
        "event_recall": _event_recall(y, predictions),
        "threshold_from_development": threshold,
        "rows": len(x),
    }


def train_public_foundation(cache_dir, output_dir, profile="full", device="cuda"):
    """Train the one final classifier; the last SMD entity is cross-domain validation."""
    output_dir = Path(output_dir)
    if output_dir.exists():
        raise FileExistsError(f"Output path exists: {output_dir}")
    datasets = [load_psm(cache_dir), *[load_smd(cache_dir, machine) for machine in _machines(profile)]]
    examples = [_examples(dataset) for dataset in datasets]
    development, held_out = examples[:-1], examples[-1]
    dev_x, dev_y = _balanced(development)
    fit_x, stop_x, fit_y, stop_y = train_test_split(
        dev_x, dev_y, test_size=.15, stratify=dev_y, random_state=42,
    )
    dev_weight = float((fit_y == 0).sum() / max(1, (fit_y == 1).sum()))
    with threadpool_limits(limits=1):
        validation_model = _new_model(device, dev_weight, early_stopping_rounds=50)
        validation_model.fit(fit_x, fit_y, eval_set=[(stop_x, stop_y)], verbose=False)
        stop_scores = validation_model.predict_proba(stop_x)[:, 1]
        precision, recall, thresholds = precision_recall_curve(stop_y, stop_scores)
        stop_f1 = 2 * precision[:-1] * recall[:-1] / np.maximum(
            precision[:-1] + recall[:-1], 1e-12,
        )
        threshold = float(thresholds[int(np.argmax(stop_f1))])
        validation = _validation_metrics(validation_model, held_out, threshold)
        final_x, final_y = _balanced(examples)
        final_weight = float((final_y == 0).sum() / max(1, (final_y == 1).sum()))
        best_rounds = int(getattr(validation_model, "best_iteration", 699)) + 1
        began = time.perf_counter()
        final_model = _new_model(device, final_weight, estimators=best_rounds).fit(final_x, final_y)
        training_seconds = time.perf_counter() - began
    output_dir.mkdir(parents=True)
    model_path = output_dir / "foundation.ubj"
    final_model.save_model(model_path)
    metadata = {
        "artifact_type": "public-foundation",
        "model_family": "XGBClassifier",
        "training_device": device,
        "feature_schema_version": SCHEMA,
        "feature_names": FEATURES,
        "profile": profile,
        "dataset_revisions": {"PSM": PSM_REVISION, "SMD": SMD_REVISION},
        "datasets": [item[0] for item in examples],
        "validation": validation,
        "training_rows": len(final_x),
        "positive_rows": int(final_y.sum()),
        "training_seconds": training_seconds,
        "boosting_rounds": best_rounds,
        "sklearn_version": sklearn.__version__,
        "xgboost_version": xgboost.__version__,
        "model_sha256": hashlib.sha256(model_path.read_bytes()).hexdigest(),
    }
    (output_dir / "metadata.json").write_text(json.dumps(metadata, indent=2), encoding="utf-8")
    (output_dir / "READY").write_text("Complete foundation artifact\n", encoding="utf-8")
    return metadata
