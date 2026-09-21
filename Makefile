# Makefile raíz para redis-red-electrica-xm.
#
# Convenciones:
# - Las recetas son wrappers sobre docker-compose para que un contribuidor
#   nuevo pueda arrancar todo sin recordar la sintaxis exacta.
# - `make test` corre pytest sin levantar Docker (usa fakeredis).
# - `make demo` solo imprime instrucciones: el demo en vivo es por docker.

# Directorio donde vive docker-compose.yml
COMPOSE_DIR := infra
COMPOSE_FILE := $(COMPOSE_DIR)/docker-compose.yml

# Backend Python principal
PYTHON ?= python
PIP    ?= $(PYTHON) -m pip

.PHONY: help up up-redis up-dev down restart logs logs-redis logs-publisher \
        logs-subscriber logs-api redis-cli ps \
        install install-dev venv test test-cov test-unit test-int \
        lint lint-fix format type-check clean demo ci \
        build-dashboard build publish-locks

help: ## Show this help. Defaults to first target.
	@$(MAKE) -p 2>/dev/null | grep -E '^[a-zA-Z_-]+:.*?## .*$$' | sort | \
	  awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-20s\033[0m %s\n", $$1, $$2}'

##@ Docker

up: ## Levanta el stack completo en background (Redis + publisher + subscriber + API)
	docker compose -f $(COMPOSE_FILE) up -d

up-redis: ## Solo Redis — para correr publisher/subscriber/api a mano (Plan A de DEPLOY.md)
	docker compose -f $(COMPOSE_FILE) up -d redis

up-dev: ## Stack completo + dashboard en modo dev dentro de Docker (perfil `dev`)
	docker compose -f $(COMPOSE_FILE) --profile dev up -d

build: ## Construye las imágenes de publisher, subscriber y API
	docker compose -f $(COMPOSE_FILE) build

down: ## Detiene y borra contenedores (conserva el volumen redis-data)
	docker compose -f $(COMPOSE_FILE) --profile dev down

restart: down up ## Reinicia el stack completo

ps: ## Lista contenedores del stack y su estado
	docker compose -f $(COMPOSE_FILE) ps

logs: ## Sigue los logs de todos los servicios (Ctrl-C para salir)
	docker compose -f $(COMPOSE_FILE) logs -f --tail=100

logs-redis: ## Solo logs de Redis
	docker compose -f $(COMPOSE_FILE) logs -f --tail=100 redis

logs-publisher: ## Solo logs del publisher
	docker compose -f $(COMPOSE_FILE) logs -f --tail=100 publisher

logs-subscriber: ## Solo logs del subscriber
	docker compose -f $(COMPOSE_FILE) logs -f --tail=100 subscriber

logs-api: ## Solo logs de la API
	docker compose -f $(COMPOSE_FILE) logs -f --tail=100 api

redis-cli: ## Shell interactivo de redis-cli contra el contenedor Redis
	docker compose -f $(COMPOSE_FILE) exec redis redis-cli

##@ Instalación Python

venv: ## Crea un virtualenv en .venv si no existe
	@test -d .venv || $(PYTHON) -m venv .venv
	@echo "venv listo. Activar con:  source .venv/bin/activate  (Linux/macOS)  o  .\.venv\Scripts\Activate.ps1  (Windows)"

install: venv ## Instala el proyecto en modo editable (runtime deps)
	$(PIP) install -e .

install-dev: venv ## Instala proyecto + dependencias de desarrollo
	$(PIP) install -e ".[dev]"

##@ Tests

test: ## Ejecuta la suite completa de pytest
	$(PYTHON) -m pytest

test-unit: ## Solo tests unitarios (sin integración)
	$(PYTHON) -m pytest tests/unit

test-int: ## Solo tests de integración
	$(PYTHON) -m pytest tests/integration

test-cov: ## Ejecuta pytest con reporte de cobertura en terminal
	$(PYTHON) -m pytest --cov=. --cov-report=term-missing --cov-report=html:htmlcov

##@ Calidad de código

lint: ## Lint con ruff (sin autocorrección)
	$(PYTHON) -m ruff check .

lint-fix: ## Lint con autocorrección de ruff
	$(PYTHON) -m ruff check . --fix

format: ## Formateo con ruff (formateador oficial)
	$(PYTHON) -m ruff format .

type-check: ## Type checking con mypy
	$(PYTHON) -m mypy .

ci: lint type-check test ## Pipeline de CI local: lint + types + tests

##@ Frontend (dashboard)

build-dashboard: ## Construye el bundle de producción del dashboard
	cd dashboard && pnpm install --frozen-lockfile && pnpm build

publish-locks: ## Mensaje informativo sobre versionar lockfiles
	@echo "INFO: 'pnpm-lock.yaml' se commitea al repo para builds reproducibles."

### Utilidad

demo: ## Imprime instrucciones para correr el demo en vivo
	@echo ""
	@echo "=== Demo del Monitor de Red Eléctrica Colombiana ==="
	@echo ""
	@echo "1) Levantar el stack completo (Redis + publisher + subscriber + API):"
	@echo "   make up"
	@echo "   Comprobar:  curl localhost:8000/api/health"
	@echo ""
	@echo "2) Ver que los eventos fluyen:"
	@echo "   make logs-publisher"
	@echo ""
	@echo "3) Arrancar el dashboard en modo dev:"
	@echo "   cd dashboard && pnpm dev"
	@echo "   Abre http://localhost:5173"
	@echo ""
	@echo "4) Inspeccionar Redis en vivo:"
	@echo "   make redis-cli"
	@echo "   > KEYS '*'"
	@echo "   > SUBSCRIBE energy-events"
	@echo ""
	@echo "5) Apagar todo:"
	@echo "   make down"
	@echo ""

clean: ## Limpia artefactos locales (NO borra el venv ni el volumen docker)
	rm -rf .pytest_cache .ruff_cache .mypy_cache htmlcov .coverage
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name "*.egg-info" -exec rm -rf {} + 2>/dev/null || true
