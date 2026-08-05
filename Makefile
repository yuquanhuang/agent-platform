PYTHON ?= uv run python
HARNESS := $(PYTHON) scripts/harness.py

.PHONY: format format-check lint typecheck test build check check-all backend-check frontend-check contract-check harness-dry-run

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

harness-dry-run:
	$(HARNESS) check --dry-run
