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

ADMIN         := src/admin
ADMIN_VENV    := .venv-admin
ADMIN_PY      := $(ADMIN_VENV)/bin/python

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
	@echo "Host admin app (PySide6):"
	@echo "  make admin        install (first run) and launch the desktop admin app"
	@echo "  make admin-clean  remove the admin virtualenv"
	@echo ""
	@echo "Both at once, which is the whole loop: run 'make gui' in one"
	@echo "terminal and 'make admin' in another, then put"
	@echo "http://localhost:8080 in the admin app's Settings -> device URL."
	@echo "The gui container IS the daemon -- there is no third process."
	@echo ""
	@echo "Override the published port with PLAYSTICK_GUI_PORT, and the library"
	@echo "with PLAYSTICK_GUI_LIBRARY=/path/to/movies."
	@echo ""
	@echo "The sample library is generated once, on first run:"
	@echo "  PLAYSTICK_GUI_CLIP_SECONDS=45  length of the ordinary clips"
	@echo "  PLAYSTICK_GUI_LONG_SECONDS=900 length of the one feature-length"
	@echo "                                 film, which is the only one the"
	@echo "                                 skip buttons can be judged on"
	@echo ""
	@echo "The gui container writes /etc/playstick/settings.toml at startup"
	@echo "from these; see scripts/gui-entrypoint.sh for what each one changes:"
	@echo "  PLAYSTICK_GUI_PROJECTOR=       NullProjector instead of the fake"
	@echo "  PLAYSTICK_GUI_PROJECTOR_IDLE=7 minutes before the lamp goes out"
	@echo "  PLAYSTICK_GUI_NAS=127.0.0.1:9999"
	@echo "                                 a dead port, to see the whole"
	@echo "                                 'the NAS is off' path and its button"

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

# The admin app runs on the host that encodes the library, not the stick, so it
# lives in its own virtualenv rather than the dependency-light device runtime.
# Auto-created on first `make admin`; editable install so a code edit needs no
# reinstall. Delete with `make admin-clean` or run pip install -e src/admin again.
$(ADMIN_VENV):
	python3 -m venv $(ADMIN_VENV)
	$(ADMIN_PY) -m pip install --upgrade pip
	$(ADMIN_PY) -m pip install -e $(ADMIN)

.PHONY: admin
admin: $(ADMIN_VENV)
	$(ADMIN_PY) -m playstick_admin

.PHONY: admin-clean
admin-clean:
	rm -rf $(ADMIN_VENV)
