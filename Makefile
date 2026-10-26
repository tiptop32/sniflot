.PHONY: install test lint run docker

install:
	pip install -e ".[dev,mcp]"

test:
	pytest

lint:
	ruff check src tests
	ruff format --check src tests
	mypy

run:
	sniflot serve

docker:
	docker compose up -d --build
