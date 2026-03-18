.PHONY: help up down build logs restart test lint format typecheck pre-commit health

help:
	@echo "LibraryAI — available targets:"
	@echo "  up          Start all services (docker compose up -d)"
	@echo "  down        Stop all services"
	@echo "  build       Build all service images"
	@echo "  logs        Tail all service logs"
	@echo "  restart     Restart all services"
	@echo "  test        Run test suite"
	@echo "  lint        Check code style (ruff + black + isort)"
	@echo "  format      Auto-format code (black + isort + ruff --fix)"
	@echo "  typecheck   Run mypy type checks"
	@echo "  pre-commit  Run all pre-commit hooks"
	@echo "  health      Curl /health on all API services"

up: .env
	docker compose up -d

down:
	docker compose down

build:
	docker compose build

logs:
	docker compose logs -f

restart:
	docker compose restart

test:
	pytest tests/ -v

lint:
	ruff check .
	black --check .
	isort --check .

format:
	black .
	isort .
	ruff check --fix .

typecheck:
	mypy .

pre-commit:
	pre-commit run --all-files

health:
	@for port in 8000 8001 8002 8003 8004 8005; do \
		printf "%-40s" "http://localhost:$$port/health"; \
		curl -sf http://localhost:$$port/health && echo " OK" || echo " FAIL"; \
	done

# Ensure .env exists before starting services
.env:
	@test -f .env || (echo "ERROR: .env missing — run: cp .env.example .env" && exit 1)
