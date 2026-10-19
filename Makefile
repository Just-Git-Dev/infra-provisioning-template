# Local gate == CI. `make ci` runs exactly the checks .github/workflows/ci.yml runs, in CI's
# order; ci.yml calls the same targets, so the two cannot drift (tests/test_ci_make_targets.py
# fails if they do). Tool pins below must equal the SC_VERSION / AL_VERSION in ci.yml.
SHELL := bash
.SHELLFLAGS := -eo pipefail -c

SHELLCHECK_VERSION := 0.11.0
ACTIONLINT_VERSION := 1.7.12

# Order = CI's order. Add a check by adding a target and listing it here AND in ci.yml.
CHECKS := venv tests tools actionlint canary shellcheck composite pinned

# CI runs python3.12; prefer it, then 3.13 (the laptop's python3 may be newer with no pytest).
PYBIN := $(shell command -v python3.12 || command -v python3.13 || command -v python3)
VENV := .venv
PY := $(VENV)/bin/python

.PHONY: ci hooks $(CHECKS)

ci:
	@if [ "$$(git config core.hooksPath 2>/dev/null)" != ".githooks" ]; then \
	  echo "ci: warning: pre-push hook not installed; run 'make hooks'"; fi
	@n=0; for t in $(CHECKS); do \
	  echo "== ci: $$t"; \
	  $(MAKE) --no-print-directory $$t || { echo "ci: FAILED at target '$$t' ($$n of $(words $(CHECKS)) checks passed before it)"; exit 1; }; \
	  n=$$((n+1)); \
	done; echo "ci: OK, $$n of $(words $(CHECKS)) checks ran and passed"

hooks:
	git config core.hooksPath .githooks
	@echo "pre-push hook installed (core.hooksPath=.githooks); bypass: git push --no-verify"

# Build .venv from the pinned requirements on first run; rebuild when the file changes.
venv: $(VENV)/.stamp
$(VENV)/.stamp: requirements-ci.txt
	$(PYBIN) -m venv $(VENV)
	$(PY) -m pip install --quiet -r requirements-ci.txt
	touch $@

# Engine unit tests (gcloud/kubectl seams stubbed, no cloud access), the bootstrap anchors
# (gcloud stubbed on PATH), and this gate's own tests.
tests: venv
	$(PY) tests/test_engine.py
	$(PY) tests/test_kubernetes.py
	$(PY) tests/test_anchor.py
	$(PY) tests/test_ci_make_targets.py
	$(PY) tests/test_pre_push_hook.py

# Fails when a local linter differs from the version CI pins: another version is not the same gate.
tools:
	@bad=0; \
	got=$$(shellcheck --version 2>/dev/null | sed -n 's/^version: //p'); \
	[ "$$got" = "$(SHELLCHECK_VERSION)" ] || { echo "tools: shellcheck '$$got' != pinned $(SHELLCHECK_VERSION); install: brew install shellcheck (or the v$(SHELLCHECK_VERSION) release from github.com/koalaman/shellcheck)"; bad=1; }; \
	got=$$(actionlint --version 2>/dev/null | sed -n 1p); \
	[ "$$got" = "$(ACTIONLINT_VERSION)" ] || { echo "tools: actionlint '$$got' != pinned $(ACTIONLINT_VERSION); install: go install github.com/rhysd/actionlint/cmd/actionlint@v$(ACTIONLINT_VERSION) (or the release from github.com/rhysd/actionlint)"; bad=1; }; \
	[ $$bad = 0 ] && echo "tools: shellcheck $(SHELLCHECK_VERSION), actionlint $(ACTIONLINT_VERSION)"

# actionlint runs shellcheck over every `run:` body; the explicit path stops it falling back to another one.
actionlint: tools
	actionlint -shellcheck="$$(command -v shellcheck)"

# Positive control: a wrong -shellcheck path makes actionlint report success with no shellcheck at all.
canary: tools
	@d=.ci-canary; rm -rf $$d; mkdir -p $$d/.github/workflows; trap 'rm -rf $$d' EXIT; \
	printf 'on: push\njobs:\n  a:\n    runs-on: ubuntu-latest\n    steps:\n      - run: |\n          f=$$(ls)\n          rm $$f\n' \
	  > $$d/.github/workflows/canary.yml; \
	cd $$d; out=$$(actionlint -shellcheck="$$(command -v shellcheck)" .github/workflows/canary.yml 2>&1 || true); \
	grep -q SC2086 <<<"$$out" || { echo "::error::canary SC2086 not reported; shellcheck is not running inside actionlint"; echo "$$out"; exit 1; }; \
	echo "canary: SC2086 reported (examined 1 canary workflow)"

# actionlint's shellcheck pass covers `run:` bodies only; bootstrap/*.sh are the anchors.
shellcheck: tools
	shellcheck bootstrap/*.sh

# actionlint lints workflows, not action.yml: extract the composite action's run: bodies and shellcheck them.
composite: tools venv
	$(PY) scripts/extract_action_shell.py > .ci-action-steps.sh
	rc=0; shellcheck --shell=bash .ci-action-steps.sh || rc=$$?; rm -f .ci-action-steps.sh; exit $$rc

# A moved tag on an action in our workflows or action.yml is a supply-chain event for every consumer.
# Python, not `grep -P`: BSD grep has no -P, so locally the grep errored and the step still passed.
pinned: venv
	$(PY) scripts/check_pinned_actions.py .github/workflows action.yml
