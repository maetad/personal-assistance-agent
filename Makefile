# Name of the Hermes container as defined in docker-compose.yml
CONTAINER ?= hermes-agent
PLUGIN ?= behavior-logger

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
	@echo "  make create-profile NAME=... TOKEN=.. Create & configure a profile"
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

create-profile:
ifndef NAME
	$(error NAME is missing. Usage: make create-profile NAME=dad TOKEN=123456:ABC...)
endif
ifndef TOKEN
	$(error TOKEN is missing. Usage: make create-profile NAME=dad TOKEN=123456:ABC...)
endif
	@echo "--> Creating Hermes profile '$(NAME)'..."
	docker exec -it $(CONTAINER) hermes profile create $(NAME)
	@echo "--> Setting Telegram Bot Token..."
	docker exec -it $(CONTAINER) hermes -p $(NAME) config set TELEGRAM_BOT_TOKEN "$(TOKEN)"
	@echo "--> Starting gateway service..."
	docker exec -it $(CONTAINER) hermes -p $(NAME) gateway start
	@echo "--> Profile '$(NAME)' successfully created and gateway started!"

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
