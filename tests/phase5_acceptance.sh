#!/usr/bin/env bash
# PLAN.md phase 5 acceptance: the regex loop.
#
#   tests/phase5_acceptance.sh            # offline parts only
#   tests/phase5_acceptance.sh --cases    # ...plus the ten cases, which need :8092
#
# The ten cases run the real model and take a few minutes; :8092 is --parallel 1 and
# shared with Hermes, so they queue behind whatever Hermes is doing.
set -euo pipefail

cd "$(dirname "$0")/.."
VENV="${VENV:-$HOME/venvs/workbench}"
[ -x "$VENV/bin/python" ] || { echo "no venv at $VENV -- run scripts/bootstrap.sh" >&2; exit 2; }

echo ">> the runner, the timeout, and the loop's contract (no model)"
"$VENV/bin/python" tests/phase5_driver.py offline

if [ "${1:-}" = "--cases" ]; then
    echo; echo ">> ten cases against :8092"
    "$VENV/bin/python" tests/phase5_driver.py cases
fi
