.PHONY: check

PYTHON ?= python

check:
	$(PYTHON) -m ruff check backend
	$(PYTHON) -m ruff format --check backend
	$(PYTHON) -m mypy backend/src
	$(PYTHON) -m pytest --cov=opspilot --cov-report=term-missing backend/tests
