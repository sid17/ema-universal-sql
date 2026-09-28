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

## seed: load the demo tenants, connectors and policies. (Phase 1)
seed:
	@echo "seed: no-op — seeding lands in Phase 1 (control plane + connectors)."

## test: the unit suite. No Docker required, so a fresh clone can run it first.
test:
	$(PY) -m pytest -q tests/unit

## test-integration: route-level tests against the running stack. Needs `make up`.
test-integration:
	$(PY) -m pytest -q tests/integration

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
