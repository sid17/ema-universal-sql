# Every entry point for this repo. `demo`, `e2e` and `load` are declared now as
# stubs naming their phase — a target introduced late is a target forgotten.
#
# `python` is not on PATH in this environment; every Python command goes through
# the pinned virtualenv.

PY := .venv/bin/python
RUFF := .venv/bin/ruff
BASE_URL ?= http://localhost:8000
HEALTH_URL ?= $(BASE_URL)/healthz
HEALTH_TIMEOUT ?= 90

.PHONY: up down seed test test-integration test-mode e2e load load-seed load-mt demo demo-detail trace scrape connectors artifacts fmt

## up: build and start the stack, then wait until /healthz actually answers.
# Polled, not slept: the submission gate times cold-to-serving, so the wait has
# to end the moment the app is ready and fail loudly if it never gets there.
up:
	docker compose up -d --build
	@echo "waiting for $(HEALTH_URL) (timeout $(HEALTH_TIMEOUT)s)..."
	@start=$$(date +%s); \
	until curl -fsS $(HEALTH_URL) >/dev/null 2>&1; do \
		elapsed=$$(( $$(date +%s) - start )); \
		if [ $$elapsed -ge $(HEALTH_TIMEOUT) ]; then \
			echo "FAILED: $(HEALTH_URL) did not answer within $(HEALTH_TIMEOUT)s"; \
			docker compose ps; \
			docker compose logs --tail=80 app; \
			exit 1; \
		fi; \
		sleep 1; \
	done; \
	echo "healthy after $$(( $$(date +%s) - start ))s"

## down: stop the stack and remove its containers.
down:
	docker compose down

## seed: load connectors, grants, secrets, policies and budgets from config/*.yaml.
# Runs INSIDE the app container: postgres publishes no host port, so the
# developer's machine cannot reach it directly. Re-runnable — every write is an
# upsert, so reseeding after a demo is safe.
seed:
	docker compose exec -T app python -m scripts.seed

## test: the unit suite. No Docker required, so a fresh clone can run it first.
test:
	$(PY) -m pytest -q tests/unit

## test-integration: route-level and control-plane tests against the running
# stack. Needs `make up` (and `make seed` for the seed tests). SEED_TEST_DATABASE_URL
# points at the published 55432 mapping, not the in-compose 5432.
SEED_TEST_DATABASE_URL ?= postgresql://postgres:postgres@localhost:55432/universal_sql
test-integration: test-mode
	SEED_TEST_DATABASE_URL="$(SEED_TEST_DATABASE_URL)" $(PY) -m pytest -q tests/integration

## test-mode: recreate the app with TEST_MODE=1, enabling the /v1/test/* hooks.
# `make demo` and the integration suite both need POST /v1/test/fail-next, which
# 404s in a normal run. Phase 0 guarded those routes deliberately and this does
# NOT reverse that: the default stays off, and turning it on is an explicit,
# visible step rather than a compose default nobody reads. Idempotent — compose
# only recreates the container when the value actually changes.
# `--build`, not just `up`: `src/` is COPYed into the image, so without it a
# code change since the last `make up` leaves the container serving stale
# routes — and a /v1/test/* hook that 404s looks exactly like TEST_MODE being
# off, which is a confusing thing to debug.
test-mode:
	TEST_MODE=1 docker compose up -d --wait --build app

## trace: capture one live query and render the waterfall artifact.
# Truncates the span log first, so the artifact provably describes the code that
# is checked out rather than whatever accumulated across previous runs (ADR-045).
# The renderer runs inside the app container — no host Python needed.
trace:
	./scripts/trace.sh

## e2e: Playwright browser specs. (Phase 3)
e2e:
	@echo "e2e: no-op — Playwright UI specs land in Phase 3."

## load: k6 at ~500 RPS for 60s, writing docs/artifacts/load/k6-summary.txt.
# Runs k6 from its own container (the `load` compose profile), so a fresh clone
# needs only Docker — no host k6 binary. The sub-60s quickstart claim depends on
# a reviewer never having to install anything.
#
# OTEL_EXPORTER=none for the duration of the run, then restored. Exporting ~500
# spans/second to JSONL is measurement overhead on the very latency being
# measured — a Phase 0 watch-out carried forward. The README says the number
# excludes span export rather than letting it pretend otherwise. Restoring
# afterwards matters because `make trace` needs the file sink back.
#
# `-` on the k6 line: a FAILED THRESHOLD IS A RESULT, not a build error. The
# summary must still be written and the app must still be restored (ADR-044).
RATE ?= 500
DURATION ?= 60s
load:
	@echo "recreating app with OTEL_EXPORTER=none (span export would distort the measurement)..."
	OTEL_EXPORTER=none docker compose up -d --wait app
	-RATE=$(RATE) DURATION=$(DURATION) docker compose --profile load run --rm k6
	@echo "restoring the file span exporter..."
	docker compose up -d --wait app
	@echo "wrote docs/artifacts/load/k6-summary.txt"

## load-seed: create the synthetic load tenants (tenant_load_00 ... _NN).
# Separate from `make seed`, which is config-driven and describes the DEMO.
# Twenty identical tenants are not configuration anyone would author by hand.
LOAD_TENANTS ?= 20
# 5000/60s by default so S1-S3 measure the ENGINE. Set LOAD_MAX_REQUESTS=84 to
# seed GitHub's real 5,000/hour quota — that is what makes S4 throttle.
LOAD_MAX_REQUESTS ?= 5000
load-seed:
	docker compose exec -T -e LOAD_TENANTS=$(LOAD_TENANTS) -e LOAD_MAX_REQUESTS=$(LOAD_MAX_REQUESTS) app python -m scripts.seed_load_tenants

