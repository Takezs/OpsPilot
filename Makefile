.PHONY: check eval-smoke

PYTHON ?= python

check:
	$(PYTHON) -m ruff check backend
	$(PYTHON) -m ruff format --check backend
	$(PYTHON) -m mypy backend/src
	$(PYTHON) -m pytest --cov=opspilot --cov-report=term-missing backend/tests

eval-smoke:
	cd backend && ../.venv/Scripts/python -m opspilot.evaluation.dev_smoke
