# Mandate Rescue — convenience targets
# Usage: make <target>

.PHONY: up down test benchmark audit chaos clean

## Start the app (creates mandate_rescue.db file if absent, then docker compose up)
up:
	@if [ ! -f mandate_rescue.db ]; then touch mandate_rescue.db; fi
	docker compose up --build

## Stop and remove containers
down:
	docker compose down

## Run the fast pytest suite
test:
	pytest -q -m "not slow"

## Run all tests including slow volume tests
test-all:
	pytest -q

## Run the reproducible benchmark (seed=42, 30 runs)
benchmark:
	python benchmark.py --n-runs 30 --seed 42

## Run the standalone correctness audit
audit:
	python backend/seed.py && python backend/agent.py && python backend/audit_check.py

## Run all 7 adversarial chaos scenarios
chaos:
	python backend/chaos_test.py

## Remove generated artifacts
clean:
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -name "*.pyc" -delete 2>/dev/null || true
	find . -name ".pytest_cache" -type d -exec rm -rf {} + 2>/dev/null || true
