.PHONY: install format lint typecheck security contracts test integration check helm terraform audit-deps image run clean

CHART := deployments/helm/lab-result-integrity

install:
	uv sync

format:
	uv run black src tests
	uv run ruff check src tests --fix

lint:
	uv run ruff check src tests
	uv run black --check src tests

typecheck:
	uv run mypy

security:
	uv run python -m bandit -q -r src -c pyproject.toml

contracts:
	uv run python -m lab_result_integrity.contracts --check

test:
	uv run pytest

integration:
	uv run pytest tests/integration -m integration --no-cov

check: lint typecheck security contracts test

helm:
	helm lint $(CHART) -f $(CHART)/ci/test-values.yaml
	helm template lri $(CHART) -f $(CHART)/ci/test-values.yaml > /dev/null
	! helm template lri $(CHART) > /dev/null 2>&1

terraform:
	terraform fmt -recursive -check deployments/terraform
	for c in aws azure gcp; do terraform -chdir=deployments/terraform/$$c init -backend=false -input=false && terraform -chdir=deployments/terraform/$$c validate; done

audit-deps:
	# agentic-core (git dependency) is audited in its own repository.
	uv export --frozen --no-dev --no-hashes --no-emit-project | grep -v '^agentic-core @' > requirements.audit.txt
	uvx pip-audit -r requirements.audit.txt --no-deps --disable-pip
	rm -f requirements.audit.txt

image:
	docker build -t lab-result-integrity:dev .

run:
	docker compose up --build

clean:
	rm -rf dist build .pytest_cache .mypy_cache .ruff_cache .coverage coverage.xml
