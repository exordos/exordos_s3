SHELL := bash
SSH_KEY ?= ~/.ssh/id_ed25519.pub

all: help

help:
	@echo "build            - build the s3aas element manifest + DP image"
	@echo "wheel            - build the exordos_s3 Python wheel"
	@echo "lint             - run ruff check"
	@echo "format           - run ruff format"
	@echo "test             - run unit tests via tox"
	@echo "functional       - run functional tests (needs a live core with s3aas)"
	@echo "typecheck        - run mypy"

build:
	exordos build -i $(SSH_KEY) -f .

wheel:
	tox -e wheel

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
