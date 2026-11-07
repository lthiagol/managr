# managr
#
# One plugin: a modal, layered keymap for Herdr.
#
#   make help   list every target
#   make test   the test suites
#   make check  lint + test

SHELL := /bin/sh
HERDR ?= herdr
PYTHON ?= python3
PLUGIN_ID ?= managr
HERE := $(CURDIR)

.DEFAULT_GOAL := help
.PHONY: help test check lint tree explain link unlink enable disable \
        config-install config-uninstall config-status config-print status \
        install uninstall reload clean open print live-check \
        live-workspace-check \
        verify-claims api-check

## help: list targets
help:
	@echo "managr - a modal, layered keymap for Herdr"
	@echo
	@grep -E '^## ' $(MAKEFILE_LIST) | sed 's/^## /  /' | sort
	@echo
	@echo "vars: HERDR=$(HERDR) PYTHON=$(PYTHON)"

## test: run the test suites (no Herdr server needed)
test:
	@$(PYTHON) tests/test_entrypoint.py
	@$(PYTHON) tests/test_layers.py
	@$(PYTHON) tests/test_table.py
	@$(PYTHON) tests/test_visuals.py
	@$(PYTHON) tests/test_api_check.py
	@$(PYTHON) tests/test_shared.py
	@$(PYTHON) tests/test_configure.py
	@$(PYTHON) tests/test_overlay_pty.py

## lint: byte-compile everything
lint:
	@$(PYTHON) -m compileall -q src scripts bin >/dev/null
	@echo "compiles cleanly"

## check: lint + test, on every python
check: lint test
	@echo
	@echo "=== the system python (3.9 on macOS) ==="
	@if [ -x /usr/bin/python3 ]; then \
		echo "  version: $$(/usr/bin/python3 -c 'import sys; print(sys.version.split()[0])')"; \
		for t in tests/test_*.py; do \
			/usr/bin/python3 $$t 2>&1 | tail -1 | sed "s|^|  $$t: |"; \
		done; \
	else \
		echo "  (no /usr/bin/python3)"; \
	fi

## tree: print the layer tree
tree:
	@$(PYTHON) bin/managr tree

## explain: what can be executed and what is a hint row
explain:
	@$(PYTHON) bin/managr explain

## open: open the overlay right now (without the keybinding)
#
# The action id is the local one, `open`, and the plugin is named by
# `--plugin`. The qualified form (`managr.open`) together with `--plugin`
# is rejected by Herdr as an invalid action id, so it is used on its own.
open:
	@$(HERDR) plugin action invoke open --plugin $(PLUGIN_ID)

## link: register this directory as a Herdr plugin
link:
	@$(HERDR) plugin link $(HERE)

## unlink: deregister the plugin
unlink:
	@$(HERDR) plugin unlink $(PLUGIN_ID)

## enable / disable: toggle without unlinking
enable:
	@$(HERDR) plugin enable $(PLUGIN_ID)

disable:
	@$(HERDR) plugin disable $(PLUGIN_ID)

## config-install: add the keybinding to config.toml
config-install:
	@$(PYTHON) scripts/configure.py install

## config-uninstall: remove the keybinding from config.toml
config-uninstall:
	@$(PYTHON) scripts/configure.py uninstall

## config-status: show the current binding
config-status:
	@$(PYTHON) scripts/configure.py status

## print: the binding snippet, to paste by hand
print:
	@$(PYTHON) scripts/configure.py print

## status: a one-screen summary of the install
status:
	@$(HERDR) plugin list --plugin $(PLUGIN_ID) 2>&1 | sed 's/^/  /'
	@echo
	@$(PYTHON) scripts/configure.py status
	@echo
	@$(PYTHON) bin/managr tree | head -14 | sed 's/^/  /'

## reload: ask a running Herdr to re-read its config
reload:
	@$(HERDR) server reload-config >/dev/null && echo "config reloaded" \
		|| echo "no running server; skipped"

## live-check: drive the real overlay against the running Herdr, through a pty
live-check:
	@$(PYTHON) scripts/live_check.py

## live-workspace-check: the overlay closes itself when the workspace moves
live-workspace-check:
	@$(PYTHON) scripts/live_workspace_check.py

## verify-claims: prove the regression guards are real, by breaking the code
verify-claims:
	@$(PYTHON) scripts/verify_claims.py

## api-check: measure the action map against the running Herdr
api-check:
	@$(PYTHON) scripts/api_check.py

## install: link the plugin, add the binding, reload
install:
	@$(MAKE) --no-print-directory link
	@$(MAKE) --no-print-directory config-install
	@$(HERDR) config check || echo "WARNING: herdr config check reported problems"
	@$(MAKE) --no-print-directory reload
	@echo
	@echo "installed. press your prefix then space to open it."
	@echo "see the tree with:  make tree"

## uninstall: remove the binding, then unlink
uninstall:
	@$(MAKE) --no-print-directory config-uninstall
	@$(HERDR) plugin unlink $(PLUGIN_ID) 2>/dev/null || true
	@$(MAKE) --no-print-directory reload
	@echo "uninstalled."

## clean: remove bytecode caches
clean:
	@find . -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
	@find . -name '*.pyc' -delete 2>/dev/null || true
	@echo "cleaned"
