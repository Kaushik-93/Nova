COMPOSE := docker compose

.DEFAULT_GOAL := help
.PHONY: help env up down reset logs ps seed test test-llm lock skill agent

help: ## Show this help
	@grep -hE '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  %-8s %s\n", $$1, $$2}'

env: ## Create .env with generated secrets (keeps an existing one)
	@sh infra/scripts/init-env.sh

up: env ## Build and start the whole stack, seeded, and wait until it is healthy
	$(COMPOSE) up -d --build --wait --wait-timeout 600
	@. ./.env; \
	  echo; \
	  echo "Nova is up."; \
	  echo "  Superset  $$SUPERSET_PUBLIC_URL  (user: $$SUPERSET_ADMIN_USER, password in .env)"; \
	  echo "  Trino     http://localhost:$$TRINO_HOST_PORT"

down: ## Stop the stack, keeping data
	$(COMPOSE) down

reset: ## Stop the stack and delete all of its data
	$(COMPOSE) down -v --remove-orphans

logs: ## Follow logs (make logs s=superset for one service)
	$(COMPOSE) logs -f $(s)

ps: ## Show service status
	$(COMPOSE) ps -a

seed: env ## Rebuild the demo data from scratch
	$(COMPOSE) run --rm --build seed python -m seed.load --force

test: up ## Run all tests against the running stack
	$(COMPOSE) --profile test run --rm --build tests
	$(COMPOSE) --profile test run --rm --build agent-tests

test-llm: up ## Run the tests that call the real model (these cost money)
	$(COMPOSE) --profile test run --rm --build agent-tests pytest -c agent/pyproject.toml --rootdir=/app agent/tests/llm -q -m llm

skill: up ## Profile a source: make skill CATALOG=demo_rental DIALECT=postgres NAME="Rental platform"
	$(COMPOSE) --profile test run --rm --build -T agent-tests python -m agent.subagents.profiler --catalog $(CATALOG) --dialect $(DIALECT) --name "$(or $(NAME),$(CATALOG))"

agent: up ## Build a dashboard: make agent PROMPT="..." (add THREAD=name to change one)
	$(COMPOSE) --profile test run --rm --build -T agent-tests python -m agent.cli --prompt "$(PROMPT)" --thread "$(THREAD)"

lock: ## Regenerate agent/requirements.lock (needs uv)
	uv pip compile agent/requirements.txt --python-version 3.13 --python-platform linux -q -o agent/requirements.lock
