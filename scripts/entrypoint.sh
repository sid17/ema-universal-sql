#!/bin/sh
# Start uvicorn with WORKERS processes, after preparing the metrics directory.
#
# WHY A SCRIPT AND NOT A `CMD` ARRAY. With more than one worker,
# `prometheus_client` keeps each process's samples in files under
# PROMETHEUS_MULTIPROC_DIR and never removes them. Those files must be wiped
# ONCE, before any worker forks — a wipe inside the app's lifespan would run
# per worker and race the others, and no wipe at all would sum the previous
# run's counters into this one's. A parent shell is the only place that happens
# exactly once.
#
# `exec` so uvicorn becomes PID 1 and receives SIGTERM directly; without it
# `docker compose down` waits for the 10s timeout on every stop.
set -e

WORKERS="${WORKERS:-8}"

if [ -n "$PROMETHEUS_MULTIPROC_DIR" ]; then
  mkdir -p "$PROMETHEUS_MULTIPROC_DIR"
  rm -f "$PROMETHEUS_MULTIPROC_DIR"/*.db
fi

echo "starting uvicorn with ${WORKERS} worker(s)"
exec uvicorn src.main:app --host 0.0.0.0 --port 8000 --workers "$WORKERS"
