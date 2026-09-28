#!/usr/bin/env bash
#
# Capture ONE live query's trace and render the waterfall artifact.
#
# Three things this does that a bare `python scripts/waterfall.py` cannot:
#
#   1. **Truncates the span log first.** ADR-018 opens it in append mode, so it
#      accumulates across every run and every branch. Rendering "the newest
#      trace" from a 34MB file risks an artifact that quietly describes code
#      that is no longer checked out (ADR-045).
#   2. **Forces a LIVE fetch** with `max_staleness_ms: 0`. A cache hit costs
#      ~0.4ms, so a cached trace would show the connectors as free and make the
#      waterfall argue the opposite of what it exists to show.
#   3. **Waits for the batch exporter.** `BatchSpanProcessor` flushes on a
#      timer, so the spans are not in the file the instant the response returns.
#      Polled rather than slept: the renderer already fails loudly on an
#      incomplete trace, so retrying it until it succeeds IS the readiness
#      check, and it ends the moment the data is there.
#
# Needs only bash, curl and a running stack — the renderer runs INSIDE the app
# container, so no host Python is required. TEST_MODE is not needed either.

set -euo pipefail

BASE="${BASE_URL:-http://localhost:8000}"
TIMEOUT="${TRACE_TIMEOUT:-30}"
LOG="traces/spans.jsonl"

CANONICAL='SELECT pr.title, pr.author, issue.key, issue.status FROM github.pull_requests pr JOIN jira.issues issue ON pr.issue_key = issue.key WHERE pr.repo = '"'"'ema/core'"'"' AND pr.state = '"'"'open'"'"' AND issue.status = '"'"'In Progress'"'"' ORDER BY issue.updated DESC LIMIT 50'

mkdir -p traces docs
: > "$LOG"

token=$(curl -fsS -XPOST "$BASE/v1/auth/mock-token" \
  -H 'content-type: application/json' \
  -d '{"user":"alice","role":"support","tenant":"tenant_acme"}' \
  | sed -n 's/.*"token":"\([^"]*\)".*/\1/p')

if [ -z "$token" ]; then
  echo "FAILED: could not mint a token against $BASE — is the stack up (\`make up\`)?" >&2
  exit 1
fi

trace=$(curl -fsS -XPOST "$BASE/v1/query" \
  -H "authorization: Bearer $token" \
  -H 'content-type: application/json' \
  -d "{\"sql\": \"${CANONICAL}\", \"max_staleness_ms\": 0}" \
  | sed -n 's/.*"trace_id":"\([^"]*\)".*/\1/p')

if [ -z "$trace" ]; then
  echo "FAILED: the query returned no trace_id. Is OTEL_EXPORTER=file? Is the stack seeded (\`make seed\`)?" >&2
  exit 1
fi

echo "captured trace $trace — waiting for the batch exporter to flush (up to ${TIMEOUT}s)..."

start=$(date +%s)
until docker compose exec -T app python scripts/waterfall.py \
        --trace-id "$trace" \
        --out traces/trace-waterfall.txt \
        --svg traces/trace-waterfall.svg >/dev/null 2>&1; do
  if [ $(( $(date +%s) - start )) -ge "$TIMEOUT" ]; then
    echo "FAILED: trace $trace never became complete within ${TIMEOUT}s. Renderer said:" >&2
    docker compose exec -T app python scripts/waterfall.py --trace-id "$trace" >&2 || true
    exit 1
  fi
  sleep 1
done

mv traces/trace-waterfall.txt traces/trace-waterfall.svg docs/
cat docs/trace-waterfall.txt
echo "wrote docs/trace-waterfall.txt and docs/trace-waterfall.svg"
