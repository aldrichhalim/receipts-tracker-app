# Narmada: everyday tasks. `make` on its own lists them.
#
#   make setup      first-time environment
#   make run        launch from source
#   make test       run the tests
#   make clean      remove build output
#   make build      build dist/ReceiptScanner.app
#   make release    publish the current version on GitHub

PYTHON  ?= python3
VENV    := .venv
VENV_PY := $(VENV)/bin/python

# One source of truth for the version: receiptscanner/__init__.py.
VERSION := $(shell sed -n 's/^__version__ = "\(.*\)"/\1/p' receiptscanner/__init__.py)
ARCH    := $(shell uname -m)
APP     := dist/ReceiptScanner.app
ZIP     := dist/ReceiptScanner-v$(VERSION)-macos-$(ARCH).zip

.DEFAULT_GOAL := help
.PHONY: help setup run test test-fast clean build verify package release

help: ## List the available tasks
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk -F':.*## ' '{ printf "  make %-10s %s\n", $$1, $$2 }'

$(VENV_PY):
	$(PYTHON) -m venv $(VENV)

setup: $(VENV_PY) ## Create .venv and install the app, test and build tools
	$(VENV_PY) -m pip install -r requirements.txt
	@echo "Tesseract is needed for source runs: brew install tesseract tesseract-lang"

run: $(VENV_PY) ## Launch the app from source
	$(VENV_PY) main.py

test: $(VENV_PY) ## Run every test this machine can (about a minute)
	$(VENV_PY) -m pytest

test-fast: $(VENV_PY) ## Run the tests that need no Tesseract, display or data/ (seconds)
	$(VENV_PY) -m pytest -m "not ocr and not gui and not fixtures"

clean: ## Remove build output (keeps .venv, data/ and the knowledge graph)
	rm -rf build dist *.egg-info .pytest_cache
	find . -path ./$(VENV) -prune -o -name __pycache__ -type d -exec rm -rf {} +

build: $(VENV_PY) ## Build dist/ReceiptScanner.app with PyInstaller
	$(VENV_PY) -m PyInstaller --noconfirm --clean ReceiptScanner.spec

verify: ## Check the built app is self-contained (Homebrew off the PATH)
	@test -d $(APP) || { echo "No $(APP): run 'make build' first."; exit 1; }
	@scratch=$$(mktemp -d); \
	RECEIPTSCANNER_CONFIG=$$scratch/config.json HOME=$$scratch \
	  env -u TESSDATA_PREFIX PATH=/usr/bin:/bin $(APP)/Contents/MacOS/ReceiptScanner --self-test; \
	status=$$?; rm -rf $$scratch; exit $$status

package: ## Zip the built app as dist/ReceiptScanner-v<version>-macos-<arch>.zip
	@test -d $(APP) || { echo "No $(APP): run 'make build' first."; exit 1; }
	rm -f $(ZIP)
	ditto -c -k --sequesterRsrc --keepParent $(APP) $(ZIP)
	@echo "Wrote $(ZIP)"

release: ## Test, build, verify and publish v<version> on GitHub (asks first)
	@VERSION=$(VERSION) ZIP=$(ZIP) ./scripts/release.sh
