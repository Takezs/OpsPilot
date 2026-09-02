.PHONY: migrate seed check test-integration test-e2e eval-smoke release-candidate

PYTHON ?= .venv/Scripts/python.exe
RUFF ?= .venv/Scripts/ruff.exe
MYPY ?= .venv/Scripts/mypy.exe
ALEMBIC ?= .venv/Scripts/alembic.exe

migrate:
	cd backend && ../$(ALEMBIC) -c alembic.ini upgrade head

seed:
	cd backend && ../$(PYTHON) -m opspilot.release

check:
	cd backend && ../$(RUFF) format --check src tests alembic ../evaluation
	cd backend && ../$(RUFF) check src tests alembic ../evaluation
	cd backend && ../$(MYPY) --no-incremental src
	cd backend && ../$(PYTHON) -m pytest -q -p no:cacheprovider --basetemp=../.pytest-temp-release
	cd frontend && npm run test && npm run typecheck && npm run build

test-integration:
	cd backend && ../$(PYTHON) -m pytest -q -p no:cacheprovider -m integration --basetemp=../.pytest-temp-release-integration

test-e2e:
	cd frontend && npm run test:e2e

eval-smoke:
	cd backend && ../$(PYTHON) -m opspilot.evaluation.dev_smoke

release-candidate: migrate seed check test-integration test-e2e eval-smoke
