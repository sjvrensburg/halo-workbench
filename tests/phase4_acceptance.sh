#!/usr/bin/env bash
# PLAN.md phase 4 acceptance: the pattern layer, the verifier, and the PDF path.
#
#   tests/phase4_acceptance.sh
#
# No GPU and no running app. The model layer needs :8092 and is exercised by hand --
# but the rule that makes it safe (a proposal that is not present verbatim is dropped)
# is pure, and is checked here.
set -euo pipefail

cd "$(dirname "$0")/.."
VENV="${VENV:-$HOME/venvs/workbench}"
[ -x "$VENV/bin/python" ] || { echo "no venv at $VENV -- run scripts/bootstrap.sh" >&2; exit 2; }

TMP="$(mktemp -d /tmp/workbench-phase4-XXXXXX)"
trap 'rm -rf "$TMP"' EXIT

exec "$VENV/bin/python" tests/phase4_driver.py "$TMP"
