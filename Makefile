SHELL := bash
SSH_KEY    ?= ~/.ssh/id_ed25519.pub
OUTPUT_DIR ?= /tmp/s3aas-build
# Address the VMs reach the artifact server (this host) at.
HTTP_HOST  ?= 10.20.0.1
ENDPOINT   ?= http://10.20.0.2/api/core
USERNAME   ?= admin
PASSWORD   ?=

all: help

help:
	@echo "build            - build the s3aas element manifest + DP image"
	@echo "env              - build, serve and install everything into a live Core"
	@echo "env-clean        - stop the artifact server started by 'env'"
	@echo "wheel            - build the exordos_s3 Python wheel"
	@echo "lint             - run ruff check"
	@echo "format           - run ruff format"
	@echo "test             - run unit tests via tox"
	@echo "functional       - run functional tests (needs 'env')"
	@echo "typecheck        - run mypy"

build:
	exordos build -i $(SSH_KEY) -f .

# Prepares a full E2E environment against a bootstrapped Core and prints the
# env vars the functional suite needs. Same entry point the CI uses.
env:
	python exordos_s3/tests/functional/prepare_env.py \
		--output-dir $(OUTPUT_DIR) \
		--http-host $(HTTP_HOST) \
		--endpoint $(ENDPOINT) \
		--username $(USERNAME) \
		--password $(PASSWORD)

env-clean:
	python exordos_s3/tests/functional/prepare_env.py \
		--output-dir $(OUTPUT_DIR) --cleanup

wheel:
	python -m build --wheel

lint:
	tox -e ruff-check

format:
	tox -e ruff

test:
	tox -e py312

functional:
	tox -e py312-functional

typecheck:
	tox -e mypy
