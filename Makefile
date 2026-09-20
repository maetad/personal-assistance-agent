# read -p needs bash, not the POSIX sh that's /bin/sh on some systems.
SHELL := /bin/bash

# Name of the Hermes container as defined in docker-compose.yml
CONTAINER ?= hermes-agent
PLUGIN ?= behavior-logger

# Default model config applied to new profiles (override on the command line,
# e.g. make create-profile NAME=dad TOKEN=... MODEL_PROVIDER=openrouter MODEL_NAME=foo/bar)
MODEL_PROVIDER ?= nous
MODEL_NAME ?= upstage/solar-pro4:free
MODEL_BASE_URL ?= https://inference-api.nousresearch.com/v1

.PHONY: help up down status create-profile list-profiles start-gateway stop-gateway logs install-plugin

# Default command: display usage guide
help:
	@echo "========================================================"
	@echo "           Hermes Agent Multi-Profile Manager           "
	@echo "========================================================"
	@echo "Commands:"
	@echo "  make up                               Launch Docker stack"
	@echo "  make down                             Stop Docker stack"
	@echo "  make status                           Check running containers & profiles"
	@echo "  make list-profiles                    List active profiles"
	@echo "  make create-profile                   Create a profile set up like 'pan' (prompts for"
	@echo "                                         Name/Token/WebUI password, or pass NAME=... TOKEN=..."
	@echo "                                         WEBUI_PASSWORD=... to skip prompts)"
	@echo "                        [MODEL_PROVIDER=nous MODEL_NAME=upstage/solar-pro4:free MODEL_BASE_URL=...]"
	@echo "  make install-plugin NAME=...          Install plugins/behavior-logger into a profile"
	@echo "  make start-gateway NAME=...           Start gateway for a profile"
	@echo "  make stop-gateway NAME=...            Stop gateway for a profile"
	@echo "  make logs                             Tail container logs"
	@echo "========================================================"

up:
	docker compose up -d

down:
	docker compose down

status:
	@echo "--- Docker Containers ---"
	docker compose ps
	@echo ""
	@echo "--- Hermes Profiles ---"
	docker exec -it $(CONTAINER) hermes profile list

list-profiles:
	docker exec -it $(CONTAINER) hermes profile list

# NAME/TOKEN/WEBUI_PASSWORD can be passed on the command line (make create-profile
# NAME=dad TOKEN=... WEBUI_PASSWORD=...) to skip prompts, e.g. for scripting.
create-profile:
	@NAME="$(NAME)"; TOKEN="$(TOKEN)"; WEBUI_PASSWORD="$(WEBUI_PASSWORD)"; \
	[ -n "$$NAME" ] || read -p "Name: " NAME; \
	[ -n "$$TOKEN" ] || read -p "Telegram token: " TOKEN; \
	[ -n "$$WEBUI_PASSWORD" ] || read -p "WebUI password: " WEBUI_PASSWORD; \
	echo "--> Creating Hermes profile '$$NAME'..."; \
	docker exec -it $(CONTAINER) hermes profile create $$NAME; \
	echo "--> Setting Telegram Bot Token..."; \
	docker exec -it $(CONTAINER) hermes -p $$NAME config set TELEGRAM_BOT_TOKEN "$$TOKEN"; \
	echo "--> Configuring model ($(MODEL_PROVIDER) / $(MODEL_NAME), matching 'pan')..."; \
	docker exec -it $(CONTAINER) hermes -p $$NAME config set model.provider $(MODEL_PROVIDER); \
	docker exec -it $(CONTAINER) hermes -p $$NAME config set model.default $(MODEL_NAME); \
	docker exec -it $(CONTAINER) hermes -p $$NAME config set model.base_url $(MODEL_BASE_URL); \
	docker exec -it $(CONTAINER) hermes -p $$NAME config set agent.reasoning_effort medium; \
	docker exec -it $(CONTAINER) hermes -p $$NAME config set agent.max_tokens 4000; \
	echo "--> Logging in to $(MODEL_PROVIDER) (follow the browser/device prompt)..."; \
	docker exec -it $(CONTAINER) hermes -p $$NAME auth add $(MODEL_PROVIDER); \
	echo "--> Installing behavior-logger plugin and starting gateway..."; \
	$(MAKE) install-plugin NAME=$$NAME CONTAINER=$(CONTAINER); \
	echo "--> Setting hermes-webui password for '$$NAME'..."; \
	NAME_UPPER=$$(echo $$NAME | tr '[:lower:]' '[:upper:]'); \
	if grep -q "^HERMES_WEBUI_PASSWORD_$$NAME_UPPER=" .env 2>/dev/null; then \
		sed -i "s/^HERMES_WEBUI_PASSWORD_$$NAME_UPPER=.*/HERMES_WEBUI_PASSWORD_$$NAME_UPPER=$$WEBUI_PASSWORD/" .env; \
	else \
		echo "HERMES_WEBUI_PASSWORD_$$NAME_UPPER=$$WEBUI_PASSWORD" >> .env; \
	fi; \
	echo "--> Profile '$$NAME' successfully created, configured like 'pan', and gateway started!"; \
	echo "--> To expose it in hermes-webui, copy the webui-pan block in docker-compose.yaml to webui-$$NAME (see README)."

# Copies plugins/$(PLUGIN) from this repo into a profile's plugin dir, enables it, and
# restarts that profile's gateway to pick it up.
# ponytail: overwrites plugins.enabled wholesale (fine while behavior-logger is the only
# plugin); switch to a read-merge-write if a profile ever needs more than one plugin.
install-plugin:
ifndef NAME
	$(error NAME is missing. Usage: make install-plugin NAME=dad [PLUGIN=behavior-logger])
endif
	@echo "--> Installing plugin '$(PLUGIN)' into profile '$(NAME)'..."
	docker exec $(CONTAINER) mkdir -p /opt/data/profiles/$(NAME)/plugins/$(PLUGIN)
	docker cp plugins/$(PLUGIN)/. $(CONTAINER):/opt/data/profiles/$(NAME)/plugins/$(PLUGIN)/
	docker exec -it $(CONTAINER) hermes -p $(NAME) config set plugins.enabled '["$(PLUGIN)"]'
	docker exec -it $(CONTAINER) hermes -p $(NAME) gateway stop
	docker exec -it $(CONTAINER) hermes -p $(NAME) gateway start
	docker exec -it $(CONTAINER) hermes -p $(NAME) plugins doctor $(PLUGIN)

start-gateway:
ifndef NAME
	$(error NAME is missing. Usage: make start-gateway NAME=dad)
endif
	docker exec -it $(CONTAINER) hermes -p $(NAME) gateway start

stop-gateway:
ifndef NAME
	$(error NAME is missing. Usage: make stop-gateway NAME=dad)
endif
	docker exec -it $(CONTAINER) hermes -p $(NAME) gateway stop

logs:
	docker compose logs -f hermes
