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

.DEFAULT_GOAL := help
.PHONY: help test check install uninstall status reload clean \
        tree explain open live-check verify-claims api-check

## help: list targets
help:
	@echo "managr - a modal, layered keymap for Herdr"
	@echo
	@grep -E '^## ' $(MAKEFILE_LIST) | sed 's/^## /  /' | sort
	@echo
	@echo "vars: HERDR=$(HERDR) PYTHON=$(PYTHON)"

## test: run the test suites (no Herdr server needed)
test:
	@$(MAKE) --no-print-directory -C v2 test

## check: lint + test, on every python
check:
	@$(MAKE) --no-print-directory -C v2 check

## install: link the plugin, add the binding, reload
install:
	@$(MAKE) --no-print-directory -C v2 install

## uninstall: remove the binding, then unlink
uninstall:
	@$(MAKE) --no-print-directory -C v2 uninstall

## status: a one-screen summary of the install
status:
	@$(MAKE) --no-print-directory -C v2 status

## tree: print the layer tree
tree:
	@$(MAKE) --no-print-directory -C v2 tree

## explain: what can be executed and what is a hint row
explain:
	@$(MAKE) --no-print-directory -C v2 explain

## open: open the overlay right now (without the keybinding)
open:
	@$(MAKE) --no-print-directory -C v2 open

## live-check: drive the real overlay against the running Herdr
live-check:
	@$(MAKE) --no-print-directory -C v2 live-check

## verify-claims: prove the regression guards are real, by breaking the code
verify-claims:
	@$(MAKE) --no-print-directory -C v2 verify-claims

## api-check: measure the action map against the running Herdr
api-check:
	@$(MAKE) --no-print-directory -C v2 api-check

## reload: ask a running Herdr to re-read its config
reload:
	@$(MAKE) --no-print-directory -C v2 reload

## clean: remove bytecode caches
clean:
	@$(MAKE) --no-print-directory -C v2 clean
