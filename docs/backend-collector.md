# Backend, collection, and monitoring

[Back to README](../README.md)

Run commands from the repository root. `uv sync --locked` installs the locked
environment. The native backend uses one worker, binds to localhost, and samples
telemetry every 10 seconds. No GPU is required.

## Record measured telemetry

Start a bounded 60-minute normal recording on Windows:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/start-recording.ps1
Get-Content runs/recording-status.json
```

The launcher starts the backend, request generator, collector, and watcher in the
background. Logs and PIDs are written under `runs/recording-<timestamp>/`. The
watcher stops the backend after collection and records verified child exit codes.

One representative normal baseline is enough for local calibration. Additional
normal recordings are useful for measuring false alarms over a longer period,
but they are not required to train the public classifier.

The equivalent manual setup uses three terminals:

```powershell
# Terminal A
uv run service-monitor serve --enable-dev-scenarios

# Terminal B
uv run service-monitor traffic --profile normal-cycle --duration-minutes 65

# Terminal C
uv run service-monitor collect --db data/train.sqlite --duration-minutes 60
```

The normal-cycle profile rotates through 1, 3, and 6 requests per second every
five minutes. Use `--profile constant --rate 2` for a smaller fixed load.

Collector mode validates and stores telemetry without loading scikit-learn or a
trained model. Temporary network errors are reported and retried on the next
poll. Malformed telemetry terminates collection visibly.

## Export a dataset

```powershell
uv run service-monitor export --db data/train.sqlite --out data/exports/train.csv
uv run service-monitor inspect-data --input data/exports/train.csv
```

Existing CSV files are never overwritten. A one-hour recording produces about
360 samples before warmup and filtering. Local calibration requires at least 286
consecutive valid rows, which yields 256 feature vectors after warmup.

The collector ignores backend samples created before the collection session.
This prevents overlap between adjacent datasets. A restart requires a new feature
warmup window.

## Controlled fault injection

Fault injection is available only when the backend starts with
`--enable-dev-scenarios`. Trigger one fault at a time:

```powershell
uv run service-monitor scenario --name slow-response --delay-ms 600 --duration-seconds 180
uv run service-monitor scenario-status

uv run service-monitor scenario --name error-burst --error-probability 0.2 --duration-seconds 180
uv run service-monitor scenario --name memory-growth --step-mib 2 --cap-mib 64 --duration-seconds 300
```

Stop an active fault early with:

```powershell
uv run service-monitor scenario-reset
```

Fault controls are bounded. Memory growth is capped, only one fault may be active,
and the backend releases allocated blocks when the fault expires or is reset.

The stored `phase` and `fault_id` fields are offline evaluation annotations. They
are excluded from model features and are not shown by the operational monitor.
They identify known fault windows when validation and test reports are calculated.

## Calibrate and evaluate the public model

```powershell
uv run service-monitor calibrate --foundation models/foundation-public-v1 --baseline data/exports/train-baseline-20260919T132930.csv --out models/checkout-universal-v1
uv run service-monitor evaluate --model models/checkout-universal-v1 --input data/exports/test-20260919T180539.csv --report reports/checkout-universal-v1-test.json
```

The foundation classifier is trained in the Colab notebook. The local baseline
must contain normal observations only. Test data contains normal observations
and labeled faults and stays untouched until this final evaluation.

## Run the operational terminal monitor

The launcher starts the local checkout service and a constant request stream in
the background. The foreground process is the actual monitor:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/start-local-monitor.ps1
```

The terminal continuously shows CPU, memory, request rate, p95 latency, error
rate, model score, and incident state. It does not inject faults automatically.
After approximately five minutes of warmup, run the printed fault command from a
second terminal to verify detection. Press `Ctrl+C` to stop the monitor and its
local child processes.

To monitor an already running compatible service directly:

```powershell
uv run service-monitor monitor --url http://127.0.0.1:8000 --model models/checkout-universal-v1 --db data/monitor.sqlite --duration-minutes 60 --allow-experimental --pretty
uv run service-monitor incidents --db data/monitor.sqlite --limit 10
```

The model service ID must match the backend service ID. Live monitoring rejects
synthetic models. One SQLite database accepts only one collector or monitor writer.

The feature builder needs 31 consecutive valid samples before the first score.
An incident opens after at least three anomalous predictions within five samples
and resolves after five consecutive normal predictions. Missing data does not
resolve an incident.

## Stored data

- `metrics`: validated telemetry and unique sample identity.
- `predictions`: model version, score, threshold, and decision.
- `incidents`: OPEN, RESOLVED, or INTERRUPTED lifecycle records.
- `sessions`: target URL, mode, start time, and end time.

SQLite uses WAL mode. Reads and exports can run while the writer is active.
Generated databases, models, logs, and reports remain outside Git.
