FROM ghcr.io/astral-sh/uv:0.10.12 AS uv
FROM python:3.12-slim
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never PYTHONUNBUFFERED=1 \
    OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 MKL_NUM_THREADS=1 \
    PATH="/app/.venv/bin:$PATH"
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv sync --locked --no-dev --no-editable && \
    useradd --create-home monitor && mkdir -p /app/data && chown monitor:monitor /app/data
USER monitor
ENTRYPOINT ["service-monitor"]
CMD ["--help"]
