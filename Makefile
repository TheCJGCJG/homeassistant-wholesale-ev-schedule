.PHONY: test build shell clean lint format format-check security check

build:
	docker build -t wholesale-ev-schedule-test .

test: build
	docker run --rm wholesale-ev-schedule-test

test-compose:
	docker-compose run --rm test

test-coverage:
	docker-compose run --rm test pytest --cov=custom_components/wholesale_ev_schedule --cov-report=html
	@echo "Coverage report generated in htmlcov/index.html"

lint:
	docker-compose run --rm lint

format:
	docker-compose run --rm lint ruff format custom_components tests

format-check:
	docker-compose run --rm format-check

security:
	docker-compose run --rm security

check: test-compose lint format-check security

shell:
	docker-compose run --rm test /bin/bash

clean:
	rm -rf __pycache__ .pytest_cache .coverage htmlcov
	find . -type d -name __pycache__ -exec rm -rf {} +
	find . -type f -name "*.pyc" -delete
