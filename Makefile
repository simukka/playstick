# Playstick -- build and run the phone UI.
#
# The UI is TypeScript under src/player, bundled into one self-contained file,
# src/player/dist/playstick-ui.html. The `gui` service (docker compose) serves it
# at http://localhost:8080/ and re-reads the file on every request, so a rebuild
# is picked up by a browser reload; a Python change needs `make gui-restart`.
#
# Node runs through src/player/dx, which uses a local node when there is one and
# node:22 in Docker otherwise -- nothing here needs node installed on the host.

PLAYER        := src/player
DX            := ./dx
COMPOSE       := docker compose
NODE_MODULES  := $(PLAYER)/node_modules

.DEFAULT_GOAL := help

.PHONY: help
help:
	@echo "Playstick GUI:"
	@echo "  make install      install the UI toolchain (node deps, in Docker)"
	@echo "  make build        bundle the UI -> src/player/dist/playstick-ui.html"
	@echo "  make typecheck    type-check the UI"
	@echo "  make test         run the UI unit tests"
	@echo "  make bench        run the UI benchmarks"
	@echo "  make check        typecheck + test + build"
	@echo "  make gui          build the UI and serve it at http://localhost:8080/"
	@echo "  make gui-restart  reload the daemon after a Python change"
	@echo "  make gui-down     stop the gui and drop its volumes"
	@echo "  make clean        remove the build output and node deps"
	@echo ""
	@echo "Override the published port with PLAYSTICK_GUI_PORT, and the library"
	@echo "with PLAYSTICK_GUI_LIBRARY=/path/to/movies."

# Auto-installed on first use of any node target; delete node_modules or run
# `make install` to refresh.
$(NODE_MODULES):
	cd $(PLAYER) && $(DX) npm install --no-audit --no-fund

.PHONY: install
install: $(NODE_MODULES)

.PHONY: build
build: $(NODE_MODULES)
	cd $(PLAYER) && $(DX) npm run build

.PHONY: typecheck
typecheck: $(NODE_MODULES)
	cd $(PLAYER) && $(DX) npm run typecheck

.PHONY: test
test: $(NODE_MODULES)
	cd $(PLAYER) && $(DX) npm test

.PHONY: bench
bench: $(NODE_MODULES)
	cd $(PLAYER) && $(DX) npm run bench

.PHONY: check
check: typecheck test build

# The gui service bind-mounts dist/playstick-ui.html, so it must exist before the
# container starts; `build` is a prerequisite. --build so the image also picks up
# any Dockerfile or dependency change. Ctrl-C to stop. The page is then served
# from the mount and reloads live -- no rebuild of the image for a UI edit.
.PHONY: gui
gui: build
	$(COMPOSE) up --build gui

.PHONY: gui-restart
gui-restart:
	$(COMPOSE) restart gui

.PHONY: gui-down
gui-down:
	$(COMPOSE) down -v

.PHONY: clean
clean:
	rm -rf $(PLAYER)/dist $(NODE_MODULES)
