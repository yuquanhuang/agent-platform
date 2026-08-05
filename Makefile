PYTHON ?= uv run python
HARNESS := $(PYTHON) scripts/harness.py

.PHONY: format format-check lint typecheck test build check check-all backend-check frontend-check contract-check generate-contracts generated-check harness-dry-run

format:
	$(HARNESS) format

format-check:
	$(HARNESS) format-check

lint:
	$(HARNESS) lint

typecheck:
	$(HARNESS) typecheck

test:
	$(HARNESS) test

build:
	$(HARNESS) build

check:
	$(HARNESS) check

check-all:
	$(HARNESS) check --scope all

backend-check:
	$(HARNESS) check --scope backend

frontend-check:
	$(HARNESS) check --scope frontend

contract-check:
	$(HARNESS) contract-check --scope contracts

generate-contracts:
	$(PYTHON) scripts/generate_contracts.py

generated-check:
	$(PYTHON) scripts/generate_contracts.py --check

harness-dry-run:
	$(HARNESS) check --dry-run
