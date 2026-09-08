# Prax Bot — common commands. Run `make` to list them.

.DEFAULT_GOAL := help
.PHONY: help setup install doctor web ui session demo test bench

help:  ## List these commands
	@grep -E '^[a-z]+:.*## ' $(MAKEFILE_LIST) | sed 's/:.*## /\t/' | awk -F'\t' '{printf "  \033[36m%-9s\033[0m %s\n", $$1, $$2}'

setup:  ## Install dependencies into a local .venv
	uv sync

install:  ## Put a plain `prax` command on your PATH (uv tool install, no shell config edits)
	uv tool install --editable .

doctor:  ## Check credentials, the model, tool calling and the sandbox
	uv run prax doctor

web:  ## Open the web interface on this repository (a Token Factory sandbox)
	uv run prax web

ui:  ## Open the full-screen terminal interface
	uv run prax ui

session:  ## A plain prompt on this repository, for pipes and scripts
	uv run prax session

demo:  ## Try it with no sandbox: a throwaway repo with a failing test, run locally
	@mkdir -p /tmp/praxbot-demo
	@cd /tmp/praxbot-demo && git init -q 2>/dev/null; \
	  printf 'def slug(s):\n    return s.lower().replace(" ", "-")\n' > slugify.py; \
	  printf 'from slugify import slug\n\n\ndef test_basic():\n    assert slug("Hello World") == "hello-world"\n\n\ndef test_trims_spaces():\n    assert slug("  Hi  There  ") == "hi-there"\n' > test_slugify.py; \
	  git add -A && git -c user.email=demo@local -c user.name=demo commit -qm base 2>/dev/null; true
	@echo 'A repo with a failing test is at /tmp/praxbot-demo. Try: "make test_trims_spaces pass"'
	uv run prax web --repo /tmp/praxbot-demo --backend local --setup 'pip install -q pytest'

test:  ## Run the test suite
	uv run pytest -q

bench:  ## Run the ten-task benchmark (set SRC=/path/to/clones)
	uv run python scripts/bench.py --src $${SRC:?set SRC=/path/to/clones}

