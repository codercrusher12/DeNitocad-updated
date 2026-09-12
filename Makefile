.PHONY: install install-dev run test test-fast lint fmt typecheck check clean docker-build docker-run

install:
	pip install -r requirements.txt

install-dev:
	pip install -e ".[dev]"
	pre-commit install

run:
	python web_app.py

# Full suite, including geometry tests that need a real CadQuery install.
test:
	pytest --cov --cov-report=term-missing

# Everything except CadQuery-dependent geometry tests - useful when
# iterating on the API/parser/validator layers without a full OCCT build.
test-fast:
	pytest -m "not requires_cadquery" --cov --cov-report=term-missing

lint:
	ruff check .

fmt:
	ruff format .
	ruff check --fix .

typecheck:
	mypy .

# What CI runs, in order - use this before pushing.
check: lint typecheck test-fast

clean:
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	rm -rf .pytest_cache .mypy_cache .ruff_cache htmlcov .coverage coverage.xml

docker-build:
	docker build -t nitocad:latest .

docker-run:
	docker run --rm -p 8000:8000 --env-file .env nitocad:latest
