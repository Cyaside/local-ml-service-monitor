# ML model and training

[Back to README](../README.md)

## Final model

The project uses one `XGBClassifier`. There is no runtime ensemble and no
detector selection menu. XGBoost trains with CUDA in Colab, while the same saved
trees run through `xgboost-cpu` on the laptop. This keeps inference light without
making public-data training wait on the laptop CPU.

The model input is a fixed set of 16 causal temporal features. For every source,
raw dimensions are normalized against that source's normal median and IQR. A
31-sample window then describes:

- the maximum, mean, and 90th percentile absolute robust deviation;
- the fraction of dimensions beyond three robust scale units;
- positive and negative changes over 1, 6, and 30 samples;
- the difference between six-sample and thirty-sample means;
- the largest positive and negative current robust values.

This representation lets one classifier consume PSM with 25 dimensions, SMD
with up to 38 dimensions, and the local service with five metrics. The local
metrics remain CPU, RSS memory, requests per minute, p95 latency, and error rate.

## Public training in Colab

Open `notebooks/train_universal_anomaly_model_colab.ipynb` in Google Colab. The
notebook downloads source data from pinned revisions of:

- [eBay PSM](https://github.com/eBay/RANSynCoders)
- [SMD from OmniAnomaly](https://github.com/NetManAIOps/OmniAnomaly)

No manual dataset upload is required. Choose **Run all** once; the notebook
requests a GPU, downloads every source file, trains the model, and downloads the
completed artifact as `telemetry-foundation-full.zip`.

The notebook runs the `full` profile: PSM and all 28 SMD machines. The final SMD
entity is excluded while cross-domain validation is measured, then the one final
classifier is fit on all selected entities.

The artifact contains:

```text
models/foundation-public-v1/
  foundation.ubj     final public classifier
  metadata.json      data revisions, schema, held-out metrics, and checksum
  READY              completion marker written last
```

## Local adaptation and calibration

Public datasets do not have the same metric semantics or scale as the checkout
service. The local step adds 80 boosting rounds from the labeled validation run,
then computes the reference profile and false-alarm threshold from a separate
normal baseline. The result is still one XGBoost model file:

```powershell
uv run service-monitor calibrate `
  --foundation models/foundation-public-v1 `
  --baseline data/exports/train-baseline-20260919T132930.csv `
  --validation data/exports/validation-20260919T160854.csv `
  --out models/checkout-universal-v1
```

The validation recording supplies labeled normal and fault windows for local
adaptation. The normal baseline independently supplies five medians, five IQR
scales, and the 99.5th percentile model-score threshold. The untouched test
recording is not used by either step.

The calibrated artifact adds `profile.json`, `calibration.json`, and service
metadata. Model and schema checksums are verified before XGBoost loads the UBJ
model file.

## Final local test

Evaluate once on the untouched local test recording:

```powershell
uv run service-monitor evaluate `
  --model models/checkout-universal-v1 `
  --input data/exports/test-20260919T180539.csv `
  --report reports/checkout-universal-v1-test.json
```

The report compares the ML model with the existing static latency, error, and
memory baseline. It records fault recall, false incidents per observable normal
hour, feature coverage, detection delay, per-fault results, and incident
lifecycles. Runtime loading remains an explicit opt-in through
`--allow-experimental`, so deployment cannot happen accidentally from a newly
calibrated artifact.

## Live behavior

The first score needs 31 consecutive valid samples, or roughly five minutes at
the ten-second sampling interval. A gap, invalid telemetry, or service restart
clears the rolling window. Three abnormal scores within five samples open an
incident; five consecutive normal scores resolve it.
