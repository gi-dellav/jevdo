#!/usr/bin/env bash
# Run the python-qa eval against OpenRouter (Jev via OpenRouter System One API).
#
# Usage:
#   export OPENROUTER_API_KEY=...   # from https://openrouter.ai/settings/keys
#   ./run_eval.sh [extra jevdo args...]
#
# Validates both TOML files load, then runs every [[test]] in eval.toml
# (plans only — nothing executes) and appends JSONL events to eval-results.jsonl.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"

: "${OPENROUTER_API_KEY:?Set OPENROUTER_API_KEY (https://openrouter.ai/settings/keys)}"

jevdo --provider openrouter \
  --env environment.toml \
  --cwd workspace \
  --eval eval.toml \
  --log eval-results.jsonl \
  "$@"
