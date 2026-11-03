# The commands a reviewer runs, and the commands a deployer runs.
#
#   make help     what is here
#   make setup    install the dependencies
#   make test     the unit suite
#   make verify   everything: unit, API, deep, scale
#   make fresh    rebuild the environment from an empty ledger
#   make release  stage the publication package

PYTHON ?= python3
PORT   ?= 8095
LEDGER ?= ws://127.0.0.1:6006
ERP    ?= http://127.0.0.1:8096

.PHONY: help setup test verify fresh reset up down status scale release lint clean

help:
	@echo ""
	@echo "  setup    install the dependencies"
	@echo "  test     the unit suite"
	@echo "  verify   unit, API, deep, and scale - the full check"
	@echo "  fresh    rebuild the ledger and the environment from nothing"
	@echo "  up       start the ledger, the ERP, the signer, the service"
	@echo "  down     stop everything"
	@echo "  status   what is running"
	@echo "  scale    the million-instruction scale harness"
	@echo "  release  stage the publication package (does NOT publish)"
	@echo "  clean    remove the generated state and the caches"
	@echo ""

setup:
	$(PYTHON) -m pip install -r requirements-dev.txt

test:
	PYTHONPATH=src $(PYTHON) -m pytest tests -q

verify:
	$(PYTHON) dev_env.py verify-all --allow-move

fresh:
	$(PYTHON) dev_env.py fresh

reset:
	$(PYTHON) dev_env.py reset

up:
	$(PYTHON) dev_env.py up

down:
	$(PYTHON) dev_env.py down

status:
	$(PYTHON) dev_env.py status

scale:
	$(PYTHON) scale_test.py --instructions 1000000

release:
	$(PYTHON) build_release.py

lint:
	$(PYTHON) -m compileall -q src tests

clean:
	$(PYTHON) -c "import shutil,pathlib; [shutil.rmtree(p) for p in pathlib.Path('.').rglob('__pycache__') if p.is_dir()]; shutil.rmtree('.pytest_cache', ignore_errors=True)"
