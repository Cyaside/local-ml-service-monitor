# Docker

[Back to README](../README.md)

Docker Desktop must be running with Linux containers. Each service is limited to
one CPU and 512 MiB of memory. The backend, traffic generator, and collector run
as a non-root user.

```powershell
docker compose up --build -d
docker compose ps
docker compose logs --tail 20 collector
```

The health endpoint is available at `http://127.0.0.1:8012/health`. The published
port is bound to loopback only. Traffic and collection stop after 30 minutes.
Stop the remaining backend with:

```powershell
docker compose down
```

SQLite data is stored in the `telemetry` named volume and is preserved by
`docker compose down`. Export it after the collector stops:

```powershell
docker compose run --rm --no-deps collector export --db /app/data/container.sqlite --out /app/data/container.csv
docker compose cp collector:/app/data/container.csv ./container.csv
```

Export refuses to overwrite an existing CSV. Use a new filename for each export.
Do not run `docker compose down -v` when the telemetry volume must be retained.

The default Compose service ID is `checkout-container`. Calibrate a separate
service artifact from a normal container recording and the public foundation
model. The Windows artifact for `checkout-local` is intentionally not mounted
because process CPU and RSS depend on the operating environment.

Fault injection endpoints are disabled in the default Compose configuration.
They can be enabled for controlled validation by adding
`--enable-dev-scenarios` to the backend command. Do not publish those endpoints
to an external network.

Telemetry, models, logs, `.env` files, and private planning files are excluded
from the image build context.
