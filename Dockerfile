# Slim app image (ADR-001: Python 3.11 + FastAPI).
# Dependency install is its own layer so editing src/ does not re-resolve deps —
# the submission gate times `make up` cold-to-serving at under 60s.
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Build the wheel from pyproject alone. hatchling needs the package directory to
# exist, so a placeholder stands in; the real sources are copied over it below
# and picked up from /app via PYTHONPATH.
COPY pyproject.toml ./
RUN mkdir -p src \
    && touch src/__init__.py \
    && pip install --no-cache-dir .

COPY src/ ./src/
COPY migrations/ ./migrations/
# config/ and scripts/ are what `make seed` runs. They must be IN the image:
# postgres and redis publish no host ports (see docker-compose.yml), so seeding
# happens inside the container rather than from the developer's machine.
COPY config/ ./config/
COPY scripts/ ./scripts/

ENV PYTHONPATH=/app

EXPOSE 8000

# 8 workers by default. Measured ~4x the throughput of one: a single event loop
# serialises every request behind ~15ms of synchronous work and knees at ~50-65
# RPS, where eight hold ~200+. Override with `WORKERS=1` for a single-process
# run (debugging, or reading /metrics without the multiprocess collector).
ENV WORKERS=8 \
    PROMETHEUS_MULTIPROC_DIR=/tmp/prometheus

CMD ["/app/scripts/entrypoint.sh"]