## load-mt: S3 — the realistic multi-tenant scenario at $(RATE) QPS for $(DURATION).
#
# EVERY setting is passed on BOTH commands, and that is not redundancy.
# `--build` is not optional either: `scripts/` and `src/` are COPYed into the
# image, not bind-mounted, so without it a load run measures whatever code was
# baked in last time. That silently ignored a rate-limit change once.
#
# `docker compose run` honours `depends_on:`, so starting k6 RECREATES `app`
# from the base compose file — silently discarding anything set only on the
# `up` line. That produced a completely wrong measurement once already
# (SYNTHETIC_ROWS=0, so every query joined an empty dataset and still passed
# its checks). The assertion below is the guard: it reads the value back out
# of the running container and refuses to measure a stack it did not configure.
SYNTHETIC_ROWS ?= 200
SYNTHETIC_KEYSPACE ?= 20
MISS_RATE ?= 0.05
WARMUP ?= 20s
LOAD_ENV = OTEL_EXPORTER=none \
	   SYNTHETIC_ROWS=$(SYNTHETIC_ROWS) \
	   SYNTHETIC_KEYSPACE=$(SYNTHETIC_KEYSPACE) \
	   LOAD_TENANTS=$(LOAD_TENANTS) \
	   MISS_RATE=$(MISS_RATE) \
	   WARMUP=$(WARMUP) \
	   RATE=$(RATE) \
	   DURATION=$(DURATION) \
	   LOAD_MAX_REQUESTS=$(LOAD_MAX_REQUESTS)

load-mt:
	@echo "recreating app: OTEL_EXPORTER=none, SYNTHETIC_ROWS=$(SYNTHETIC_ROWS), WORKERS=$${WORKERS:-8}"
	$(LOAD_ENV) docker compose up -d --wait --build app
	@got=$$(docker compose exec -T app printenv SYNTHETIC_ROWS); 	 if [ "$$got" != "$(SYNTHETIC_ROWS)" ]; then 	   echo "ABORT: app reports SYNTHETIC_ROWS=$$got, expected $(SYNTHETIC_ROWS)"; exit 1; 	 fi; 	 echo "  verified: app has SYNTHETIC_ROWS=$$got, $$(docker compose exec -T app sh -c 'tr "\0" "\n" < /proc/1/cmdline | tail -1') workers"
	$(MAKE) load-seed
	@echo "flushing the freshness cache so the run starts from a known state..."
	@docker compose exec -T redis redis-cli flushdb
	-$(LOAD_ENV) SCRIPT=multitenant.js docker compose --profile load run --rm k6
	@echo "restoring defaults (file span exporter, fixtures)..."
	docker compose up -d --wait app
	@echo "wrote docs/artifacts/load/k6-multitenant.txt"

## connectors: show what goes in and out of each connector.
# Runs INSIDE the app container for the same reason `make seed` does: postgres
# and redis publish no host ports, and the point of this tool is that it uses
# the REAL control plane, the REAL token bucket and the REAL freshness cache.
# Wiring it to fakes would only prove the fakes agree with each other.
#
# Writes docs/artifacts/connectors/connector-walkthrough.txt. It queries
# tenant_load and drains tenant_globex, never tenant_acme — `make demo` needs
# acme's 5+2 GitHub budget intact for its 429 to be deterministic.
#
# Ad-hoc, for poking at it directly:
#   docker compose exec app python -m src.connectorlab fetch github.pull_requests \
#     --where repo=ema/core --where state=open --limit 5
connectors:
	@mkdir -p docs/artifacts/connectors
	docker compose exec -T app python -m src.connectorlab --out - > docs/artifacts/connectors/connector-walkthrough.txt
	@echo "wrote docs/artifacts/connectors/connector-walkthrough.txt ($$(wc -l < docs/artifacts/connectors/connector-walkthrough.txt) lines)"

## scrape: capture /metrics as a submission artifact.
# Run it AFTER `make demo` or `make load`, or the histograms are empty and the
# rate_limit_remaining gauge has no samples — the gauge is fed by a query.
scrape:
	@mkdir -p docs/artifacts/metrics
	curl -fsS $(BASE_URL)/metrics > docs/artifacts/metrics/metrics-scrape.txt
	@echo "wrote docs/artifacts/metrics/metrics-scrape.txt ($$(wc -l < docs/artifacts/metrics/metrics-scrape.txt) lines)"

## artifacts: regenerate every reproducible submission artifact, in order.
# demo first (it exercises the stack), then trace, then the scrape — which must
# come last so it captures metrics the other two produced.
artifacts: demo demo-detail trace connectors scrape
	@find docs/artifacts -type f | sort

## demo: the scripted walkthrough, teed to docs/artifacts/demo/demo-output.txt.
# Four calls covering four of the five hard parts with no UI and no observability
# stack — the artifact insurance, so a slip in Phase 3 or 4 cannot leave the
# submission with no demo at all.
demo: test-mode
	./scripts/demo.sh

## demo-detail: the coverage matrix — ~34 scenes answering the brief's detailed
# requirement list line by line, teed to docs/artifacts/demo/demo-detail.txt.
#
# A SECOND artifact, not a replacement. demo.sh is a narrative a reviewer reads
# in 60 seconds; this one is the checklist they tick. Different readers, so they
# change for unrelated reasons and neither should grow into the other.
#
# Needs TEST_MODE for the /v1/test/* hooks, same as `demo`. §3 drains
# tenant_acme's GitHub bucket deliberately; every section resets first, so the
# run reproduces from any starting state and leaves one behind for `make trace`.
demo-detail: test-mode
	./scripts/demo_detail.sh

## fmt: format the tree.
fmt:
	$(RUFF) format src tests
