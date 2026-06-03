SHELL := bash
SSH_KEY    ?= ~/.ssh/id_ed25519.pub
REPOSITORY ?= http://10.20.0.1:8080/exordos-elements
INDEX_URL  ?= http://10.20.0.1:8080/simple/

all: help

help:
	@echo "build            - build the s3aas element manifest + DP image"
	@echo "install          - install s3aas element into Core"
	@echo "wheel            - build Python wheel for exordos_paas_s3"
	@echo "publish-wheel    - copy wheel to local pip index"
	@echo "lint             - run ruff check"
	@echo "format           - run ruff format"
	@echo "test             - run unit tests via tox"
	@echo "functional       - run functional tests (needs live stand)"
	@echo "typecheck        - run mypy"

build:
	exordos build -c exordos/exordos.yaml -i $(SSH_KEY) -f \
		--manifest-var repository=$(REPOSITORY) \
		--manifest-var index_url=$(INDEX_URL)

install:
	exordos em elements install output/manifests/s3aas.yaml

wheel:
	python -m build --wheel

publish-wheel: wheel
	cp dist/exordos_paas_s3-*.whl /srv/exordos-local-repo/simple/

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
