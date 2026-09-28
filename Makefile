# Every entry point for this repo. `demo`, `e2e` and `load` are declared now as
# stubs naming their phase — a target introduced late is a target forgotten.
#
# `python` is not on PATH in this environment; every Python command goes through
# the pinned virtualenv.

PY := .venv/bin/python
RUFF := .venv/bin/ruff
HEALTH_URL ?= http://localhost:8000/healthz
HEALTH_TIMEOUT ?= 90

.PHONY: up down seed test test-integration e2e load demo fmt

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
test-integration:
	SEED_TEST_DATABASE_URL="$(SEED_TEST_DATABASE_URL)" $(PY) -m pytest -q tests/integration

## e2e: Playwright browser specs. (Phase 3)
e2e:
	@echo "e2e: no-op — Playwright UI specs land in Phase 3."

## load: k6 load profile, ~500-1k QPS for 60s. (Phase 4)
load:
	@echo "load: no-op — the k6 load profile lands in Phase 4."

## demo: the scripted walkthrough of the five hard parts. (Phase 2)
demo:
	@echo "demo: no-op — the scripted walkthrough is filled in at Phase 2."

## fmt: format the tree.
fmt:
	$(RUFF) format src tests
