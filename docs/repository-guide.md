# Repository contents

[Back to README](../README.md)

## Files included in Git

| Path | Purpose |
|---|---|
| `src/` | Backend, collector, monitor, and ML implementation |
| `tests/` | Automated verification |
| `scripts/` | Reproducible recording and launch commands |
| `notebooks/` | Google Colab public-model training |
| `docs/`, `README.md` | System documentation and the published evaluation summary |
| `pyproject.toml`, `uv.lock`, `.python-version` | Reproducible Python environment |
| `.gitignore`, `.gitattributes`, `.editorconfig` | Repository and editor rules |
| `Dockerfile`, `compose.yaml`, `.dockerignore`, `.github/` | Packaging and CI |

The backend and collector belong in the same repository because a fresh clone
must be able to run the full data path.

## Local-only files

| Path | Contents |
|---|---|
| `local-ml-service-monitor-plan/` | Private planning notes |
| `.venv/` and caches | Recreated dependencies and caches |
| `data/` | Telemetry, public dataset cache, CSV files, and SQLite databases |
| `models/` | Foundation and calibrated service artifacts |
| `runs/` | Process status, PIDs, and logs |
| `reports/` | Generated evaluation and prediction output |
| `.env`, `.env.*` | Local configuration and secrets |

CSV and JSON files are not ignored globally, so small fixtures and public
configuration can still be committed in appropriate directories. Binary model
files and SQLite databases are ignored regardless of location.

`.gitignore` does not delete local files. It only prevents untracked generated
files from being added.

## Before a commit

Inspect the exact file set before updating the repository:

```powershell
git status --short --untracked-files=all
git add .
git diff --cached --stat
git diff --cached --name-only
```

Only source code, tests, documentation, and configuration should be staged.
`local-ml-service-monitor-plan/`, `.venv/`, `data/`, `models/`, `runs/`, and
`reports/` must remain absent from the staged list.

## Routine verification

```powershell
uv sync --locked
uv run pytest -q
git status --short
git diff --cached --name-only
```

Generated evaluation output remains under ignored `reports/`. The reviewed
local acceptance result is copied to `docs/results/` so the numbers published
in the README have a versioned machine-readable source. No license has been
selected for the project source. PSM retains its CC BY 4.0 data license, and
public data is downloaded from its original repository rather than copied into
this repository.
