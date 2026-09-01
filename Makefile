PY ?= python3
VENV := .venv
BIN := $(VENV)/bin

.DEFAULT_GOAL := help

.PHONY: help
help:
	@echo "Unnet — un-nets a lumped Razorpay payout back to the order"
	@echo
	@echo "  make setup      create the venv and install"
	@echo "  make gen        regenerate synthetic fixtures + ground truth"
	@echo "  make recon      run one reconciliation"
	@echo "  make eval       score against ground truth -> docs/metrics.json"
	@echo "  make ablation   rules-only vs rules+model -> docs/METRICS.md"
	@echo "  make test       run the test suite"
	@echo "  make web        build the dashboard (needs node)"
	@echo "  make serve      start API + dashboard on :8000"
	@echo "  make demo       gen + recon + serve, offline, no API key needed"
	@echo "  make record     record model cassettes (needs a provider configured)"

$(BIN)/python:
	$(PY) -m venv $(VENV)
	$(BIN)/pip install --upgrade pip
	$(BIN)/pip install -e ".[dev]"

.PHONY: setup
setup: $(BIN)/python

.PHONY: gen
gen: setup
	$(BIN)/python -m unnet.cli gen
	$(BIN)/python -m unnet.cli gen --profile messy

.PHONY: recon
recon: setup
	$(BIN)/python -m unnet.cli recon

.PHONY: eval
eval: setup
	$(BIN)/python -m unnet.cli eval

.PHONY: ablation
ablation: setup
	$(BIN)/python -m unnet.cli ablation

.PHONY: test
test: setup
	$(BIN)/python -m pytest -q

.PHONY: web
web:
	cd web && npm install && npm run build

.PHONY: serve
serve: setup
	$(BIN)/python -m unnet.cli serve

# Everything a reviewer needs, with no API key and no network.
.PHONY: demo
demo: setup gen
	$(BIN)/python -m unnet.cli recon
	$(BIN)/python -m unnet.cli ablation
	@echo
	@echo "Dashboard on http://127.0.0.1:8000"
	$(BIN)/python -m unnet.cli serve

.PHONY: record
record: setup
	UNNET_LLM_RECORD=1 $(BIN)/python -m unnet.cli --provider $${UNNET_LLM_PROVIDER:-local} ablation

.PHONY: clean
clean:
	rm -rf data/*.db data/*.db-wal data/*.db-shm .pytest_cache
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
