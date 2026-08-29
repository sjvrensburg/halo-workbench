#!/usr/bin/env bash
# PLAN.md phase 6b acceptance: protected spans, the three checks, and the drift guard.
#
#   tests/phase6_acceptance.sh           # offline
#   tests/phase6_acceptance.sh --live    # ...plus a real round trip through :8094
set -euo pipefail

cd "$(dirname "$0")/.."
VENV="${VENV:-$HOME/venvs/workbench}"
[ -x "$VENV/bin/python" ] || { echo "no venv at $VENV -- run scripts/bootstrap.sh" >&2; exit 2; }

"$VENV/bin/python" tests/phase6_driver.py offline
[ "${1:-}" = "--live" ] && "$VENV/bin/python" tests/phase6_driver.py live || true
